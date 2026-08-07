/**
 * App shell and routing.
 *
 * Two surfaces, two routes, two independent sessions: signing into the admin
 * panel does not sign you into a student portal, and vice versa. The `role`
 * state is derived from which token is present rather than kept separately, so
 * there is no way for the UI to believe it is signed in when it holds no token.
 */

import { useCallback, useState } from "react";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { getToken } from "./api";
import { Admin } from "./pages/Admin";
import { AdminLogin, StudentLogin } from "./pages/Login";
import { Portal } from "./pages/Portal";

function StudentRoute() {
  // A counter, not a boolean: it forces a re-read of localStorage after sign-in
  // or sign-out without duplicating the token into React state, where the two
  // could disagree.
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((n) => n + 1), []);
  const signedIn = getToken("student") !== null;

  return signedIn ? (
    <Portal key={revision} onSignOut={refresh} />
  ) : (
    <StudentLogin onSignedIn={refresh} />
  );
}

function AdminRoute() {
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((n) => n + 1), []);
  const signedIn = getToken("admin") !== null;

  return signedIn ? (
    <Admin key={revision} onSignOut={refresh} />
  ) : (
    <AdminLogin onSignedIn={refresh} />
  );
}

export default function App() {
  const { pathname } = useLocation();

  return (
    <div className="app">
      <header className="app__bar">
        <div className="app__brand">
          <span className="app__mark">EU</span>
          <div>
            <strong>Eurisko University</strong>
            <span className="muted"> Faculty of Engineering</span>
          </div>
        </div>
        <nav className="app__nav">
          <Link className={pathname === "/" ? "active" : ""} to="/">
            Student portal
          </Link>
          <Link className={pathname.startsWith("/admin") ? "active" : ""} to="/admin">
            Administration
          </Link>
        </nav>
      </header>

      <main className="app__main">
        <Routes>
          <Route path="/" element={<StudentRoute />} />
          <Route path="/admin" element={<AdminRoute />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>

      <footer className="app__foot muted">
        Answers about policy come from the Course Catalogue and Student Handbook
        and are cited. Personal answers are scoped to your own record.
      </footer>
    </div>
  );
}
