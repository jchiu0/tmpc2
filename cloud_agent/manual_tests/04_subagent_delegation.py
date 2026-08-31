import argparse
import importlib
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

support = importlib.import_module("02_multiple_runs")
PYTHON = support.PYTHON
ROOT = support.ROOT
github_file = support.github_file
request_json = support.request_json
require_tcp_service = support.require_tcp_service
start_worker = support.start_worker
wait_for_api = support.wait_for_api


def run_and_poll(
    environment: dict[str, str],
    log_file,
    base_url: str,
    agent_id: str,
    run_id: str,
) -> tuple[dict, list[dict]]:
    worker = start_worker(environment, log_file)
    cursor = "0"
    events: list[dict] = []
    deadline = time.monotonic() + 180
    try:
        while time.monotonic() < deadline:
            response = request_json(
                "GET",
                (
                    f"{base_url}/v1/agents/{agent_id}/runs/"
                    f"{run_id}/events?after={cursor}"
                ),
            )
            events.extend(response["events"])
            cursor = response["nextCursor"]
            if response["status"] == "ERROR":
                raise RuntimeError(f"run {run_id} failed")
            if response["status"] == "FINISHED":
                for event in reversed(events):
                    if event["type"] == "run.status":
                        result = event["payload"].get("result")
                        if result:
                            return result, events
                raise RuntimeError(f"run {run_id} has no terminal result")
            if worker.poll() not in {None, 0}:
                raise RuntimeError(
                    f"worker exited with code {worker.returncode}"
                )
            time.sleep(0.25)
        raise RuntimeError(f"timed out waiting for run {run_id}")
    finally:
        if worker.poll() is None:
            worker.terminate()
        worker.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run real sequential subagent delegation"
    )
    parser.add_argument(
        "--repo", default="https://github.com/jchiu0/scratch1"
    )
    parser.add_argument(
        "--starting-ref",
        default="cursor/create-a-concise-readme-md-expla-386dcb",
    )
    parser.add_argument("--port", type=int, default=8012)
    args = parser.parse_args()

    if not PYTHON.exists():
        raise RuntimeError(f"project Python environment not found: {PYTHON}")
    require_tcp_service("127.0.0.1", 6379, "Redis")
    require_tcp_service("127.0.0.1", 8765, "Grok MCP server")

    github_token = os.getenv("GITHUB_TOKEN") or subprocess.run(
        ["gh", "auth", "token"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    test_id = uuid.uuid4().hex[:8]
    branch = f"cursor/subagent-{test_id}"
    database = ROOT / "cloud_agent" / "data" / f"subagent-{test_id}.db"
    log_file = (
        ROOT / "cloud_agent" / "logs" / f"04_subagent-{test_id}.log"
    )
    stream = f"cloud-agents-subagent-{test_id}"
    group = f"subagent-{test_id}-workers"
    base_url = f"http://127.0.0.1:{args.port}"
    environment = {
        **os.environ,
        "CLOUD_AGENT_DB": str(database),
        "AGENT_STREAM": stream,
        "AGENT_CONSUMER_GROUP": group,
        "GITHUB_TOKEN": github_token,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
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
                str(args.port),
            ],
            cwd=ROOT,
            env=environment,
            stdout=service_log,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_for_api(base_url)
            created = request_json(
                "POST",
                f"{base_url}/v1/agents",
                {
                    "prompt": {
                        "text": """
Delegate this complete implementation to the implementer subagent:

Solve the LeetCode 3Sum problem in three_sum.py. Implement
three_sum(nums: list[int]) -> list[list[int]] so it returns every unique
triplet whose values sum to zero. Each triplet must be sorted, the result must
be deterministic, and the function must not mutate its input.

Create test_three_sum.py using unittest. Cover the canonical example,
duplicate values, all zeroes, no solution, and input preservation.

Do not solve or write files yourself. After delegation succeeds, finish.
""".strip()
                    },
                    "repos": [
                        {"url": args.repo, "startingRef": args.starting_ref}
                    ],
                    "name": "Sequential subagent end to end",
                    "autoCreatePR": False,
                    "outputBranch": branch,
                    "customSubagents": [
                        {
                            "name": "implementer",
                            "description": """
Solves algorithmic problems and writes comprehensive unit tests
""".strip(),
                            "prompt": """
Act as the implementation owner for the delegated task.
Implement a clear and efficient solution and comprehensive tests.
Do not finish until both production code and tests are complete.
""".strip(),
                        }
                    ],
                },
            )
            agent_id = created["agent"]["id"]
            run_id = created["run"]["id"]
            result, events = run_and_poll(
                environment, log_file, base_url, agent_id, run_id
            )
            event_types = [event["type"] for event in events]
            if "subagent.started" not in event_types:
                raise RuntimeError("parent did not delegate to the subagent")
            if "subagent.finished" not in event_types:
                raise RuntimeError("subagent did not finish")
            child_tool_calls = [
                event["payload"]["content"]
                for event in events
                if event["type"] == "subagent.message"
                and event["payload"].get("kind") == "tool_call"
            ]
            if sum(
                call.count('"write_file"') for call in child_tool_calls
            ) < 2:
                raise RuntimeError("subagent did not create all required files")

            implementation = github_file(
                args.repo,
                result["branch"],
                "three_sum.py",
                github_token,
            )
            tests = github_file(
                args.repo,
                result["branch"],
                "test_three_sum.py",
                github_token,
            )
            with tempfile.TemporaryDirectory() as temp:
                checkout = Path(temp)
                (checkout / "three_sum.py").write_text(implementation)
                (checkout / "test_three_sum.py").write_text(tests)
                subprocess.run(
                    [str(PYTHON), "-m", "unittest", "test_three_sum.py"],
                    cwd=checkout,
                    check=True,
                )
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "agentId": agent_id,
                        "runId": run_id,
                        "branch": result["branch"],
                        "commit": result["commit"],
                        "implementation": implementation,
                        "database": str(database),
                        "log": str(log_file),
                    },
                    indent=2,
                )
            )
        finally:
            service.terminate()
            try:
                service.wait(timeout=5)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait(timeout=5)


if __name__ == "__main__":
    main()
