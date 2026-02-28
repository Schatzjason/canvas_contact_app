# Canvas Student Interaction Tracker — Project Prompt

## What we're building

A Flask web app for college instructors that shows, per course, when they last
interacted with each student via Canvas LMS — aggregating data from the Canvas
inbox (conversations) and discussion boards. Students are displayed in a table
where the x-axis shows the last 21 days, with an icon in each day cell where
an interaction occurred.

The app is being built in phases:
1. Single instructor (local dev, Canvas hosted at instructure.com)
2. Multi-instructor at CCSF
3. Eventually: any Canvas institution (multi-tenant)

Design all decisions with phase 1 complete and phase 2–3 non-breaking to add.

---

## Tech stack

- **Python / Flask** (app factory pattern with blueprints)
- **PostgreSQL** via Flask-SQLAlchemy
- **Flask-Migrate** (Alembic under the hood) for all schema changes
- **Canvas LMS REST API** — authenticated via Canvas OAuth 2.0
- **python-dotenv** for config
- **No frontend framework** — server-rendered Jinja2 templates with clean,
  minimal CSS (no Bootstrap, no Tailwind). Should look professional enough to
  demo to other instructors but not over-engineered.

---

## Project structure

Open to improvements, but start from this shape:

```
canvas_app/
├── app/
│   ├── __init__.py          # app factory
│   ├── models/
│   │   └── user.py
│   ├── routes/
│   │   ├── auth.py
│   │   └── dashboard.py
│   ├── services/
│   │   └── canvas_client.py
│   └── templates/
│       ├── base.html
│       └── dashboard/
│           ├── index.html
│           └── course.html
├── migrations/              # managed by Flask-Migrate, do not hand-edit
├── .env.example
├── requirements.txt
└── run.py
```

Read all existing source files before making any changes. Extend rather than
rewrite unless there is a clear reason to.

---

## Canvas OAuth 2.0

Open to improvements, but requirements are:

- Institution base URL, client ID, and client secret come from `.env`
- Standard authorization code flow; redirect URI: `http://localhost:5000/auth/callback`
- Store `access_token`, `refresh_token`, and `token_expires_at` per user in Postgres
- Implement token refresh when `token_expires_at` is set and expired
- Canvas Developer Key needs these API scopes at minimum:
  - `url:GET|/api/v1/users/self`
  - `url:GET|/api/v1/courses`
  - `url:GET|/api/v1/courses/:course_id/enrollments`
  - `url:GET|/api/v1/conversations`
  - `url:GET|/api/v1/courses/:course_id/discussion_topics`
  - `url:GET|/api/v1/courses/:course_id/discussion_topics/:topic_id/entries`

---

## Data model & caching strategy

### What lives in Postgres

1. **Users** — OAuth tokens, Canvas user ID, name, email
2. **API response cache** — keyed by endpoint + params, with a TTL
   - Model: `canvas_cache(id, cache_key, response_json, fetched_at, ttl_seconds)`
   - TTL defaults: conversations = 15 min, discussion entries = 15 min, enrollments = 60 min
   - `CanvasClient` checks cache before hitting the API; writes through on miss
   - Cache failures log a warning and fall through to live API — never hard-fail
3. **Interaction log** — every interaction event surfaced from Canvas, persisted
   for historical trending
   - Model: `interaction_event(id, user_id, course_id, student_canvas_id, event_type, occurred_at, source_id)`
   - `event_type`: `conversation` | `discussion_entry` | `discussion_reply`
   - `source_id`: Canvas object ID — used to deduplicate on upsert
   - On each sync, upsert events; compute last interaction per student from
     this table, not from live API data
   - Unique constraint on `(event_type, source_id)` to support upsert target

### What is always fetched live

- Course list (lightweight, always fresh)
- User identity (`/users/self`) at login time only

---

## Core feature: student interaction timeline

**Route:** `GET /course/<course_id>`

**Display:** An HTML table. One row per enrolled student. The x-axis is 21
columns representing the last 21 days (today − 20 through today), with column
headers showing the date. Each cell shows a simple icon or marker if one or
more interactions occurred on that day, and is empty otherwise.

The visual design of the icon/marker is intentionally left open — make a
reasonable first pass and the instructor will iterate.

**Row contents:**
- Student name (leftmost column, links to detail view)
- 21 day columns as described above
- Row background color based on days since last interaction:
  - **Green**: within `STALE_WARN_DAYS` days
  - **Yellow**: between `STALE_WARN_DAYS` and `STALE_ALERT_DAYS` days
  - **Red**: beyond `STALE_ALERT_DAYS` days, or no interaction ever

**Sorting:** Students with no interaction ever appear first. Remaining students
sorted by last interaction date ascending (least recent first).

**Config** (`.env`):
```
STALE_WARN_DAYS=7
STALE_ALERT_DAYS=14
```

Clicking a student name opens a detail view listing all logged interaction
events for that student in that course, newest first.

---

## Canvas API aggregation logic

`last_interaction_per_student()` in `canvas_client.py` aggregates:

1. **Conversations** (`/api/v1/conversations?scope=sent`):
   - Match `participants` IDs against enrolled student IDs
   - Use `last_message_at` or `last_authored_at` as the timestamp

2. **Discussion entries** (`/api/v1/courses/:id/discussion_topics/:id/entries`):
   - Each entry has `user_id` and `created_at`
   - `recent_replies` on each entry also has `user_id` and `created_at`
   - Do **not** recurse into full reply threads for now (TODO)

All Canvas timestamps are ISO 8601 with Z suffix — normalize to UTC-aware
`datetime` objects throughout. Never compare tz-aware and tz-naive datetimes.

---

## Error handling

- Canvas API 4xx/5xx → catch, flash message, do not raise a 500
- Expired/invalid token → redirect to `/auth/login` with flash message
- Cache failure → log warning, fall through to live API

---

## Testing

- `pytest` + `pytest-flask`; separate test DB via `TEST_DATABASE_URL` env var
- Write unit tests for:
  - `last_interaction_per_student()` using fixture JSON (no live Canvas calls)
  - Cache hit/miss logic
  - Staleness threshold classification
- No integration tests against live Canvas in this phase

---

## Implementation order

1. Flask-Migrate setup — replace `db.create_all()` with proper migrations;
   generate initial migration from existing models
2. Cache model + cache layer in `CanvasClient`
3. `InteractionEvent` model + upsert logic (using `insert().on_conflict_do_update()`)
4. Sync service that pulls Canvas data → populates `interaction_event`
5. Course timeline table view with color-coded rows and 21-day x-axis
6. Student detail view (full event history, newest first)
7. Pytest scaffolding with fixtures

---

## Constraints

- One file per concern — no route logic in models, no service logic in routes
- All DB access via SQLAlchemy ORM; raw SQL only for upsert via
  `sqlalchemy.dialects.postgresql.insert().on_conflict_do_update()`
- Flask-Migrate owns all schema changes — never call `db.create_all()` in
  production paths
- `.env` never committed; `.env.example` always kept in sync with all vars
- Inline comments only where non-obvious; no docstrings on simple CRUD methods
