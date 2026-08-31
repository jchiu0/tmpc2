import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cloud_agent.lib.runner import (
    AgentRequest,
    AgentError,
    SubagentDefinition,
    commit_marker,
    edit_with_grok,
    generated_branch,
    parse_action,
    parse_actions,
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

    def test_parses_multiple_actions(self) -> None:
        actions = parse_actions(
            """
[
  {"action":"read_file","path":"README.md"},
  {"action":"read_file","path":"three_sum.py"}
]
""".strip()
        )
        self.assertEqual(
            [action["path"] for action in actions],
            ["README.md", "three_sum.py"],
        )


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
    async def test_grok_can_correct_malformed_action_json(self) -> None:
        responses = [
            '{"action":"write_file","path":"bad.txt","content":"line one\nline two"}',
            '{"action":"finish","summary":"corrected"}',
        ]
        events: list[tuple[str, dict]] = []

        class FakeClient:
            def __init__(self, _: str):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def call_tool(self, _: str, arguments: dict):
                return SimpleNamespace(
                    is_error=False,
                    structured_content={"result": responses.pop(0)},
                    content=[],
                )

        with tempfile.TemporaryDirectory() as temp:
            with patch("cloud_agent.lib.runner.Client", FakeClient):
                result = await edit_with_grok(
                    Path(temp),
                    "Create a file",
                    "http://mcp",
                    on_event=lambda event_type, payload: events.append(
                        (event_type, payload)
                    ),
                )
        self.assertEqual(result, "corrected")
        conversation = [
            payload
            for event_type, payload in events
            if event_type == "conversation.message"
        ]
        self.assertEqual(
            [message["kind"] for message in conversation],
            ["prompt", "invalid_action", "tool_result", "final_response"],
        )
        self.assertIn("corrected valid JSON", conversation[2]["content"])

    async def test_prior_run_history_precedes_current_prompt(self) -> None:
        requests: list[list[dict[str, str]]] = []
        responses = [
            '{"action":"write_file","path":"tests.txt","content":"covered"}',
            '{"action":"finish","summary":"done"}',
        ]
        events: list[tuple[str, dict]] = []

        class FakeClient:
            def __init__(self, _: str):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def call_tool(self, _: str, arguments: dict):
                requests.append(list(arguments["messages"]))
                return SimpleNamespace(
                    is_error=False,
                    structured_content={"result": responses.pop(0)},
                    content=[],
                )

        history = (
            {"role": "user", "content": "Create a README"},
            {"role": "assistant", "content": "README created"},
        )
        with tempfile.TemporaryDirectory() as temp:
            with patch("cloud_agent.lib.runner.Client", FakeClient):
                summary = await edit_with_grok(
                    Path(temp),
                    "Add tests",
                    "http://mcp",
                    on_event=lambda event_type, payload: events.append(
                        (event_type, payload)
                    ),
                    history=history,
                )
        self.assertEqual(summary, "done")
        self.assertEqual(requests[0][1:3], list(history))
        self.assertIn("Add tests", requests[0][3]["content"])
        self.assertEqual(
            requests[1][-2:],
            [
                {
                    "role": "assistant",
                    "content": (
                        '{"action":"write_file","path":"tests.txt",'
                        '"content":"covered"}'
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        'Action result:\n{"written": "tests.txt"}\n'
                        "Return the next JSON action or action array."
                    ),
                },
            ],
        )
        conversation = [
            payload
            for event_type, payload in events
            if event_type == "conversation.message"
        ]
        self.assertEqual(
            [message["kind"] for message in conversation],
            [
                "prompt",
                "tool_call",
                "tool_result",
                "final_response",
            ],
        )
        self.assertEqual(
            conversation[-1]["content"],
            '{"action":"finish","summary":"done"}',
        )

    async def test_subagent_uses_clean_context_and_shared_workspace(self) -> None:
        requests: list[dict] = []
        responses = [
            (
                '{"action":"delegate","subagent":"implementer",'
                '"prompt":"Create child.txt"}'
            ),
            (
                '{"action":"write_file","path":"child.txt",'
                '"content":"written by child"}'
            ),
            '{"action":"finish","summary":"child complete"}',
            '{"action":"finish","summary":"parent complete"}',
        ]
        events: list[tuple[str, dict]] = []

        class FakeClient:
            def __init__(self, _: str):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def call_tool(self, _: str, arguments: dict):
                requests.append(arguments)
                return SimpleNamespace(
                    is_error=False,
                    structured_content={"result": responses.pop(0)},
                    content=[],
                )

        definition = SubagentDefinition(
            name="implementer",
            description="Implements focused file changes",
            prompt="You are a focused implementation subagent.",
            model="grok-child",
        )
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            with patch("cloud_agent.lib.runner.Client", FakeClient):
                summary = await edit_with_grok(
                    workspace,
                    "Parent task",
                    "http://mcp",
                    on_event=lambda event_type, payload: events.append(
                        (event_type, payload)
                    ),
                    history=(
                        {"role": "user", "content": "Earlier parent context"},
                    ),
                    subagents=(definition,),
                )
            self.assertEqual(
                (workspace / "child.txt").read_text(),
                "written by child",
            )

        self.assertEqual(summary, "parent complete")
        self.assertEqual(requests[1]["model"], "grok-child")
        child_messages = requests[1]["messages"]
        self.assertIn("focused implementation", child_messages[0]["content"])
        self.assertNotIn("Earlier parent context", str(child_messages))
        self.assertNotIn("delegate", child_messages[0]["content"])
        resumed_parent = requests[-1]["messages"]
        self.assertIn("child complete", resumed_parent[-1]["content"])
        self.assertNotIn("written by child", str(resumed_parent))
        self.assertEqual(
            [
                event_type
                for event_type, _ in events
                if event_type.startswith("subagent.")
            ],
            [
                "subagent.started",
                "subagent.message",
                "subagent.message",
                "subagent.message",
                "subagent.message",
                "subagent.finished",
            ],
        )

    async def test_readonly_subagent_cannot_write(self) -> None:
        responses = [
            (
                '{"action":"delegate","subagent":"reviewer",'
                '"prompt":"Try to write forbidden.txt"}'
            ),
            (
                '{"action":"write_file","path":"forbidden.txt",'
                '"content":"not allowed"}'
            ),
            '{"action":"finish","summary":"write was blocked"}',
            '{"action":"finish","summary":"review complete"}',
        ]

        class FakeClient:
            def __init__(self, _: str):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def call_tool(self, _: str, arguments: dict):
                return SimpleNamespace(
                    is_error=False,
                    structured_content={"result": responses.pop(0)},
                    content=[],
                )

        definition = SubagentDefinition(
            name="reviewer",
            description="Reviews without edits",
            prompt="Review only.",
            readonly=True,
        )
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            with patch("cloud_agent.lib.runner.Client", FakeClient):
                summary = await edit_with_grok(
                    workspace,
                    "Review the repository",
                    "http://mcp",
                    subagents=(definition,),
                )
            self.assertFalse((workspace / "forbidden.txt").exists())
        self.assertEqual(summary, "review complete")

    async def test_readonly_subagents_run_with_bounded_parallelism(self) -> None:
        calls = 0
        active_children = 0
        max_active_children = 0

        class FakeClient:
            def __init__(self, _: str):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def call_tool(self, _: str, arguments: dict):
                nonlocal calls, active_children, max_active_children
                call = calls
                calls += 1
                if call == 0:
                    response = """
[
  {"action":"delegate","subagent":"reviewer-a","prompt":"Review A"},
  {"action":"delegate","subagent":"reviewer-b","prompt":"Review B"},
  {"action":"delegate","subagent":"reviewer-a","prompt":"Review C"}
]
""".strip()
                elif call in {1, 2, 3}:
                    active_children += 1
                    max_active_children = max(
                        max_active_children, active_children
                    )
                    await asyncio.sleep(0.01)
                    active_children -= 1
                    response = (
                        '{"action":"finish","summary":"review complete"}'
                    )
                else:
                    response = (
                        '{"action":"finish","summary":"parent complete"}'
                    )
                return SimpleNamespace(
                    is_error=False,
                    structured_content={"result": response},
                    content=[],
                )

        subagents = (
            SubagentDefinition(
                name="reviewer-a",
                description="Reviews A",
                prompt="Review only.",
                readonly=True,
            ),
            SubagentDefinition(
                name="reviewer-b",
                description="Reviews B",
                prompt="Review only.",
                readonly=True,
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            with patch("cloud_agent.lib.runner.Client", FakeClient):
                result = await edit_with_grok(
                    Path(temp),
                    "Run both reviews",
                    "http://mcp",
                    subagents=subagents,
                )
        self.assertEqual(result, "parent complete")
        self.assertEqual(max_active_children, 2)

    async def test_recovers_already_published_run(self) -> None:
        pull_requests: list[dict] = []

        class FakeGitHub:
            def __init__(self, _: str):
                pass

            def default_branch(self) -> str:
                return "main"

            def get_ref(self, _: str) -> str:
                return "published-sha"

            def commit_message(self, _: str) -> str:
                return commit_marker("run-123") + " completed"

            def ensure_pull_request(self, **arguments) -> dict:
                pull_requests.append(arguments)
                return {"number": 7, "url": "https://github.com/example/pull/7"}

            def close(self) -> None:
                pass

        request = AgentRequest(
            prompt="Create a README",
            repo="https://github.com/example/repo",
            starting_ref="main",
            output_branch="cursor/test",
            auto_create_pr=True,
            idempotency_key="run-123",
        )
        with patch("cloud_agent.lib.runner.GitHubGitApi", FakeGitHub):
            result = await run_agent(request)
        self.assertTrue(result["recovered"])
        self.assertEqual(result["commit"], "published-sha")
        self.assertEqual(result["pullRequest"]["number"], 7)
        self.assertEqual(pull_requests[0]["head"], "cursor/test")
        self.assertEqual(pull_requests[0]["base"], "main")


if __name__ == "__main__":
    unittest.main()
