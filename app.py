from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, jsonify, render_template, request, send_file
from dotenv import load_dotenv

load_dotenv()

try:
    from psycopg.rows import dict_row
    import psycopg
except ImportError:  # SQLite-only local installs still work without psycopg.
    psycopg = None
    dict_row = None

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DATABASE_PATH", "/tmp/leads.db" if os.environ.get("VERCEL") else BASE_DIR / "data" / "leads.db"))
STATUSES = ("NEW", "CONTACTED", "REPLIED", "MEETING_BOOKED", "CLOSED")
STATUS_LABELS = {"NEW": "New", "CONTACTED": "Contacted", "REPLIED": "Replied", "MEETING_BOOKED": "Meeting booked", "CLOSED": "Closed"}
LEAD_FIELDS = {"name", "company", "email", "event", "notes", "status", "aiSummary", "emailDraft"}
COLUMN_MAP = {"aiSummary": "ai_summary", "emailDraft": "email_draft"}

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

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


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def postgres_enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


def db() -> Any:
    if postgres_enabled():
        if psycopg is None:
            raise RuntimeError("DATABASE_URL is set but psycopg is not installed.")
        conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
        conn.execute("""CREATE TABLE IF NOT EXISTS leads (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, company TEXT NOT NULL, email TEXT NOT NULL,
            event TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
            ai_summary TEXT, email_draft TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        seed_if_empty(conn)
        conn.commit()
        return conn
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS leads (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, company TEXT NOT NULL, email TEXT NOT NULL,
        event TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
        ai_summary TEXT, email_draft TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    seed_if_empty(conn)
    conn.commit()
    return conn


def seed_if_empty(conn: Any) -> None:
    if conn.execute("SELECT COUNT(*) AS row_count FROM leads").fetchone()["row_count"] == 0:
        stamp = now()
        insert_sql = "INSERT INTO leads (id,name,company,email,event,notes,status,ai_summary,email_draft,created_at,updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)" if not postgres_enabled() else "INSERT INTO leads (id,name,company,email,event,notes,status,ai_summary,email_draft,created_at,updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        seed_rows = [(str(uuid.uuid4()), *lead, None, None, stamp, stamp) for lead in SAMPLE_LEADS]
        if postgres_enabled():
            with conn.cursor() as cursor:
                cursor.executemany(insert_sql, seed_rows)
        else:
            conn.executemany(insert_sql, seed_rows)


def placeholder() -> str:
    return "%s" if postgres_enabled() else "?"


def serialize(row: Any) -> dict[str, Any]:
    def value(key: str) -> Any:
        return row[key]
    def iso(value: Any) -> str:
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    return {"id": value("id"), "name": value("name"), "company": value("company"), "email": value("email"), "event": value("event"), "notes": value("notes"), "status": value("status"), "aiSummary": value("ai_summary"), "emailDraft": value("email_draft"), "createdAt": iso(value("created_at")), "updatedAt": iso(value("updated_at"))}


def validate(payload: dict[str, Any], partial: bool = False) -> tuple[dict[str, Any], str | None]:
    unknown = set(payload) - LEAD_FIELDS
    if unknown:
        return {}, f"Unknown fields: {', '.join(sorted(unknown))}"
    clean: dict[str, Any] = {}
    for field in LEAD_FIELDS:
        if field not in payload:
            if partial:
                continue
            if field == "notes": clean[field] = ""; continue
            if field == "status": clean[field] = "NEW"; continue
            if field in {"aiSummary", "emailDraft"}: clean[field] = None; continue
        value = payload.get(field)
        value = None if value is None and field in {"aiSummary", "emailDraft"} else str(value).strip()
        if field in {"name", "company", "email", "event"} and not value:
            return {}, f"{field.title()} is required."
        if field == "email" and ("@" not in value or "." not in value.split("@")[-1]):
            return {}, "Enter a valid email address."
        if field == "status" and value not in STATUSES:
            return {}, "Choose a valid follow-up status."
        clean[field] = value
    return clean, None


def query_leads() -> list[dict[str, Any]]:
    conn = db(); p = placeholder(); sql = "SELECT * FROM leads WHERE 1=1"; args: list[str] = []
    q, event, status = request.args.get("q", "").strip(), request.args.get("event", "").strip(), request.args.get("status", "").strip()
    if q:
        sql += f" AND (lower(name) LIKE {p} OR lower(company) LIKE {p} OR lower(email) LIKE {p})"; args += [f"%{q.lower()}%"] * 3
    if event: sql += f" AND event = {p}"; args.append(event)
    if status in STATUSES: sql += f" AND status = {p}"; args.append(status)
    rows = [serialize(row) for row in conn.execute(sql + " ORDER BY created_at DESC", args).fetchall()]; conn.close(); return rows


def get_lead(lead_id: str) -> dict[str, Any] | None:
    conn = db(); row = conn.execute(f"SELECT * FROM leads WHERE id = {placeholder()}", (lead_id,)).fetchone(); conn.close()
    return serialize(row) if row else None


@app.get("/")
def home():
    return render_template("index.html", statuses=STATUSES, status_labels=STATUS_LABELS, database_mode="Neon PostgreSQL" if postgres_enabled() else "local SQLite")


@app.get("/api/leads")
def list_api():
    leads = query_leads()
    return jsonify({"leads": leads, "events": sorted({lead["event"] for lead in leads}), "database": "postgres" if postgres_enabled() else "sqlite"})


@app.post("/api/leads")
def create_api():
    data, error = validate(request.get_json(silent=True) or {})
    if error: return jsonify({"error": error}), 400
    lead_id, stamp, p = str(uuid.uuid4()), now(), placeholder(); conn = db()
    sql = f"INSERT INTO leads (id,name,company,email,event,notes,status,ai_summary,email_draft,created_at,updated_at) VALUES ({','.join([p] * 11)})"
    values = (lead_id, data["name"], data["company"], data["email"], data["event"], data["notes"], data["status"], data["aiSummary"], data["emailDraft"], stamp, stamp)
    if postgres_enabled():
        with conn.cursor() as cursor:
            cursor.execute(sql, values)
            cursor.execute(f"SELECT * FROM leads WHERE id = {p}", (lead_id,))
            row = cursor.fetchone()
    else:
        conn.execute(sql, values)
        row = conn.execute(f"SELECT * FROM leads WHERE id = {p}", (lead_id,)).fetchone()
    conn.commit(); conn.close(); return jsonify({"lead": serialize(row)}), 201


@app.patch("/api/leads/<lead_id>")
def update_api(lead_id: str):
    data, error = validate(request.get_json(silent=True) or {}, partial=True)
    if error: return jsonify({"error": error}), 400
    if not data: return jsonify({"error": "Provide at least one lead field."}), 400
    p, conn = placeholder(), db(); assignments = ", ".join(f"{COLUMN_MAP.get(key, key)} = {p}" for key in data); values = list(data.values()) + [now(), lead_id]
    sql = f"UPDATE leads SET {assignments}, updated_at = {p} WHERE id = {p}"
    if postgres_enabled():
        with conn.cursor() as cursor:
            cursor.execute(sql, values)
            cursor.execute(f"SELECT * FROM leads WHERE id = {p}", (lead_id,))
            row = cursor.fetchone()
    else:
        conn.execute(sql, values)
        row = conn.execute(f"SELECT * FROM leads WHERE id = {p}", (lead_id,)).fetchone()
    conn.commit(); conn.close()
    return jsonify({"lead": serialize(row)}) if row else (jsonify({"error": "Lead not found."}), 404)


@app.delete("/api/leads/<lead_id>")
def delete_api(lead_id: str):
    conn = db()
    if postgres_enabled():
        with conn.cursor() as cursor:
            cursor.execute(f"DELETE FROM leads WHERE id = {placeholder()}", (lead_id,))
            deleted = cursor.rowcount
    else:
        cursor = conn.execute(f"DELETE FROM leads WHERE id = {placeholder()}", (lead_id,))
        deleted = cursor.rowcount
    conn.commit(); conn.close()
    return jsonify({"success": True}) if deleted else (jsonify({"error": "Lead not found."}), 404)


@app.post("/api/leads/<lead_id>/ai")
def ai_api(lead_id: str):
    lead = get_lead(lead_id)
    if not lead: return jsonify({"error": "Lead not found."}), 404
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key: return jsonify({"error": "Gemini is not configured. Add GEMINI_API_KEY to the server environment."}), 503
    action = (request.get_json(silent=True) or {}).get("action")
    if action not in {"summarize", "draft"}: return jsonify({"error": "Choose summarize or draft."}), 400
    task = "Summarize the conversation notes in 2 concise bullet points and identify the next step." if action == "summarize" else "Write a warm, concise follow-up email with a subject line and a clear next step. Do not invent facts."
    prompt = f"You are a thoughtful B2B event follow-up assistant. {task}\nPerson: {lead['name']}\nCompany: {lead['company']}\nEvent: {lead['event']}\nNotes: {lead['notes']}"
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    req = Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}", data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=25) as response:
            result = json.loads(response.read().decode())
        content = result["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (HTTPError, URLError, KeyError, IndexError, TimeoutError, json.JSONDecodeError) as exc:
        app.logger.warning("Gemini request failed: %s", exc)
        return jsonify({"error": "Gemini could not create a response right now."}), 502
    return jsonify({"action": action, "content": content})


@app.get("/export.csv")
def export_csv():
    output = io.StringIO(); writer = csv.writer(output); writer.writerow(["Name", "Company", "Email", "Event", "Follow-up status", "Notes"])
    for lead in query_leads(): writer.writerow([lead["name"], lead["company"], lead["email"], lead["event"], STATUS_LABELS.get(lead["status"], lead["status"]), lead["notes"]])
    return send_file(io.BytesIO(output.getvalue().encode("utf-8-sig")), mimetype="text/csv", as_attachment=True, download_name="gatherline-leads.csv")


@app.errorhandler(Exception)
def handle_unexpected_error(error: Exception):
    app.logger.exception("Unhandled application error: %s", error)
    if request.path.startswith("/api/"):
        return jsonify({"error": "The API could not complete this request. Check the server logs."}), 500
    raise error


if __name__ == "__main__":
    db().close()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG") == "1")
