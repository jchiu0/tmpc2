import argparse
import importlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


support = importlib.import_module("02_multiple_runs")
polling = importlib.import_module("04_subagent_delegation")
PYTHON = support.PYTHON
ROOT = support.ROOT

APP_BUILDER_SOURCE = """
import sys

from cloud_agent.workflow import StateMachine, activity, state


@activity
def verify_application(ctx, _input):
    check = ctx.run_command([
        sys.executable,
        "-c",
        (
            "from task_tracker import add_task, complete_task;"
            "tasks=[];add_task(tasks,'Ship app');"
            "assert tasks;complete_task(tasks,0)"
        ),
    ])
    passed = check["exitCode"] == 0
    (ctx.workspace / "EVALUATION.md").write_text(
        "# Evaluation\\n\\n"
        + ("PASS" if passed else "FAIL")
        + "\\n\\n"
        + check["stdout"]
        + check["stderr"]
    )
    return {"passed": passed, "check": check}


class AppBuilder(StateMachine):
    initial_state = "requirements"

    @state
    def requirements(self, ctx, event):
        if event.type == "entered":
            return ctx.run_llm(
                "requirements",
                '''
Fill in requirements using best effort for a minimal Python task-tracker
library with only add_task(tasks, title) and complete_task(tasks, index).
Explicitly exclude persistence, CLI, classes, and external dependencies.
Write the requirements and acceptance criteria to APP_REQUIREMENTS.md.
'''.strip(),
            )
        ctx.state["requirements"] = event.result["summary"]
        return ctx.transition("planning")

    @state
    def planning(self, ctx, event):
        if event.type == "entered":
            return ctx.run_llm(
                "planning",
                '''
Read APP_REQUIREMENTS.md and write APP_PLAN.md with ordered implementation
and test subtasks for only those two functions. Do not expand scope and do not
implement the application in this step.
'''.strip(),
            )
        ctx.state["plan"] = event.result["summary"]
        return ctx.transition("executing")

    @state
    def executing(self, ctx, event):
        attempt = ctx.state.get("executionAttempt", 0)
        if event.type == "entered":
            return ctx.run_llm(
                f"executing-{attempt}",
                '''
Read APP_REQUIREMENTS.md and APP_PLAN.md. Implement a small task-tracker
library in task_tracker.py with add_task(tasks, title) and
complete_task(tasks, index). If EVALUATION.md reports a failure, address it.
Keep the implementation concise.
'''.strip(),
            )
        ctx.state["implementation"] = event.result["summary"]
        return ctx.transition("evaluating")

    @state
    def evaluating(self, ctx, event):
        attempt = ctx.state.get("executionAttempt", 0)
        if event.type == "entered":
            return ctx.run_python(
                f"evaluating-{attempt}",
                "verify_application",
            )
        verification = event.result["activityResult"]
        ctx.state["evaluation"] = verification
        if not verification["passed"]:
            ctx.state["executionAttempt"] = attempt + 1
            return ctx.transition("executing")
        return ctx.complete("Task-tracker app built and evaluated")
"""


