import { useEffect, useMemo, useState } from "react";
import "./styles.css";

async function parseApiError(response) {
  try {
    const data = await response.json();
    if (typeof data?.detail === "string") return data.detail;
    if (Array.isArray(data?.detail)) {
      return data.detail.map((item) => item.msg || String(item)).join(" ");
    }
  } catch {
    /* ignore non-JSON errors */
  }
  return response.statusText || "Request failed";
}

async function apiGet(resource) {
  const response = await fetch(`/api/${resource}`);
  if (!response.ok) throw new Error(await parseApiError(response));
  return response.json();
}

async function apiCreate(resource, values) {
  const response = await fetch(`/api/${resource}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
  if (!response.ok) throw new Error(await parseApiError(response));
  return response.json();
}

async function apiUpdate(resource, id, values) {
  const response = await fetch(`/api/${resource}/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
  if (!response.ok) throw new Error(await parseApiError(response));
  return response.json();
}

async function apiDelete(resource, id) {
  const response = await fetch(`/api/${resource}/${id}`, { method: "DELETE" });
  if (!response.ok) throw new Error(await parseApiError(response));
}

function parseFlashcards(content) {
  if (!content || !String(content).trim()) {
    throw new Error("The AI returned an empty response.");
  }
  let text = String(content).trim();
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fenced) text = fenced[1].trim();
  const start = text.indexOf("[");
  const end = text.lastIndexOf("]");
  if (start !== -1 && end > start) text = text.slice(start, end + 1);
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    throw new Error("Could not read flashcards from the AI response.");
  }
  const list = Array.isArray(data)
    ? data
    : Array.isArray(data?.cards)
      ? data.cards
      : Array.isArray(data?.flashcards)
        ? data.flashcards
        : null;
  if (!list) {
    throw new Error("Could not read flashcards from the AI response.");
  }
  const normalized = list
    .map((item) => {
      if (!item || typeof item !== "object") return null;
      const question = String(item.question || item.q || item.front || "").trim();
      const answer = String(item.answer || item.a || item.back || "").trim();
      if (!question || !answer) return null;
      return { question, answer };
    })
    .filter(Boolean);
  if (normalized.length < 8) {
    throw new Error("The AI did not return eight flashcards. Try again.");
  }
  return normalized.slice(0, 8);
}

