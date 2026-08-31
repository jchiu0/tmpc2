import argparse
import importlib
import json
import os
import subprocess
import uuid

support = importlib.import_module("02_multiple_runs")


TOKEN = "BRANCH_CONTEXT_TOKEN_4C2B"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one agent across two chained output branches"
    )
    parser.add_argument(
        "--repo",
        default="https://github.com/jchiu0/scratch1",
    )
    parser.add_argument(
        "--starting-ref",
        default="cursor/create-a-concise-readme-md-expla-386dcb",
    )
    parser.add_argument("--port", type=int, default=8012)
    args = parser.parse_args()

    if not support.PYTHON.exists():
        raise RuntimeError(
            f"project Python environment not found: {support.PYTHON}"
        )
    support.require_tcp_service("127.0.0.1", 6379, "Redis")
    support.require_tcp_service("127.0.0.1", 8765, "Grok MCP server")

    github_token = os.getenv("GITHUB_TOKEN") or subprocess.run(
        ["gh", "auth", "token"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    test_id = uuid.uuid4().hex[:8]
    branch_a = f"cursor/multiple-runs-a-{test_id}"
    branch_b = f"cursor/multiple-runs-b-{test_id}"
    database = (
        support.ROOT
        / "cloud_agent"
        / "data"
        / f"multiple-branches-{test_id}.db"
    )
    log_file = (
        support.ROOT
        / "cloud_agent"
        / "logs"
        / f"03_multiple-branches-{test_id}.log"
    )
    stream = f"cloud-agents-multiple-branches-{test_id}"
    group = f"multiple-branches-{test_id}-workers"
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
                str(support.PYTHON),
                "-m",
                "uvicorn",
                "cloud_agent.service.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.port),
            ],
            cwd=support.ROOT,
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
                    "prompt": {
                        "text": f"""
Create MULTIPLE_BRANCHES_E2E.md containing only:

# Branch A

Do not write {TOKEN} into the file. Include {TOKEN} exactly once in your final
completion summary so the next run can recover it from conversation history.
""".strip()
                    },
                    "repos": [
                        {"url": args.repo, "startingRef": args.starting_ref}
                    ],
                    "name": "Multiple runs and branches end to end",
                    "workOnCurrentBranch": False,
                    "autoCreatePR": False,
                    "outputBranch": branch_a,
                },
            )
            agent_id = created["agent"]["id"]
            first_run_id = created["run"]["id"]
            first_result = support.run_and_poll(
                environment,
                log_file,
                base_url,
                agent_id,
                first_run_id,
            )
            if first_result["branch"] != branch_a:
                raise RuntimeError("first run published an unexpected branch")
            if TOKEN not in first_result["summary"]:
                raise RuntimeError(
                    "first run did not include the token in its final response"
                )

            followup = support.request_json(
                "POST",
                f"{base_url}/v1/agents/{agent_id}/runs",
                {
                    "prompt": {
                        "text": """
Change the heading in MULTIPLE_BRANCHES_E2E.md to `# Branch B`, then append a
blank line and the exact context token from your previous final response.
""".strip()
                    },
                    "outputBranch": branch_b,
                },
            )
            second_run_id = followup["run"]["id"]
            second_result = support.run_and_poll(
                environment,
                log_file,
                base_url,
                agent_id,
                second_run_id,
            )
            content = support.github_file(
                args.repo,
                branch_b,
                "MULTIPLE_BRANCHES_E2E.md",
                github_token,
            )
            if "# Branch B" not in content or TOKEN not in content:
                raise RuntimeError(
                    "second run did not use branch state and conversation"
                )
            if second_result["branch"] != branch_b:
                raise RuntimeError("second run did not publish branch B")
            if (
                support.github_commit_parent(
                    args.repo, second_result["commit"], github_token
                )
                != first_result["commit"]
            ):
                raise RuntimeError(
                    "branch B commit is not based on branch A commit"
                )

            print(
                json.dumps(
                    {
                        "status": "passed",
                        "agentId": agent_id,
                        "firstRunId": first_run_id,
                        "secondRunId": second_run_id,
                        "branchA": branch_a,
                        "commitA": first_result["commit"],
                        "branchB": branch_b,
                        "commitB": second_result["commit"],
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
