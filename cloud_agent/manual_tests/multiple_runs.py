import argparse
import base64
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / "cloud_agent" / ".venv" / "bin" / "python"
TOKEN = "CONTEXT_TOKEN_73A9"


def request_json(
    method: str, url: str, payload: dict | None = None
) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        return json.load(response)


def wait_for_api(base_url: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            request_json("GET", f"{base_url}/openapi.json")
            return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise RuntimeError("cloud agent API did not start")


def require_tcp_service(host: str, port: int, name: str) -> None:
    try:
        with socket.create_connection((host, port), timeout=1):
            return
    except OSError as error:
        raise RuntimeError(f"{name} is not reachable at {host}:{port}") from error


def start_worker(
    environment: dict[str, str], log_file: Path
) -> subprocess.Popen:
    return subprocess.Popen(
        [
            str(PYTHON),
            "-m",
            "cloud_agent.service.worker",
            "--once",
            "--log-file",
            str(log_file),
        ],
        cwd=ROOT,
        env=environment,
    )


def run_and_poll(
    environment: dict[str, str],
    log_file: Path,
    base_url: str,
    agent_id: str,
    run_id: str,
) -> dict:
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
            if response["status"] in {"FINISHED", "ERROR"}:
                if response["status"] != "FINISHED":
                    raise RuntimeError(
                        f"run {run_id} ended with status {response['status']}"
                    )
                for event in reversed(events):
                    if event["type"] == "run.status":
                        result = event["payload"].get("result")
                        if result:
                            return result
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
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=5)


def github_file(repo: str, branch: str, path: str, token: str) -> str:
    repository = repo.removeprefix("https://github.com/").removesuffix(".git")
    encoded_path = urllib.parse.quote(path)
    encoded_ref = urllib.parse.quote(branch, safe="")
    request = urllib.request.Request(
        (
            f"https://api.github.com/repos/{repository}/contents/"
            f"{encoded_path}?ref={encoded_ref}"
        ),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    return base64.b64decode(payload["content"]).decode()


def github_commit_parent(repo: str, commit: str, token: str) -> str:
    repository = repo.removeprefix("https://github.com/").removesuffix(".git")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/git/commits/{commit}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    return payload["parents"][0]["sha"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a real two-run conversation test"
    )
    parser.add_argument(
        "--repo",
        default="https://github.com/jchiu0/scratch1",
    )
    parser.add_argument(
        "--starting-ref",
        default="cursor/create-a-concise-readme-md-expla-386dcb",
    )
    parser.add_argument("--port", type=int, default=8011)
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
    branch = f"cursor/multiple-runs-{test_id}"
    database = ROOT / "cloud_agent" / "data" / f"multiple-runs-{test_id}.db"
    log_file = (
        ROOT / "cloud_agent" / "logs" / f"multiple-runs-{test_id}.log"
    )
    stream = f"cloud-agents-multiple-runs-{test_id}"
    group = f"multiple-runs-{test_id}-workers"
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
                        "text": f"""
Create MULTIPLE_RUNS_E2E.md containing only:

# Multiple Runs E2E

Do not write {TOKEN} into the file. Include {TOKEN} exactly once in your final
completion summary so a follow-up run can recover it from conversation history.
""".strip()
                    },
                    "repos": [
                        {"url": args.repo, "startingRef": args.starting_ref}
                    ],
                    "name": "Multiple runs end to end",
                    "workOnCurrentBranch": False,
                    "autoCreatePR": False,
                    "outputBranch": branch,
                },
            )
            agent_id = created["agent"]["id"]
            first_run_id = created["run"]["id"]
            first_result = run_and_poll(
                environment,
                log_file,
                base_url,
                agent_id,
                first_run_id,
            )
            if TOKEN not in first_result["summary"]:
                raise RuntimeError(
                    "first run did not include the context token in its summary"
                )
            if first_result["branch"] != branch or not first_result["commit"]:
                raise RuntimeError("first run published an unexpected branch")

            followup = request_json(
                "POST",
                f"{base_url}/v1/agents/{agent_id}/runs",
                {
                    "prompt": {
                        "text": """
Append a blank line and the exact context token from your previous final
response to MULTIPLE_RUNS_E2E.md. Do not invent or alter the token.
""".strip()
                    }
                },
            )
            second_run_id = followup["run"]["id"]
            second_result = run_and_poll(
                environment,
                log_file,
                base_url,
                agent_id,
                second_run_id,
            )
            content = github_file(
                args.repo, second_result["branch"], "MULTIPLE_RUNS_E2E.md",
                github_token,
            )
            if TOKEN not in content:
                raise RuntimeError(
                    "second run did not recover the token from prior context"
                )
            if second_result["branch"] != first_result["branch"]:
                raise RuntimeError("follow-up run created a different branch")
            if second_result["commit"] == first_result["commit"]:
                raise RuntimeError("follow-up run did not create a new commit")
            if (
                github_commit_parent(
                    args.repo, second_result["commit"], github_token
                )
                != first_result["commit"]
            ):
                raise RuntimeError(
                    "follow-up commit is not based on the first run commit"
                )

            print(
                json.dumps(
                    {
                        "status": "passed",
                        "agentId": agent_id,
                        "firstRunId": first_run_id,
                        "secondRunId": second_run_id,
                        "branch": second_result["branch"],
                        "commit": second_result["commit"],
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
