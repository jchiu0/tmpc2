import asyncio
import hashlib
import inspect
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp.client import Client

from .github import GitHubApiError, GitHubGitApi


MAX_STEPS = 30
MAX_FILE_BYTES = 200_000
MAX_WRITE_BYTES = 1_000_000
MAX_LISTED_FILES = 500
MAX_ACTIONS_PER_TURN = 8
MAX_PARALLEL_ACTIONS = 2
MAX_DELEGATED_PROMPT_CHARS = 20_000
MAX_SUBAGENT_RESULT_CHARS = 20_000
MAX_PR_TITLE_WORDS = 30
DEFAULT_MCP_URL = "http://127.0.0.1:8765/mcp"


class AgentError(RuntimeError):
    pass


@dataclass(frozen=True)
class SubagentDefinition:
    name: str
    description: str
    prompt: str
    model: str = "inherit"
    readonly: bool = False


@dataclass(frozen=True)
class AgentRequest:
    prompt: str
    repo: str
    starting_ref: str | None = None
    work_on_current_branch: bool = False
    output_branch: str | None = None
    auto_create_pr: bool = False
    mcp_url: str = DEFAULT_MCP_URL
    idempotency_key: str | None = None
    history: tuple[dict[str, str], ...] = ()
    subagents: tuple[SubagentDefinition, ...] = ()


EventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]


async def emit(
    callback: EventCallback | None, event_type: str, payload: dict[str, Any]
) -> None:
    if callback is None:
        return
    result = callback(event_type, payload)
    if inspect.isawaitable(result):
        await result


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


def generated_branch(prompt: str, suffix: str | None = None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:32]
    return f"cursor/{slug or 'agent-change'}-{suffix or uuid.uuid4().hex[:6]}"


def commit_marker(idempotency_key: str) -> str:
    return f"[cloud-agent:{idempotency_key}]"


def recovered_result(
    request: AgentRequest,
    starting_ref: str,
    branch: str,
    commit: str,
    summary: str = "Recovered previously published run",
) -> dict[str, Any]:
    return {
        "status": "finished",
        "repo": request.repo,
        "startingRef": starting_ref,
        "workOnCurrentBranch": request.work_on_current_branch,
        "branch": branch,
        "commit": commit,
        "summary": summary,
        "recovered": True,
    }


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
    resolved_root = root.resolve()
    directory = safe_path(resolved_root, relative, allow_root=True)
    if not directory.exists() or not directory.is_dir():
        raise AgentError(f"directory not found: {relative}")

    files: list[str] = []
    for path in directory.rglob("*"):
        if ".git" in path.parts or path.is_symlink() or not path.is_file():
            continue
        files.append(path.relative_to(resolved_root).as_posix())
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


