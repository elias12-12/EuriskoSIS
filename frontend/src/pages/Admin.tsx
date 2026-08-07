/**
 * The admin panel: documents, read-only browsers, and the behaviour config form.
 *
 * The browsers are read-only because the dataset is frozen ("Do not edit. Any
 * change invalidates comparison across teams") -- there are no write endpoints
 * behind them to call. The one thing this panel really does change is
 * `assistant_settings`, and it says so plainly, because that is the phase's exit
 * check: saving here must alter the very next chat response with no restart.
 */

import { useCallback, useEffect, useState } from "react";
import {
  admin,
  ApiError,
  clearToken,
  type AssistantSettings,
  type BrowsePage,
  type DocumentStatus,
  type FilterOptions,
  type IngestReport,
} from "../api";

type Tab = "settings" | "documents" | "students" | "courses" | "enrollments";

const TABS: { id: Tab; label: string }[] = [
  { id: "settings", label: "Assistant behaviour" },
  { id: "documents", label: "Documents" },
  { id: "students", label: "Students" },
  { id: "courses", label: "Courses" },
  { id: "enrollments", label: "Enrollments" },
];

const TONES = ["friendly", "neutral", "formal"];
const LENGTHS = ["brief", "standard", "detailed"];

// Offered as a convenience, not a constraint -- the field is free text, because
// the model name is provider-qualified and a new model should not need a
// frontend release to become selectable.
const SUGGESTED_MODELS = [
  "openai:gpt-5-mini",
  "openai:gpt-5",
  "anthropic:claude-opus-4-5",
  "anthropic:claude-haiku-4-5",
];

function SettingsForm() {
  const [settings, setSettings] = useState<AssistantSettings | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    admin
      .settings()
      .then(setSettings)
      .catch((caught) => setError(String(caught)));
  }, []);

  if (error) return <p className="callout callout--error">{error}</p>;
  if (!settings) return <p className="muted">Loading&hellip;</p>;

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!settings) return;
    setSaving(true);
    setStatus(null);
    setError(null);
    try {
      const saved = await admin.saveSettings(settings);
      setSettings(saved);
      setStatus("Saved. The next message any student sends will use these settings.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="panel form" onSubmit={save}>
      <p className="callout">
        These take effect on the <strong>next chat request</strong>. Nothing is
        cached and nothing restarts &mdash; the settings row is read at the start
        of every turn.
      </p>

      <label>
        <span>Tone</span>
        <select
          value={settings.tone}
          onChange={(e) => setSettings({ ...settings, tone: e.target.value })}
        >
          {TONES.map((tone) => (
            <option key={tone} value={tone}>
              {tone}
            </option>
          ))}
        </select>
      </label>

      <label>
        <span>Response length</span>
        <select
          value={settings.response_length}
          onChange={(e) =>
            setSettings({ ...settings, response_length: e.target.value })
          }
        >
          {LENGTHS.map((length) => (
            <option key={length} value={length}>
              {length}
            </option>
          ))}
        </select>
      </label>

      <label>
        <span>Model</span>
        <input
          value={settings.model_name}
          list="model-suggestions"
          onChange={(e) => setSettings({ ...settings, model_name: e.target.value })}
        />
        <datalist id="model-suggestions">
          {SUGGESTED_MODELS.map((model) => (
            <option key={model} value={model} />
          ))}
        </datalist>
        <small className="muted">
          Provider-qualified. Switching provider also needs that provider&rsquo;s
          API key in the environment.
        </small>
      </label>

      <label>
        <span>Temperature ({settings.temperature})</span>
        <input
          type="range"
          min="0"
          max="2"
          step="0.05"
          value={Number(settings.temperature)}
          onChange={(e) =>
            setSettings({ ...settings, temperature: e.target.value })
          }
        />
        <small className="muted">
          Low values keep answers close to what the tools returned, which is what
          a record-and-policy assistant wants.
        </small>
      </label>

      <div className="form__actions">
        <button type="submit" disabled={saving}>
          {saving ? "Saving..." : "Save"}
        </button>
        {status && <span className="ok-note">{status}</span>}
      </div>

      <p className="muted">
        Academic policy is deliberately not editable here. The C&minus; gate, the
        attempt limit and the credit caps come from the Handbook and live in the
        backend&rsquo;s configuration &mdash; an admin form that could change them
        would make wrong graduation answers a supported feature.
      </p>
    </form>
  );
}

