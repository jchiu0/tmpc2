import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from cloud_agent.service import app as app_module
from cloud_agent.python_runner import run_python_activity
from cloud_agent.service.queue import AgentQueue
from cloud_agent.service.storage import AgentStore
from cloud_agent.service.worker import build_agent_request, process_message
from cloud_agent.workflow import WorkflowContext
from cloud_agent.workflow_runtime import invoke_activity, invoke_workflow


WORKFLOW_SOURCE = """
from cloud_agent.workflow import StateMachine, state


class AppBuilder(StateMachine):
    initial_state = "requirements"

    @state
    def requirements(self, ctx, event):
        if event.type == "entered":
            return ctx.run("requirements", "Write requirements")
        ctx.state["requirements"] = event.result["summary"]
        return ctx.transition("planning")

    @state
    def planning(self, ctx, event):
        if event.type == "entered":
            return ctx.run("planning", "Write an implementation plan")
        ctx.state["plan"] = event.result["summary"]
        return ctx.transition("executing")

    @state
    def executing(self, ctx, event):
        attempt = ctx.state.get("executionAttempt", 0)
        if event.type == "entered":
            return ctx.run(f"execute-{attempt}", "Implement the application")
        ctx.state["implementation"] = event.result["summary"]
        return ctx.transition("evaluating")

    @state
    def evaluating(self, ctx, event):
        attempt = ctx.state.get("executionAttempt", 0)
        if event.type == "entered":
            return ctx.run(f"evaluate-{attempt}", "Evaluate the application")
        if event.result["summary"] == "evaluation failed":
            ctx.state["executionAttempt"] = attempt + 1
            return ctx.transition("executing")
        ctx.state["evaluation"] = event.result["summary"]
        return ctx.complete("Application built and evaluated")
"""

CHECKPOINT_SOURCE = """
from cloud_agent.workflow import StateMachine, state


class CheckpointMachine(StateMachine):
    initial_state = "compute"

    @state
    def compute(self, ctx, event):
        if event.type == "entered":
            command = ctx.run_command(
                ["python", "-c", "print(6 * 7)"]
            )
            ctx.state["answer"] = int(command["stdout"])
            return ctx.run("computed", result="Computed without an LLM")
        return ctx.complete(f"answer={ctx.state['answer']}")
"""

INPUT_SOURCE = """
from cloud_agent.workflow import StateMachine, state


class ApprovalMachine(StateMachine):
    initial_state = "work"

    @state
    def work(self, ctx, event):
        if event.type == "entered":
            return ctx.run("work", "Do initial work")
        return ctx.transition("approval")

    @state
    def approval(self, ctx, event):
        if event.type == "entered":
            return ctx.wait_for_user("approval", "Approve the work?")
        ctx.state["response"] = event.response
        return ctx.complete("approved")
"""

PYTHON_SOURCE = """
from cloud_agent.workflow import StateMachine, activity, state


@activity
def verify(ctx, input):
    (ctx.workspace / "verified.txt").write_text(input["message"])
    command = ctx.run_command(["python", "-c", "print('checked')"])
    return {"passed": command["exitCode"] == 0}


class PythonMachine(StateMachine):
    initial_state = "verify"

    @state
    def verify(self, ctx, event):
        if event.type == "entered":
            return ctx.run_python(
                "verify", "verify", {"message": "verified"}
            )
        ctx.state["verification"] = event.result["activityResult"]
        return ctx.complete("verified")
"""


