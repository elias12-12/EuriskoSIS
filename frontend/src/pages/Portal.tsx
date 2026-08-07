/**
 * The student portal.
 *
 * Every request here goes to `/me/*` and carries only the session token. There
 * is no student ID anywhere in this file, which is the point: the surface a
 * student's browser talks to cannot name a different student.
 */

import { useEffect, useState } from "react";
import {
  ApiError,
  clearToken,
  student,
  type Appointment,
  type CourseHistory,
  type DegreeProgress,
  type Profile,
  type Schedule,
} from "../api";
import { ChatPanel } from "../components/ChatPanel";
import { DegreeProgressPanel } from "../components/DegreeProgressPanel";

type Tab = "overview" | "schedule" | "history" | "progress" | "chat";

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "progress", label: "Degree progress" },
  { id: "schedule", label: "Schedule" },
  { id: "history", label: "Academic history" },
  { id: "chat", label: "Assistant" },
];

export function Portal({ onSignOut }: { onSignOut: () => void }) {
  const [tab, setTab] = useState<Tab>("overview");
  const [profile, setProfile] = useState<Profile | null>(null);
  const [schedule, setSchedule] = useState<Schedule | null>(null);
  const [history, setHistory] = useState<CourseHistory | null>(null);
  const [progress, setProgress] = useState<DegreeProgress | null>(null);
  const [appointments, setAppointments] = useState<Appointment[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const [p, s, h, d, a] = await Promise.all([
          student.profile(),
          student.schedule(),
          student.courses(),
          student.degreeProgress(),
          student.appointments(),
        ]);
        if (cancelled) return;
        setProfile(p);
        setSchedule(s);
        setHistory(h);
        setProgress(d);
        setAppointments(a);
      } catch (caught) {
        if (cancelled) return;
        // An expired session is the common case, and it should return the user
        // to the login screen rather than showing a broken page.
        if (caught instanceof ApiError && caught.status === 401) {
          clearToken("student");
          onSignOut();
          return;
        }
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, [onSignOut]);

  if (error) return <p className="callout callout--error">{error}</p>;
  if (!profile || !schedule || !history || !progress) {
    return <p className="muted">Loading your record&hellip;</p>;
  }

  const gpa = profile.academics.gpa;

  return (
    <div className="portal">
      <header className="portal__head">
        <div>
          <h2>
            {profile.first_name} {profile.last_name}
          </h2>
          <p className="muted">
            {profile.student_id} &middot; {profile.program_name} &middot; advisor{" "}
            {profile.advisor_name}
          </p>
        </div>
        <div className="portal__head-actions">
          <span
            className={
              profile.academic_status === "Good standing"
                ? "badge badge--satisfied"
                : "badge badge--untouched"
            }
          >
            {profile.academic_status}
          </span>
          <button
            type="button"
            className="link-button"
            onClick={() => {
              void student.logout().catch(() => undefined);
              clearToken("student");
              onSignOut();
            }}
          >
            Sign out
          </button>
        </div>
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

      {tab === "overview" && (
        <section className="panel">
          <div className="stat-row">
            <div className="stat">
              <p className="eyebrow">Cumulative GPA</p>
              {/* Null, not 0.00. A first-term student has no GPA, and 0.00
                  would read as failing -- the backend is careful about this
                  and the UI must not undo it. */}
              <p className="headline-number">{gpa ?? "—"}</p>
              {gpa === null && <p className="muted">No graded courses yet</p>}
            </div>
            <div className="stat">
              <p className="eyebrow">Credits earned</p>
              <p className="headline-number">{profile.academics.credits_earned}</p>
              <p className="muted">of {profile.total_credits_required} required</p>
            </div>
            <div className="stat">
              <p className="eyebrow">In progress</p>
              <p className="headline-number">
                {profile.academics.credits_in_progress}
              </p>
              <p className="muted">{schedule.term_code}</p>
            </div>
            <div className="stat">
              <p className="eyebrow">Expected graduation</p>
              <p className="headline-number headline-number--secondary">
                {profile.expected_graduation_term}
              </p>
            </div>
          </div>

          <dl className="detail-grid">
            <dt>Email</dt>
            <dd>{profile.email}</dd>
            <dt>Programme</dt>
            <dd>
              {profile.program_name} ({profile.program_code})
            </dd>
            <dt>Entry term</dt>
            <dd>{profile.entry_term_name}</dd>
            <dt>Advisor</dt>
            <dd>{profile.advisor_name}</dd>
          </dl>

          <h3>Advisor appointments</h3>
          {appointments.length === 0 ? (
            <p className="muted">
              None booked. Ask the assistant to suggest a time &mdash; it will
              propose one and wait for you to confirm before anything is booked.
            </p>
          ) : (
            <ul className="appointments">
              {appointments.map((appointment) => (
                <li key={appointment.id}>
                  <strong>
                    {new Date(appointment.proposed_time).toLocaleString()}
                  </strong>{" "}
                  with {appointment.advisor_name}
                  <span className="muted"> &mdash; {appointment.reason}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {tab === "progress" && <DegreeProgressPanel progress={progress} />}

      {tab === "schedule" && (
        <section className="panel">
          <h3>
            {schedule.term_code} &mdash; {schedule.total_credits} credits
          </h3>
          {schedule.classes.length === 0 ? (
            <p className="muted">Not registered for any classes this term.</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Course</th>
                  <th>Title</th>
                  <th>Credits</th>
                  <th>Days</th>
                  <th>Time</th>
                  <th>Room</th>
                  <th>Instructor</th>
                </tr>
              </thead>
              <tbody>
                {schedule.classes.map((entry) => (
                  <tr key={entry.course_code}>
                    <td className="mono">{entry.course_code}</td>
                    <td>{entry.title}</td>
                    <td>{entry.credits}</td>
                    <td>{entry.days}</td>
                    <td className="mono">
                      {entry.start_time.slice(0, 5)}&ndash;{entry.end_time.slice(0, 5)}
                    </td>
                    <td>{entry.room}</td>
                    <td>{entry.instructor}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      {tab === "history" && (
        <section className="panel">
          <h3>Academic history</h3>
          {history.courses.length === 0 ? (
            <p className="muted">No courses on record yet.</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Term</th>
                  <th>Course</th>
                  <th>Title</th>
                  <th>Credits</th>
                  <th>Grade</th>
                  <th>Counts toward GPA</th>
                </tr>
              </thead>
              <tbody>
                {history.courses.map((entry) => (
                  <tr key={`${entry.term_code}-${entry.course_code}`}>
                    <td className="mono">{entry.term_code}</td>
                    <td className="mono">{entry.course_code}</td>
                    <td>{entry.title}</td>
                    <td>{entry.credits}</td>
                    <td>
                      {entry.grade ?? (
                        <span className="muted">{entry.status}</span>
                      )}
                    </td>
                    <td className="muted">
                      {entry.grade === null
                        ? "—"
                        : entry.included_in_gpa
                          ? `yes (${entry.grade_points})`
                          : "no"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="muted">
            W and P grades earn no grade points and are excluded from the GPA
            entirely &mdash; from both the points and the credits attempted
            (Student Handbook, section 1.2).
          </p>
        </section>
      )}

      {tab === "chat" && <ChatPanel />}
    </div>
  );
}