function DocumentsPanel() {
  const [documents, setDocuments] = useState<DocumentStatus[]>([]);
  const [reports, setReports] = useState<IngestReport[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    admin
      .documents()
      .then(setDocuments)
      .catch((caught) => setError(String(caught)));
  }, []);

  useEffect(refresh, [refresh]);

  async function run(force: boolean) {
    setBusy(true);
    setError(null);
    setReports(null);
    try {
      setReports(await admin.reingest(force));
      refresh();
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 503
          ? "Ingestion needs OPENAI_API_KEY. Chunking can still be inspected with scripts/inspect_chunks.py."
          : String(caught),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel">
      <div className="form__actions">
        <button type="button" onClick={() => void run(false)} disabled={busy}>
          Re-run ingestion
        </button>
        <button
          type="button"
          className="secondary"
          onClick={() => void run(true)}
          disabled={busy}
        >
          Force re-embed
        </button>
        <span className="muted">
          Unchanged files are skipped by hash; force after a chunker change.
        </span>
      </div>

      {error && <p className="callout callout--error">{error}</p>}

      {documents.length === 0 ? (
        <p className="callout callout--warn">
          Nothing ingested yet. Run ingestion to make document search work.
        </p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Document</th>
              <th>Status</th>
              <th>Pages</th>
              <th>Chunks</th>
              <th>Embedded</th>
              <th>Replace</th>
            </tr>
          </thead>
          <tbody>
            {documents.map((document) => (
              <tr key={document.filename}>
                <td>
                  {document.title}
                  <br />
                  <small className="muted mono">{document.filename}</small>
                </td>
                <td>
                  <span
                    className={
                      document.status === "ready"
                        ? "badge badge--satisfied"
                        : "badge badge--untouched"
                    }
                  >
                    {document.status}
                  </span>
                  {document.error && (
                    <div className="error-note">{document.error}</div>
                  )}
                </td>
                <td>{document.page_count ?? "—"}</td>
                <td>{document.chunk_count}</td>
                {/* Shown apart from chunk_count: "58 chunks, 0 searchable" is a
                    real state that one combined number would hide. */}
                <td
                  className={
                    document.chunk_count !== document.embedded_count
                      ? "error-note"
                      : undefined
                  }
                >
                  {document.embedded_count}
                </td>
                <td>
                  <input
                    type="file"
                    accept="application/pdf"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (!file) return;
                      setBusy(true);
                      admin
                        .replaceDocument(document.filename, file)
                        .then((report) => {
                          setReports([report]);
                          refresh();
                        })
                        .catch((caught) => setError(String(caught)))
                        .finally(() => setBusy(false));
                    }}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {reports && (
        <ul className="reports">
          {reports.map((report) => (
            <li key={report.filename}>
              <strong>{report.filename}</strong>: {report.status}
              {report.unchanged
                ? " (unchanged, skipped)"
                : ` — ${report.chunk_count} chunks`}
              {report.error && <span className="error-note"> {report.error}</span>}
            </li>
          ))}
        </ul>
      )}

      <p className="muted">
        Only the two registered documents can be replaced. A PDF with no
        registered chunker would either be rejected or run through a generic one,
        which would put ungrounded text behind a citation.
      </p>
    </section>
  );
}

function Browser({
  columns,
  fetchPage,
  filters,
}: {
  columns: string[];
  fetchPage: (query: Record<string, string>) => Promise<BrowsePage>;
  filters: { key: string; label: string; options?: string[] }[];
}) {
  const [page, setPage] = useState<BrowsePage | null>(null);
  const [query, setQuery] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const active = Object.fromEntries(
      Object.entries(query).filter(([, value]) => value !== ""),
    );
    fetchPage(active)
      .then(setPage)
      .catch((caught) => setError(String(caught)));
  }, [query, fetchPage]);

  return (
    <section className="panel">
      <div className="filters">
        {filters.map((filter) => (
          <label key={filter.key}>
            <span>{filter.label}</span>
            {filter.options ? (
              <select
                value={query[filter.key] ?? ""}
                onChange={(e) =>
                  setQuery({ ...query, [filter.key]: e.target.value })
                }
              >
                <option value="">any</option>
                {filter.options.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </select>
            ) : (
              <input
                value={query[filter.key] ?? ""}
                placeholder="type to filter"
                onChange={(e) =>
                  setQuery({ ...query, [filter.key]: e.target.value })
                }
              />
            )}
          </label>
        ))}
      </div>

      {error && <p className="callout callout--error">{error}</p>}

      {page && (
        <>
          <p className="muted">
            {page.items.length} shown of {page.total} matching
          </p>
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  {columns.map((column) => (
                    <th key={column}>{column.replace(/_/g, " ")}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {page.items.map((row, index) => (
                  <tr key={index}>
                    {columns.map((column) => (
                      <td key={column}>{String(row[column] ?? "—")}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

export function Admin({ onSignOut }: { onSignOut: () => void }) {
  const [tab, setTab] = useState<Tab>("settings");
  const [options, setOptions] = useState<FilterOptions | null>(null);

  useEffect(() => {
    admin
      .filters()
      .then(setOptions)
      .catch((caught) => {
        if (caught instanceof ApiError && caught.status === 401) {
          clearToken("admin");
          onSignOut();
        }
      });
  }, [onSignOut]);

  return (
    <div className="portal">
      <header className="portal__head">
        <div>
          <h2>Administration</h2>
          <p className="muted">
            Browsers are read-only; the dataset is frozen. Behaviour settings are
            live.
          </p>
        </div>
        <button
          type="button"
          className="link-button"
          onClick={() => {
            void admin.logout().catch(() => undefined);
            clearToken("admin");
            onSignOut();
          }}
        >
          Sign out
        </button>
      </header>

      <nav className="tabs">
        {TABS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            className={tab === entry.id ? "tab tab--active" : "tab"}
            onClick={() => setTab(entry.id)}
          >
            {entry.label}
          </button>
        ))}
      </nav>

      {tab === "settings" && <SettingsForm />}
      {tab === "documents" && <DocumentsPanel />}

      {tab === "students" && (
        <Browser
          fetchPage={admin.students}
          columns={[
            "student_id",
            "first_name",
            "last_name",
            "email",
            "program_code",
            "entry_term",
            "expected_graduation_term",
            "academic_status",
            "advisor_name",
          ]}
          filters={[
            { key: "search", label: "Search" },
            { key: "program_code", label: "Programme", options: options?.programs },
            {
              key: "academic_status",
              label: "Standing",
              options: options?.academic_statuses,
            },
          ]}
        />
      )}

      {tab === "courses" && (
        <Browser
          fetchPage={admin.courses}
          columns={["course_code", "title", "credits", "prerequisites"]}
          filters={[
            { key: "search", label: "Search" },
            { key: "subject", label: "Subject", options: options?.subjects },
          ]}
        />
      )}

      {tab === "enrollments" && (
        <Browser
          fetchPage={admin.enrollments}
          columns={[
            "student_id",
            "first_name",
            "last_name",
            "term_code",
            "course_code",
            "title",
            "credits",
            "grade",
            "status",
          ]}
          filters={[
            { key: "student_id", label: "Student ID" },
            { key: "course_code", label: "Course code" },
            { key: "term_code", label: "Term", options: options?.terms },
            { key: "grade", label: "Grade", options: options?.grades },
          ]}
        />
      )}
    </div>
  );
}
