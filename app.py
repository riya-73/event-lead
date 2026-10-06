from __future__ import annotations

import csv
import io
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DATABASE_PATH", BASE_DIR / "data" / "leads.db"))
STATUSES = ("NEW", "CONTACTED", "REPLIED", "MEETING_BOOKED", "CLOSED")
STATUS_LABELS = {"NEW": "New", "CONTACTED": "Contacted", "REPLIED": "Replied", "MEETING_BOOKED": "Meeting booked", "CLOSED": "Closed"}

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


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS leads (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, company TEXT NOT NULL, email TEXT NOT NULL,
        event TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
        ai_summary TEXT, email_draft TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    if conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0:
        stamp = now()
        conn.executemany("INSERT INTO leads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            (str(uuid.uuid4()), *lead, None, None, stamp, stamp) for lead in SAMPLE_LEADS
        ])
        conn.commit()
    return conn


def serialize(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "company": row["company"], "email": row["email"], "event": row["event"], "notes": row["notes"], "status": row["status"], "aiSummary": row["ai_summary"], "emailDraft": row["email_draft"], "createdAt": row["created_at"], "updatedAt": row["updated_at"]}


def validate(payload: dict[str, Any], partial: bool = False) -> tuple[dict[str, Any], str | None]:
    fields = {"name", "company", "email", "event", "notes", "status"}
    unknown = set(payload) - fields
    if unknown:
        return {}, f"Unknown fields: {', '.join(sorted(unknown))}"
    clean: dict[str, Any] = {}
    for field in fields:
        if field not in payload:
            if partial:
                continue
            if field == "notes":
                clean["notes"] = ""
                continue
            if field == "status":
                clean["status"] = "NEW"
                continue
        value = str(payload.get(field, "")).strip()
        if field in {"name", "company", "email", "event"} and not value:
            return {}, f"{field.title()} is required."
        if field == "email" and ("@" not in value or "." not in value.split("@")[-1]):
            return {}, "Enter a valid email address."
        if field == "status" and value not in STATUSES:
            return {}, "Choose a valid follow-up status."
        clean[field] = value
    if not partial:
        clean.setdefault("notes", "")
        clean.setdefault("status", "NEW")
    return clean, None


def query_leads() -> list[dict[str, Any]]:
    conn = db(); sql = "SELECT * FROM leads WHERE 1=1"; args: list[str] = []
    q, event, status = request.args.get("q", "").strip(), request.args.get("event", "").strip(), request.args.get("status", "").strip()
    if q:
        sql += " AND (lower(name) LIKE ? OR lower(company) LIKE ? OR lower(email) LIKE ?)"; args += [f"%{q.lower()}%"] * 3
    if event: sql += " AND event = ?"; args.append(event)
    if status in STATUSES: sql += " AND status = ?"; args.append(status)
    rows = [serialize(row) for row in conn.execute(sql + " ORDER BY created_at DESC", args).fetchall()]; conn.close(); return rows


@app.get("/")
def home():
    return render_template("index.html", statuses=STATUSES, status_labels=STATUS_LABELS)


@app.get("/api/leads")
def list_api():
    leads = query_leads()
    return jsonify({"leads": leads, "events": sorted({lead["event"] for lead in leads})})


@app.post("/api/leads")
def create_api():
    data, error = validate(request.get_json(silent=True) or {})
    if error: return jsonify({"error": error}), 400
    lead_id, stamp = str(uuid.uuid4()), now(); conn = db()
    conn.execute("INSERT INTO leads (id,name,company,email,event,notes,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (lead_id, data["name"], data["company"], data["email"], data["event"], data["notes"], data["status"], stamp, stamp)); conn.commit()
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone(); conn.close(); return jsonify({"lead": serialize(row)}), 201


@app.patch("/api/leads/<lead_id>")
def update_api(lead_id: str):
    data, error = validate(request.get_json(silent=True) or {}, partial=True)
    if error: return jsonify({"error": error}), 400
    if not data: return jsonify({"error": "Provide at least one lead field."}), 400
    conn = db(); values = list(data.values()) + [now(), lead_id]; columns = ", ".join(f"{key if key != 'notes' else 'notes'} = ?" for key in data)
    conn.execute(f"UPDATE leads SET {columns}, updated_at = ? WHERE id = ?", values); conn.commit(); row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone(); conn.close()
    return (jsonify({"lead": serialize(row)}) if row else (jsonify({"error": "Lead not found."}), 404))


@app.delete("/api/leads/<lead_id>")
def delete_api(lead_id: str):
    conn = db(); cursor = conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,)); conn.commit(); conn.close()
    return jsonify({"success": bool(cursor.rowcount)}) if cursor.rowcount else (jsonify({"error": "Lead not found."}), 404)


@app.get("/export.csv")
def export_csv():
    output = io.StringIO(); writer = csv.writer(output); writer.writerow(["Name", "Company", "Email", "Event", "Follow-up status", "Notes"])
    for lead in query_leads(): writer.writerow([lead["name"], lead["company"], lead["email"], lead["event"], STATUS_LABELS.get(lead["status"], lead["status"]), lead["notes"]])
    return send_file(io.BytesIO(output.getvalue().encode("utf-8-sig")), mimetype="text/csv", as_attachment=True, download_name="gatherline-leads.csv")


if __name__ == "__main__":
    db().close()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG") == "1")
