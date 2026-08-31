# Study Flashcards — Implementation Plan

## Goal

Ship a small, single-user study prototype: an anonymous student types a topic or pastes notes, the AI generates eight concise Q&A flashcards, and the student can save a deck, flip/review cards, edit or delete cards, and delete decks. Data persists in SQLite across visits. The UI is mobile-first, colorful, and keyboard/screen-reader usable. Backend is declared only in `app_spec.json`; frontend is React (Vite).

## Requirements

**In scope (v1)**
- One anonymous student; no accounts, login, or sharing.
- Input: typed topic **or** pasted notes (one field is enough; student chooses by what they type).
- Named AI action that returns **exactly eight** concise question-and-answer flashcards from that input.
- Persist decks and cards in SQLite via the supplied database wrapper (not the browser).
- After generation, student can: save the deck, open it later, flip cards (front/back), review through the deck, edit question/answer, delete a card, delete a deck.
- UI: clean, colorful, mobile-first; explicit **loading**, **empty**, and **error** states.
- Accessibility: keyboard navigation (generate, save, flip, next/prev, edit, delete) and screen-reader labels on controls and card faces.
- “Done” path: generate → save → review → edit → delete a deck, end to end.
- Backend plus Playwright E2E tests must pass (tests live in the existing harness, not in generated files).

**Out of scope**
- Multi-user, login, sharing, spaced repetition, PDFs/uploads, chat tutor, study plans, progress stats, dark-mode toggle as a product feature.

**Assumptions (explicit)**
- Runtime already provides FastAPI, the SQLite wrapper, `/api/ai/<action>`, and CRUD derived from `app_spec.json`. No backend Python, no SQL, no extra frameworks.
- A **deck** has a title (from topic or a short label derived from notes), source text, and timestamps. A **card** belongs to one deck and has `question` and `answer`.
- Generation creates an unsaved working set in the UI; **Save** writes the deck + eight cards through the wrapper APIs. Regenerating before save replaces the working set, not stored decks.
- Single implicit user: no `user_id` column.
- AI may fail or return malformed output; the UI must show an error and keep prior saved decks intact.
- Playwright tests will hit real backend routes and visible UI labels/roles; markup and `app_spec.json` names should be stable and semantic.
- Title for pasted notes: first ~60 characters of the notes, or `"Untitled deck"` if empty after trim.

## Proposed files

Only these four files are generated:

1. **`app_spec.json`**
   - App name/description for a flashcard study tool.
   - Entities (wrapper-backed, no SQL):
     - `decks`: `id`, `title` (string), `source_text` (string), `created_at`.
     - `cards`: `id`, `deck_id` (FK → decks), `question` (string), `answer` (string), `created_at`.
   - One named AI action, e.g. `generate_flashcards`, served at `/api/ai/generate_flashcards`.
     - Input: `source_text` (topic or notes).
     - Output: array of eight `{ question, answer }` objects; instruct the model to be concise and study-oriented.
   - Declare only behavior the runtime already supports (entity CRUD + named AI actions). No custom backend handlers.

2. **`frontend/src/App.jsx`**
   - Single-page React app using the Vite scaffold.
   - Talk to entity APIs and `POST /api/ai/generate_flashcards` only.
   - Screens/states in one layout: deck list, generate form, review/edit for the selected deck.
   - Keyboard-friendly controls and `aria-*` labels (form, loading, errors, card front/back, deck list).

3. **`frontend/src/styles.css`**
   - Mobile-first, colorful, spacious enough for touch and focus rings.
   - Styles for empty, loading, error, card flip, and deck list — no extra CSS toolchain.

4. **`README.md`**
   - What the app does, how to run the given runtime, how generate/save/review/edit/delete works, and that data lives in SQLite through the wrapper.

Do **not** add other files, frameworks, `localStorage`, raw SQL, serverless pieces, or generated backend code.

## Implementation steps

1. **Declare data + AI in `app_spec.json`**
   - Add `decks` and `cards` as above; cascade or app-level delete of cards when a deck is removed (whichever the wrapper supports; if no cascade, frontend deletes cards then deck).
   - Define `generate_flashcards` with a strict prompt: eight Q&A pairs, short questions, short answers, no extra commentary. Map input/output fields so the frontend can parse a list of eight cards.

2. **Map UI to runtime APIs (no new backend)**
   - List/create/update/delete decks and cards via the generated entity routes.
   - Generate: POST source text to `/api/ai/generate_flashcards`; on success, hold eight cards in component state until Save.
   - Save: create a `deck`, then create eight `cards` with that `deck_id`.
   - Edit card: update `question`/`answer` on that card.
   - Delete card: delete one card row; allow a deck to end up with fewer than eight cards.
   - Delete deck: remove cards then deck (or cascade).

3. **Build the student flow in `App.jsx`**
   - **Empty:** no decks and no working set — prompt to type a topic or paste notes; primary Generate control.
   - **Generate form:** textarea + Generate; disable while in-flight; show loading copy for screen readers.
   - **Working set:** show eight cards; Save deck; discard/regenerate.
   - **Deck list:** saved decks (title + card count); open and delete; load list on mount so visits restore data.
   - **Review:** one card at a time; flip front/back; previous/next; edit in place (or a small form) and persist; delete card with a confirm control.
   - **Errors:** network/AI/validation messages that do not wipe the list.
   - **A11y:** labeled form fields, buttons with names (“Flip card”, “Save deck”, etc.), `aria-live` for loading/errors, focus visible, card faces announced as question vs answer. Tab order: form → generate → list → review controls.

4. **Style in `styles.css`**
   - Narrow column on small screens; larger type for card text; distinct colors for question vs answer, primary vs destructive actions.
   - Loading (disabled + spinner/text), empty illustration/copy, error banner.
   - Focus outlines and adequate tap targets; no hover-only actions.

5. **Document in `README.md`**
   - Happy path, persistence, AI action name, and that Playwright/backend checks are expected to pass against this UI and spec.

6. **Align with E2E without adding test files**
   - Keep roles, names, and heading structure stable so harness tests can find Generate, Save, Flip, Edit, Delete, and empty/error text.
   - After generate, eight cards must appear; after save, the deck must remain after reload (list fetched from API).

## Evaluation criteria

- Student can type a topic or paste notes, generate eight Q&A cards, save a deck, reload, review (flip + next/prev), edit a card, delete a card, and delete a deck — all through wrapper APIs + one AI action.
- No login; no `localStorage` as source of truth; no raw SQL or handwritten backend.
- Only `app_spec.json`, `frontend/src/App.jsx`, `frontend/src/styles.css`, and `README.md` are introduced or changed for the app.
- Empty, loading, and error states are visible and announced.
- Full flow is usable with keyboard only; interactive elements have accessible names.
- Backend health and Playwright E2E suite pass against this prototype.
- Scope stays a small flashcard prototype: eight-card generation + CRUD review, not a tutor, scheduler, or multi-user product.
