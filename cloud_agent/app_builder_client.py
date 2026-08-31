import argparse
import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Any


def request_json(
    method: str, url: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API returned {error.code}: {detail}") from error


def build_source(task: str) -> str:
    encoded_task = json.dumps(task)
    return f'''
import sys
from pathlib import Path

from cloud_agent.workflow import StateMachine, activity, state

TASK = {encoded_task}


@activity
def lint_application(ctx, _input):
    compile_result = ctx.run_command(
        [sys.executable, "-m", "compileall", "-q", "."]
    )
    lint_result = ctx.run_command(["ruff", "check", "."])
    return {{
        "passed": (
            compile_result["exitCode"] == 0
            and lint_result["exitCode"] == 0
        ),
        "compile": compile_result,
        "lint": lint_result,
    }}


@activity
def verify_application(ctx, _input):
    has_tests = any(
        any(ctx.workspace.glob(pattern))
        for pattern in ("test_*.py", "tests/test_*.py")
    )
    test_result = (
        ctx.run_command([sys.executable, "-m", "pytest", "-q"])
        if has_tests
        else {{"exitCode": 0, "stdout": "No pytest tests found", "stderr": ""}}
    )
    return {{
        "passed": test_result["exitCode"] == 0,
        "tests": test_result,
    }}


class AppBuilder(StateMachine):
    initial_state = "requirements"

    @state
    def requirements(self, ctx, event):
        if event.type == "entered":
            attempt = ctx.state.get("requirementsAttempt", 0)
            feedback = ctx.state.get("requirementsFeedback", "")
            return ctx.run_llm(
                f"requirements-{{attempt}}",
                "Fill in requirements using best effort for this task. "
                "Write APP_REQUIREMENTS.md with concrete acceptance criteria. "
                "Task:\\n" + TASK + (
                    "\\nUser feedback:\\n" + feedback if feedback else ""
                ),
            )
        ctx.state["requirements"] = event.result["summary"]
        return ctx.transition("requirements_approval")

    @state
    def requirements_approval(self, ctx, event):
        if event.type == "entered":
            return ctx.wait_for_user(
                "requirements-approval",
                "Requirements are ready. Enter 'approve' to continue, "
                "or provide feedback to revise them:",
            )
        response = event.response.strip()
        if response.lower() in {{"approve", "approved", "yes", "y"}}:
            return ctx.transition("planning")
        ctx.state["requirementsFeedback"] = response
        ctx.state["requirementsAttempt"] = (
            ctx.state.get("requirementsAttempt", 0) + 1
        )
        return ctx.transition("requirements")

    @state
    def planning(self, ctx, event):
        if event.type == "entered":
            return ctx.run_llm(
                "planning",
                "Read APP_REQUIREMENTS.md. Write APP_PLAN.md with ordered, "
                "independently verifiable implementation subtasks.",
            )
        ctx.state["plan"] = event.result["summary"]
        return ctx.transition("executing")

    @state
    def executing(self, ctx, event):
        attempt = ctx.state.get("executionAttempt", 0)
        if event.type == "entered":
            failure = {{
                "lint": ctx.state.get("lint"),
                "evaluation": ctx.state.get("evaluation"),
            }}
            return ctx.run_llm(
                f"executing-{{attempt}}",
                "Read APP_REQUIREMENTS.md and APP_PLAN.md. Implement all "
                "remaining subtasks and add tests. Fix this prior "
                "verification failure if present:\\n"
                + str(failure),
                copy_context=False,
            )
        ctx.state["implementation"] = event.result["summary"]
        return ctx.transition("linting")

    @state
    def linting(self, ctx, event):
        attempt = ctx.state.get("executionAttempt", 0)
        if event.type == "entered":
            return ctx.run_python(
                f"linting-{{attempt}}",
                "lint_application",
            )
        lint = event.result["activityResult"]
        ctx.state["lint"] = lint
        if not lint["passed"]:
            if attempt >= 2:
                return ctx.fail("Linting failed after three attempts")
            ctx.state["executionAttempt"] = attempt + 1
            return ctx.transition("executing")
        return ctx.transition("evaluating")

    @state
    def evaluating(self, ctx, event):
        attempt = ctx.state.get("executionAttempt", 0)
        if event.type == "entered":
            return ctx.run_python(
                f"evaluating-{{attempt}}",
                "verify_application",
            )
        verification = event.result["activityResult"]
        ctx.state["evaluation"] = verification
        if not verification["passed"]:
            if attempt >= 2:
                return ctx.fail("Evaluation failed after three attempts")
            ctx.state["executionAttempt"] = attempt + 1
            return ctx.transition("executing")
        return ctx.complete("Application built and evaluated")
'''.lstrip()


def print_responses(events: list[dict[str, Any]]) -> None:
    for event in events:
        if event["type"] != "conversation.message":
            continue
        payload = event["payload"]
        if (
            payload.get("role") != "assistant"
            or payload.get("kind") != "final_response"
        ):
            continue
        content = payload.get("content", "")
        try:
            action = json.loads(content)
            message = action.get("summary", content)
        except (json.JSONDecodeError, AttributeError):
            message = content
        print(f"\nAgent: {message}", flush=True)


def format_duration(milliseconds: int | None) -> str:
    if milliseconds is None:
        return "unknown"
    return f"{milliseconds / 1000:.2f}s"


def run(args: argparse.Namespace) -> int:
    task = input("What should the app builder create? ").strip()
    if not task:
        raise RuntimeError("task must not be empty")
    branch = args.output_branch or f"cursor/app-builder-{uuid.uuid4().hex[:8]}"
    created = request_json(
        "POST",
        f"{args.base_url}/v1/agents",
        {
            "source": {
                "language": "python",
                "code": build_source(task),
                "entrypoint": "AppBuilder",
            },
            "repos": [
                {"url": args.repo, "startingRef": args.starting_ref}
            ],
            "name": task[:100],
            "autoCreatePR": args.auto_create_pr,
            "outputBranch": branch,
        },
    )
    agent_id = created["agent"]["id"]
    run_id = created["run"]["id"]
    cursor = "0"
    last_state: tuple[str | None, int | None] | None = None
    timed_runs: set[str] = set()
    print(f"Agent: {agent_id}\nBranch: {branch}", flush=True)

    while True:
        response = request_json(
            "GET",
            (
                f"{args.base_url}/v1/agents/{agent_id}/runs/"
                f"{run_id}/events?after={cursor}&limit=100"
            ),
        )
        print_responses(response["events"])
        cursor = response["nextCursor"]
        workflow = request_json(
            "GET", f"{args.base_url}/v1/agents/{agent_id}/state"
        )
        if (
            response["status"] in {"FINISHED", "ERROR"}
            and run_id not in timed_runs
        ):
            timing = request_json(
                "GET",
                f"{args.base_url}/v1/agents/{agent_id}/runs/{run_id}",
            )
            print(
                f"Run {timing.get('workflowKey') or run_id}: "
                f"execution={format_duration(timing['durationMs'])}, "
                f"queue={format_duration(timing['queueDurationMs'])}",
                flush=True,
            )
            timed_runs.add(run_id)
        current_state = (workflow["state"], workflow["version"])
        if current_state != last_state:
            print(
                f"State: {workflow['state']} "
                f"(checkpoint {workflow['version']})",
                flush=True,
            )
            last_state = current_state
        if workflow["status"] == "COMPLETED":
            print(f"\nCompleted: {workflow['result']}", flush=True)
            return 0
        if workflow["status"] == "USER_INPUT":
            requested = workflow["userInput"]
            response_text = input(f"\nAgent: {requested['prompt']}\nYou: ")
            resumed = request_json(
                "POST",
                f"{args.base_url}/v1/agents/{agent_id}/input",
                {"response": response_text},
            )
            run_id = resumed["run"]["id"]
            cursor = "0"
            continue
        if workflow["status"] == "FAILED" or response["status"] == "ERROR":
            print(
                f"\nFailed: {workflow.get('result') or 'run failed'}",
                flush=True,
            )
            return 1
        next_run_id = workflow["latestRunId"]
        if response["status"] == "FINISHED" and next_run_id != run_id:
            run_id = next_run_id
            cursor = "0"
        time.sleep(args.poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create an app-builder workflow and poll its responses"
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--starting-ref", default="main")
    parser.add_argument("--output-branch")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument(
        "--auto-create-pr", action=argparse.BooleanOptionalAction, default=False
    )
    args = parser.parse_args()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
