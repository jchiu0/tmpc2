import argparse
import importlib
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

support = importlib.import_module("02_multiple_runs")
subagent_support = importlib.import_module("04_subagent_delegation")
PYTHON = support.PYTHON
ROOT = support.ROOT
github_file = support.github_file
request_json = support.request_json
require_tcp_service = support.require_tcp_service
wait_for_api = support.wait_for_api
run_and_poll = subagent_support.run_and_poll


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run two readonly delegations followed by one writer"
    )
    parser.add_argument(
        "--repo", default="https://github.com/jchiu0/scratch1"
    )
    parser.add_argument(
        "--starting-ref",
        default="cursor/create-a-concise-readme-md-expla-386dcb",
    )
    parser.add_argument("--port", type=int, default=8013)
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
    branch = f"cursor/parallel-subagents-{test_id}"
    database = (
        ROOT / "cloud_agent" / "data" / f"parallel-subagents-{test_id}.db"
    )
    log_file = (
        ROOT
        / "cloud_agent"
        / "logs"
        / f"05_parallel-subagents-{test_id}.log"
    )
    stream = f"cloud-agents-parallel-subagents-{test_id}"
    group = f"parallel-subagents-{test_id}-workers"
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
Perform exactly three subagent delegations.

Your first response must be one action array containing exactly:
1. Delegate algorithm analysis to algorithm-analyst.
2. Delegate test planning to test-analyst.

Wait for both readonly results. Then perform the third delegation by asking
implementer to use both analyses to solve LeetCode 3Sum. It must create
three_sum.py and test_three_sum.py with comprehensive tests.
Do not solve or edit files yourself. Finish after the implementer succeeds.
""".strip()
                    },
                    "repos": [
                        {"url": args.repo, "startingRef": args.starting_ref}
                    ],
                    "name": "Bounded parallel subagent end to end",
                    "autoCreatePR": False,
                    "outputBranch": branch,
                    "customSubagents": [
                        {
                            "name": "algorithm-analyst",
                            "description": """
Analyzes algorithms and edge cases without editing files
""".strip(),
                            "prompt": """
Analyze the delegated algorithm problem.
Return concise implementation guidance and do not edit files.
""".strip(),
                            "readonly": True,
                        },
                        {
                            "name": "test-analyst",
                            "description": """
Designs test coverage without editing files
""".strip(),
                            "prompt": """
Analyze the delegated problem.
Return a concise test plan and do not edit files.
""".strip(),
                            "readonly": True,
                        },
                        {
                            "name": "implementer",
                            "description": """
Implements algorithms and their unit tests
""".strip(),
                            "prompt": """
Implement the delegated solution and comprehensive automated test coverage.
Do not finish until both production code and tests are complete.
""".strip(),
                        },
                    ],
                },
            )
            agent_id = created["agent"]["id"]
            run_id = created["run"]["id"]
            result, events = run_and_poll(
                environment, log_file, base_url, agent_id, run_id
            )

            lifecycle = [
                (event["type"], event["payload"].get("name"))
                for event in events
                if event["type"]
                in {"subagent.started", "subagent.finished"}
            ]
            starts = [
                item[1] for item in lifecycle if item[0] == "subagent.started"
            ]
            if starts != [
                "algorithm-analyst",
                "test-analyst",
                "implementer",
            ]:
                raise RuntimeError(
                    f"expected exactly three ordered delegations, got {starts}"
                )

            first_finish = min(
                index
                for index, item in enumerate(lifecycle)
                if item[0] == "subagent.finished"
            )
            second_analyst_start = lifecycle.index(
                ("subagent.started", "test-analyst")
            )
            if second_analyst_start > first_finish:
                raise RuntimeError(
                    "readonly analyst delegations did not overlap"
                )
            implementer_start = lifecycle.index(
                ("subagent.started", "implementer")
            )
            analyst_finishes = [
                lifecycle.index(("subagent.finished", name))
                for name in ("algorithm-analyst", "test-analyst")
            ]
            if implementer_start < max(analyst_finishes):
                raise RuntimeError(
                    "writable implementation began before analyses finished"
                )

            implementation = github_file(
                args.repo, result["branch"], "three_sum.py", github_token
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
                    [
                        str(PYTHON),
                        "-m",
                        "pytest",
                        "-q",
                        "test_three_sum.py",
                    ],
                    cwd=checkout,
                    check=True,
                )

            print(
                json.dumps(
                    {
                        "status": "passed",
                        "agentId": agent_id,
                        "runId": run_id,
                        "delegations": starts,
                        "branch": result["branch"],
                        "commit": result["commit"],
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
