# aaron_taylor_events_app

A weekly automation that researches upcoming Bay Area events, texts two
people (Aaron and Tay) a ballot, and adds mutually-approved events to a
shared Google Calendar. Built from `sfeventsworkflowspec.md`.

- Full build notes, decisions, and what's been validated vs. not:
  [`docs/BUILD_NOTES.md`](docs/BUILD_NOTES.md)
- DB schema: [`sql/schema.sql`](sql/schema.sql)
- Config keys: [`.env.example`](.env.example)

## Quick start

```
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
pytest
ENABLE_SCHEDULER=false uvicorn app.main:app --reload
```

`privacy.html` and `terms.html` are the carrier-facing pages for A2P 10DLC
SMS registration.
