from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

from cloud_agent.workflow import (
    PythonActivityContext,
    StateMachine,
    WorkflowContext,
    WorkflowEvent,
)


def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    source = str(payload["source"])
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    expected_hash = payload.get("sourceHash")
    if expected_hash is not None and expected_hash != source_hash:
        raise ValueError("workflow source hash does not match")

    with tempfile.TemporaryDirectory(prefix="workflow-source-") as temp:
        source_path = Path(temp) / "workflow_source.py"
        source_path.write_text(source)
        module_name = f"cloud_agent_user_workflow_{source_hash}"
        spec = importlib.util.spec_from_file_location(module_name, source_path)
        if spec is None or spec.loader is None:
            raise ValueError("could not load workflow source")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        if payload.get("mode") == "activity":
            activity_name = str(payload["activity"])
            function = getattr(module, activity_name, None)
            if not callable(function) or not getattr(
                function, "__workflow_activity__", False
            ):
                raise ValueError(
                    f"unknown Python activity: {activity_name}"
                )
            workspace = Path(payload["workspace"]).resolve()
            if not workspace.is_dir():
                raise ValueError("activity workspace does not exist")
            state_data = payload.get("stateData") or {}
            if not isinstance(state_data, dict):
                raise ValueError("workflow state data must be an object")
            previous_cwd = Path.cwd()
            try:
                os.chdir(workspace)
                result = function(
                    PythonActivityContext(workspace, state_data),
                    payload.get("input"),
                )
            finally:
                os.chdir(previous_cwd)
            json.dumps(result)
            return {
                "activity": activity_name,
                "result": result,
                "sourceHash": source_hash,
            }

        entrypoint = str(payload["entrypoint"])
        workflow_class = getattr(module, entrypoint, None)
        if (
            not isinstance(workflow_class, type)
            or not issubclass(workflow_class, StateMachine)
        ):
            raise ValueError("workflow entrypoint must extend StateMachine")
        workflow = workflow_class()
        state_name = payload.get("stateName") or workflow.initial_state
        if not isinstance(state_name, str) or not state_name:
            raise ValueError("workflow state name is required")
        state_data = payload.get("stateData") or {}
        if not isinstance(state_data, dict):
            raise ValueError("workflow state data must be an object")
        event_payload = payload.get("event") or {"type": "entered"}
        event_type = event_payload.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("workflow event type is required")

        context = WorkflowContext(state_data)
        command = workflow.invoke(
            state_name,
            context,
            WorkflowEvent(event_type, event_payload.get("payload")),
        )
        json.dumps(context.state)
        return {
            "stateName": state_name,
            "stateData": context.state,
            "command": command.to_dict(),
            "sourceHash": source_hash,
        }


def main() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        payload = json.load(sys.stdin)
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            response = invoke(payload)
        response["stdout"] = stdout.getvalue()
        response["stderr"] = stderr.getvalue()
        print(json.dumps({"type": "result", "payload": response}))
    except BaseException as error:
        print(
            json.dumps(
                {
                    "type": "error",
                    "error": str(error),
                    "errorType": type(error).__name__,
                    "traceback": traceback.format_exc(),
                    "stdout": stdout.getvalue(),
                    "stderr": stderr.getvalue(),
                }
            )
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