def run_scenario(
    *,
    repo: str,
    starting_ref: str,
    port: int,
    prefix: str,
    crash_run_index: int | None = None,
) -> dict:
    support.require_tcp_service("127.0.0.1", 6379, "Redis")
    support.require_tcp_service("127.0.0.1", 8765, "Grok MCP server")
    github_token = os.getenv("GITHUB_TOKEN") or subprocess.run(
        ["gh", "auth", "token"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    test_id = uuid.uuid4().hex[:8]
    branch = f"cursor/state-machine-{prefix}-{test_id}"
    database = (
        ROOT / "cloud_agent" / "data" / f"{prefix}-{test_id}.db"
    )
    log_file = (
        ROOT / "cloud_agent" / "logs" / f"{prefix}-{test_id}.log"
    )
    stream = f"cloud-agents-{prefix}-{test_id}"
    group = f"{prefix}-{test_id}-workers"
    base_url = f"http://127.0.0.1:{port}"
    environment = {
        **os.environ,
        "CLOUD_AGENT_DB": str(database),
        "AGENT_STREAM": stream,
        "AGENT_CONSUMER_GROUP": group,
        "GITHUB_TOKEN": github_token,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    if crash_run_index is not None:
        environment["AGENT_STALE_AFTER_MS"] = "1000"
    subprocess.run(
        ["redis-cli", "DEL", stream],
        check=True,
        capture_output=True,
        text=True,
    )
    log_file.parent.mkdir(parents=True, exist_ok=True)

    with log_file.open("w") as service_log:
        service = subprocess.Popen(
            [
                str(PYTHON),
                "-m",
                "uvicorn",
                "cloud_agent.service.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            env=environment,
            stdout=service_log,
            stderr=subprocess.STDOUT,
        )
        try:
            support.wait_for_api(base_url)
            created = support.request_json(
                "POST",
                f"{base_url}/v1/agents",
                {
                    "source": {
                        "language": "python",
                        "code": APP_BUILDER_SOURCE,
                        "entrypoint": "AppBuilder",
                    },
                    "repos": [{"url": repo, "startingRef": starting_ref}],
                    "name": "State-machine app builder",
                    "autoCreatePR": False,
                    "outputBranch": branch,
                },
            )
            agent_id = created["agent"]["id"]
            run_id = created["run"]["id"]
            run_ids: list[str] = []
            results: list[dict] = []
            recovered_attempts = None
            for index in range(8):
                if index == crash_run_index:
                    with log_file.open("a") as worker_log:
                        worker_a = subprocess.Popen(
                            [
                                str(PYTHON),
                                "-m",
                                "cloud_agent.service.worker",
                                "--once",
                                "--execution-delay",
                                "30",
                                "--log-file",
                                str(log_file),
                            ],
                            cwd=ROOT,
                            env=environment,
                            stdout=worker_log,
                            stderr=subprocess.STDOUT,
                        )
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        with sqlite3.connect(database) as connection:
                            row = connection.execute(
                                """
                                SELECT status, attempt_count FROM runs
                                WHERE run_id = ?
                                """,
                                (run_id,),
                            ).fetchone()
                        if row == ("RUNNING", 1):
                            break
                        time.sleep(0.1)
                    else:
                        worker_a.terminate()
                        worker_a.wait(timeout=5)
                        raise RuntimeError("delayed worker did not claim run")
                    worker_a.terminate()
                    worker_a.wait(timeout=5)
                    time.sleep(1.2)
                result, _ = polling.run_and_poll(
                    environment, log_file, base_url, agent_id, run_id
                )
                if index == crash_run_index:
                    with sqlite3.connect(database) as connection:
                        recovered_attempts = connection.execute(
                            """
                            SELECT attempt_count FROM runs WHERE run_id = ?
                            """,
                            (run_id,),
                        ).fetchone()[0]
                    if recovered_attempts != 2:
                        raise RuntimeError(
                            "replacement worker did not reclaim the run"
                        )
                run_ids.append(run_id)
                results.append(result)
                workflow = support.request_json(
                    "GET", f"{base_url}/v1/agents/{agent_id}/state"
                )
                if workflow["status"] == "COMPLETED":
                    break
                run_id = workflow["latestRunId"]
            if len(run_ids) < 4 or len(set(run_ids)) != len(run_ids):
                raise RuntimeError(
                    "workflow did not create distinct phase runs"
                )
            if workflow["status"] != "COMPLETED":
                raise RuntimeError("workflow did not complete")

            files = {
                name: support.github_file(
                    repo, branch, name, github_token
                )
                for name in (
                    "APP_REQUIREMENTS.md",
                    "APP_PLAN.md",
                    "task_tracker.py",
                    "EVALUATION.md",
                )
            }
            with tempfile.TemporaryDirectory() as temp:
                checkout = Path(temp)
                for name, content in files.items():
                    (checkout / name).write_text(content)
                (checkout / "test_verification.py").write_text(
                    """
from task_tracker import add_task, complete_task


def test_task_tracker_round_trip():
    tasks = []
    add_task(tasks, "Ship app")
    assert tasks
    assert "Ship app" in str(tasks[0])
    complete_task(tasks, 0)
    assert any(
        marker in str(tasks[0]).lower()
        for marker in ("done", "complete", "true")
    )
""".lstrip()
                )
                subprocess.run(
                    [
                        str(PYTHON),
                        "-m",
                        "pytest",
                        "-q",
                        "test_verification.py",
                    ],
                    cwd=checkout,
                    check=True,
                )
            return {
                "status": "passed",
                "agentId": agent_id,
                "runIds": run_ids,
                "branch": branch,
                "commits": [result["commit"] for result in results],
                "workflow": workflow,
                "recoveredAttempts": recovered_attempts,
                "database": str(database),
                "log": str(log_file),
            }
        finally:
            service.terminate()
            try:
                service.wait(timeout=5)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the state-machine app-builder happy path"
    )
    parser.add_argument(
        "--repo", default="https://github.com/jchiu0/scratch1"
    )
    parser.add_argument(
        "--starting-ref",
        default="cursor/create-a-concise-readme-md-expla-386dcb",
    )
    parser.add_argument("--port", type=int, default=8015)
    args = parser.parse_args()
    print(
        json.dumps(
            run_scenario(
                repo=args.repo,
                starting_ref=args.starting_ref,
                port=args.port,
                prefix="07_state-machine-happy",
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
