import re
import sqlite3
from pathlib import Path
from typing import Any


IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
SQL_TYPES = {
    "text": "TEXT",
    "integer": "INTEGER",
    "real": "REAL",
    "boolean": "INTEGER",
}


class DatabaseError(ValueError):
    pass


class Database:
    """The only layer allowed to access SQLite."""

    def __init__(self, path: Path, spec: dict[str, Any]) -> None:
        self.path = path
        self.resources = self._validate_spec(spec)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._create_tables()

    @staticmethod
    def _validate_spec(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
        resources = spec.get("resources")
        if not isinstance(resources, list) or not resources:
            raise DatabaseError("The app spec must contain resources")

        validated: dict[str, dict[str, Any]] = {}
        for resource in resources:
            name = resource.get("name") if isinstance(resource, dict) else None
            fields = resource.get("fields") if isinstance(resource, dict) else None
            if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
                raise DatabaseError(f"Invalid resource name: {name}")
            if name in validated:
                raise DatabaseError(f"Duplicate resource: {name}")
            if not isinstance(fields, list) or not fields:
                raise DatabaseError(f"Resource {name} must contain fields")

            field_map: dict[str, dict[str, Any]] = {}
            for field in fields:
                field_name = field.get("name") if isinstance(field, dict) else None
                field_type = field.get("type") if isinstance(field, dict) else None
                required = field.get("required", False) if isinstance(field, dict) else None
                if not isinstance(field_name, str) or not IDENTIFIER.fullmatch(field_name):
                    raise DatabaseError(f"Invalid field name: {field_name}")
                if field_name in {"id", "created_at", "updated_at"}:
                    raise DatabaseError(f"Reserved field name: {field_name}")
                if field_name in field_map:
                    raise DatabaseError(f"Duplicate field: {field_name}")
                if field_type not in SQL_TYPES or not isinstance(required, bool):
                    raise DatabaseError(f"Invalid definition for field: {field_name}")
                field_map[field_name] = {"type": field_type, "required": required}
            validated[name] = field_map
        return validated

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _create_tables(self) -> None:
        with self._connect() as connection:
            for resource, fields in self.resources.items():
                columns = ["id INTEGER PRIMARY KEY AUTOINCREMENT"]
                for name, definition in fields.items():
                    required = " NOT NULL" if definition["required"] else ""
                    columns.append(f'"{name}" {SQL_TYPES[definition["type"]]}{required}')
                columns.extend(
                    [
                        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
                        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
                    ]
                )
                connection.execute(
                    f'CREATE TABLE IF NOT EXISTS "{resource}" ({", ".join(columns)})'
                )

    def _fields(self, resource: str) -> dict[str, dict[str, Any]]:
        try:
            return self.resources[resource]
        except KeyError as error:
            raise DatabaseError(f"Unknown resource: {resource}") from error

    @staticmethod
    def _normalize(value: Any, field_type: str) -> Any:
        if field_type == "boolean":
            if not isinstance(value, bool):
                raise DatabaseError("Boolean fields require true or false")
            return int(value)
        if field_type == "integer" and not isinstance(value, int):
            raise DatabaseError("Integer fields require an integer")
        if field_type == "real" and not isinstance(value, (int, float)):
            raise DatabaseError("Real fields require a number")
        if field_type == "text" and not isinstance(value, str):
            raise DatabaseError("Text fields require a string")
        return value

    def _validate_values(
        self, resource: str, values: dict[str, Any], partial: bool
    ) -> dict[str, Any]:
        fields = self._fields(resource)
        unknown = set(values).difference(fields)
        if unknown:
            raise DatabaseError(f"Unknown fields: {sorted(unknown)}")
        if not partial:
            missing = [
                name
                for name, definition in fields.items()
                if definition["required"] and name not in values
            ]
            if missing:
                raise DatabaseError(f"Missing required fields: {missing}")
        return {
            name: self._normalize(value, fields[name]["type"])
            for name, value in values.items()
        }

    @staticmethod
    def _row(row: sqlite3.Row | None, fields: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for name, definition in fields.items():
            if definition["type"] == "boolean" and result.get(name) is not None:
                result[name] = bool(result[name])
        return result

    def list(self, resource: str) -> list[dict[str, Any]]:
        fields = self._fields(resource)
        with self._connect() as connection:
            rows = connection.execute(
                f'SELECT * FROM "{resource}" ORDER BY id DESC'
            ).fetchall()
        return [self._row(row, fields) for row in rows]  # type: ignore[misc]

    def get(self, resource: str, item_id: int) -> dict[str, Any] | None:
        fields = self._fields(resource)
        with self._connect() as connection:
            row = connection.execute(
                f'SELECT * FROM "{resource}" WHERE id = ?', (item_id,)
            ).fetchone()
        return self._row(row, fields)

    def create(self, resource: str, values: dict[str, Any]) -> dict[str, Any]:
        clean = self._validate_values(resource, values, partial=False)
        if not clean:
            raise DatabaseError("At least one value is required")
        columns = ", ".join(f'"{name}"' for name in clean)
        placeholders = ", ".join("?" for _ in clean)
        with self._connect() as connection:
            cursor = connection.execute(
                f'INSERT INTO "{resource}" ({columns}) VALUES ({placeholders})',
                tuple(clean.values()),
            )
            item_id = cursor.lastrowid
        return self.get(resource, int(item_id))  # type: ignore[return-value]

    def update(
        self, resource: str, item_id: int, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        clean = self._validate_values(resource, values, partial=True)
        if not clean:
            raise DatabaseError("At least one value is required")
        assignments = ", ".join(f'"{name}" = ?' for name in clean)
        with self._connect() as connection:
            cursor = connection.execute(
                f'UPDATE "{resource}" SET {assignments}, '
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (*clean.values(), item_id),
            )
        return self.get(resource, item_id) if cursor.rowcount else None

    def delete(self, resource: str, item_id: int) -> bool:
        self._fields(resource)
        with self._connect() as connection:
            cursor = connection.execute(
                f'DELETE FROM "{resource}" WHERE id = ?', (item_id,)
            )
        return cursor.rowcount > 0
