# By Module — design spec

A new section on the student page (`app/templates/dashboard/student.html`) that shows a student's full-course progress as a vertical table: one row per module, columns for Messages and each assignment group. Sits between the Notes / Check-back section and the existing 21-day timeline. The 21-day timeline is renamed from "Last 21 Days" to "Recent Activity" and stays as-is.

The two views answer different questions: the new table answers "how is this student doing across the whole course," the timeline answers "what has happened lately."

## Visual reference

`docs/mockups/by_module_table.html` — open in a browser. The implementation should match its layout, spacing, typography, and icon vocabulary.

## Layout

CSS grid:

```
grid-template-columns: 160px repeat(N, minmax(70px, 1fr));
```

where `N = 1 + len(assignment_groups)` (Messages plus one column per assignment group).

- Module column: fixed 160px, right-aligned text.
- Data columns: equal-width, share the remainder.
- No gridlines, no row separators. Whitespace is the only structure.
- Header row: column titles in IBM Plex Mono, 10px, uppercase, `letter-spacing: 0.08em`, `var(--text-tertiary)`. Centered over each data column.
- Data rows: `min-height: 56px`, `padding: 12px 4px`. Icons centered.
- Module cell: name + number on top (13px sans, weight 500), short start date below (10px mono, `var(--text-tertiary)`). Format the date as `Mon DD` (e.g. `Apr 21`).

## Sort order

`SELECT … FROM course_module WHERE course_id = ? ORDER BY position DESC` — highest position at the top, Module 0 at the bottom. Empty modules (no events in any column) still render: just the module label, all data cells blank.

## Columns

1. **Messages** (always present, leftmost data column).
2. **One column per assignment group**, in the order returned by Canvas (`get_assignment_groups`, which orders by group `position`).

If the course has no assignment groups, render Messages plus a single placeholder column titled "—" (rare; only happens for unusual course setups).

## Cell content rules

### Messages column

Includes `InteractionEvent` rows where `event_type` is one of `conversation`, `group_conversation`, or `student_message`, and `course_module.start_date <= occurred_at::date <= course_module.end_date`.

Icon vocabulary (reuse from existing student page):

- `conversation` → `icon-msg` (solid blue square).
- `group_conversation` → `icon-grp` (solid green square).
- `student_message` → `icon-msg.student` (dashed blue square).

### Assignment-group columns

Includes `InteractionEvent` rows where `event_type = 'submission'` AND the submission's assignment belongs to the group AND the assignment's `due_at` falls within the module's date range.

Icon vocabulary:

- On-time submission → `icon-sub` (solid gray document, full opacity).
- Late submission → `icon-sub.late` (same glyph, `opacity: 0.55`).
- Missing / no submission → blank cell. Empty space communicates absence.

Determine late/on-time by checking the cached submission's `late` field (Canvas returns it on the submission object). Look up the submission via `canvas_cache` using the `source_id` on the `InteractionEvent` row, exactly the same pattern dashboard.py already uses for hover text (see lines 396–515 of `app/routes/dashboard.py`).

### Discussions

**Drop ungraded discussion entries from this view.** They remain in the existing 21-day timeline. Only graded discussions, which belong to an assignment group via Canvas's normal mechanism, appear in the table. They render as standard `icon-sub` glyphs in their group's column — no special treatment.

## Stacking multiple events in one cell

When a cell has more than one event, stack icons with the existing app's pattern: front icon top-left at `(0, 0)`, subsequent icons offset 6px down and 6px right (like the `icon-stack` already in `student.html`, just compressed).

- 1 event → single icon.
- 2 events → `icon-stack.size-2` (38×38), front + back.
- 3 events → `icon-stack.size-3` (44×44), front + mid + back.
- 4+ events → render the first 3 stacked plus a `+N` indicator to the right (mono, 11px, `var(--text-secondary)`). E.g. 5 events show 3 icons + "+2".

When stacking mixed types (e.g. one instructor and one student message), put the most-recent on the front (top-left, full visibility) and older ones behind.

## Empty-state behavior

- Empty cell → render the cell with no children. Just whitespace.
- Empty row (module with zero matching events across all columns) → render the module label cell, all data cells empty.
- Course with no `course_module` rows yet → render the section title plus a single placeholder line: "No modules synced for this course yet." Style it like `notes-status` (`font-family: var(--mono); font-size: 11px; color: var(--text-tertiary); padding: 12px 0`).

## Click behavior

Reuse the existing day-drawer (`#detail-drawer` in `student.html`). When a non-empty cell is clicked, open the drawer with content scoped to that cell:

