# frontend

React student portal and admin panel. Built in Phase 6 (see
[PROJECT_PLAN.md](../PROJECT_PLAN.md)); reasoning is in
[DESIGN_NOTES.md](../DESIGN_NOTES.md), summarised in [DESIGN.md](../DESIGN.md).

Vite + React 19 + TypeScript, no UI framework and no state library. Three runtime
dependencies (`react`, `react-dom`, `react-router-dom`) because the app is two
surfaces over a REST API and anything more would be scaffolding without a load to
carry.

```
src/
  api.ts                          the only place this app talks to the backend
  App.tsx                         shell, routing, and the two independent sessions
  pages/Login.tsx                 student (ID only) and admin (shared password)
  pages/Portal.tsx                profile, schedule, history, progress, assistant
  pages/Admin.tsx                 behaviour config, documents, read-only browsers
  components/DegreeProgressPanel.tsx
  components/ChatPanel.tsx
```

## Two things worth reading the code for

**`DegreeProgressPanel.tsx`** is the one that matters. The plan calls "surplus
credits in one category don't offset another" the #1 thing students in the brief
get wrong, so the component refuses to lead with the number that causes the
mistake: the headline is *categories satisfied*, the credit total is captioned as
**not** the graduation test and never drawn as a single bar, surplus credits are
rendered outside the category bar with a note saying where they cannot go, and an
untouched category gets the loudest treatment on the page.

**`api.ts`** holds two tokens under separate keys, student and admin, because
they are separate principals on the backend. No function in that file takes a
student ID — every personal request goes to `/me/*` and the server reads identity
from the session.

## Running it

```bash
docker compose up -d          # http://localhost:5173
```

Or on the host, against a backend already running on :8000:

```bash
npm install
npm run dev
npm run build                 # tsc -b && vite build
```

`VITE_API_BASE` overrides the API origin (default `http://localhost:8000`). The
browser runs on the host, so it reaches the API through the published port — not
through the compose network, where `backend` resolves only from inside another
container.

The dev server is what runs in Docker, matching the backend's `--reload`. A
production image would add a build stage and serve `dist/` behind a static
server.
