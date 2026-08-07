/**
 * Both login screens.
 *
 * The student form takes an ID and nothing else -- the brief says that is
 * sufficient identity. The five IDs are listed because this is a demonstration
 * stack with a fixed dataset, and making a reviewer guess them would be
 * unhelpful theatre; there is no secret here to protect.
 *
 * The admin form takes the shared password from the environment. Separate
 * screen, separate endpoint, separate token, because they are separate
 * principals on the backend.
 */

import { useState } from "react";
import { admin, setToken, student } from "../api";

const KNOWN_STUDENTS = [
  { id: "S2023011", note: "4th year, on track" },
  { id: "S2023027", note: "4th year, weak Gen Ed, no capstone" },
  { id: "S2024019", note: "3rd year, ordinary progression" },
  { id: "S2025008", note: "2nd year, academic probation" },
  { id: "S2026042", note: "1st term, no history yet" },
];

export function StudentLogin({ onSignedIn }: { onSignedIn: () => void }) {
  const [studentId, setStudentId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function signIn(id: string) {
    setBusy(true);
    setError(null);
    try {
      const response = await student.login(id.trim().toUpperCase());
      setToken("student", response.access_token);
      onSignedIn();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel login">
      <h2>Student portal</h2>
      <p className="muted">Sign in with your student ID.</p>

      <form
        className="login__form"
        onSubmit={(event) => {
          event.preventDefault();
          void signIn(studentId);
        }}
      >
        <input
          value={studentId}
          onChange={(event) => setStudentId(event.target.value)}
          placeholder="S2023011"
          aria-label="Student ID"
          autoFocus
        />
        <button type="submit" disabled={busy || !studentId.trim()}>
          Sign in
        </button>
      </form>

      {error && <p className="callout callout--error">{error}</p>}

      <h3>Demonstration accounts</h3>
      <ul className="login__accounts">
        {KNOWN_STUDENTS.map((entry) => (
          <li key={entry.id}>
            <button type="button" onClick={() => void signIn(entry.id)} disabled={busy}>
              <span className="mono">{entry.id}</span>
              <span className="muted">{entry.note}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function AdminLogin({ onSignedIn }: { onSignedIn: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  return (
    <section className="panel login">
      <h2>Administration</h2>
      <p className="muted">
        Sign in with the shared administrator password (<code>ADMIN_PASSWORD</code>).
      </p>

      <form
        className="login__form"
        onSubmit={(event) => {
          event.preventDefault();
          setBusy(true);
          setError(null);
          admin
            .login(password)
            .then((response) => {
              setToken("admin", response.access_token);
              onSignedIn();
            })
            .catch((caught) =>
              setError(caught instanceof Error ? caught.message : String(caught)),
            )
            .finally(() => setBusy(false));
        }}
      >
        <input
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          placeholder="Administrator password"
          aria-label="Administrator password"
          autoFocus
        />
        <button type="submit" disabled={busy || !password}>
          Sign in
        </button>
      </form>

      {error && <p className="callout callout--error">{error}</p>}
    </section>
  );
}