class WorkflowTests(unittest.TestCase):
    def test_run_llm_controls_context_copying(self) -> None:
        context = WorkflowContext({})
        copied = context.run_llm("copied", "Continue")
        isolated = context.run_llm(
            "isolated", "Start clean", copy_context=False
        )
        self.assertTrue(copied.arguments["copyContext"])
        self.assertFalse(isolated.arguments["copyContext"])

    def test_worker_honors_run_context_policy(self) -> None:
        class FakeStore:
            history_calls = 0

            def conversation_before(self, run_id):
                self.history_calls += 1
                return [{"role": "user", "content": run_id}]

            def get_subagents(self, agent_id):
                return []

        run = {
            "prompt": "Implement",
            "repo_url": "https://github.com/example/app",
            "starting_ref": "main",
            "work_on_current_branch": 0,
            "output_branch": "cursor/test",
            "auto_create_pr": 0,
            "mcp_url": "http://localhost/mcp",
            "agent_id": "bc-test",
            "copy_context": 0,
        }
        store = FakeStore()
        isolated = build_agent_request(run, store, "run-isolated")
        self.assertEqual(isolated.history, ())
        self.assertEqual(store.history_calls, 0)
        run["copy_context"] = 1
        copied = build_agent_request(run, store, "run-copied")
        self.assertEqual(len(copied.history), 1)
        self.assertEqual(store.history_calls, 1)

    def test_legacy_run_kinds_migrate_to_typed_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = AgentStore(Path(temp) / "agents.db")
            store.initialize()
            store.create_agent_and_run(
                agent_id="bc-legacy",
                run_id="run-legacy-llm",
                name="Legacy",
                prompt="Implement",
                repo_url="https://github.com/example/app",
                starting_ref="main",
                working_branch="cursor/legacy",
                work_on_current_branch=False,
                auto_create_pr=False,
                output_branch="cursor/legacy",
                mcp_url="http://localhost/mcp",
            )
            with store._connection() as connection:
                connection.execute(
                    """
                    UPDATE runs SET run_kind = 'coding'
                    WHERE run_id = 'run-legacy-llm'
                    """
                )
            store.initialize()
            self.assertEqual(
                store.get_run("run-legacy-llm")["run_kind"], "llm"
            )

    def test_runtime_imports_source_and_returns_command(self) -> None:
        import hashlib

        invocation = invoke_workflow(
            source=WORKFLOW_SOURCE,
            source_hash=hashlib.sha256(
                WORKFLOW_SOURCE.encode("utf-8")
            ).hexdigest(),
            entrypoint="AppBuilder",
            state_name=None,
            state_data={},
            event={"type": "entered", "payload": {}},
        )
        self.assertEqual(invocation.state_name, "requirements")
        self.assertEqual(invocation.command["type"], "run")
        self.assertEqual(
            invocation.command["arguments"]["key"], "requirements"
        )

    def test_failed_evaluation_returns_to_execution(self) -> None:
        import hashlib

        source_hash = hashlib.sha256(
            WORKFLOW_SOURCE.encode("utf-8")
        ).hexdigest()
        failed = invoke_workflow(
            source=WORKFLOW_SOURCE,
            source_hash=source_hash,
            entrypoint="AppBuilder",
            state_name="evaluating",
            state_data={"executionAttempt": 0},
            event={
                "type": "run_completed",
                "payload": {"result": {"summary": "evaluation failed"}},
            },
        )
        self.assertEqual(failed.command["type"], "transition")
        self.assertEqual(failed.command["arguments"]["state"], "executing")
        self.assertEqual(failed.state_data["executionAttempt"], 1)
        retry = invoke_workflow(
            source=WORKFLOW_SOURCE,
            source_hash=source_hash,
            entrypoint="AppBuilder",
            state_name="executing",
            state_data=failed.state_data,
            event={"type": "entered", "payload": {}},
        )
        self.assertEqual(retry.command["arguments"]["key"], "execute-1")

    def test_internal_command_feeds_checkpoint_only_run(self) -> None:
        import hashlib

        invocation = invoke_workflow(
            source=CHECKPOINT_SOURCE,
            source_hash=hashlib.sha256(
                CHECKPOINT_SOURCE.encode("utf-8")
            ).hexdigest(),
            entrypoint="CheckpointMachine",
            state_name=None,
            state_data={},
            event={"type": "entered", "payload": {}},
        )
        self.assertEqual(invocation.state_data["answer"], 42)
        self.assertIsNone(invocation.command["arguments"]["prompt"])
        self.assertEqual(
            invocation.command["arguments"]["result"],
            "Computed without an LLM",
        )

    def test_python_activity_runs_in_workspace(self) -> None:
        import hashlib

        with tempfile.TemporaryDirectory() as temp:
            invocation = invoke_activity(
                source=PYTHON_SOURCE,
                source_hash=hashlib.sha256(
                    PYTHON_SOURCE.encode("utf-8")
                ).hexdigest(),
                activity="verify",
                workspace=temp,
                state_data={},
                input={"message": "verified"},
            )
            self.assertEqual(invocation.result, {"passed": True})
            self.assertEqual(
                (Path(temp) / "verified.txt").read_text(), "verified"
            )

    def test_python_activity_publishes_workspace_changes(self) -> None:
        import hashlib

        published: dict = {}

        class FakeGitHub:
            def __init__(self, repo):
                pass

            def get_ref(self, branch):
                return None

            def default_branch(self):
                return "main"

            def download_ref(self, ref, workspace):
                workspace.mkdir(parents=True)
                (workspace / "base.txt").write_text("base")
                return "base-commit"

            def create_commit(self, workspace, message, parent_sha):
                self.assertions = None
                published["content"] = (
                    workspace / "verified.txt"
                ).read_text()
                published["message"] = message
                published["parent"] = parent_sha
                return "python-commit"

            def write_ref(self, branch, commit, existing):
                published["ref"] = (branch, commit, existing)

            def close(self):
                pass

        run = {
            "run_id": "run-python",
            "repo_url": "https://github.com/example/app",
            "starting_ref": "main",
            "output_branch": "cursor/python",
            "work_on_current_branch": 0,
            "python_activity": "verify",
            "python_input_json": '{"message": "verified"}',
            "source_code": PYTHON_SOURCE,
            "source_hash": hashlib.sha256(
                PYTHON_SOURCE.encode("utf-8")
            ).hexdigest(),
            "workflow_state_json": "{}",
        }
        with patch(
            "cloud_agent.python_runner.GitHubGitApi", FakeGitHub
        ):
            result = run_python_activity(run)
        self.assertEqual(result["commit"], "python-commit")
        self.assertEqual(published["content"], "verified")
        self.assertEqual(published["parent"], "base-commit")
        self.assertEqual(
            published["ref"],
            ("cursor/python", "python-commit", None),
        )

    def test_source_agent_runs_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = AgentStore(Path(temp) / "agents.db")
            queue = AgentQueue(
                "redis://127.0.0.1:6379/15",
                "workflow-happy-path",
                "workflow-workers",
            )
            queue.client.delete(queue.stream)
            responses = iter(
                [
                    "requirements ready",
                    "plan ready",
                    "implementation ready",
                    "evaluation passed",
                ]
            )

            async def executor(request, on_event=None):
                return {
                    "status": "finished",
                    "summary": next(responses),
                    "branch": request.output_branch,
                    "commit": f"commit-{request.idempotency_key}",
                }

            try:
                with (
                    patch.object(app_module, "store", store),
                    patch.object(app_module, "queue", queue),
                    TestClient(app_module.app) as client,
                ):
                    response = client.post(
                        "/v1/agents",
                        json={
                            "source": {
                                "language": "python",
                                "code": WORKFLOW_SOURCE,
                                "entrypoint": "AppBuilder",
                            },
                            "repos": [
                                {
                                    "url": "https://github.com/example/app",
                                    "startingRef": "main",
                                }
                            ],
                            "autoCreatePR": False,
                        },
                    )
                    self.assertEqual(response.status_code, 202, response.text)
                    created = response.json()
                    run_ids: list[str] = []
                    for index in range(5):
                        message = queue.read(
                            f"worker-{index}", block_ms=100
                        )
                        self.assertIsNotNone(message)
                        run_ids.append(message.run_id)
                        process_message(
                            message,
                            store,
                            queue,
                            f"worker-{index}",
                            stale_after_ms=60_000,
                            executor=executor,
                        )
                    self.assertEqual(len(set(run_ids)), 5)
                    state_response = client.get(
                        f"/v1/agents/{created['agent']['id']}/state"
                    )
                    self.assertEqual(state_response.status_code, 200)
                    workflow = state_response.json()
                    self.assertEqual(workflow["status"], "COMPLETED")
                    self.assertEqual(workflow["state"], "evaluating")
                    self.assertEqual(
                        workflow["stateData"]["evaluation"],
                        "evaluation passed",
                    )
                    self.assertEqual(
                        workflow["result"],
                        "Application built and evaluated",
                    )
                    for completed_run_id in run_ids:
                        timing_response = client.get(
                            f"/v1/agents/{created['agent']['id']}/runs/"
                            f"{completed_run_id}"
                        )
                        self.assertEqual(timing_response.status_code, 200)
                        timing = timing_response.json()
                        self.assertIsNotNone(timing["startedAt"])
                        self.assertIsNotNone(timing["finishedAt"])
                        self.assertGreaterEqual(timing["queueDurationMs"], 0)
                        self.assertGreaterEqual(timing["durationMs"], 0)
                    followup = client.post(
                        f"/v1/agents/{created['agent']['id']}/runs",
                        json={"prompt": {"text": "Interrupt workflow"}},
                    )
                    self.assertEqual(followup.status_code, 409)
                    self.assertEqual(
                        followup.json()["detail"]["code"],
                        "workflow_autonomous",
                    )
            finally:
                queue.client.delete(queue.stream)
                queue.close()

    def test_user_input_is_persisted_and_resumes_same_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = AgentStore(Path(temp) / "agents.db")
            queue = AgentQueue(
                "redis://127.0.0.1:6379/15",
                f"workflow-input-{uuid.uuid4()}",
                "workflow-workers",
            )

            async def executor(request, on_event=None):
                return {
                    "status": "finished",
                    "summary": "initial work complete",
                    "branch": request.output_branch,
                    "commit": "commit-work",
                }

            try:
                with (
                    patch.object(app_module, "store", store),
                    patch.object(app_module, "queue", queue),
                    TestClient(app_module.app) as client,
                ):
                    created = client.post(
                        "/v1/agents",
                        json={
                            "source": {
                                "language": "python",
                                "code": INPUT_SOURCE,
                                "entrypoint": "ApprovalMachine",
                            },
                            "repos": [
                                {"url": "https://github.com/example/app"}
                            ],
                            "autoCreatePR": False,
                        },
                    ).json()
                    first = queue.read("worker-one", block_ms=100)
                    process_message(
                        first,
                        store,
                        queue,
                        "worker-one",
                        stale_after_ms=60_000,
                        executor=executor,
                    )
                    work = queue.read("worker-work", block_ms=100)
                    process_message(
                        work,
                        store,
                        queue,
                        "worker-work",
                        stale_after_ms=60_000,
                        executor=executor,
                    )
                    waiting = client.get(
                        f"/v1/agents/{created['agent']['id']}/state"
                    ).json()
                    self.assertEqual(waiting["status"], "USER_INPUT")
                    self.assertEqual(
                        waiting["userInput"]["prompt"], "Approve the work?"
                    )
                    resumed = client.post(
                        f"/v1/agents/{created['agent']['id']}/input",
                        json={"response": "approve"},
                    )
                    self.assertEqual(resumed.status_code, 202, resumed.text)
                    second = queue.read("worker-two", block_ms=100)
                    process_message(
                        second,
                        store,
                        queue,
                        "worker-two",
                        stale_after_ms=60_000,
                        executor=executor,
                    )
                    completed = client.get(
                        f"/v1/agents/{created['agent']['id']}/state"
                    ).json()
                    self.assertEqual(completed["status"], "COMPLETED")
                    self.assertEqual(
                        completed["stateData"]["response"], "approve"
                    )
            finally:
                queue.client.delete(queue.stream)
                queue.close()

    def test_worker_dispatches_explicit_python_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = AgentStore(Path(temp) / "agents.db")
            queue = AgentQueue(
                "redis://127.0.0.1:6379/15",
                f"workflow-python-{uuid.uuid4()}",
                "workflow-workers",
            )
            observed: dict = {}

            def python_executor(run, on_event=None):
                observed.update(run)
                return {
                    "status": "finished",
                    "summary": "Python activity verify completed",
                    "branch": run["output_branch"],
                    "commit": "python-commit",
                    "activityResult": {"passed": True},
                }

            try:
                with (
                    patch.object(app_module, "store", store),
                    patch.object(app_module, "queue", queue),
                    patch(
                        "cloud_agent.service.worker.run_python_activity",
                        python_executor,
                    ),
                    TestClient(app_module.app) as client,
                ):
                    created = client.post(
                        "/v1/agents",
                        json={
                            "source": {
                                "language": "python",
                                "code": PYTHON_SOURCE,
                                "entrypoint": "PythonMachine",
                            },
                            "repos": [
                                {"url": "https://github.com/example/app"}
                            ],
                            "autoCreatePR": False,
                        },
                    ).json()
                    message = queue.read("worker-python", block_ms=100)
                    process_message(
                        message,
                        store,
                        queue,
                        "worker-python",
                        stale_after_ms=60_000,
                    )
                    message = queue.read("worker-python", block_ms=100)
                    process_message(
                        message,
                        store,
                        queue,
                        "worker-python",
                        stale_after_ms=60_000,
                    )
                    self.assertEqual(observed["run_kind"], "python")
                    self.assertEqual(observed["python_activity"], "verify")
                    self.assertEqual(
                        observed["python_input_json"],
                        '{"message": "verified"}',
                    )
                    state = client.get(
                        f"/v1/agents/{created['agent']['id']}/state"
                    ).json()
                    self.assertEqual(state["status"], "COMPLETED")
                    self.assertEqual(
                        state["stateData"]["verification"],
                        {"passed": True},
                    )
            finally:
                queue.client.delete(queue.stream)
                queue.close()


if __name__ == "__main__":
    unittest.main()