export default function App() {
  const [sourceInput, setSourceInput] = useState("");
  const [decks, setDecks] = useState([]);
  const [cards, setCards] = useState([]);
  const [workingCards, setWorkingCards] = useState([]);
  const [selectedDeckId, setSelectedDeckId] = useState(null);
  const [cardIndex, setCardIndex] = useState(0);
  const [flipped, setFlipped] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editQuestion, setEditQuestion] = useState("");
  const [editAnswer, setEditAnswer] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      try {
        const [deckRows, cardRows] = await Promise.all([
          apiGet("decks"),
          apiGet("cards"),
        ]);
        if (!cancelled) {
          setDecks(deckRows);
          setCards(cardRows);
          setError("");
        }
      } catch (err) {
        if (!cancelled) {
          setError(err.message || "Could not load saved decks.");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  const selectedDeck = useMemo(
    () => decks.find((deck) => deck.id === selectedDeckId) || null,
    [decks, selectedDeckId],
  );

  const selectedCards = useMemo(
    () =>
      cards
        .filter((card) => card.deck_id === selectedDeckId)
        .slice()
        .sort((a, b) => a.id - b.id),
    [cards, selectedDeckId],
  );

  const currentCard = selectedCards[cardIndex] || null;

  function openDeck(deckId) {
    setSelectedDeckId(deckId);
    setCardIndex(0);
    setFlipped(false);
    setEditing(false);
    setError("");
    setStatus("Reviewing deck.");
  }

  async function handleCreate(event) {
    event.preventDefault();
    const text = sourceInput.trim();
    if (!text) {
      setError("Enter a topic or paste notes first.");
      return;
    }
    setBusy("save");
    setError("");
    setStatus("Saving deck...");
    try {
      const deck = await apiCreate("decks", {
        title: text,
        source_text: text,
      });
      const createdCards = [];
      for (const card of workingCards) {
        const saved = await apiCreate("cards", {
          deck_id: deck.id,
          question: card.question,
          answer: card.answer,
        });
        createdCards.push(saved);
      }
      setDecks((prev) => [deck, ...prev.filter((item) => item.id !== deck.id)]);
      if (createdCards.length) {
        setCards((prev) => [...createdCards, ...prev]);
        setWorkingCards([]);
      }
      setSourceInput("");
      openDeck(deck.id);
      setStatus("Deck saved.");
    } catch (err) {
      setError(err.message || "Could not save the deck.");
      setStatus("");
    } finally {
      setBusy("");
    }
  }

  async function handleGenerate(event) {
    event.preventDefault();
    const text = sourceInput.trim();
    if (!text) {
      setError("Enter a topic or paste notes first.");
      return;
    }
    setBusy("generate");
    setError("");
    setStatus("Generating eight flashcards...");
    try {
      const response = await fetch("/api/ai/generate_flashcards", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: text }),
      });
      if (!response.ok) throw new Error(await parseApiError(response));
      const data = await response.json();
      const nextCards = parseFlashcards(data.content);
      setWorkingCards(nextCards);
      setStatus("Eight flashcards ready. Save the deck to keep them.");
    } catch (err) {
      setError(err.message || "Could not generate flashcards.");
      setStatus("");
    } finally {
      setBusy("");
    }
  }

  async function handleDeleteDeck(deck) {
    if (!window.confirm(`Delete deck "${deck.title}" and its cards?`)) return;
    setError("");
    try {
      const related = cards.filter((card) => card.deck_id === deck.id);
      for (const card of related) {
        await apiDelete("cards", card.id);
      }
      await apiDelete("decks", deck.id);
      setCards((prev) => prev.filter((card) => card.deck_id !== deck.id));
      setDecks((prev) => prev.filter((item) => item.id !== deck.id));
      if (selectedDeckId === deck.id) {
        setSelectedDeckId(null);
        setCardIndex(0);
        setFlipped(false);
        setEditing(false);
      }
      setStatus("Deck deleted.");
    } catch (err) {
      setError(err.message || "Could not delete the deck.");
    }
  }

  function startEdit() {
    if (!currentCard) return;
    setEditQuestion(currentCard.question);
    setEditAnswer(currentCard.answer);
    setEditing(true);
  }

  async function handleSaveEdit(event) {
    event.preventDefault();
    if (!currentCard) return;
    const question = editQuestion.trim();
    const answer = editAnswer.trim();
    if (!question || !answer) {
      setError("Question and answer are both required.");
      return;
    }
    setBusy("edit");
    setError("");
    try {
      const updated = await apiUpdate("cards", currentCard.id, {
        question,
        answer,
      });
      setCards((prev) =>
        prev.map((item) => (item.id === updated.id ? { ...item, ...updated } : item)),
      );
      setEditing(false);
      setStatus("Card updated.");
    } catch (err) {
      setError(err.message || "Could not update the card.");
    } finally {
      setBusy("");
    }
  }

  async function handleDeleteCard(card) {
    if (!window.confirm("Delete this card?")) return;
    setError("");
    try {
      await apiDelete("cards", card.id);
      const remaining = selectedCards.filter((item) => item.id !== card.id);
      setCards((prev) => prev.filter((item) => item.id !== card.id));
      setFlipped(false);
      setEditing(false);
      setCardIndex((index) => Math.max(0, Math.min(index, remaining.length - 1)));
      setStatus("Card deleted.");
    } catch (err) {
      setError(err.message || "Could not delete the card.");
    }
  }

  function goToCard(nextIndex) {
    setCardIndex(nextIndex);
    setFlipped(false);
    setEditing(false);
  }

  const isEmpty = !loading && decks.length === 0 && workingCards.length === 0;

  return (
    <div className="app" data-testid="app-root">
      <header className="hero">
        <p className="eyebrow">Study Flashcards</p>
        <h1>Turn a topic into eight cards you can actually review</h1>
        <p className="lede">
          Type a topic or paste notes, generate concise Q&amp;A cards, then flip,
          edit, and keep only what helps you study.
        </p>
      </header>

      <div className="live-region" aria-live="polite" aria-atomic="true">
        {loading ? "Loading saved decks..." : status}
      </div>

      {error ? (
        <div className="banner error" role="alert">
          {error}
        </div>
      ) : null}

      <section className="panel" aria-labelledby="generate-heading">
        <h2 id="generate-heading">Create a deck</h2>
        <form className="generate-form" onSubmit={handleCreate}>
          <label htmlFor="primary-input">Topic or pasted notes</label>
          <textarea
            id="primary-input"
            data-testid="primary-input"
            name="source"
            required
            rows={5}
            value={sourceInput}
            onChange={(event) => setSourceInput(event.target.value)}
            placeholder="Photosynthesis, Spanish irregular verbs, or paste class notes..."
            disabled={Boolean(busy)}
          />
          <div className="actions">
            <button
              type="submit"
              className="primary"
              data-testid="create-submit"
              disabled={busy === "save"}
            >
              {busy === "save" ? "Saving..." : "Save deck"}
            </button>
            <button
              type="button"
              className="accent"
              onClick={handleGenerate}
              disabled={Boolean(busy)}
              aria-busy={busy === "generate"}
            >
              {busy === "generate" ? "Generating..." : "Generate flashcards"}
            </button>
          </div>
        </form>
        {busy === "generate" ? (
          <p className="loading-copy">
            <span className="spinner" aria-hidden="true" />
            Generating eight concise flashcards...
          </p>
        ) : null}
      </section>

      {workingCards.length > 0 ? (
        <section className="panel" aria-labelledby="draft-heading">
          <div className="section-head">
            <h2 id="draft-heading">Draft flashcards</h2>
            <button
              type="button"
              className="ghost"
              onClick={() => {
                setWorkingCards([]);
                setStatus("Draft flashcards discarded.");
              }}
            >
              Discard draft
            </button>
          </div>
          <p className="hint">Save the deck to store these eight cards.</p>
          <ol className="draft-list">
            {workingCards.map((card, index) => (
              <li key={`draft-${index}`}>
                <p>
                  <span className="tag">Q</span> {card.question}
                </p>
                <p>
                  <span className="tag answer">A</span> {card.answer}
                </p>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      <section className="panel" aria-labelledby="decks-heading">
        <h2 id="decks-heading">Saved decks</h2>
        {loading ? (
          <p className="loading-copy">
            <span className="spinner" aria-hidden="true" />
            Loading decks...
          </p>
        ) : null}
        {isEmpty ? (
          <div className="empty">
            <div className="empty-art" aria-hidden="true">
              <span />
              <span />
              <span />
            </div>
            <p>No decks yet. Type a topic or paste notes to get started.</p>
          </div>
        ) : null}
        {!loading && decks.length === 0 && workingCards.length > 0 ? (
          <p className="hint">Save the draft to keep this deck across visits.</p>
        ) : null}
        <div className="deck-list">
          {decks.map((deck) => {
            const count = cards.filter((card) => card.deck_id === deck.id).length;
            return (
              <article
                key={deck.id}
                className={`deck-row${selectedDeckId === deck.id ? " is-selected" : ""}`}
                data-testid="resource-item"
              >
                <div className="deck-row-body">
                  <h3>{deck.title}</h3>
                  <p>
                    {count} {count === 1 ? "card" : "cards"}
                  </p>
                </div>
                <div className="deck-row-actions">
                  <button
                    type="button"
                    onClick={() => openDeck(deck.id)}
                    aria-label={`Review deck ${deck.title}`}
                  >
                    Review
                  </button>
                  <button
                    type="button"
                    className="danger"
                    data-testid="delete-button"
                    aria-label={`Delete deck ${deck.title}`}
                    onClick={() => handleDeleteDeck(deck)}
                  >
                    Delete
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      </section>

      {selectedDeck ? (
        <section className="panel review" aria-labelledby="review-heading">
          <div className="section-head">
            <h2 id="review-heading">Review</h2>
            <button
              type="button"
              className="ghost"
              onClick={() => {
                setSelectedDeckId(null);
                setEditing(false);
              }}
            >
              Close review
            </button>
          </div>
          <p className="hint">{selectedDeck.title}</p>
          {selectedCards.length === 0 ? (
            <div className="empty compact">
              <p>This deck has no cards yet. Generate flashcards, then save.</p>
            </div>
          ) : (
            <>
              <p className="progress">
                Card {cardIndex + 1} of {selectedCards.length}
              </p>
              {editing ? (
                <form className="edit-form" onSubmit={handleSaveEdit}>
                  <label htmlFor="edit-question">Question</label>
                  <textarea
                    id="edit-question"
                    rows={3}
                    value={editQuestion}
                    onChange={(event) => setEditQuestion(event.target.value)}
                  />
                  <label htmlFor="edit-answer">Answer</label>
                  <textarea
                    id="edit-answer"
                    rows={3}
                    value={editAnswer}
                    onChange={(event) => setEditAnswer(event.target.value)}
                  />
                  <div className="actions">
                    <button type="submit" className="primary" disabled={busy === "edit"}>
                      {busy === "edit" ? "Saving..." : "Save card"}
                    </button>
                    <button type="button" className="ghost" onClick={() => setEditing(false)}>
                      Cancel
                    </button>
                  </div>
                </form>
              ) : (
                <>
                  <button
                    type="button"
                    className={`study-card ${flipped ? "answer" : "question"}`}
                    onClick={() => setFlipped((value) => !value)}
                    aria-label={flipped ? "Flip card to question" : "Flip card to answer"}
                  >
                    <span className="face-label">{flipped ? "Answer" : "Question"}</span>
                    <span className="face-text">
                      {flipped ? currentCard.answer : currentCard.question}
                    </span>
                  </button>
                  <div className="actions wrap">
                    <button
                      type="button"
                      onClick={() => goToCard(Math.max(0, cardIndex - 1))}
                      disabled={cardIndex === 0}
                      aria-label="Previous card"
                    >
                      Previous
                    </button>
                    <button
                      type="button"
                      className="accent"
                      onClick={() => setFlipped((value) => !value)}
                      aria-label={flipped ? "Flip card to question" : "Flip card"}
                    >
                      Flip card
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        goToCard(Math.min(selectedCards.length - 1, cardIndex + 1))
                      }
                      disabled={cardIndex >= selectedCards.length - 1}
                      aria-label="Next card"
                    >
                      Next
                    </button>
                    <button type="button" onClick={startEdit} aria-label="Edit card">
                      Edit
                    </button>
                    <button
                      type="button"
                      className="danger"
                      onClick={() => handleDeleteCard(currentCard)}
                      aria-label="Delete card"
                    >
                      Delete card
                    </button>
                  </div>
                </>
              )}
            </>
          )}
        </section>
      ) : null}
    </div>
  );
}
