# Study Flashcards

A single-user study prototype. An anonymous student types a topic or pastes notes, generates eight concise question-and-answer flashcards, then saves a deck, flips cards, edits them, and deletes cards or decks. Nothing is stored in the browser; decks and cards persist in SQLite through the runtime API.

## Run

1. Install backend packages: `pip install -r backend/requirements.txt`
2. From `backend/`, start the API: `python -m uvicorn main:app --host 127.0.0.1 --port 8000`
3. From `frontend/`, install and start the UI: `npm install` then `npm run dev`
4. Open the Vite URL (proxies `/api` to the backend).

## Student flow

1. Enter a topic or pasted notes in the main field.
2. **Save deck** creates a deck immediately via `POST /api/decks`.
3. **Generate flashcards** calls `POST /api/ai/generate_flashcards` with `{ "input": "..." }` and reads eight `{ question, answer }` pairs from `{ "content": "..." }`.
4. Saving again after generation stores those eight cards on a new deck via `POST /api/cards`.
5. Open a deck to flip, move previous/next, edit, or delete a card. Delete a deck (confirm dialog) removes its cards, then the deck.

Reload the page to confirm saved decks come back from `/api/decks` and `/api/cards`.

## Checks

Backend health is `GET /api/health`. From `frontend/`, `npm run test:e2e` runs the Playwright suite against this UI and spec.
