import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from mcp.client import Client

from github_api import GitHubGitApi


MAX_STEPS = 30
MAX_FILE_BYTES = 200_000
MAX_WRITE_BYTES = 1_000_000
MAX_LISTED_FILES = 500
DEFAULT_MCP_URL = "http://127.0.0.1:8765/mcp"


class AgentError(RuntimeError):
    pass


def validate_repo_url(repo: str) -> None:
    if not repo.startswith("https://github.com/"):
        raise AgentError("repo must be an https://github.com/ URL")


def validate_branch(branch: str) -> None:
    forbidden = set(" ~^:?*[\\")
    components = branch.split("/")
    invalid = (
        not branch
        or branch == "@"
        or branch.startswith("/")
        or branch.endswith(("/", "."))
        or "//" in branch
        or ".." in branch
        or "@{" in branch
        or any(ord(character) < 32 or character in forbidden for character in branch)
        or any(
            not component
            or component.startswith(".")
            or component.endswith(".lock")
            for component in components
        )
    )
    if invalid:
        raise AgentError(f"invalid branch name: {branch}")


def select_branches(
    starting_ref: str | None,
    default_branch: str,
    work_on_current_branch: bool,
    output_branch: str | None,
    prompt: str,
) -> tuple[str, str]:
    base = starting_ref or default_branch
    validate_branch(base)

    if work_on_current_branch:
        return base, base

    branch = output_branch or generated_branch(prompt)
    validate_branch(branch)
    return base, branch


def generated_branch(prompt: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:32]
    return f"cursor/{slug or 'agent-change'}-{uuid.uuid4().hex[:6]}"


def workspace_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        candidate
        for candidate in root.rglob("*")
        if candidate.is_file() and ".git" not in candidate.parts
    ):
        if path.is_symlink():
            raise AgentError(f"symlinks are not supported: {path}")
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def safe_path(root: Path, relative: str, allow_root: bool = False) -> Path:
    requested = Path(relative)
    if requested.is_absolute() or ".git" in requested.parts:
        raise AgentError("path must stay inside the workspace and outside .git")
    resolved_root = root.resolve()
    resolved = (root / requested).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise AgentError("path escapes the workspace")
    if resolved == resolved_root and not allow_root:
        raise AgentError("a file path is required")
    return resolved


def list_files(root: Path, relative: str = ".") -> list[str]:
    directory = safe_path(root, relative, allow_root=True)
    if not directory.exists() or not directory.is_dir():
        raise AgentError(f"directory not found: {relative}")

    files: list[str] = []
    for path in directory.rglob("*"):
        if ".git" in path.parts or path.is_symlink() or not path.is_file():
            continue
        files.append(path.relative_to(root).as_posix())
        if len(files) >= MAX_LISTED_FILES:
            break
    return sorted(files)


