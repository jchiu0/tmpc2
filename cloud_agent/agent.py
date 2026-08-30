import argparse
import asyncio
import json
import sys

from .lib.runner import AgentRequest, DEFAULT_MCP_URL, run_agent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a simple local cloud agent")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--starting-ref")
    parser.add_argument("--work-on-current-branch", action="store_true")
    parser.add_argument("--output-branch")
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    request = AgentRequest(
        prompt=args.prompt,
        repo=args.repo,
        starting_ref=args.starting_ref,
        work_on_current_branch=args.work_on_current_branch,
        output_branch=args.output_branch,
        mcp_url=args.mcp_url,
    )
    try:
        print(json.dumps(asyncio.run(run_agent(request))))
    except Exception as error:
        print(json.dumps({"status": "error", "error": str(error)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
