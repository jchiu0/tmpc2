# Simple Local Cloud Agent

This script implements a minimal Cursor-style coding agent locally:

1. Download a GitHub repository ref into a temporary directory.
2. Ask Grok for bounded file actions through the local MCP server.
3. Apply text-file changes without model-generated shell commands.
4. Create Git blobs, a tree, a commit, and the output ref through GitHub's API.
5. Print the branch and commit as JSON.
6. Delete the temporary checkout.

## Prerequisites

Start the Grok tool server in a separate terminal:

```bash
./local_tool_server/start.sh
```

Set `GITHUB_TOKEN` or `GH_TOKEN` to a token with repository contents write
access. The script uses GitHub APIs for both checkout and publishing. Restart
the MCP server after changing its implementation.

## Create a generated branch

From the project root:

```bash
./cloud_agent/run.sh \
  --repo https://github.com/jchiu0/scratch1 \
  --starting-ref main \
  --prompt "Create a README describing this scratch repository"
```

The agent creates and pushes a branch such as
`cursor/create-a-readme-describing-th-12ab34`.

To choose the branch name:

```bash
./cloud_agent/run.sh \
  --repo https://github.com/jchiu0/scratch1 \
  --starting-ref main \
  --output-branch cursor/my-test \
  --prompt "Create a README"
```

## Write to the working branch

This mode updates `startingRef` through the GitHub API without force:

```bash
./cloud_agent/run.sh \
  --repo https://github.com/jchiu0/scratch1 \
  --starting-ref main \
  --work-on-current-branch \
  --prompt "Improve the README"
```

## Result

The final line is queryable JSON:

```json
{
  "status": "finished",
  "repo": "https://github.com/jchiu0/scratch1",
  "startingRef": "main",
  "workOnCurrentBranch": false,
  "branch": "cursor/create-a-readme-12ab34",
  "commit": "0123456789abcdef",
  "summary": "Create repository README"
}
```

## Safety limits

- Grok can only list, read, and write UTF-8 text files under the temporary
  checkout.
- `.git`, absolute paths, traversal, and symlink escapes are rejected.
- Grok cannot execute shell commands.
- GitHub API uploads are limited to 10 MB per workspace in this prototype.
- The workspace is on the host, not in a security boundary. Docker isolation
  is intentionally deferred until the local flow is proven.
