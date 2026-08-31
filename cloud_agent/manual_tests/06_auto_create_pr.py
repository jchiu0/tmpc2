import argparse
import importlib
import json
import os
import subprocess
import tempfile
import urllib.request
import uuid
from pathlib import Path

from cloud_agent.lib.runner import MAX_PR_TITLE_WORDS


support = importlib.import_module("02_multiple_runs")
subagent_support = importlib.import_module("04_subagent_delegation")
PYTHON = support.PYTHON
ROOT = support.ROOT


def github_pull_request(repo: str, number: int, token: str) -> dict:
    repository = repo.removeprefix("https://github.com/").removesuffix(".git")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/pulls/{number}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create and verify an automatic pull request"
    )
    parser.add_argument(
        "--repo", default="https://github.com/jchiu0/scratch1"
    )
    parser.add_argument(
        "--starting-ref",
        default="cursor/create-a-concise-readme-md-expla-386dcb",
    )
    parser.add_argument("--port", type=int, default=8014)
    args = parser.parse_args()

    support.require_tcp_service("127.0.0.1", 6379, "Redis")
    support.require_tcp_service("127.0.0.1", 8765, "Grok MCP server")
    github_token = os.getenv("GITHUB_TOKEN") or subprocess.run(
        ["gh", "auth", "token"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    test_id = uuid.uuid4().hex[:8]
    branch = f"cursor/auto-pr-{test_id}"
    database = ROOT / "cloud_agent" / "data" / f"auto-pr-{test_id}.db"
    log_file = (
        ROOT / "cloud_agent" / "logs" / f"06_auto-pr-{test_id}.log"
    )
    stream = f"cloud-agents-auto-pr-{test_id}"
    group = f"auto-pr-{test_id}-workers"
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
            support.wait_for_api(base_url)
            created = support.request_json(
                "POST",
                f"{base_url}/v1/agents",
                {
                    "prompt": {
                        "text": """
Solve LeetCode's Longest Substring Without Repeating Characters problem.

Create longest_substring.py with:
length_of_longest_substring(text: str) -> int

Use an efficient sliding-window algorithm. Create test_longest_substring.py
with comprehensive pytest coverage, including empty input, repeated
characters, spaces, and Unicode. Include these exact regression cases:
length_of_longest_substring("a🙂b🙂c") == 3 and
length_of_longest_substring("東京トウキョウ") == 6.
""".strip()
                    },
                    "repos": [
                        {"url": args.repo, "startingRef": args.starting_ref}
                    ],
                    "name": "Automatic PR end to end",
                    "autoCreatePR": True,
                    "outputBranch": branch,
                },
            )
            agent_id = created["agent"]["id"]
            run_id = created["run"]["id"]
            result, _ = subagent_support.run_and_poll(
                environment, log_file, base_url, agent_id, run_id
            )
            pull_request = result.get("pullRequest")
            if not pull_request:
                raise RuntimeError("run did not return a pull request")

            remote = github_pull_request(
                args.repo, pull_request["number"], github_token
            )
            if remote["state"] != "open":
                raise RuntimeError("automatic pull request is not open")
            if remote["head"]["ref"] != branch:
                raise RuntimeError("pull request has the wrong head branch")
            if remote["base"]["ref"] != args.starting_ref:
                raise RuntimeError("pull request has the wrong base branch")
            if len(remote["title"].split()) > MAX_PR_TITLE_WORDS:
                raise RuntimeError(
                    "pull request title exceeds "
                    f"{MAX_PR_TITLE_WORDS} words"
                )

            implementation = support.github_file(
                args.repo,
                result["branch"],
                "longest_substring.py",
                github_token,
            )
            tests = support.github_file(
                args.repo,
                result["branch"],
                "test_longest_substring.py",
                github_token,
            )
            if not tests.strip():
                raise RuntimeError("agent did not create the requested tests")
            with tempfile.TemporaryDirectory() as temp:
                checkout = Path(temp)
                (checkout / "longest_substring.py").write_text(implementation)
                (checkout / "test_verification.py").write_text(
                    """
from longest_substring import length_of_longest_substring


def test_trusted_cases():
    cases = {
        "": 0,
        "abcabcbb": 3,
        "bbbbb": 1,
        "pwwkew": 3,
        "a b c": 3,
        "ab  cd": 3,
        "a🙂b🙂c": 3,
        "東京トウキョウ": 6,
    }
    for text, expected in cases.items():
        assert length_of_longest_substring(text) == expected
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

            print(
                json.dumps(
                    {
                        "status": "passed",
                        "agentId": agent_id,
                        "runId": run_id,
                        "branch": branch,
                        "commit": result["commit"],
                        "pullRequest": pull_request,
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
