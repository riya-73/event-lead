from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

load_dotenv()

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # SQLite-only local installs still work without psycopg.
    psycopg = None
    dict_row = None

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(
    os.environ.get(
        "DATABASE_PATH",
        "/tmp/leads.db" if os.environ.get("VERCEL") else BASE_DIR / "data" / "leads.db",
    )
)

STATUSES = ("NEW", "CONTACTED", "REPLIED", "MEETING_BOOKED", "CLOSED")
STATUS_LABELS = {
    "NEW": "New",
    "CONTACTED": "Contacted",
    "REPLIED": "Replied",
    "MEETING_BOOKED": "Meeting booked",
    "CLOSED": "Closed",
}

FIELD_ORDER = ("name", "company", "email", "event", "notes", "status", "aiSummary", "emailDraft")
LEAD_FIELDS = set(FIELD_ORDER)
REQUIRED_FIELDS = {"name", "company", "email", "event"}
NULLABLE_FIELDS = {"aiSummary", "emailDraft"}
DEFAULTS = {"notes": "", "status": "NEW", "aiSummary": None, "emailDraft": None}
COLUMN_MAP = {"aiSummary": "ai_summary", "emailDraft": "email_draft"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = Flask(__name__)
app.json.sort_keys = False  # Flask >= 2.3 (JSON_SORT_KEYS config was removed)

SAMPLE_LEADS = [
    ("Maya Chen", "Northstar Analytics", "maya.chen@northstaranalytics.com", "SaaStr Annual 2026", "VP of Growth. Exploring attribution tools for their move upmarket. Send the enterprise case study and ask about their current stack.", "NEW"),
    ("Oliver Grant", "Monarch Health", "oliver.grant@monarchhealth.io", "HLTH 2026", "Director of Partnerships. Wants to streamline partner onboarding across 30 clinics. Offer a walkthrough with their operations lead.", "CONTACTED"),
    ("Sofia Alvarez", "Form & Function", "sofia@formandfunction.co", "Web Summit 2026", "Founder. Asked about workflow automation and whether it works with Linear. Actively looking to save engineering time.", "REPLIED"),
    ("Ethan Brooks", "Aperture Cloud", "ethan.brooks@aperturecloud.com", "SaaStr Annual 2026", "Head of RevOps. Consolidating three sales tools. Needs SSO, Salesforce sync, and a clear migration plan.", "MEETING_BOOKED"),
    ("Priya Nair", "Greenhouse Labs", "priya.nair@greenhouselabs.ai", "Climate Tech Connect", "Product lead. Interested in the API and data export. Keep the door open and send a useful API resource.", "NEW"),
    ("Lucas Moreau", "Atelier Commerce", "lucas@ateliercommerce.fr", "Shoptalk Europe", "COO. Looking at reducing abandoned checkouts across their EU storefronts. GDPR and multilingual support are must-haves.", "CLOSED"),
    ("Amara Okafor", "Fieldnote Systems", "amara@fieldnotesystems.com", "Web Summit 2026", "Customer Experience lead. Evaluating options with her support manager. Interested in the shared inbox demo.", "CONTACTED"),
    ("Noah Kim", "Relay Financial", "noah.kim@relayfinancial.com", "Fintech Devcon", "Engineering manager. Wants to understand SOC 2 coverage and data retention options. Send security documentation.", "REPLIED"),
    ("Isabella Romano", "Studio Meridian", "isabella@studiomeridian.design", "Config 2026", "Design systems director. Building a shared component library. Connect her with a solutions engineer.", "MEETING_BOOKED"),
]

SCHEMA = """CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    company TEXT NOT NULL,
    email TEXT NOT NULL,
    event TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    ai_summary TEXT,
    email_draft TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)"""

INSERT_SQL = (
    "INSERT INTO leads (id, name, company, email, event, notes, status, ai_summary, "
    "email_draft, created_at, updated_at) VALUES (" + ", ".join(["?"] * 11) + ")"
)


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #
def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def postgres_enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


def run(conn: Any, sql: str, params: tuple | list = ()) -> Any:
    """Execute SQL written with '?' placeholders on either backend."""
    if postgres_enabled():
        sql = sql.replace("?", "%s")
    return conn.execute(sql, params)


def connect() -> Any:
    if postgres_enabled():
        if psycopg is None:
            raise RuntimeError("DATABASE_URL is set but psycopg is not installed.")
        return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


_init_lock = threading.Lock()
_initialized = False


def init_db() -> None:
    """Create the table and seed sample data once per process, not once per request."""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        conn = connect()
        try:
            run(conn, SCHEMA)
            count = run(conn, "SELECT COUNT(*) AS row_count FROM leads").fetchone()["row_count"]
            if count == 0:
                stamp = now()
                for lead in SAMPLE_LEADS:
                    run(conn, INSERT_SQL, (str(uuid.uuid4()), *lead, None, None, stamp, stamp))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        _initialized = True


@contextmanager
def get_db() -> Iterator[Any]:
    """Yield a connection; always commit/rollback and close it."""
    init_db()
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def serialize(row: Any) -> dict[str, Any]:
    def iso(value: Any) -> str:
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    return {
        "id": row["id"],
        "name": row["name"],
        "company": row["company"],
        "email": row["email"],
        "event": row["event"],
        "notes": row["notes"],
        "status": row["status"],
        "aiSummary": row["ai_summary"],
        "emailDraft": row["email_draft"],
        "createdAt": iso(row["created_at"]),
        "updatedAt": iso(row["updated_at"]),
    }


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate(payload: Any, partial: bool = False) -> tuple[dict[str, Any], str | None]:
    if not isinstance(payload, dict):
        return {}, "Request body must be a JSON object."

    unknown = set(payload) - LEAD_FIELDS
    if unknown:
        return {}, f"Unknown fields: {', '.join(sorted(unknown))}"

    clean: dict[str, Any] = {}
    for field in FIELD_ORDER:
        if field in payload:
            raw = payload[field]
        elif partial:
            continue
        else:
            raw = DEFAULTS.get(field)

        if field in NULLABLE_FIELDS:
            value = None if raw is None else str(raw).strip()
        else:
            value = "" if raw is None else str(raw).strip()  # avoids the string "None"

        if field in REQUIRED_FIELDS and not value:
            return {}, f"{field.title()} is required."
        if field == "email" and not EMAIL_RE.match(value):
            return {}, "Enter a valid email address."
        if field == "status" and value not in STATUSES:
            return {}, "Choose a valid follow-up status."
        clean[field] = value
    return clean, None


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def query_leads(q: str = "", event: str = "", status: str = "") -> list[dict[str, Any]]:
    sql = "SELECT * FROM leads WHERE 1=1"
    args: list[str] = []
    if q:
        pattern = f"%{escape_like(q.lower())}%"
        sql += (
            " AND (lower(name) LIKE ? ESCAPE '\\' OR lower(company) LIKE ? ESCAPE '\\'"
            " OR lower(email) LIKE ? ESCAPE '\\')"
        )
        args += [pattern] * 3
    if event:
        sql += " AND event = ?"
        args.append(event)
    if status in STATUSES:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY created_at DESC, id"
    with get_db() as conn:
        return [serialize(row) for row in run(conn, sql, args).fetchall()]


def list_events() -> list[str]:
    with get_db() as conn:
        rows = run(conn, "SELECT DISTINCT event FROM leads ORDER BY event").fetchall()
    return [row["event"] for row in rows]


def get_lead(lead_id: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = run(conn, "SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return serialize(row) if row else None


def filters_from_request() -> dict[str, str]:
    return {
        "q": request.args.get("q", "").strip(),
        "event": request.args.get("event", "").strip(),
        "status": request.args.get("status", "").strip(),
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/")
def home():
    return render_template(
        "index.html",
        statuses=STATUSES,
        status_labels=STATUS_LABELS,
        database_mode="Neon PostgreSQL" if postgres_enabled() else "local SQLite",
    )


@app.get("/api/leads")
def list_api():
    return jsonify(
        {
            "leads": query_leads(**filters_from_request()),
            "events": list_events(),  # all events, not just those in the filtered result
            "database": "postgres" if postgres_enabled() else "sqlite",
        }
    )


@app.post("/api/leads")
def create_api():
    data, error = validate(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error}), 400

    lead_id, stamp = str(uuid.uuid4()), now()
    values = (
        lead_id, data["name"], data["company"], data["email"], data["event"],
        data["notes"], data["status"], data["aiSummary"], data["emailDraft"], stamp, stamp,
    )
    with get_db() as conn:
        run(conn, INSERT_SQL, values)
        row = run(conn, "SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return jsonify({"lead": serialize(row)}), 201


@app.patch("/api/leads/<lead_id>")
def update_api(lead_id: str):
    data, error = validate(request.get_json(silent=True) or {}, partial=True)
    if error:
        return jsonify({"error": error}), 400
    if not data:
        return jsonify({"error": "Provide at least one lead field."}), 400

    assignments = ", ".join(f"{COLUMN_MAP.get(key, key)} = ?" for key in data)
    values = [*data.values(), now(), lead_id]
    with get_db() as conn:
        run(conn, f"UPDATE leads SET {assignments}, updated_at = ? WHERE id = ?", values)
        row = run(conn, "SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not row:
        return jsonify({"error": "Lead not found."}), 404
    return jsonify({"lead": serialize(row)})


@app.delete("/api/leads/<lead_id>")
def delete_api(lead_id: str):
    with get_db() as conn:
        deleted = run(conn, "DELETE FROM leads WHERE id = ?", (lead_id,)).rowcount
    if not deleted:
        return jsonify({"error": "Lead not found."}), 404
    return jsonify({"success": True})


@app.post("/api/leads/<lead_id>/ai")
def ai_api(lead_id: str):
    payload = request.get_json(silent=True)
    action = payload.get("action") if isinstance(payload, dict) else None
    if action not in {"summarize", "draft"}:
        return jsonify({"error": "Choose summarize or draft."}), 400

    lead = get_lead(lead_id)
    if not lead:
        return jsonify({"error": "Lead not found."}), 404

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({"error": "Gemini is not configured. Add GEMINI_API_KEY to the server environment."}), 503

    task = (
        "Summarize the conversation notes in 2 concise bullet points and identify the next step."
        if action == "summarize"
        else "Write a warm, concise follow-up email with a subject line and a clear next step. Do not invent facts."
    )
    prompt = (
        f"You are a thoughtful B2B event follow-up assistant. {task}\n"
        f"Person: {lead['name']}\nCompany: {lead['company']}\n"
        f"Event: {lead['event']}\nNotes: {lead['notes']}"
    )
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    req = Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},  # key kept out of the URL/logs
        method="POST",
    )
    try:
        with urlopen(req, timeout=25) as response:
            result = json.loads(response.read().decode())
        content = result["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        # OSError covers HTTPError, URLError and timeouts; ValueError covers bad JSON.
        app.logger.warning("Gemini request failed: %s", type(exc).__name__)
        return jsonify({"error": "Gemini could not create a response right now."}), 502
    return jsonify({"action": action, "content": content})


def csv_safe(value: Any) -> str:
    """Prevent spreadsheet formula injection when the CSV is opened in Excel/Sheets."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@app.get("/export.csv")
def export_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Name", "Company", "Email", "Event", "Follow-up status", "Notes"])
    for lead in query_leads(**filters_from_request()):
        writer.writerow(
            [
                csv_safe(lead["name"]),
                csv_safe(lead["company"]),
                csv_safe(lead["email"]),
                csv_safe(lead["event"]),
                STATUS_LABELS.get(lead["status"], lead["status"]),
                csv_safe(lead["notes"]),
            ]
        )
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        mimetype="text/csv",
        as_attachment=True,
        download_name="gatherline-leads.csv",
    )


@app.errorhandler(Exception)
def handle_unexpected_error(error: Exception):
    # Normal HTTP errors (404, 405, ...) keep their own status code instead of becoming 500s.
    if isinstance(error, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"error": error.description}), error.code
        return error
    app.logger.exception("Unhandled application error: %s", error)
    if request.path.startswith("/api/"):
        return jsonify({"error": "The API could not complete this request. Check the server logs."}), 500
    return "Internal Server Error", 500


if __name__ == "__main__":
    init_db()
    debug = os.environ.get("FLASK_DEBUG") == "1"
    # Never expose the Werkzeug debugger on all network interfaces.
    app.run(host="127.0.0.1" if debug else "0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=debug)
