from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.client import Client

from prompts import (
    EVALUATE_SYSTEM_PROMPT,
    IMPLEMENT_SYSTEM_PROMPT,
    PLAN_SYSTEM_PROMPT,
    REQUIREMENTS_SYSTEM_PROMPT,
)


DEFAULT_MODEL = "grok-4.6"
DEFAULT_MCP_URL = "http://127.0.0.1:8765/mcp"
MAX_CONTEXT_BYTES = 100_000
MAX_WRITE_BYTES = 1_000_000
GENERATED_FILES = {
    "app_spec.json",
    "frontend/src/App.jsx",
    "frontend/src/styles.css",
    "README.md",
}
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
FIELD_TYPES = {"text", "integer", "real", "boolean"}
SNAPSHOT_IGNORED_PARTS = {
    ".agent",
    ".git",
    ".venv",
    "__pycache__",
    "data",
    "dist",
    "node_modules",
    "playwright-report",
    "test-results",
}


class AgentError(RuntimeError):
    pass


def parse_json_response(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise AgentError("The model did not return a JSON object")

    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise AgentError(f"The model returned invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise AgentError("The model response must be a JSON object")
    return value


def safe_path(workspace: Path, relative_path: str) -> Path:
    requested = Path(relative_path)
    if requested.is_absolute() or ".git" in requested.parts:
        raise AgentError(f"Unsafe output path: {relative_path}")

    root = workspace.resolve()
    resolved = (workspace / requested).resolve()
    if resolved == root or root not in resolved.parents:
        raise AgentError(f"Output path escapes the workspace: {relative_path}")
    return resolved


def write_generated_files(workspace: Path, payload: dict[str, Any]) -> list[str]:
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise AgentError("Implementation response contains no files")

    written: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            raise AgentError("Each generated file must be an object")
        relative_path = item.get("path")
        content = item.get("content")
        if not isinstance(relative_path, str) or not isinstance(content, str):
            raise AgentError("Each generated file needs string path and content")
        if relative_path not in GENERATED_FILES:
            raise AgentError(f"Generated path is not allowed: {relative_path}")
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise AgentError(f"Generated file is too large: {relative_path}")

        path = safe_path(workspace, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(relative_path)
    missing = GENERATED_FILES.difference(written)
    if missing:
        raise AgentError(f"Implementation omitted required files: {sorted(missing)}")
    validate_app_spec(workspace / "app_spec.json")
    validate_frontend(workspace)
    return written


def validate_app_spec(path: Path) -> None:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise AgentError(f"app_spec.json is invalid: {error}") from error

    resources = spec.get("resources") if isinstance(spec, dict) else None
    if not isinstance(resources, list) or not resources:
        raise AgentError("app_spec.json must define at least one resource")
    resource_names: set[str] = set()
    for resource in resources:
        name = resource.get("name") if isinstance(resource, dict) else None
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
            raise AgentError("Resource names must be lowercase snake_case")
        if name in resource_names:
            raise AgentError(f"Duplicate resource name: {name}")
        resource_names.add(name)
        fields = resource.get("fields")
        if not isinstance(fields, list) or not fields:
            raise AgentError("Every resource must define at least one field")
        field_names: set[str] = set()
        for field in fields:
            field_name = field.get("name") if isinstance(field, dict) else None
            if not isinstance(field_name, str) or not IDENTIFIER.fullmatch(field_name):
                raise AgentError("Field names must be lowercase snake_case")
            if field_name in {"id", "created_at", "updated_at"}:
                raise AgentError(f"Reserved field name: {field_name}")
            if field_name in field_names:
                raise AgentError(f"Duplicate field name: {field_name}")
            field_names.add(field_name)
            if field.get("type") not in FIELD_TYPES:
                raise AgentError(f"Unsupported field type: {field.get('type')}")
            if not isinstance(field.get("required", False), bool):
                raise AgentError("Field required values must be booleans")

    ai_actions = spec.get("ai_actions", [])
    if not isinstance(ai_actions, list):
        raise AgentError("ai_actions must be a list")
    action_names: set[str] = set()
    for action in ai_actions:
        name = action.get("name") if isinstance(action, dict) else None
        prompt = action.get("system_prompt") if isinstance(action, dict) else None
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
            raise AgentError("AI action names must be lowercase snake_case")
        if name in action_names:
            raise AgentError(f"Duplicate AI action name: {name}")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 10_000:
            raise AgentError(f"Invalid system prompt for AI action: {name}")
        action_names.add(name)


def validate_frontend(workspace: Path) -> None:
    app = (workspace / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")
    if '"./styles.css"' not in app and "'./styles.css'" not in app:
        raise AgentError("App.jsx must import ./styles.css")
    if "/api/" not in app:
        raise AgentError("App.jsx must use the fixed /api endpoints")
    for test_id in (
        "app-root",
        "primary-input",
        "create-submit",
        "resource-item",
        "delete-button",
    ):
        if f'data-testid="{test_id}"' not in app:
            raise AgentError(f"App.jsx must define data-testid={test_id}")


def prepare_workspace(workspace: Path) -> None:
    if workspace.exists() and workspace.is_symlink():
        raise AgentError("The workspace cannot be a symlink")
    workspace.mkdir(parents=True, exist_ok=True)
    template = Path(__file__).parent / "template"
    for source in template.rglob("*"):
        if not source.is_file():
            continue
        destination = workspace / source.relative_to(template)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def write_artifact(workspace: Path, name: str, content: str) -> None:
    path = safe_path(workspace, f".agent/{name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def workspace_snapshot(workspace: Path) -> str:
    chunks: list[str] = []
    size = 0
    if not workspace.exists():
        return "(empty workspace)"

    for path in sorted(workspace.rglob("*")):
        if (
            not path.is_file()
            or SNAPSHOT_IGNORED_PARTS.intersection(path.parts)
            or path.is_symlink()
        ):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        chunk = f"\n--- {path.relative_to(workspace).as_posix()} ---\n{content}"
        encoded_size = len(chunk.encode("utf-8"))
        if size + encoded_size > MAX_CONTEXT_BYTES:
            chunks.append("\n(snapshot truncated)")
            break
        chunks.append(chunk)
        size += encoded_size
    return "".join(chunks) or "(empty workspace)"


async def ask_model(client: Client, model: str, instructions: str, prompt: str) -> str:
    result = await client.call_tool(
        "ask_grok",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": prompt},
            ],
        },
    )
    if result.is_error:
        message = " ".join(
            item.text for item in result.content if hasattr(item, "text")
        )
        raise AgentError(message or "MCP model call failed")
    if result.structured_content and "result" in result.structured_content:
        return str(result.structured_content["result"])
    return "".join(item.text for item in result.content if hasattr(item, "text"))


async def gather_requirements(
    client: Client,
    model: str,
    task: str,
    preset_answers: list[str] | None = None,
) -> list[dict[str, str]]:
    raw = await ask_model(
        client,
        model,
        REQUIREMENTS_SYSTEM_PROMPT,
        f"""
Original task:
{task}
""".strip(),
    )
    payload = parse_json_response(raw)
    questions = payload.get("questions")
    if not isinstance(questions, list) or not questions:
        raise AgentError("Requirements response contains no questions")
    if preset_answers is not None and len(preset_answers) != len(questions):
        raise AgentError(
            f"Expected {len(questions)} preset answers, got {len(preset_answers)}. "
            f"Questions: {json.dumps(questions)}"
        )

    answers: list[dict[str, str]] = []
    print("\nClarify the requirements (press Enter to accept an open assumption):")
    for index, item in enumerate(questions, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("question"), str):
            raise AgentError("Each requirement question needs a question string")
        question = item["question"]
        if preset_answers is None:
            answer = input(f"\n{index}. {question}\n> ").strip()
        else:
            answer = preset_answers[index - 1].strip()
            print(f"\n{index}. {question}\n> {answer}")
        answers.append({"question": question, "answer": answer or "Open assumption"})
    return answers


async def create_plan(
    client: Client,
    model: str,
    task: str,
    requirements: list[dict[str, str]],
    feedback: str = "",
) -> str:
    prompt = f"""
Original task:
{task}

Clarified requirements:
{json.dumps(requirements, indent=2)}
""".strip()
    if feedback:
        prompt += f"""

User feedback on the previous plan:
{feedback}
"""
    return await ask_model(client, model, PLAN_SYSTEM_PROMPT, prompt)


async def approve_plan(
    client: Client,
    model: str,
    task: str,
    requirements: list[dict[str, str]],
    auto_approve: bool = False,
) -> str:
    plan = await create_plan(client, model, task, requirements)
    while True:
        print(f"\nProposed plan\n{'=' * 13}\n{plan}")
        if auto_approve:
            print("\nPlan auto-approved for deterministic test run.")
            return plan
        choice = input("\n[P]roceed, [R]evise, or [Q]uit? ").strip().lower()
        if choice in {"", "p", "proceed"}:
            return plan
        if choice in {"q", "quit"}:
            raise AgentError("Stopped before implementation")
        if choice in {"r", "revise"}:
            feedback = input("What should change in the plan?\n> ").strip()
            plan = await create_plan(client, model, task, requirements, feedback)
            continue
        print("Please enter P, R, or Q.")


async def implement(
    client: Client,
    model: str,
    task: str,
    requirements: list[dict[str, str]],
    plan: str,
    workspace: Path,
) -> tuple[str, list[str]]:
    prompt = f"""
Original task:
{task}

Clarified requirements:
{json.dumps(requirements, indent=2)}

Approved plan:
{plan}

Current workspace:
{workspace_snapshot(workspace)}
""".strip()
    raw = await ask_model(client, model, IMPLEMENT_SYSTEM_PROMPT, prompt)
    payload = parse_json_response(raw)
    written = write_generated_files(workspace, payload)
    return str(payload.get("summary", "Implementation generated")), written


async def run_command(
    command: list[str], cwd: Path, timeout: int = 300
) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PYTHON": sys.executable},
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        output, _ = await process.communicate()
        return 124, output.decode(errors="replace") + "\nTimed out."
    return process.returncode or 0, output.decode(errors="replace")


async def run_e2e(workspace: Path) -> dict[str, Any]:
    frontend = workspace / "frontend"
    if shutil.which("npm") is None:
        return {"status": "skipped", "output": "npm is not installed"}

    for command in (
        ["npm", "install"],
        ["npx", "playwright", "install", "chromium"],
    ):
        returncode, output = await run_command(command, frontend)
        if returncode:
            return {
                "status": "failed",
                "command": " ".join(command),
                "returncode": returncode,
                "output": output[-20_000:],
            }

    returncode, output = await run_command(
        ["npm", "run", "test:e2e"], frontend, timeout=180
    )
    return {
        "status": "passed" if returncode == 0 else "failed",
        "returncode": returncode,
        "output": output[-20_000:],
    }


async def evaluate(
    client: Client,
    model: str,
    task: str,
    requirements: list[dict[str, str]],
    plan: str,
    workspace: Path,
    e2e_result: dict[str, Any],
) -> str:
    prompt = f"""
Original task:
{task}

Clarified requirements:
{json.dumps(requirements, indent=2)}

Approved plan:
{plan}

Generated files:
{workspace_snapshot(workspace)}

E2E result:
{json.dumps(e2e_result, indent=2)}
""".strip()
    return await ask_model(client, model, EVALUATE_SYSTEM_PROMPT, prompt)


async def run(args: argparse.Namespace) -> None:
    from mcp.client import Client

    workspace = Path(args.workspace)
    prepare_workspace(workspace)
    preset_answers = None
    if args.answers_file:
        answers_value = json.loads(Path(args.answers_file).read_text(encoding="utf-8"))
        if not isinstance(answers_value, list) or not all(
            isinstance(answer, str) for answer in answers_value
        ):
            raise AgentError("The answers file must contain a JSON array of strings")
        preset_answers = answers_value
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

    async with Client(args.mcp_url) as client:
        print(f"Task: {args.task}")
        requirements = await gather_requirements(
            client, args.model, args.task, preset_answers
        )
        write_artifact(workspace, "task.txt", args.task + "\n")
        write_artifact(
            workspace,
            "requirements.json",
            json.dumps(requirements, indent=2) + "\n",
        )
        plan = await approve_plan(
            client,
            args.model,
            args.task,
            requirements,
            auto_approve=args.approve_plan,
        )
        write_artifact(workspace, "plan.md", plan + "\n")

        print("\nImplementing...")
        summary, written = await implement(
            client, args.model, args.task, requirements, plan, workspace
        )
        print(f"{summary}\nWritten: {', '.join(written)}")

        if args.skip_e2e:
            e2e_result = {"status": "skipped", "output": "Disabled by --skip-e2e"}
        else:
            print("\nRunning Playwright E2E...")
            e2e_result = await run_e2e(workspace)
        write_artifact(
            workspace, "e2e.json", json.dumps(e2e_result, indent=2) + "\n"
        )
        print(f"E2E: {e2e_result['status']}")

        print("\nEvaluating...")
        evaluation = await evaluate(
            client, args.model, args.task, requirements, plan, workspace, e2e_result
        )
        write_artifact(workspace, "evaluation.md", evaluation + "\n")
        print(f"\nEvaluation\n{'=' * 10}\n{evaluation}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Turn a vague task into requirements, a plan, files, and an evaluation"
    )
    parser.add_argument("--task", required=True, help="The initial vague user task")
    parser.add_argument(
        "--workspace",
        default="generated",
        help="Local directory where generated files are written",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    parser.add_argument("--answers-file")
    parser.add_argument("--approve-plan", action="store_true")
    parser.add_argument("--skip-e2e", action="store_true")
    return parser.parse_args()


def main() -> None:
    try:
        asyncio.run(run(parse_args()))
    except (AgentError, KeyboardInterrupt) as error:
        print(f"\nError: {error}")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