- Header: `Module N: Name — Group Name` (or `Module N: Name — Messages`).
- Body: one section per event, using the existing `.drawer-section-label` and `.drawer-section-text` styles. Reuse the same SQL extraction patterns from `dashboard.py` (lines 638–681) to pull message bodies, submission assignment names, etc.

The drawer's existing close behavior, escape-key, and click-outside handlers stay as they are.

## Section composition

Insert the new section in `student.html` between the Notes section (which ends at the closing `</div>` of `<div class="cb-row">…</div>`) and the existing "Last 21 Days" section. Rename the existing section title from `Last 21 Days` to `Recent Activity` — the spine + dot timeline below it stays unchanged.

```
{# existing: pinned post #}
{# existing: notes / check-back #}
{# NEW: by-module table #}
{# existing: recent activity (renamed from Last 21 Days) #}
```

## Code placement

- **Template:** new section in `app/templates/dashboard/student.html`. Inline the CSS in the existing `{% block extra_styles %}` to keep the template self-contained, matching the page's current pattern.
- **Route:** extend `dashboard.student(course_id, student_id)` in `app/routes/dashboard.py`. The route already does heavy data assembly; **the new logic should be extracted to a helper service** to avoid making the route worse. Suggested location: `app/services/by_module.py` with a function like:

  ```python
  def build_by_module_view(course_id: int, student_id: int, tz: ZoneInfo) -> dict:
      """Returns {
          'modules': [{'id', 'name', 'position', 'start_date', ...}],
          'columns': [{'key': 'messages' | <group_id>, 'label': str}],
          'cells': {(module_id, column_key): [{'event_type', 'source_id', 'late', ...}]},
          'drawer_payload': {(module_id, column_key): {'header': str, 'sections': [...]}},
      }"""
  ```

  The route passes the result into the template under a single key (e.g. `by_module=...`) and the template iterates.

- **Tests:** add `tests/test_by_module.py` with coverage for: empty course (no modules), module with no events, module with mixed event types, late submission, +N overflow, message attribution by date range. Use the existing fixture pattern in `tests/test_routes.py`.

## Data dependencies

The implementation must read from these sources:

- `course_module` table (start_date, end_date, name, position, canvas_module_id).
- `interaction_event` table, filtered by `(course_id, student_canvas_id)`.
- `canvas_cache` JSONB rows for:
  - `/api/v1/courses/{course_id}/assignment_groups` — gives column list and group IDs.
  - `/api/v1/courses/{course_id}/assignments` — gives `assignment_group_id` and `due_at` for each assignment, indexed by assignment id.
  - `/api/v1/courses/{course_id}/assignments/{aid}/submissions` — to look up `late` on a submission by source_id.
  - `/api/v1/conversations` (sent + inbox scopes) — already used for message bodies in the day-drawer SQL.

If any required cache row is missing, render the cell as if no event existed and log at warning level — don't crash the page.

## Out of scope (later PRs)

- Submission state visualization beyond on-time / late / blank (no separate icon for excused, missing-but-overdue, graded vs ungraded).
- Hover tooltips for icons (the drawer is the detail surface).
- Sticky header row when scrolling.
- A separate column for ungraded discussions.
- Mobile/narrow viewport adjustments — assume ≥980px viewport for now.

## Acceptance criteria

1. New section titled "By Module" appears between Notes and Recent Activity on the student page.
2. Existing "Last 21 Days" section is renamed to "Recent Activity"; its body is unchanged.
3. Rows are modules sorted by `position` descending; Module 0 (lowest position) at bottom.
4. Modules with zero matching events still render as empty rows with just the label.
5. Course with no `course_module` rows shows the documented placeholder, not a crash.
6. Columns are `Messages` plus one column per assignment group from Canvas, in `position` order.
7. Messages cell contains the documented icons by message direction; submissions cell shows `icon-sub` (full opacity for on-time, `opacity: 0.55` for late, blank for missing).
8. Multi-event cells stack up to 3 icons with `(0,0)` / `(6,6)` / `(12,12)` offsets and append `+N` for additional events.
9. Clicking a non-empty cell opens the existing `#detail-drawer` with the cell's events; clicking again or pressing Escape closes it.
10. Page still renders if the Canvas cache is cold or partially populated (warn-log, don't crash).
11. Tests in `tests/test_by_module.py` pass: empty-course, empty-module, mixed-events, late-submission, overflow stacking, message-attribution-by-date.
12. No new N+1 queries: assignment-group lookup and submission late-flag lookup must be batched per page render.
