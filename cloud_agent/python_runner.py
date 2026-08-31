from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Callable

from cloud_agent.lib.github import GitHubGitApi
from cloud_agent.lib.runner import (
    AgentError,
    commit_marker,
    validate_repo_url,
    workspace_digest,
)
from cloud_agent.workflow_runtime import invoke_activity


EventCallback = Callable[[str, dict[str, Any]], None]


def run_python_activity(
    run: dict[str, Any], on_event: EventCallback | None = None
) -> dict[str, Any]:
    validate_repo_url(run["repo_url"])
    github = GitHubGitApi(run["repo_url"])
    try:
        starting_ref = run["starting_ref"] or github.default_branch()
        branch = run["output_branch"] or starting_ref
        existing_sha = github.get_ref(branch)
        marker = commit_marker(run["run_id"])
        recovered = bool(
            existing_sha
            and github.commit_message(existing_sha).startswith(marker)
        )
        if (
            existing_sha
            and not run["work_on_current_branch"]
            and not recovered
        ):
            raise AgentError(f"output branch already exists: {branch}")

        checkout_ref = branch if recovered else starting_ref
        with tempfile.TemporaryDirectory(prefix="cloud-agent-python-") as temp:
            workspace = Path(temp) / "workspace"
            parent_sha = github.download_ref(checkout_ref, workspace)
            original_digest = workspace_digest(workspace)
            if on_event:
                on_event(
                    "python.started",
                    {"activity": run["python_activity"]},
                )
            invocation = invoke_activity(
                source=run["source_code"],
                source_hash=run["source_hash"],
                activity=run["python_activity"],
                workspace=str(workspace),
                state_data=json.loads(run["workflow_state_json"]),
                input=json.loads(run["python_input_json"])
                if run["python_input_json"]
                else None,
            )
            if on_event:
                on_event(
                    "python.finished",
                    {
                        "activity": invocation.activity,
                        "result": invocation.result,
                        "stdout": invocation.stdout,
                        "stderr": invocation.stderr,
                    },
                )
            commit = existing_sha if recovered else None
            if workspace_digest(workspace) != original_digest:
                summary = f"Python activity {invocation.activity}"
                commit = github.create_commit(
                    workspace,
                    f"{marker} {summary}"[:120],
                    parent_sha,
                )
                github.write_ref(
                    branch,
                    commit,
                    existing_sha if existing_sha else None,
                )
            return {
                "status": "finished",
                "repo": run["repo_url"],
                "startingRef": starting_ref,
                "workOnCurrentBranch": bool(
                    run["work_on_current_branch"]
                ),
                "branch": branch,
                "commit": commit,
                "summary": f"Python activity {invocation.activity} completed",
                "activityResult": invocation.result,
                "stdout": invocation.stdout,
                "stderr": invocation.stderr,
            }
    finally:
        github.close()
