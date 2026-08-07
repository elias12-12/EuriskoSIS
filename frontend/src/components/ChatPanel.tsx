/**
 * The chat panel.
 *
 * `conversation_id` is held in state and passed back on every turn, which is
 * what makes "what about next term?" resolve without repeating the question --
 * the backend replays that conversation's stored history.
 *
 * The tools the agent called are shown under each reply. That is not decoration:
 * "did it actually look anything up, or is it improvising?" is the first
 * question when an answer looks wrong, and reading it off the prose is guesswork.
 * It also makes the human-in-the-loop flow legible -- you can see
 * `request_advisor_appointment` fire on one turn and `confirm_advisor_appointment`
 * only after you say yes.
 */

import { useRef, useState } from "react";
import { ApiError, student } from "../api";

interface Turn {
  role: "user" | "assistant";
  text: string;
  tools?: string[];
  model?: string;
}

const SUGGESTIONS = [
  "What's my schedule this term?",
  "Am I allowed to register for MECH 310?",
  "When is the last day to drop a course without a W?",
  "How is my GPA calculated?",
  "Who do I contact about a scholarship?",
];

export function ChatPanel() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const conversationId = useRef<number | null>(null);

  async function send(message: string) {
    const text = message.trim();
    if (!text || busy) return;

    setTurns((previous) => [...previous, { role: "user", text }]);
    setDraft("");
    setBusy(true);
    setError(null);

    try {
      const response = await student.chat(text, conversationId.current);
      conversationId.current = response.conversation_id;
      setTurns((previous) => [
        ...previous,
        {
          role: "assistant",
          text: response.reply,
          tools: response.tool_calls,
          model: response.model_name,
        },
      ]);
    } catch (caught) {
      const detail =
        caught instanceof ApiError
          ? caught.status === 503
            ? "The assistant is not configured: OPENAI_API_KEY is missing. Everything else on this page still works."
            : caught.message
          : String(caught);
      setError(detail);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel chat">
      <div className="chat__log">
        {turns.length === 0 && (
          <div className="chat__empty">
            <p>Ask about your record, or about University policy.</p>
            <ul className="chat__suggestions">
              {SUGGESTIONS.map((suggestion) => (
                <li key={suggestion}>
                  <button type="button" onClick={() => void send(suggestion)}>
                    {suggestion}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        {turns.map((turn, index) => (
          <div key={index} className={`bubble bubble--${turn.role}`}>
            <p className="bubble__text">{turn.text}</p>
            {turn.tools && turn.tools.length > 0 && (
              <p className="bubble__tools">
                {turn.tools.map((tool) => (
                  <code key={tool}>{tool}</code>
                ))}
                <span className="muted"> &middot; {turn.model}</span>
              </p>
            )}
            {turn.role === "assistant" && turn.tools?.length === 0 && (
              <p className="bubble__tools muted">
                answered without calling a tool
              </p>
            )}
          </div>
        ))}

        {busy && <p className="muted chat__thinking">Thinking&hellip;</p>}
        {error && <p className="callout callout--error">{error}</p>}
      </div>

      <form
        className="chat__composer"
        onSubmit={(event) => {
          event.preventDefault();
          void send(draft);
        }}
      >
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Ask a question..."
          aria-label="Message"
          disabled={busy}
        />
        <button type="submit" disabled={busy || !draft.trim()}>
          Send
        </button>
      </form>
    </section>
  );
}
