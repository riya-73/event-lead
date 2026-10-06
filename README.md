# Gatherline · AI Event Lead Manager

A full-stack event lead manager for the Even8 assignment. It uses **Flask**, **Neon PostgreSQL in production**, **SQLite for local development**, and **Gemini for server-side summaries and follow-up drafts**.

## What works

- Add, edit, delete, search, and filter leads.
- Store name, company, email, event, notes, and follow-up status.
- Leads persist in Neon PostgreSQL when `DATABASE_URL` is configured.
- Nine sample leads across seven events are seeded automatically when the database is empty.
- Gemini actions summarize notes and draft follow-up messages without exposing the API key to the browser.
- Leads and Events tabs, dark mode, compact view, CSV export, and keyboard shortcuts.
- No Overview tab.

## Local setup

```bash
python -m venv .venv
. .venv/bin/activate                 # PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open <http://localhost:5000>. Without `DATABASE_URL`, the app uses `data/leads.db`. With `DATABASE_URL`, it uses Neon and creates/seeds the schema automatically.

## Environment variables

```env
# Neon pooled URL used by normal app requests
DATABASE_URL=postgresql://USER:PASSWORD@HOST-pooler.REGION.aws.neon.tech/DB?sslmode=require

# Direct URL for one-off migrations/admin scripts
DIRECT_URL=postgresql://USER:PASSWORD@HOST.REGION.aws.neon.tech/DB?sslmode=require

# Server-side Gemini configuration
AI_PROVIDER=gemini
GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-2.5-flash
```

In Vercel, set `DATABASE_URL` for **Production** to the pooled Neon URL. Do not use `DATABASE_PATH` in production. The app automatically seeds the nine sample leads on the first successful request when Neon is empty.

## Vercel deployment

Import this repository into Vercel. The project includes `api/index.py` and `vercel.json`. Use **Other** as the framework preset and leave build/output commands blank. Add the environment variables above, then redeploy.

Check the deployment with:

```text
https://YOUR-VERCEL-DOMAIN.vercel.app/api/health
```

A healthy response includes `"status":"ok"` and `"database":"postgres"`.

## API

- `GET /api/health` — runtime and database health check
- `GET /api/leads?q=&event=&status=` — list and filter leads
- `POST /api/leads` — create a lead
- `PATCH /api/leads/<id>` — update a lead or saved AI text
- `DELETE /api/leads/<id>` — delete a lead
- `POST /api/leads/<id>/ai` with `{ "action": "summarize" | "draft" }` — call Gemini
- `GET /export.csv` — download filtered leads as CSV

## Tests

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```
