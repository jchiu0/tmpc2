from __future__ import annotations

import copy
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


MAX_COMMAND_OUTPUT_CHARS = 100_000


class WorkflowError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkflowCommand:
    type: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WorkflowEvent:
    def __init__(self, event_type: str, payload: dict[str, Any] | None = None):
        self.type = event_type
        self.payload = payload or {}

    def __getattr__(self, name: str) -> Any:
        try:
            return self.payload[name]
        except KeyError as error:
            raise AttributeError(name) from error


class WorkflowContext:
    def __init__(self, state_data: dict[str, Any]):
        self.state = copy.deepcopy(state_data)

    def run(
        self,
        key: str,
        prompt: str | None = None,
        *,
        result: str | None = None,
    ) -> WorkflowCommand:
        if prompt is not None:
            return self.run_llm(key, prompt)
        if prompt is None and result is None:
            result = "Workflow state checkpointed"
        return self._keyed(
            "run",
            key,
            {
                "runType": "python",
                "prompt": None,
                "activity": None,
                "input": None,
                "result": result,
            },
        )

    def run_llm(
        self,
        key: str,
        prompt: str,
        *,
        copy_context: bool = True,
    ) -> WorkflowCommand:
        if not prompt.strip():
            raise WorkflowError("LLM prompt is required")
        return self._keyed(
            "run",
            key,
            {
                "runType": "llm",
                "prompt": prompt,
                "copyContext": copy_context,
            },
        )

    def run_python(
        self,
        key: str,
        activity: str,
        input: Any = None,
    ) -> WorkflowCommand:
        if not activity.strip():
            raise WorkflowError("Python activity name is required")
        json_safe(input)
        return self._keyed(
            "run",
            key,
            {"runType": "python", "activity": activity, "input": input},
        )

    def run_command(
        self,
        command: list[str],
        *,
        timeout_seconds: int = 300,
        cwd: str = ".",
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not command or not all(isinstance(part, str) for part in command):
            raise WorkflowError("command must be a non-empty list of strings")
        completed = subprocess.run(
            command,
            cwd=cwd,
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "command": command,
            "exitCode": completed.returncode,
            "stdout": completed.stdout[-MAX_COMMAND_OUTPUT_CHARS:],
            "stderr": completed.stderr[-MAX_COMMAND_OUTPUT_CHARS:],
        }

    def wait_for_user(self, key: str, prompt: str) -> WorkflowCommand:
        if not prompt.strip():
            raise WorkflowError("user input prompt is required")
        return self._keyed(
            "wait_for_user", key, {"prompt": prompt}
        )

    def transition(self, state: str) -> WorkflowCommand:
        if not state:
            raise WorkflowError("transition state is required")
        return WorkflowCommand("transition", {"state": state})

    def complete(self, result: str = "") -> WorkflowCommand:
        return WorkflowCommand("complete", {"result": result})

    def fail(self, error: str) -> WorkflowCommand:
        if not error:
            raise WorkflowError("failure error is required")
        return WorkflowCommand("fail", {"error": error})

    @staticmethod
    def _keyed(
        command_type: str, key: str, arguments: dict[str, Any]
    ) -> WorkflowCommand:
        if not key:
            raise WorkflowError(f"{command_type} key is required")
        return WorkflowCommand(command_type, {"key": key, **arguments})


def state(function: Callable) -> Callable:
    setattr(function, "__workflow_state__", True)
    return function


def activity(function: Callable) -> Callable:
    setattr(function, "__workflow_activity__", True)
    return function


class PythonActivityContext:
    def __init__(self, workspace: Path, state_data: dict[str, Any]):
        self.workspace = workspace
        self.state = copy.deepcopy(state_data)

    def run_command(
        self,
        command: list[str],
        *,
        timeout_seconds: int = 300,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        context = WorkflowContext(self.state)
        return context.run_command(
            command,
            timeout_seconds=timeout_seconds,
            cwd=str(self.workspace),
            env=env,
        )


def json_safe(value: Any) -> None:
    import json

    try:
        json.dumps(value)
    except (TypeError, ValueError) as error:
        raise WorkflowError("value must be JSON serializable") from error


class StateMachine:
    initial_state: str

    def invoke(
        self,
        state_name: str,
        context: WorkflowContext,
        event: WorkflowEvent,
    ) -> WorkflowCommand:
        handler = getattr(self, state_name, None)
        if handler is None or not getattr(handler, "__workflow_state__", False):
            raise WorkflowError(f"unknown workflow state: {state_name}")
        command = handler(context, event)
        if not isinstance(command, WorkflowCommand):
            raise WorkflowError(
                f"state {state_name} must return a workflow command"
            )
        return command
