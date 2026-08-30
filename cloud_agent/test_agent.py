import tempfile
import unittest
from pathlib import Path

from agent import (
    AgentError,
    generated_branch,
    parse_action,
    read_file,
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


if __name__ == "__main__":
    unittest.main()
