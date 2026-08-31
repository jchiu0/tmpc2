import json
import tempfile
import unittest
from pathlib import Path

from agent import (
    AgentError,
    GENERATED_FILES,
    parse_json_response,
    prepare_workspace,
    safe_path,
    validate_app_spec,
    workspace_snapshot,
    write_artifact,
    write_generated_files,
)
from template.backend.runtime.app import load_ai_actions
from template.backend.runtime.database import Database


class ParsingTests(unittest.TestCase):
    def test_parses_fenced_json(self) -> None:
        self.assertEqual(parse_json_response('```json\n{"ok": true}\n```'), {"ok": True})

    def test_rejects_non_json(self) -> None:
        with self.assertRaises(AgentError):
            parse_json_response("not json")


class ConstraintTests(unittest.TestCase):
    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(AgentError):
                safe_path(Path(temp), "../outside")

    def test_rejects_generated_backend_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            payload = {
                "files": [{"path": "backend/custom.py", "content": "import sqlite3"}]
            }
            with self.assertRaises(AgentError):
                write_generated_files(Path(temp), payload)

    def test_validates_app_spec(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "app_spec.json"
            path.write_text(
                json.dumps(
                    {
                        "resources": [
                            {
                                "name": "todos",
                                "fields": [
                                    {
                                        "name": "title",
                                        "type": "text",
                                        "required": True,
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            validate_app_spec(path)

    def test_scaffold_contains_fixed_stack(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            prepare_workspace(workspace)
            self.assertTrue((workspace / "backend" / "main.py").is_file())
            self.assertTrue((workspace / "backend" / "runtime" / "database.py").is_file())
            self.assertTrue((workspace / "frontend" / "vite.config.js").is_file())
            self.assertTrue((workspace / "frontend" / "e2e" / "app.spec.js").is_file())
            self.assertNotIn("backend/main.py", GENERATED_FILES)

    def test_writes_stage_artifacts_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            write_artifact(workspace, "plan.md", "# Plan\n")
            self.assertEqual(
                (workspace / ".agent" / "plan.md").read_text(encoding="utf-8"),
                "# Plan\n",
            )

    def test_snapshot_skips_installed_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / "frontend" / "src").mkdir(parents=True)
            (workspace / "frontend" / "node_modules").mkdir()
            (workspace / "frontend" / "src" / "App.jsx").write_text(
                "export default function App() {}", encoding="utf-8"
            )
            (workspace / "frontend" / "node_modules" / "large.js").write_text(
                "ignored", encoding="utf-8"
            )
            snapshot = workspace_snapshot(workspace)
            self.assertIn("App.jsx", snapshot)
            self.assertNotIn("large.js", snapshot)

    def test_validates_named_ai_actions(self) -> None:
        actions = load_ai_actions(
            {
                "ai_actions": [
                    {
                        "name": "generate_flashcards",
                        "system_prompt": "Return flashcards as JSON.",
                    }
                ]
            }
        )
        self.assertEqual(
            actions["generate_flashcards"], "Return flashcards as JSON."
        )


class DatabaseRuntimeTests(unittest.TestCase):
    def test_crud_uses_declared_resource(self) -> None:
        spec = {
            "resources": [
                {
                    "name": "todos",
                    "fields": [
                        {"name": "title", "type": "text", "required": True},
                        {"name": "completed", "type": "boolean", "required": False},
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp:
            database = Database(Path(temp) / "app.db", spec)
            todo = database.create("todos", {"title": "Test", "completed": False})
            self.assertEqual(todo["title"], "Test")
            self.assertFalse(todo["completed"])

            updated = database.update("todos", todo["id"], {"completed": True})
            self.assertIsNotNone(updated)
            self.assertTrue(updated["completed"])
            self.assertEqual(len(database.list("todos")), 1)
            self.assertTrue(database.delete("todos", todo["id"]))


if __name__ == "__main__":
    unittest.main()
