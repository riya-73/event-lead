# Gatherline · Python Event Lead Manager

A focused follow-up workspace for event leads, rewritten from the original TypeScript/Next.js implementation as a self-contained **Python Flask + SQLite** application.

## Included

- Working **Leads** and **Events** navigation (there is no Overview tab).
- Event rollups with contacts, follow-up counts, meetings booked, and closed leads.
- Working “View leads” event filtering.
- Working Jordan Davis three-dot menu with dark mode, compact view, CSV export, and keyboard shortcuts.
- Persistent lead create, edit, delete, search, event/status filters, and status updates.
- Responsive layout with light/dark themes stored in browser local storage.

## Run locally

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open <http://localhost:5000>. The app creates `data/leads.db` automatically and seeds nine sample leads on first run. Set `PORT` or `DATABASE_PATH` to override the defaults.

## API

- `GET /api/leads?q=&event=&status=` — list and filter leads
- `POST /api/leads` — create a lead
- `PATCH /api/leads/<id>` — update a lead or status
- `DELETE /api/leads/<id>` — delete a lead
- `GET /export.csv` — download the currently filtered leads as CSV
