# TODO website — v1 implementation plan

## Goal

Ship a small, persisted TODO prototype: one anonymous user, one flat list, and a clean React UI that talks to the FastAPI backend declared in `app_spec.json`. Persistence is SQLite via the supplied database wrapper only.

## Requirements

Assumptions (all original questions were open):

- Audience: single anonymous user. No login, accounts, or shared lists.
- Scope: add, edit, complete/uncomplete, and delete. No due dates, priority, tags, filters, search, or sorting.
- Organization: one flat list of tasks. No projects or subtasks.
- UI: minimal, clean desktop layout that still works on narrower screens. Empty state when there are no tasks. No dark mode.
- Accessibility: usable with keyboard; form controls and buttons have visible labels. Not a full WCAG 2.1 AA audit.
- Done: working persisted CRUD UI, client-side validation, empty and error states. No automated tests.

Functional requirements:

- Create a task with a non-empty title (trim whitespace; reject empty titles).
- Optional notes/description field (plain text, may be empty).
- List all tasks, newest first.
- Toggle completed / not completed.
- Edit title and notes.
- Delete a task (with a simple confirm in the UI).
- Show loading, empty, and error states for API failures.

Data model (backend, declared only in `app_spec.json`):

- Entity: `todos`
- Fields: `id` (integer, generated), `title` (required string), `notes` (optional string), `completed` (boolean, default false), `created_at` (timestamp, server-set if the spec format allows).

API surface (CRUD only; no custom SQL):

- `GET /todos` — list
- `POST /todos` — create
- `GET /todos/{id}` — get one (if the spec format expects it)
- `PUT /todos/{id}` — update title, notes, completed
- `DELETE /todos/{id}` — delete

Out of scope: auth, multi-user, localStorage, raw SQL, extra frameworks, generated backend Python, serverless, tests.

## Proposed files

Only these files may be generated:

- `app_spec.json` — resources, fields, and REST endpoints for todos. No backend source, no SQL.
- `frontend/src/App.jsx` — list, add form, inline edit, complete toggle, delete, and fetch/error handling against the declared API.
- `frontend/src/styles.css` — layout, typography, form/list styles, empty/error states.
- `README.md` — how to run the supplied Vite + FastAPI stack, assumed API base URL, and v1 limits.

Do not add other files, frameworks, or storage.

## Implementation steps

1. Declare the `todos` resource in `app_spec.json` with the fields and CRUD routes above so the existing FastAPI + SQLite wrapper can serve them. Do not write Python, SQL, or extra backend files.
2. In `App.jsx`, on load, `GET /todos` and render the list. Handle loading and request errors.
3. Add form: title (required) and notes (optional). On submit, `POST /todos`, then refresh or append the new item. Block empty titles.
4. Each row: checkbox or button to toggle `completed` via `PUT`; inline edit for title/notes via `PUT`; delete via `DELETE` after confirm.
5. Empty state copy when the list is empty; disable submit while a request is in flight; keep completed items visible but visually distinct (e.g. strikethrough).
6. Style in `styles.css`: centered page, readable type, clear form and list, focus-visible on controls, simple responsive padding.
7. Document in `README.md`: run frontend (Vite) and backend (FastAPI) as supplied, API used by the UI, and that this is a single-user flat list with no auth.

## Evaluation criteria

- Only the four allowed files are produced; no backend code, SQL, localStorage, or extra frameworks.
- `app_spec.json` describes todos CRUD that the supplied wrapper can persist in SQLite.
- UI can add, list, edit, complete/uncomplete, and delete tasks, with data surviving a refresh.
- Empty titles cannot be saved; empty list and API errors are visible.
- Keyboard users can tab through form, list actions, and submit/toggle/delete.
- README matches the stack and the assumed v1 scope.