def read_file(root: Path, relative: str) -> str:
    path = safe_path(root, relative)
    if not path.is_file():
        raise AgentError(f"file not found: {relative}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise AgentError(f"file is too large to read: {relative}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise AgentError(f"file is not UTF-8 text: {relative}") from error


def write_file(root: Path, relative: str, content: str) -> None:
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise AgentError(f"content is too large to write: {relative}")
    path = safe_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Resolve again after creating parents to catch any existing symlink.
    path = safe_path(root, relative)
    if path.exists() and path.is_dir():
        raise AgentError(f"cannot overwrite a directory: {relative}")
    path.write_text(content, encoding="utf-8")


def parse_action(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise AgentError(f"Grok did not return a JSON action: {raw[:200]}")
    try:
        action = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise AgentError(f"Grok returned invalid JSON: {error}") from error
    if not isinstance(action, dict) or not isinstance(action.get("action"), str):
        raise AgentError("Grok action must be a JSON object with an action field")
    return action


def tool_text(result: Any) -> str:
    if result.is_error:
        message = " ".join(
            item.text for item in result.content if hasattr(item, "text")
        )
        raise AgentError(message or "MCP tool call failed")
    if result.structured_content and "result" in result.structured_content:
        return str(result.structured_content["result"])
    return "".join(
        item.text for item in result.content if hasattr(item, "text")
    )


async def ask(client: Client, messages: list[dict[str, str]]) -> str:
    return tool_text(
        await client.call_tool("ask_grok", {"messages": messages})
    )


async def edit_with_grok(
    workspace: Path, prompt: str, mcp_url: str
) -> str:
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

    instructions = """
You are a coding assistant editing a Git repository through a file-only protocol.
Respond with exactly one JSON object per turn and no commentary.
Available actions:
{"action":"list_files","path":"."}
{"action":"read_file","path":"relative/path"}
{"action":"write_file","path":"relative/path","content":"complete file content"}
{"action":"finish","summary":"short description"}

Use only relative paths. Inspect relevant files before editing them. Write complete
file contents. Do not request shell commands. Finish only when the task is done.
""".strip()
    messages = [
        {"role": "system", "content": instructions},
        {
            "role": "user",
            "content": f"Task:\n{prompt}\n\nReturn the first JSON action.",
        },
    ]

    async with Client(mcp_url) as client:
        raw = await ask(client, messages)
        for _ in range(MAX_STEPS):
            action = parse_action(raw)
            name = action["action"]
            if name == "finish":
                return str(action.get("summary", "Completed requested changes"))
            if name == "list_files":
                result: Any = {
                    "files": list_files(workspace, str(action.get("path", ".")))
                }
            elif name == "read_file":
                result = {
                    "path": action.get("path"),
                    "content": read_file(workspace, str(action.get("path", ""))),
                }
            elif name == "write_file":
                path = str(action.get("path", ""))
                content = action.get("content")
                if not isinstance(content, str):
                    raise AgentError("write_file content must be a string")
                write_file(workspace, path, content)
                result = {"written": path}
            else:
                result = {"error": f"unknown action: {name}"}

            messages.extend(
                [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": "Action result:\n"
                        + json.dumps(result)
                        + "\nReturn the next JSON action.",
                    },
                ]
            )
            raw = await ask(client, messages)
    raise AgentError(f"Grok exceeded the {MAX_STEPS}-step limit")


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    validate_repo_url(args.repo)
    github = GitHubGitApi(args.repo)
    try:
        starting_ref, branch = select_branches(
            args.starting_ref,
            github.default_branch(),
            args.work_on_current_branch,
            args.output_branch,
            args.prompt,
        )
        existing_output_sha = github.get_ref(branch)
        if not args.work_on_current_branch and existing_output_sha:
            raise AgentError(f"output branch already exists: {branch}")

        with tempfile.TemporaryDirectory(prefix="cloud-agent-") as temp:
            workspace = Path(temp) / "workspace"
            parent_sha = github.download_ref(starting_ref, workspace)
            original_digest = workspace_digest(workspace)

            summary = await edit_with_grok(
                workspace, args.prompt, args.mcp_url
            )
            if workspace_digest(workspace) == original_digest:
                return {
                    "status": "no_changes",
                    "repo": args.repo,
                    "startingRef": starting_ref,
                    "workOnCurrentBranch": args.work_on_current_branch,
                    "branch": branch,
                    "commit": None,
                    "summary": summary,
                }

            commit = github.create_commit(
                workspace, summary[:120], parent_sha
            )
            github.write_ref(
                branch,
                commit,
                existing_output_sha
                if args.work_on_current_branch
                else None,
            )
            return {
                "status": "finished",
                "repo": args.repo,
                "startingRef": starting_ref,
                "workOnCurrentBranch": args.work_on_current_branch,
                "branch": branch,
                "commit": commit,
                "summary": summary,
            }
    finally:
        github.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a simple local cloud agent")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--starting-ref")
    parser.add_argument("--work-on-current-branch", action="store_true")
    parser.add_argument("--output-branch")
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    return parser.parse_args()


def main() -> None:
    try:
        result = asyncio.run(execute(parse_args()))
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({"status": "error", "error": str(error)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
