import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from cloud_agent.lib import runner as runner_module
from cloud_agent.service import app as app_module
from cloud_agent.service.queue import AgentQueue, QueueMessage
from cloud_agent.service.storage import (
    AgentBusyError,
    AgentStore,
    StaleExecutionError,
)
from cloud_agent.service.worker import process_message


HOLD_PENDING_SCRIPT = """
import sys
import time
from cloud_agent.service.queue import AgentQueue

queue = AgentQueue(sys.argv[1], sys.argv[2], sys.argv[3])
queue.initialize()
message = queue.read("worker-a", block_ms=1000)
if message is None:
    raise SystemExit(1)
while True:
    queue.refresh_lease("worker-a", message.message_id)
    time.sleep(0.03)
"""


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = AgentStore(Path(self.temp.name) / "agents.db")
        self.stream = f"test-cloud-agents-{uuid.uuid4()}"
        self.queue = AgentQueue(
            "redis://127.0.0.1:6379/15", self.stream, "workers"
        )

    def tearDown(self) -> None:
        try:
            self.queue.client.delete(self.stream)
            self.queue.close()
        finally:
            self.temp.cleanup()

    def test_create_agent_persists_then_queues(self) -> None:
        with (
            patch.object(app_module, "store", self.store),
            patch.object(app_module, "queue", self.queue),
            TestClient(app_module.app) as client,
        ):
            response = client.post(
                "/v1/agents",
                json={
                    "prompt": {"text": "Create a README"},
                    "repos": [
                        {
                            "url": "https://github.com/jchiu0/scratch1",
                            "startingRef": "main",
                        }
                    ],
                },
            )
            self.assertEqual(response.status_code, 202)
            created = response.json()
            self.assertEqual(created["agent"]["status"], "ACTIVE")
            self.assertRegex(created["agent"]["id"], r"^bc-")
            self.assertEqual(created["run"]["status"], "CREATING")
            self.assertEqual(
                created["agent"]["latestRunId"], created["run"]["id"]
            )
            stored = self.store.get_run(created["run"]["id"])
            self.assertIsNotNone(stored)
            self.assertEqual(stored["agent_id"], created["agent"]["id"])
            self.assertEqual(stored["prompt"], "Create a README")
            queued = self.queue.read("test-consumer", block_ms=10)
            self.assertIsNotNone(queued)
            self.assertEqual(queued.run_id, created["run"]["id"])
            self.queue.acknowledge(queued.message_id)

            busy = client.post(
                f"/v1/agents/{created['agent']['id']}/runs",
                json={"prompt": {"text": "Also add tests"}},
            )
            self.assertEqual(busy.status_code, 409)
            self.assertEqual(busy.json()["detail"]["code"], "agent_busy")

            claimed = self.store.claim_execution(created["run"]["id"])
            busy_running = client.post(
                f"/v1/agents/{created['agent']['id']}/runs",
                json={"prompt": {"text": "Wait for the running attempt"}},
            )
            self.assertEqual(busy_running.status_code, 409)

            retry = self.store.claim_execution(created["run"]["id"])
            busy_retry = client.post(
                f"/v1/agents/{created['agent']['id']}/runs",
                json={"prompt": {"text": "Wait for the retry"}},
            )
            self.assertEqual(busy_retry.status_code, 409)
            self.store.finish(
                created["run"]["id"],
                retry["attempt_count"],
                {
                    "status": "finished",
                    "summary": "README created",
                    "branch": "cursor/api-followup",
                    "commit": "abc123",
                },
            )
            followup = client.post(
                f"/v1/agents/{created['agent']['id']}/runs",
                json={
                    "prompt": {
                        "text": """
Add tests for the README behavior.
Keep the existing documentation unchanged.
""".strip()
                    }
                },
            )
            self.assertEqual(followup.status_code, 202)
            followup_run = followup.json()["run"]
            self.assertEqual(followup_run["agentId"], created["agent"]["id"])
            self.assertEqual(followup_run["status"], "CREATING")
            queued_followup = self.queue.read("test-consumer", block_ms=10)
            self.assertEqual(queued_followup.run_id, followup_run["id"])
            self.queue.acknowledge(queued_followup.message_id)

            failed = self.store.claim_execution(followup_run["id"])
            self.store.fail(
                followup_run["id"],
                failed["attempt_count"],
                "expected test failure",
            )
            after_error = client.post(
                f"/v1/agents/{created['agent']['id']}/runs",
                json={"prompt": {"text": "Continue after the failed run"}},
            )
            self.assertEqual(after_error.status_code, 202)
            queued_after_error = self.queue.read(
                "test-consumer", block_ms=10
            )
            self.assertEqual(
                queued_after_error.run_id,
                after_error.json()["run"]["id"],
            )
            self.queue.acknowledge(queued_after_error.message_id)

            missing = client.post(
                "/v1/agents/bc-missing/runs",
                json={"prompt": {"text": "Also add tests"}},
            )
            self.assertEqual(missing.status_code, 404)

    def test_claim_checks_agent_and_run_and_counts_retries(self) -> None:
        self.store.initialize()
        self.store.create_agent_and_run(
            agent_id="bc-test",
            run_id="run-test",
            name="Test",
            prompt="Test prompt",
            repo_url="https://github.com/example/repo",
            starting_ref="main",
            working_branch="cursor/test",
            work_on_current_branch=False,
            auto_create_pr=False,
            output_branch=None,
            mcp_url="http://127.0.0.1:8765/mcp",
        )
        first = self.store.claim_execution("run-test")
        self.assertEqual(first["status"], "RUNNING")
        self.assertEqual(first["attempt_count"], 1)

        retry = self.store.claim_execution("run-test")
        self.assertEqual(retry["attempt_count"], 2)
        with self.assertRaises(AgentBusyError):
            self.store.create_run(
                "bc-test", "run-blocked-by-retry", "Wait for retry", "mcp"
            )
        with self.assertRaises(StaleExecutionError):
            self.store.append_event(
                "run-test", 1, "agent.response", {"content": "stale"}
            )
        with self.assertRaises(StaleExecutionError):
            self.store.append_event(
                "run-test",
                1,
                "conversation.message",
                {
                    "role": "assistant",
                    "kind": "tool_call",
                    "content": "stale",
                    "attempt": 1,
                },
            )
        with self.assertRaises(StaleExecutionError):
            self.store.finish(
                "run-test", 1, {"status": "finished", "summary": "stale"}
            )

        self.store.finish(
            "run-test",
            2,
            {
                "status": "finished",
                "summary": "done",
                "branch": "cursor/test",
                "commit": "abc123",
            },
        )
        self.assertIsNone(self.store.claim_execution("run-test"))

    def test_poll_run_events_with_cursor_and_status(self) -> None:
        self.store.initialize()
        self.store.create_agent_and_run(
            agent_id="bc-poll",
            run_id="run-poll",
            name="Polling",
            prompt="Create a README",
            repo_url="https://github.com/example/repo",
            starting_ref="main",
            working_branch="cursor/poll",
            work_on_current_branch=False,
            auto_create_pr=False,
            output_branch="cursor/poll",
            mcp_url="http://127.0.0.1:8765/mcp",
        )
        run = self.store.claim_execution("run-poll")
        self.store.append_event(
            "run-poll",
            run["attempt_count"],
            "conversation.message",
            {
                "role": "user",
                "kind": "prompt",
                "content": "Task: Create a README",
                "attempt": run["attempt_count"],
            },
        )
        self.store.append_event(
            "run-poll",
            run["attempt_count"],
            "conversation.message",
            {
                "role": "assistant",
                "kind": "final_response",
                "content": '{"action":"finish","summary":"done"}',
                "attempt": run["attempt_count"],
            },
        )

        with (
            patch.object(app_module, "store", self.store),
            patch.object(app_module, "queue", self.queue),
            TestClient(app_module.app) as client,
        ):
            first = client.get(
                "/v1/agents/bc-poll/runs/run-poll/events",
            )
            self.assertEqual(first.status_code, 200)
            first_body = first.json()
            self.assertEqual(first_body["status"], "RUNNING")
            conversation = [
                event
                for event in first_body["events"]
                if event["type"] == "conversation.message"
            ]
            self.assertEqual(
                [event["payload"]["kind"] for event in conversation],
                ["prompt", "final_response"],
            )

            empty = client.get(
                "/v1/agents/bc-poll/runs/run-poll/events",
                params={"after": first_body["nextCursor"]},
            )
            self.assertEqual(empty.json()["events"], [])

            self.store.finish(
                "run-poll",
                run["attempt_count"],
                {
                    "status": "no_changes",
                    "summary": "done",
                    "branch": "cursor/poll",
                    "commit": None,
                },
            )
            terminal = client.get(
                "/v1/agents/bc-poll/runs/run-poll/events",
                params={"after": first_body["nextCursor"]},
            )
            self.assertEqual(terminal.json()["status"], "FINISHED")
            self.assertEqual(len(terminal.json()["events"]), 1)
            self.assertEqual(
                terminal.json()["events"][0]["payload"]["status"],
                "FINISHED",
            )
            self.assertEqual(
                terminal.json()["events"][0]["type"],
                "run.status",
            )

            missing = client.get(
                "/v1/agents/bc-other/runs/run-poll/events"
            )
            self.assertEqual(missing.status_code, 404)
            invalid = client.get(
                "/v1/agents/bc-poll/runs/run-poll/events",
                params={"after": "not-a-cursor"},
            )
            self.assertEqual(invalid.status_code, 400)

    def test_concurrent_followups_allow_only_one_active_run(self) -> None:
        self.store.initialize()
        self.store.create_agent_and_run(
            agent_id="bc-concurrent",
            run_id="run-initial",
            name="Concurrent",
            prompt="Create a README",
            repo_url="https://github.com/example/repo",
            starting_ref="main",
            working_branch="cursor/concurrent",
            work_on_current_branch=False,
            auto_create_pr=False,
            output_branch="cursor/concurrent",
            mcp_url="http://127.0.0.1:8765/mcp",
        )
        initial = self.store.claim_execution("run-initial")
        self.store.finish(
            "run-initial",
            initial["attempt_count"],
            {
                "status": "finished",
                "summary": "README created",
                "branch": "cursor/concurrent",
                "commit": "abc123",
            },
        )

        barrier = threading.Barrier(3)
        outcomes: list[str] = []

        def create_followup(run_id: str) -> None:
            barrier.wait()
            try:
                self.store.create_run(
                    "bc-concurrent", run_id, f"Prompt for {run_id}", "mcp"
                )
            except AgentBusyError:
                outcomes.append("busy")
            else:
                outcomes.append("created")

        threads = [
            threading.Thread(target=create_followup, args=(f"run-{index}",))
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertCountEqual(outcomes, ["created", "busy"])

    def test_followup_excludes_incomplete_retry_transcript(self) -> None:
        self.store.initialize()
        self.store.create_agent_and_run(
            agent_id="bc-transcript-retry",
            run_id="run-transcript-retry",
            name="Transcript retry",
            prompt="Create a README",
            repo_url="https://github.com/example/repo",
            starting_ref="main",
            working_branch="cursor/transcript-retry",
            work_on_current_branch=False,
            auto_create_pr=False,
            output_branch="cursor/transcript-retry",
            mcp_url="http://127.0.0.1:8765/mcp",
        )
        abandoned = self.store.claim_execution("run-transcript-retry")
        self.store.append_event(
            "run-transcript-retry",
            abandoned["attempt_count"],
            "conversation.message",
            {
                "role": "user",
                "kind": "prompt",
                "content": "abandoned prompt",
                "attempt": abandoned["attempt_count"],
            },
        )
        self.store.append_event(
            "run-transcript-retry",
            abandoned["attempt_count"],
            "conversation.message",
            {
                "role": "assistant",
                "kind": "tool_call",
                "content": "abandoned tool call",
                "attempt": abandoned["attempt_count"],
            },
        )

        completed = self.store.claim_execution("run-transcript-retry")
        completed_messages = [
            ("user", "prompt", "completed prompt"),
            (
                "assistant",
                "final_response",
                '{"action":"finish","summary":"completed"}',
            ),
        ]
        for role, kind, content in completed_messages:
            self.store.append_event(
                "run-transcript-retry",
                completed["attempt_count"],
                "conversation.message",
                {
                    "role": role,
                    "kind": kind,
                    "content": content,
                    "attempt": completed["attempt_count"],
                },
            )
        self.store.finish(
            "run-transcript-retry",
            completed["attempt_count"],
            {
                "status": "no_changes",
                "summary": "completed",
                "branch": "cursor/transcript-retry",
                "commit": None,
            },
        )
        self.store.create_run(
            "bc-transcript-retry",
            "run-after-transcript-retry",
            "Continue",
            "mcp",
        )

        self.assertEqual(
            self.store.conversation_before("run-after-transcript-retry"),
            [
                {"role": role, "content": content}
                for role, _, content in completed_messages
            ],
        )

    def test_followup_reuses_branch_and_previous_output(self) -> None:
        self.store.initialize()
        self.store.create_agent_and_run(
            agent_id="bc-followup",
            run_id="run-first",
            name="Followup",
            prompt="Create a README",
            repo_url="https://github.com/example/repo",
            starting_ref="main",
            working_branch="cursor/followup",
            work_on_current_branch=False,
            auto_create_pr=False,
            output_branch="cursor/followup",
            mcp_url="http://127.0.0.1:8765/mcp",
        )
        with self.assertRaises(AgentBusyError):
            self.store.create_run(
                "bc-followup", "run-too-soon", "Too soon", "mcp"
            )

        first = self.store.claim_execution("run-first")
        first_messages = [
            (
                "user",
                "prompt",
                "Task:\nCreate a README\n\nReturn the first JSON action.",
            ),
            (
                "assistant",
                "tool_call",
                '{"action":"write_file","path":"README.md","content":"# Test"}',
            ),
            (
                "user",
                "tool_result",
                'Action result:\n{"written": "README.md"}\n'
                "Return the next JSON action.",
            ),
            (
                "assistant",
                "final_response",
                '{"action":"finish","summary":"README created"}',
            ),
        ]
        for role, kind, content in first_messages:
            self.store.append_event(
                "run-first",
                first["attempt_count"],
                "conversation.message",
                {
                    "role": role,
                    "kind": kind,
                    "content": content,
                    "attempt": first["attempt_count"],
                },
            )
        self.store.finish(
            "run-first",
            first["attempt_count"],
            {
                "status": "finished",
                "summary": "README created",
                "branch": "cursor/followup",
                "commit": "abc123",
            },
        )
        created = self.store.create_run(
            "bc-followup", "run-second", "Add tests", "mcp"
        )
        self.assertEqual(created["run"]["status"], "CREATING")
        second = self.store.get_run("run-second")
        self.assertEqual(second["starting_ref"], "cursor/followup")
        self.assertEqual(second["work_on_current_branch"], 1)
        self.assertEqual(
            self.store.conversation_before("run-second"),
            [
                {"role": role, "content": content}
                for role, _, content in first_messages
            ],
        )

        second_claim = self.store.claim_execution("run-second")
        self.store.finish(
            "run-second",
            second_claim["attempt_count"],
            {
                "status": "no_changes",
                "summary": "No changes were needed",
                "branch": "cursor/followup",
                "commit": None,
            },
        )
        self.store.create_run(
            "bc-followup", "run-third", "Improve the tests", "mcp"
        )
        third = self.store.get_run("run-third")
        self.assertEqual(third["starting_ref"], "cursor/followup")
        self.assertEqual(third["work_on_current_branch"], 1)
        self.assertEqual(
            self.store.conversation_before("run-third"),
            [
                *[
                    {"role": role, "content": content}
                    for role, _, content in first_messages
                ],
                {"role": "user", "content": "Add tests"},
                {"role": "assistant", "content": "No changes were needed"},
            ],
        )

    def test_followups_can_chain_new_output_branches(self) -> None:
        self.store.initialize()
        self.store.create_agent_and_run(
            agent_id="bc-branch-chain",
            run_id="run-branch-a",
            name="Branch chain",
            prompt="Create branch A",
            repo_url="https://github.com/example/repo",
            starting_ref="main",
            working_branch="cursor/branch-a",
            work_on_current_branch=False,
            auto_create_pr=False,
            output_branch="cursor/branch-a",
            mcp_url="http://127.0.0.1:8765/mcp",
        )
        first = self.store.claim_execution("run-branch-a")
        self.store.finish(
            "run-branch-a",
            first["attempt_count"],
            {
                "status": "finished",
                "summary": "Created branch A",
                "branch": "cursor/branch-a",
                "commit": "commit-a",
            },
        )

        self.store.create_run(
            "bc-branch-chain",
            "run-branch-b",
            "Create branch B",
            "mcp",
            output_branch="cursor/branch-b",
        )
        second = self.store.get_run("run-branch-b")
        self.assertEqual(second["starting_ref"], "cursor/branch-a")
        self.assertEqual(second["output_branch"], "cursor/branch-b")
        self.assertEqual(second["work_on_current_branch"], 0)

        second_claim = self.store.claim_execution("run-branch-b")
        self.store.finish(
            "run-branch-b",
            second_claim["attempt_count"],
            {
                "status": "finished",
                "summary": "Created branch B",
                "branch": "cursor/branch-b",
                "commit": "commit-b",
            },
        )
        self.store.create_run(
            "bc-branch-chain",
            "run-branch-c",
            "Create branch C",
            "mcp",
            output_branch="cursor/branch-c",
        )
        third = self.store.get_run("run-branch-c")
        self.assertEqual(third["starting_ref"], "cursor/branch-b")
        self.assertEqual(third["output_branch"], "cursor/branch-c")
        self.assertEqual(third["work_on_current_branch"], 0)

    def test_crash_after_sqlite_finish_before_ack_recovers(self) -> None:
        self.store.initialize()
        self.store.create_agent_and_run(
            agent_id="bc-finish-crash",
            run_id="run-finish-crash",
            name="Finish crash",
            prompt="Create a README",
            repo_url="https://github.com/example/repo",
            starting_ref="main",
            working_branch="cursor/finish-crash",
            work_on_current_branch=False,
            auto_create_pr=False,
            output_branch="cursor/finish-crash",
            mcp_url="http://127.0.0.1:8765/mcp",
        )

        class SimulatedCrash(BaseException):
            pass

        class CrashAfterFinishStore:
            def __init__(self, store: AgentStore):
                self.store = store

            def __getattr__(self, name: str):
                return getattr(self.store, name)

            def finish(self, run_id: str, epoch: int, result: dict) -> None:
                self.store.finish(run_id, epoch, result)
                raise SimulatedCrash

        class RecordingQueue:
            def __init__(self):
                self.acknowledgements = 0

            def refresh_lease(self, consumer: str, message_id: str) -> bool:
                return True

            def acknowledge_if_owned(
                self, consumer: str, message_id: str
            ) -> bool:
                self.acknowledgements += 1
                return True

        executions = 0

        async def execute(request, on_event=None):
            nonlocal executions
            executions += 1
            return {
                "status": "finished",
                "repo": request.repo,
                "startingRef": request.starting_ref,
                "workOnCurrentBranch": request.work_on_current_branch,
                "branch": request.output_branch,
                "commit": "abc123",
                "summary": "README created",
            }

        message = QueueMessage("message-1", "run-finish-crash")
        queue = RecordingQueue()
        with self.assertRaises(SimulatedCrash):
            process_message(
                message,
                CrashAfterFinishStore(self.store),
                queue,
                "worker-a",
                stale_after_ms=60_000,
                executor=execute,
            )

        stored = self.store.get_run(message.run_id)
        self.assertEqual(stored["status"], "FINISHED")
        self.assertEqual(queue.acknowledgements, 0)
        self.assertEqual(executions, 1)

        process_message(
            message,
            self.store,
            queue,
            "worker-b",
            stale_after_ms=60_000,
            executor=execute,
        )
        self.assertEqual(queue.acknowledgements, 1)
        self.assertEqual(executions, 1)

    def test_crash_after_git_publish_recovers_before_sqlite_finish(self) -> None:
        self.store.initialize()
        self.store.create_agent_and_run(
            agent_id="bc-git-crash",
            run_id="run-git-crash",
            name="Git crash",
            prompt="""
Create README.md.
Explain that this repository tests Git publication recovery.
""".strip(),
            repo_url="https://github.com/example/repo",
            starting_ref="main",
            working_branch="cursor/git-crash",
            work_on_current_branch=False,
            auto_create_pr=False,
            output_branch="cursor/git-crash",
            mcp_url="http://127.0.0.1:8765/mcp",
        )

        class SimulatedCrash(BaseException):
            pass

        class RecordingQueue:
            def __init__(self):
                self.acknowledgements = 0

            def refresh_lease(self, consumer: str, message_id: str) -> bool:
                return True

            def acknowledge_if_owned(
                self, consumer: str, message_id: str
            ) -> bool:
                self.acknowledgements += 1
                return True

        state = {
            "branch_head": None,
            "commit_messages": {},
            "commits": 0,
            "edits": 0,
            "crash_after_write": True,
        }

        class FakeGitHub:
            def __init__(self, repo: str):
                pass

            def default_branch(self) -> str:
                return "main"

            def get_ref(self, branch: str) -> str | None:
                return state["branch_head"]

            def commit_message(self, commit_sha: str) -> str:
                return state["commit_messages"][commit_sha]

            def download_ref(self, ref: str, destination: Path) -> str:
                destination.mkdir()
                (destination / "base.txt").write_text("base\n")
                return "base-sha"

            def create_commit(
                self, workspace: Path, message: str, parent_sha: str
            ) -> str:
                state["commits"] += 1
                commit_sha = f"commit-{state['commits']}"
                state["commit_messages"][commit_sha] = message
                return commit_sha

            def write_ref(
                self, branch: str, commit_sha: str, previous_sha: str | None
            ) -> None:
                state["branch_head"] = commit_sha
                if state["crash_after_write"]:
                    state["crash_after_write"] = False
                    raise SimulatedCrash

            def close(self) -> None:
                pass

        responses = [
            (
                '{"action":"write_file","path":"README.md",'
                '"content":"# Recovered\\n"}'
            ),
            '{"action":"finish","summary":"README created"}',
        ]

        class FakeClient:
            def __init__(self, mcp_url: str):
                pass

            async def __aenter__(self):
                state["edits"] += 1
                return self

            async def __aexit__(self, *args):
                return None

            async def call_tool(self, name: str, arguments: dict):
                return SimpleNamespace(
                    is_error=False,
                    structured_content={"result": responses.pop(0)},
                    content=[],
                )

        message = QueueMessage("message-2", "run-git-crash")
        queue = RecordingQueue()
        with (
            patch("cloud_agent.lib.runner.GitHubGitApi", FakeGitHub),
            patch("cloud_agent.lib.runner.Client", FakeClient),
        ):
            with self.assertRaises(SimulatedCrash):
                process_message(
                    message,
                    self.store,
                    queue,
                    "worker-a",
                    stale_after_ms=60_000,
                    executor=runner_module.run_agent,
                )

            interrupted = self.store.get_run(message.run_id)
            self.assertEqual(interrupted["status"], "RUNNING")
            self.assertEqual(interrupted["attempt_count"], 1)
            self.assertEqual(queue.acknowledgements, 0)

            process_message(
                message,
                self.store,
                queue,
                "worker-b",
                stale_after_ms=60_000,
                executor=runner_module.run_agent,
            )

        recovered = self.store.get_run(message.run_id)
        result = json.loads(recovered["result_json"])
        self.assertEqual(recovered["status"], "FINISHED")
        self.assertEqual(recovered["attempt_count"], 2)
        self.assertTrue(result["recovered"])
        self.assertEqual(result["commit"], "commit-1")
        self.assertEqual(state["commits"], 1)
        self.assertEqual(state["edits"], 1)
        self.assertEqual(queue.acknowledgements, 1)

        self.store.create_run(
            "bc-git-crash",
            "run-after-recovery",
            "Continue after recovery",
            "mcp",
        )
        recovered_history = self.store.conversation_before(
            "run-after-recovery"
        )
        self.assertEqual(len(recovered_history), 4)
        self.assertIn("Create README.md", recovered_history[0]["content"])
        self.assertEqual(
            recovered_history[-1],
            {
                "role": "assistant",
                "content": (
                    '{"action":"finish","summary":"README created"}'
                ),
            },
        )

    def test_killed_worker_message_is_autoclaimed(self) -> None:
        self.queue.initialize()
        self.queue.publish("run-lease-test")
        worker_a = subprocess.Popen(
            [
                sys.executable,
                "-c",
                HOLD_PENDING_SCRIPT,
                "redis://127.0.0.1:6379/15",
                self.stream,
                "workers",
            ],
            cwd=Path(__file__).resolve().parents[1],
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                pending = self.queue.client.xpending_range(
                    self.stream, "workers", "-", "+", 10
                )
                if any(item["consumer"] == "worker-a" for item in pending):
                    break
                time.sleep(0.02)
            else:
                self.fail("worker-a did not read the Redis message")

            time.sleep(0.12)
            self.assertEqual(
                self.queue.claim_stale("worker-b", min_idle_ms=100), []
            )

            worker_a.terminate()
            worker_a.wait(timeout=5)

            time.sleep(0.12)
            claimed = self.queue.claim_stale("worker-b", min_idle_ms=100)
            self.assertEqual(len(claimed), 1)
            self.assertEqual(claimed[0].run_id, "run-lease-test")
            self.assertFalse(
                self.queue.refresh_lease(
                    "worker-a", claimed[0].message_id
                )
            )
            self.queue.acknowledge(claimed[0].message_id)
        finally:
            if worker_a.poll() is None:
                worker_a.terminate()
                worker_a.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
