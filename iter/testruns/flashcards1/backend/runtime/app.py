import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from mcp.client import Client
from pydantic import BaseModel

from .database import Database, DatabaseError


MCP_URL = os.getenv("GROK_MCP_URL", "http://127.0.0.1:8765/mcp")
AI_MODEL = os.getenv("GROK_MODEL", "grok-4.6")
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


class AIRequest(BaseModel):
    input: str


def load_ai_actions(spec: dict[str, Any]) -> dict[str, str]:
    actions = spec.get("ai_actions", [])
    if not isinstance(actions, list):
        raise ValueError("ai_actions must be a list")
    result: dict[str, str] = {}
    for action in actions:
        name = action.get("name") if isinstance(action, dict) else None
        prompt = action.get("system_prompt") if isinstance(action, dict) else None
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
            raise ValueError(f"Invalid AI action name: {name}")
        if name in result:
            raise ValueError(f"Duplicate AI action: {name}")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 10_000:
            raise ValueError(f"Invalid system prompt for AI action: {name}")
        result[name] = prompt
    return result


def tool_text(result: Any) -> str:
    if result.is_error:
        message = " ".join(
            item.text for item in result.content if hasattr(item, "text")
        )
        raise RuntimeError(message or "Grok MCP call failed")
    if result.structured_content and "result" in result.structured_content:
        return str(result.structured_content["result"])
    return "".join(item.text for item in result.content if hasattr(item, "text"))


def create_app(spec_path: Path, database_path: Path) -> FastAPI:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    database = Database(database_path, spec)
    ai_actions = load_ai_actions(spec)
    app = FastAPI(title="Constrained Prototype API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/ai/{action}")
    async def run_ai_action(action: str, request: AIRequest) -> dict[str, str]:
        if action not in ai_actions:
            raise HTTPException(status_code=404, detail="Unknown AI action")
        user_input = request.input.strip()
        if not user_input:
            raise HTTPException(status_code=400, detail="Input cannot be empty")
        if len(user_input) > 20_000:
            raise HTTPException(status_code=400, detail="Input is too long")
        try:
            async with Client(MCP_URL) as client:
                result = await client.call_tool(
                    "ask_grok",
                    {
                        "model": AI_MODEL,
                        "messages": [
                            {"role": "system", "content": ai_actions[action]},
                            {"role": "user", "content": user_input},
                        ],
                    },
                )
            return {"content": tool_text(result)}
        except Exception as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @app.get("/api/{resource}")
    def list_items(resource: str) -> list[dict[str, Any]]:
        try:
            return database.list(resource)
        except DatabaseError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/{resource}", status_code=201)
    def create_item(
        resource: str, values: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        try:
            return database.create(resource, values)
        except DatabaseError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.put("/api/{resource}/{item_id}")
    def update_item(
        resource: str, item_id: int, values: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        try:
            item = database.update(resource, item_id, values)
        except DatabaseError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        return item

    @app.delete("/api/{resource}/{item_id}", status_code=204)
    def delete_item(resource: str, item_id: int) -> Response:
        try:
            deleted = database.delete(resource, item_id)
        except DatabaseError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if not deleted:
            raise HTTPException(status_code=404, detail="Item not found")
        return Response(status_code=204)

    return app
