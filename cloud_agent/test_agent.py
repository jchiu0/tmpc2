import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cloud_agent.lib.runner import (
    AgentRequest,
    AgentError,
    commit_marker,
    edit_with_grok,
    generated_branch,
    parse_action,
    read_file,
    run_agent,
    safe_path,
    select_branches,
    workspace_digest,
    write_file,
)


class ActionTests(unittest.TestCase):
    def test_parses_plain_json(self) -> None:
        self.assertEqual(
            parse_action('{"action":"finish","summary":"done"}')["action"],
            "finish",
        )

    def test_parses_fenced_json(self) -> None:
        action = parse_action(
            '```json\n{"action":"list_files","path":"."}\n```'
        )
        self.assertEqual(action["action"], "list_files")

    def test_rejects_non_json(self) -> None:
        with self.assertRaises(AgentError):
            parse_action("I would like to read a file.")


class WorkspaceTests(unittest.TestCase):
    def test_write_and_read_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_file(root, "docs/readme.txt", "hello")
            self.assertEqual(read_file(root, "docs/readme.txt"), "hello")

    def test_rejects_traversal_and_git(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(AgentError):
                safe_path(root, "../outside.txt")
            with self.assertRaises(AgentError):
                safe_path(root, ".git/config")

    def test_generated_branch_is_valid(self) -> None:
        branch = generated_branch("Add a useful README!")
        self.assertRegex(branch, r"^cursor/add-a-useful-readme-[a-f0-9]{6}$")


class BranchTests(unittest.TestCase):
    def test_selects_new_branch_from_starting_ref(self) -> None:
        starting_ref, branch = select_branches(
            "main",
            "ignored-default",
            False,
            "cursor/test-change",
            "test",
        )
        self.assertEqual(starting_ref, "main")
        self.assertEqual(branch, "cursor/test-change")

    def test_same_branch_uses_starting_ref(self) -> None:
        starting_ref, branch = select_branches(
            "main",
            "ignored-default",
            True,
            None,
            "test",
        )
        self.assertEqual(starting_ref, "main")
        self.assertEqual(branch, "main")

    def test_workspace_digest_changes_with_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            before = workspace_digest(root)
            write_file(root, "README.md", "# Test\n")
            self.assertNotEqual(before, workspace_digest(root))


class IdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_prior_run_history_precedes_current_prompt(self) -> None:
        captured: list[dict[str, str]] = []

        class FakeClient:
            def __init__(self, _: str):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def call_tool(self, _: str, arguments: dict):
                captured.extend(arguments["messages"])
                return SimpleNamespace(
                    is_error=False,
                    structured_content={
                        "result": '{"action":"finish","summary":"done"}'
                    },
                    content=[],
                )

        history = (
            {"role": "user", "content": "Create a README"},
            {"role": "assistant", "content": "README created"},
        )
        with tempfile.TemporaryDirectory() as temp:
            with patch("cloud_agent.lib.runner.Client", FakeClient):
                summary = await edit_with_grok(
                    Path(temp), "Add tests", "http://mcp", history=history
                )
        self.assertEqual(summary, "done")
        self.assertEqual(captured[1:3], list(history))
        self.assertIn("Add tests", captured[3]["content"])

    async def test_recovers_already_published_run(self) -> None:
        class FakeGitHub:
            def __init__(self, _: str):
                pass

            def default_branch(self) -> str:
                return "main"

            def get_ref(self, _: str) -> str:
                return "published-sha"

            def commit_message(self, _: str) -> str:
                return commit_marker("run-123") + " completed"

            def close(self) -> None:
                pass

        request = AgentRequest(
            prompt="Create a README",
            repo="https://github.com/example/repo",
            starting_ref="main",
            output_branch="cursor/test",
            idempotency_key="run-123",
        )
        with patch("cloud_agent.lib.runner.GitHubGitApi", FakeGitHub):
            result = await run_agent(request)
        self.assertTrue(result["recovered"])
        self.assertEqual(result["commit"], "published-sha")


if __name__ == "__main__":
    unittest.main()
