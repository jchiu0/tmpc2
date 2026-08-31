# Iterative Development

This folder focuses on developing the prompts and basic flow for an iterative
development process. Cloud-agent integration is intentionally out of scope.

1. Gather and clarify requirements.
2. Develop an implementation plan.
3. Implement the planned solution.
4. Evaluate the results and identify improvements.

## Constrained prototype

`agent.py` is an interactive local agent for vague tasks such as:

```text
Build a TODO website with UI
```

The agent asks clarification questions, drafts a plan for approval, implements
the plan, and performs a static evaluation. It does not use cloud agents,
GitHub, or generated shell commands.

Generated applications use a fixed stack:

- React and Vite for the frontend.
- Python and FastAPI for the backend.
- SQLite behind the checked-in `backend/runtime/database.py` wrapper.

The model may generate only:

- `app_spec.json`, a validated declaration of resources and fields.
- `frontend/src/App.jsx`.
- `frontend/src/styles.css`.
- `README.md`.

Everything else is copied from `template/`. The model cannot generate backend
Python, raw SQL, dependencies, Vite configuration, or arbitrary file paths.
This limits the shape of generated code; it is not a security sandbox.

Apps may declare named AI actions in `app_spec.json`. The fixed FastAPI runtime
exposes them at `/api/ai/<action>` and forwards user input to the local Grok MCP
server without exposing its credentials or arbitrary MCP tools.

## Run the agent

Start the repository's `local_tool_server`, then run:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python agent.py --task "Build a TODO website with UI"
```

The generated application is written to `generated/` by default.
The task, clarified requirements, approved plan, and evaluation are retained in
`generated/.agent/` so each stage can be inspected and iterated on.

After implementation, the agent installs the fixed frontend dependencies and
runs a Playwright test in Chromium. It verifies that the real UI can create an
item, retain it after reload through FastAPI and SQLite, and delete it without
browser errors. The result is saved as `generated/.agent/e2e.json` and included
in the final evaluation. Use `--skip-e2e` only when browser testing is
unavailable.

For repeatable test runs, pass a JSON array of answers and auto-approve the
resulting plan:

```bash
python agent.py \
  --task "Build me an AI app for studying" \
  --workspace testruns/flashcards1 \
  --answers-file testruns/flashcards1/answers.json \
  --approve-plan
```

## Run a generated application

Start the FastAPI backend:

```bash
cd generated/backend
pip install -r requirements.txt
uvicorn main:app --reload
```

In another terminal, start React:

```bash
cd generated/frontend
npm install
npm run dev
```

Open `http://localhost:5173`.

## Tests

```bash
python -m unittest
```
