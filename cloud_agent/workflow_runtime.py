from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


class WorkflowRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkflowInvocation:
    state_name: str
    state_data: dict[str, Any]
    command: dict[str, Any]
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ActivityInvocation:
    activity: str
    result: Any
    stdout: str
    stderr: str


def invoke_workflow(
    *,
    source: str,
    source_hash: str,
    entrypoint: str,
    state_name: str | None,
    state_data: dict[str, Any],
    event: dict[str, Any],
    timeout_seconds: int = 30,
) -> WorkflowInvocation:
    completed = subprocess.run(
        [sys.executable, "-m", "cloud_agent.workflow_host"],
        input=json.dumps(
            {
                "source": source,
                "sourceHash": source_hash,
                "entrypoint": entrypoint,
                "stateName": state_name,
                "stateData": state_data,
                "event": event,
            }
        ),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    try:
        frame = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise WorkflowRuntimeError(
            "workflow host returned invalid JSON: "
            f"{completed.stdout[-1000:]} {completed.stderr[-1000:]}"
        ) from error
    if completed.returncode != 0 or frame.get("type") != "result":
        raise WorkflowRuntimeError(
            str(frame.get("error") or "workflow host failed")
        )
    payload = frame["payload"]
    return WorkflowInvocation(
        state_name=payload["stateName"],
        state_data=payload["stateData"],
        command=payload["command"],
        stdout=payload.get("stdout", ""),
        stderr=payload.get("stderr", ""),
    )


def invoke_activity(
    *,
    source: str,
    source_hash: str,
    activity: str,
    workspace: str,
    state_data: dict[str, Any],
    input: Any,
    timeout_seconds: int = 330,
) -> ActivityInvocation:
    completed = subprocess.run(
        [sys.executable, "-m", "cloud_agent.workflow_host"],
        input=json.dumps(
            {
                "mode": "activity",
                "source": source,
                "sourceHash": source_hash,
                "activity": activity,
                "workspace": workspace,
                "stateData": state_data,
                "input": input,
            }
        ),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    try:
        frame = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise WorkflowRuntimeError(
            "activity host returned invalid JSON: "
            f"{completed.stdout[-1000:]} {completed.stderr[-1000:]}"
        ) from error
    if completed.returncode != 0 or frame.get("type") != "result":
        raise WorkflowRuntimeError(
            str(frame.get("error") or "activity host failed")
        )
    payload = frame["payload"]
    return ActivityInvocation(
        activity=payload["activity"],
        result=payload["result"],
        stdout=payload.get("stdout", ""),
        stderr=payload.get("stderr", ""),
    )
