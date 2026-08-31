import json
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

from .database import Database, DatabaseError


def create_app(spec_path: Path, database_path: Path) -> FastAPI:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    database = Database(database_path, spec)
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