def parse_actions(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
    object_start = text.find("{")
    array_start = text.find("[")
    starts = [start for start in (object_start, array_start) if start >= 0]
    start = min(starts) if starts else -1
    opening = text[start : start + 1]
    closing = "]" if opening == "[" else "}"
    end = text.rfind(closing)
    if start < 0 or end < start:
        raise AgentError(f"Grok did not return JSON actions: {raw[:200]}")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise AgentError(f"Grok returned invalid JSON: {error}") from error
    actions = parsed if isinstance(parsed, list) else [parsed]
    if not actions:
        raise AgentError("Grok returned an empty action list")
    if len(actions) > MAX_ACTIONS_PER_TURN:
        raise AgentError(
            f"Grok returned more than {MAX_ACTIONS_PER_TURN} actions"
        )
    if any(
        not isinstance(action, dict)
        or not isinstance(action.get("action"), str)
        for action in actions
    ):
        raise AgentError(
            "each Grok action must be a JSON object with an action field"
        )
    return actions


def parse_action(raw: str) -> dict[str, Any]:
    actions = parse_actions(raw)
    if len(actions) != 1:
        raise AgentError("expected exactly one Grok action")
    return actions[0]


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


async def ask(
    client: Client,
    messages: list[dict[str, str]],
    model: str | None = None,
) -> str:
    arguments: dict[str, Any] = {"messages": messages}
    if model and model != "inherit":
        arguments["model"] = model
    return tool_text(
        await client.call_tool("ask_grok", arguments)
    )


async def edit_with_grok(
    workspace: Path,
    prompt: str,
    mcp_url: str,
    on_event: EventCallback | None = None,
    history: tuple[dict[str, str], ...] = (),
    subagents: tuple[SubagentDefinition, ...] = (),
    system_prompt: str | None = None,
    readonly: bool = False,
    model: str | None = None,
    include_title: bool = False,
) -> str | tuple[str, str]:
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

    actions = [
        '{"action":"list_files","path":"."}',
        '{"action":"read_file","path":"relative/path"}',
    ]
    if not readonly:
        actions.append(
            '{"action":"write_file","path":"relative/path",'
            '"content":"complete file content"}'
        )
    if subagents:
        actions.append(
            '{"action":"delegate","subagent":"name",'
            '"prompt":"complete delegated task"}'
        )
    finish_action = '{"action":"finish","summary":"short description"}'
    if include_title:
        finish_action = (
            '{"action":"finish","summary":"short description",'
            '"title":"concise imperative PR title"}'
        )
    actions.append(finish_action)
    instructions = f"""
You are a coding assistant editing a Git repository through a file-only protocol.
Respond with one JSON object or one JSON array per turn and no commentary.
Available actions:
{chr(10).join(actions)}

Use only relative paths. Inspect relevant files before editing them. Write complete
file contents. Do not request shell commands. Finish only when the task is done.
""".strip()
    if subagents:
        descriptions = "\n".join(
            f"- {subagent.name}: {subagent.description}"
            for subagent in subagents
        )
        instructions += f"\n\nAvailable subagents:\n{descriptions}"
    instructions += f"""

You may return a JSON array of up to {MAX_ACTIONS_PER_TURN} independent actions.
Only list_files, read_file, and delegations to readonly subagents run in
parallel, with at most {MAX_PARALLEL_ACTIONS} concurrent actions. Never put
finish in an action array. Writable actions are always executed sequentially.
""".rstrip()
    if include_title:
        instructions += """

The finish title must summarize the overall change in imperative mood, such as
"Add request validation". Keep it specific, relevant to the completed work,
and at most {MAX_PR_TITLE_WORDS} words.
""".rstrip()
    if system_prompt:
        instructions = f"{system_prompt.strip()}\n\n{instructions}"
    current_prompt = f"""
Task:
{prompt}

Return the first JSON action or action array.
""".strip()
    messages = [
        {"role": "system", "content": instructions},
        *history,
        {"role": "user", "content": current_prompt},
    ]
    await emit(
        on_event,
        "conversation.message",
        {"role": "user", "kind": "prompt", "content": current_prompt},
    )

    async with Client(mcp_url) as client:
        raw = await ask(client, messages, model)
        for _ in range(MAX_STEPS):
            try:
                current_actions = parse_actions(raw)
            except AgentError as error:
                await emit(
                    on_event,
                    "conversation.message",
                    {
                        "role": "assistant",
                        "kind": "invalid_action",
                        "content": raw,
                    },
                )
                correction = f"""
Action error:
{error}

Return corrected valid JSON with escaped string content.
""".strip()
                await emit(
                    on_event,
                    "conversation.message",
                    {
                        "role": "user",
                        "kind": "tool_result",
                        "content": correction,
                    },
                )
                messages.extend(
                    [
                        {"role": "assistant", "content": raw},
                        {"role": "user", "content": correction},
                    ]
                )
                raw = await ask(client, messages, model)
                continue
            names = [action["action"] for action in current_actions]
            await emit(
                on_event,
                "conversation.message",
                {
                    "role": "assistant",
                    "kind": (
                        "final_response"
                        if names == ["finish"]
                        else "tool_call"
                    ),
                    "content": raw,
                },
            )
            if names == ["finish"]:
                summary = str(
                    current_actions[0].get(
                        "summary", "Completed requested changes"
                    )
                )
                if include_title:
                    title = str(current_actions[0].get("title", "")).strip()
                    if not title:
                        title = summary
                    return summary, " ".join(
                        title.split()[:MAX_PR_TITLE_WORDS]
                    )
                return summary
            if "finish" in names:
                results: list[Any] = [
                    {"error": "finish must be the only action in its turn"}
                ]
            else:
                async def execute_action(action: dict[str, Any]) -> Any:
                    name = action["action"]
                    if name == "list_files":
                        return {
                            "files": list_files(
                                workspace, str(action.get("path", "."))
                            )
                        }
                    if name == "read_file":
                        return {
                            "path": action.get("path"),
                            "content": read_file(
                                workspace, str(action.get("path", ""))
                            ),
                        }
                    if name == "write_file":
                        if readonly:
                            return {"error": "subagent is readonly"}
                        path = str(action.get("path", ""))
                        content = action.get("content")
                        if not isinstance(content, str):
                            raise AgentError(
                                "write_file content must be a string"
                            )
                        write_file(workspace, path, content)
                        return {"written": path}
                    if name != "delegate":
                        return {"error": f"unknown action: {name}"}

                    subagent_name = action.get("subagent")
                    delegated_prompt = action.get("prompt")
                    definition = next(
                        (
                            subagent
                            for subagent in subagents
                            if subagent.name == subagent_name
                        ),
                        None,
                    )
                    if definition is None:
                        return {
                            "error": f"unknown subagent: {subagent_name}"
                        }
                    if (
                        not isinstance(delegated_prompt, str)
                        or not delegated_prompt.strip()
                    ):
                        return {
                            "error": "delegate prompt must be non-empty"
                        }
                    if len(delegated_prompt) > MAX_DELEGATED_PROMPT_CHARS:
                        return {
                            "error": (
                                "delegate prompt exceeds "
                                f"{MAX_DELEGATED_PROMPT_CHARS} characters"
                            )
                        }
                    return await run_subagent(
                        workspace,
                        definition,
                        delegated_prompt,
                        mcp_url,
                        on_event,
                    )

                def parallel_safe(action: dict[str, Any]) -> bool:
                    if action["action"] in {"list_files", "read_file"}:
                        return True
                    if action["action"] != "delegate":
                        return False
                    return any(
                        subagent.name == action.get("subagent")
                        and subagent.readonly
                        for subagent in subagents
                    )

                if len(current_actions) > 1 and all(
                    parallel_safe(action) for action in current_actions
                ):
                    results = []
                    for start in range(
                        0, len(current_actions), MAX_PARALLEL_ACTIONS
                    ):
                        results.extend(
                            await asyncio.gather(
                                *(
                                    execute_action(action)
                                    for action in current_actions[
                                        start : start
                                        + MAX_PARALLEL_ACTIONS
                                    ]
                                )
                            )
                        )
                else:
                    results = []
                    for action in current_actions:
                        results.append(await execute_action(action))

            tool_result = f"""
Action result:
{json.dumps(results[0] if len(results) == 1 else {"results": results})}
Return the next JSON action or action array.
""".strip()
            await emit(
                on_event,
                "conversation.message",
                {
                    "role": "user",
                    "kind": "tool_result",
                    "content": tool_result,
                },
            )
            messages.extend(
                [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": tool_result},
                ]
            )
            raw = await ask(client, messages, model)
    raise AgentError(f"Grok exceeded the {MAX_STEPS}-step limit")


async def run_subagent(
    workspace: Path,
    definition: SubagentDefinition,
    prompt: str,
    mcp_url: str,
    on_event: EventCallback | None,
) -> dict[str, str]:
    subagent_id = f"subagent-{uuid.uuid4()}"
    await emit(
        on_event,
        "subagent.started",
        {
            "subagentId": subagent_id,
            "name": definition.name,
            "prompt": prompt,
        },
    )

    async def child_event(event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "conversation.message":
            await emit(
                on_event,
                "subagent.message",
                {
                    "subagentId": subagent_id,
                    "name": definition.name,
                    **payload,
                },
            )

    try:
        result = await edit_with_grok(
            workspace,
            prompt,
            mcp_url,
            on_event=child_event,
            system_prompt=definition.prompt,
            readonly=definition.readonly,
            model=definition.model,
        )
    except Exception as error:
        await emit(
            on_event,
            "subagent.error",
            {
                "subagentId": subagent_id,
                "name": definition.name,
                "error": str(error),
            },
        )
        raise

    await emit(
        on_event,
        "subagent.finished",
        {
            "subagentId": subagent_id,
            "name": definition.name,
            "result": result,
        },
    )
    return {
        "subagentId": subagent_id,
        "result": result[:MAX_SUBAGENT_RESULT_CHARS],
    }


async def run_agent(
    request: AgentRequest, on_event: EventCallback | None = None
) -> dict[str, Any]:
    validate_repo_url(request.repo)
    await emit(on_event, "agent.status", {"status": "preparing"})
    github = GitHubGitApi(request.repo)
    try:
        output_branch = request.output_branch
        if (
            not request.work_on_current_branch
            and output_branch is None
            and request.idempotency_key
        ):
            suffix = hashlib.sha256(
                request.idempotency_key.encode("utf-8")
            ).hexdigest()[:6]
            output_branch = generated_branch(request.prompt, suffix)
        starting_ref, branch = select_branches(
            request.starting_ref,
            github.default_branch(),
            request.work_on_current_branch,
            output_branch,
            request.prompt,
        )

        def add_pull_request(
            result: dict[str, Any], summary: str, title: str | None = None
        ) -> dict[str, Any]:
            if not request.auto_create_pr:
                return result
            if branch == starting_ref:
                raise AgentError(
                    "autoCreatePR requires an output branch different "
                    "from startingRef"
                )
            result["pullRequest"] = github.ensure_pull_request(
                head=branch,
                base=starting_ref,
                title=" ".join(
                    (title or summary or request.prompt).split()[
                        :MAX_PR_TITLE_WORDS
                    ]
                ),
                body=(
                    "Created by local Cloud Agent run "
                    f"`{request.idempotency_key or 'unknown'}`."
                ),
            )
            return result

        existing_output_sha = github.get_ref(branch)
        marker = (
            commit_marker(request.idempotency_key)
            if request.idempotency_key
            else None
        )
        existing_message = (
            github.commit_message(existing_output_sha)
            if existing_output_sha and marker
            else ""
        )
        if existing_output_sha and marker and existing_message.startswith(marker):
            recovered_summary = existing_message.removeprefix(marker).strip()
            return add_pull_request(
                recovered_result(
                    request,
                    starting_ref,
                    branch,
                    existing_output_sha,
                    recovered_summary,
                ),
                recovered_summary,
            )
        if not request.work_on_current_branch and existing_output_sha:
            raise AgentError(f"output branch already exists: {branch}")

        with tempfile.TemporaryDirectory(prefix="cloud-agent-") as temp:
            workspace = Path(temp) / "workspace"
            parent_sha = github.download_ref(starting_ref, workspace)
            original_digest = workspace_digest(workspace)

            await emit(on_event, "agent.status", {"status": "running"})
            completion = await edit_with_grok(
                workspace,
                request.prompt,
                request.mcp_url,
                on_event,
                request.history,
                request.subagents,
                include_title=request.auto_create_pr,
            )
            if isinstance(completion, tuple):
                summary, pull_request_title = completion
            else:
                summary = completion
                pull_request_title = None
            if workspace_digest(workspace) == original_digest:
                return {
                    "status": "no_changes",
                    "repo": request.repo,
                    "startingRef": starting_ref,
                    "workOnCurrentBranch": request.work_on_current_branch,
                    "branch": branch,
                    "commit": None,
                    "summary": summary,
                }

            message = summary[:120]
            if marker:
                message = f"{marker} {summary}"[:120]
            commit = github.create_commit(workspace, message, parent_sha)
            try:
                github.write_ref(
                    branch,
                    commit,
                    existing_output_sha
                    if request.work_on_current_branch
                    else None,
                )
            except GitHubApiError:
                published_sha = github.get_ref(branch)
                if (
                    not published_sha
                    or not marker
                    or not github.commit_message(published_sha).startswith(marker)
                ):
                    raise
                return add_pull_request(
                    recovered_result(
                        request, starting_ref, branch, published_sha
                    ),
                    summary,
                    pull_request_title,
                )
            return add_pull_request(
                {
                    "status": "finished",
                    "repo": request.repo,
                    "startingRef": starting_ref,
                    "workOnCurrentBranch": request.work_on_current_branch,
                    "branch": branch,
                    "commit": commit,
                    "summary": summary,
                },
                summary,
                pull_request_title,
            )
    finally:
        github.close()
