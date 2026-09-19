---
name: route-ledger
description: Keep endpoints.html (the "Route Ledger" reference doc, served live at GET /endpoints) in sync whenever an HTTP route in app/main.py is added, changed, or removed. Use this any time you add an @app.get/post/put/delete/patch handler, change a route's required params or validation, or change what it mutates in the database.
---

# Route Ledger maintenance

`endpoints.html` (repo root) is a hand-maintained, designed reference page
documenting every HTTP route this app exposes — method, purpose, required
input, and exactly how it mutates the database. It's served live at
`GET /endpoints` (see `endpoints_doc` in `app/main.py`, which just reads the
file off disk — same pattern as `/profile-editor/{person}` serving
`profile.html`).

**There is no other source of truth for this doc and no build step.** If you
add, change, or remove a route in `app/main.py`, `endpoints.html` goes stale
the moment you do — update it in the same change, not as a follow-up.

## What counts as a change that requires an update

- A new `@app.get/post/put/delete/patch(...)` handler in `app/main.py`.
- A route's required/optional params changing (new query param, new required
  body field, a validation rule that returns a new status code, etc.).
- A route's database behavior changing — a new table it touches, a mutation
  it didn't used to perform, a side effect like sending a real SMS or
  spending real API money that wasn't there before.
- A route being removed.

Cosmetic-only changes elsewhere (styling of `profile.html`, internal
refactors that don't change a route's contract) don't require touching this
file.

## File structure — how to actually edit it

Routes appear as `<div class="route" id="...">` blocks inside `.wrap`, in
the **same order they're declared in `app/main.py`** — that order is real
information the doc encodes (`app/main.py`'s own route order), not decoration,
so don't reorder for convenience. Each block has:

- `<div class="route-index">NN</div>` — two-digit position in file order.
  Inserting or removing a route means renumbering every block after it.
- `.route-head`: a plain `.method` badge (`GET`/`POST`/etc., unstyled by
  color — method isn't the semantic signal here), the `.path` in monospace
  (wrap a path parameter in `<span class="param">{name}</span>`), and one
  `.sev` badge, right-aligned, using exactly one of:
  - `sev read` / label "no mutation" — no DB write and no external
    real-world side effect.
  - `sev write` / label "writes db" — mutates Postgres, no external
    real-world side effect (no real SMS, no real billed API call).
  - `sev real` / label naming the specific effect (e.g. "real sms",
    "real api spend", "real sms possible") — anything that sends a real
    message or spends real money, regardless of whether it also writes
    to the DB.
- `<p class="purpose">` — one or two sentences, same voice as the existing
  entries (what it's for, who/what calls it).
- `<h3 class="label">Required input</h3>` followed by either
  `<p class="none-note">None.</p>` or a `<ul class="params">` — one `<li>`
  per param as `<code>name</code> <span class="tag">location, required|optional</span>`
  followed by a `<br>` and a one-line description, including the exact
  validation rule and status code on failure if there is one.
- `<h3 class="label">Database</h3>` followed by
  `<div class="mutation-block">` (add class `read` or `real` to match the
  `.sev` badge above — plain `.mutation-block` with no extra class is the
  "writes db" styling). Describe every table touched and exactly what
  changes. Use a nested `<div class="note">` (or `.note.mild` for a
  lower-severity caveat) for a single flagged caveat, or a `<ul class="gaps">`
  for a bulleted list of non-obvious behaviors (see the `/sms` entry for the
  pattern) — only when there's something genuinely worth flagging, not for
  every route.

Also update, when the count changes:
- The intro line's route count ("There are N total: ...") in `.lede`.
- The footer's "N routes" text.
- The `nav.jump` anchor list — one `<a href="#id">` per route, in the same
  order, matching each block's `id`.

## Two things this skill can't do for you

1. **The Claude Artifact copy** ("Route Ledger", published separately for a
   shareable preview) is a static snapshot on claude.ai and has no
   connection to this file — editing `endpoints.html` does not update it.
   If you're a coding agent without Artifact-publish access, just tell the
   user the artifact is now stale and ask if they want it republished from
   the updated `endpoints.html` content.
2. **`tests/test_main.py::test_endpoints_doc_serves_html`** asserts specific
   strings appear in the served page (currently `"Route Ledger"` and
   `"/ops/research/run"`). If you remove or rename the route that second
   assertion checks for, update the test alongside the doc — run
   `pytest tests/test_main.py -q` after editing to confirm nothing broke.
