# Todos prototype

Single anonymous user, one flat list. Add, edit, complete/uncomplete, and delete tasks with optional notes. Persistence is SQLite through the supplied FastAPI runtime at `/api/todos`. No login, filters, tags, or due dates.

## Run

Start the API in one terminal:

    cd backend
    pip install -r requirements.txt
    uvicorn main:app --reload --host 127.0.0.1 --port 8000

Start the UI in another terminal:

    cd frontend
    npm install
    npm run dev

Open http://localhost:5173

The Vite dev server proxies `/api` to http://127.0.0.1:8000.

## API used by the UI

- `GET /api/todos` — list tasks (newest first)
- `POST /api/todos` — create a task
- `PUT /api/todos/{id}` — update title, notes, or completed
- `DELETE /api/todos/{id}` — delete a task

## v1 includes

- Required title (whitespace trimmed; empty titles are rejected)
- Optional notes
- Toggle completed / not completed
- Inline edit for title and notes
- Delete with a confirmation prompt
- Loading, empty, and API error states

## v1 limits

- No accounts or shared lists
- No due dates, priority, tags, filters, search, or sorting
- No automated tests
