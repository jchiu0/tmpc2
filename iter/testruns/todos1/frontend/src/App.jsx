import { useEffect, useRef, useState } from "react";
import "./styles.css";

const API = "/api/todos";

function isCompleted(todo) {
  return todo.completed === true;
}

export default function App() {
  const [todos, setTodos] = useState([]);
  const [title, setTitle] = useState("");
  const [notes, setNotes] = useState("");
  const [editingId, setEditingId] = useState(null);
  const [editTitle, setEditTitle] = useState("");
  const [editNotes, setEditNotes] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const titleInputRef = useRef(null);
  const editTitleRef = useRef(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const response = await fetch(API);
        if (!response.ok) {
          throw new Error("Failed to load todos");
        }
        const data = await response.json();
        if (!cancelled) {
          setTodos(Array.isArray(data) ? data : []);
          setError("");
        }
      } catch {
        if (!cancelled) {
          setError("Could not load todos. Make sure the API is running.");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (editingId != null && editTitleRef.current) {
      editTitleRef.current.focus();
      editTitleRef.current.select();
    }
  }, [editingId]);

  async function addTodo(event) {
    event.preventDefault();
    const nextTitle = title.trim();
    const nextNotes = notes.trim();
    if (!nextTitle) {
      setError("Title cannot be empty.");
      return;
    }
    if (busy) {
      return;
    }
    setBusy(true);
    try {
      const response = await fetch(API, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: nextTitle,
          notes: nextNotes,
          completed: false,
        }),
      });
      if (!response.ok) {
        throw new Error("Failed to add todo");
      }
      const created = await response.json();
      setTodos((current) => [created, ...current]);
      setTitle("");
      setNotes("");
      setError("");
      titleInputRef.current?.focus();
    } catch {
      setError("Could not add that todo.");
    } finally {
      setBusy(false);
    }
  }

  async function toggleTodo(todo) {
    try {
      const response = await fetch(`${API}/${todo.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ completed: !isCompleted(todo) }),
      });
      if (!response.ok) {
        throw new Error("Failed to update todo");
      }
      const updated = await response.json();
      setTodos((current) =>
        current.map((item) => (item.id === updated.id ? updated : item))
      );
      setError("");
    } catch {
      setError("Could not update that todo.");
    }
  }

  function startEdit(todo) {
    setEditingId(todo.id);
    setEditTitle(todo.title ?? "");
    setEditNotes(todo.notes ?? "");
    setError("");
  }

  function cancelEdit() {
    setEditingId(null);
    setEditTitle("");
    setEditNotes("");
  }

  async function saveEdit(todo) {
    const nextTitle = editTitle.trim();
    const nextNotes = editNotes.trim();
    if (!nextTitle) {
      setError("Title cannot be empty.");
      return;
    }
    const currentNotes = (todo.notes ?? "").trim();
    if (nextTitle === todo.title && nextNotes === currentNotes) {
      cancelEdit();
      return;
    }
    try {
      const response = await fetch(`${API}/${todo.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: nextTitle, notes: nextNotes }),
      });
      if (!response.ok) {
        throw new Error("Failed to save todo");
      }
      const updated = await response.json();
      setTodos((current) =>
        current.map((item) => (item.id === updated.id ? updated : item))
      );
      cancelEdit();
      setError("");
    } catch {
      setError("Could not save those changes.");
    }
  }

  function onEditKeyDown(event, todo) {
    if (event.key === "Enter" && event.target.tagName !== "TEXTAREA") {
      event.preventDefault();
      saveEdit(todo);
    } else if (event.key === "Escape") {
      event.preventDefault();
      cancelEdit();
    }
  }

  async function deleteTodo(todo) {
    const confirmed = window.confirm(`Delete "${todo.title}"?`);
    if (!confirmed) {
      return;
    }
    try {
      const response = await fetch(`${API}/${todo.id}`, { method: "DELETE" });
      if (!response.ok) {
        throw new Error("Failed to delete todo");
      }
      setTodos((current) => current.filter((item) => item.id !== todo.id));
      if (editingId === todo.id) {
        cancelEdit();
      }
      setError("");
    } catch {
      setError("Could not delete that todo.");
    }
  }

  const empty = !loading && todos.length === 0;

  return (
    <div className="page" data-testid="app-root">
      <main className="card">
        <header className="header">
          <h1>Todos</h1>
          <p className="subtitle">
            A simple personal list. Add, edit, complete, and delete tasks.
          </p>
        </header>

        <form className="add-form" onSubmit={addTodo} aria-busy={busy}>
          <div className="field">
            <label htmlFor="new-title">Title</label>
            <div className="add-row">
              <input
                id="new-title"
                data-testid="primary-input"
                ref={titleInputRef}
                type="text"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder="What needs to be done?"
                autoComplete="off"
                autoFocus
              />
              <button type="submit" data-testid="create-submit" disabled={busy}>
                {busy ? "Adding…" : "Add"}
              </button>
            </div>
          </div>
          <div className="field">
            <label htmlFor="new-notes">Notes (optional)</label>
            <textarea
              id="new-notes"
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
              placeholder="Add a short description"
              rows={2}
            />
          </div>
        </form>

        {error ? (
          <p className="error" role="alert">
            {error}
          </p>
        ) : null}

        {loading ? (
          <p className="status">Loading todos…</p>
        ) : empty ? (
          <p className="empty">No todos yet. Add one above to get started.</p>
        ) : (
          <ul className="list">
            {todos.map((todo) => {
              const done = isCompleted(todo);
              const editing = editingId === todo.id;
              const noteText = (todo.notes ?? "").trim();
              return (
                <li
                  key={todo.id}
                  data-testid="resource-item"
                  className={done ? "item is-completed" : "item"}
                >
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={done}
                      onChange={() => toggleTodo(todo)}
                      aria-label={
                        done
                          ? `Mark ${todo.title} as not completed`
                          : `Mark ${todo.title} as completed`
                      }
                    />
                  </label>
                  {editing ? (
                    <div className="edit-fields">
                      <label className="sr-only" htmlFor={`edit-title-${todo.id}`}>
                        Edit title
                      </label>
                      <input
                        id={`edit-title-${todo.id}`}
                        ref={editTitleRef}
                        className="edit-input"
                        value={editTitle}
                        onChange={(event) => setEditTitle(event.target.value)}
                        onKeyDown={(event) => onEditKeyDown(event, todo)}
                      />
                      <label className="sr-only" htmlFor={`edit-notes-${todo.id}`}>
                        Edit notes
                      </label>
                      <textarea
                        id={`edit-notes-${todo.id}`}
                        className="edit-notes"
                        value={editNotes}
                        onChange={(event) => setEditNotes(event.target.value)}
                        onKeyDown={(event) => onEditKeyDown(event, todo)}
                        rows={2}
                      />
                      <div className="actions">
                        <button type="button" onClick={() => saveEdit(todo)}>
                          Save
                        </button>
                        <button type="button" onClick={cancelEdit}>
                          Cancel
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="body">
                      <p className="title-text">{todo.title}</p>
                      {noteText ? <p className="notes-text">{noteText}</p> : null}
                    </div>
                  )}
                  {!editing ? (
                    <div className="actions">
                      <button type="button" onClick={() => startEdit(todo)}>
                        Edit
                      </button>
                      <button
                        type="button"
                        data-testid="delete-button"
                        className="danger"
                        onClick={() => deleteTodo(todo)}
                      >
                        Delete
                      </button>
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </main>
    </div>
  );
}
