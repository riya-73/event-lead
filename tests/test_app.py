from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module


class GatherlineBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.database_path = Path(cls.temp_dir.name) / "test-leads.db"
        cls.db_path_patch = patch.object(app_module, "DB_PATH", cls.database_path)
        cls.db_path_patch.start()
        app_module.app.config.update(TESTING=True)
        cls.client = app_module.app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db_path_patch.stop()
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        # Keep every test isolated while still exercising the real SQLite schema and seed path.
        if self.database_path.exists():
            self.database_path.unlink()

    def get_leads(self, query: str = "") -> list[dict]:
        response = self.client.get(f"/api/leads{query}")
        self.assertEqual(response.status_code, 200)
        return response.get_json()["leads"]

    def test_home_page_contains_leads_events_and_settings_without_overview(self) -> None:
        response = self.client.get("/")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-section="events"', body)
        self.assertIn("Jordan Davis", body)
        self.assertIn("Dark mode", body)
        self.assertIn('id="ai-modal"', body)
        self.assertIn('id="ai-output"', body)
        self.assertNotIn("Overview", body)

    def test_health_endpoint_reports_seeded_database(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")
        self.assertEqual(response.get_json()["database"], "sqlite")
        self.assertEqual(response.get_json()["leadCount"], 9)

    def test_database_initializes_and_seeds_sample_leads(self) -> None:
        leads = self.get_leads()
        self.assertEqual(len(leads), 9)
        self.assertEqual(len({lead["event"] for lead in leads}), 7)
        self.assertTrue(self.database_path.exists())
        self.assertTrue(all(set(("id", "name", "company", "email", "event", "status")) <= lead.keys() for lead in leads))

    def test_create_route_validates_and_persists_lead(self) -> None:
        payload = {
            "name": "Jordan Test",
            "company": "Acme Labs",
            "email": "jordan@example.com",
            "event": "Demo Day",
            "notes": "Met at the booth.",
            "status": "NEW",
        }
        response = self.client.post("/api/leads", json=payload)
        self.assertEqual(response.status_code, 201)
        created = response.get_json()["lead"]
        self.assertEqual(created["name"], "Jordan Test")
        self.assertEqual(created["status"], "NEW")
        self.assertEqual(self.client.get(f"/api/leads?q={created['email']}").get_json()["leads"][0]["id"], created["id"])

        invalid = self.client.post("/api/leads", json={**payload, "email": "not-an-email"})
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("valid email", invalid.get_json()["error"])

        unknown_field = self.client.post("/api/leads", json={**payload, "unexpected": True})
        self.assertEqual(unknown_field.status_code, 400)
        self.assertIn("Unknown fields", unknown_field.get_json()["error"])

    def test_search_and_event_status_filters(self) -> None:
        search = self.client.get("/api/leads?q=maya").get_json()
        self.assertEqual([lead["name"] for lead in search["leads"]], ["Maya Chen"])

        event = self.client.get("/api/leads?event=Web%20Summit%202026").get_json()["leads"]
        self.assertEqual({lead["event"] for lead in event}, {"Web Summit 2026"})
        self.assertEqual(len(event), 2)

        status = self.client.get("/api/leads?status=NEW").get_json()["leads"]
        self.assertEqual(len(status), 2)
        self.assertTrue(all(lead["status"] == "NEW" for lead in status))

    def test_patch_route_updates_partial_fields_and_status(self) -> None:
        lead = self.get_leads()[0]
        response = self.client.patch(f"/api/leads/{lead['id']}", json={"notes": "Updated context", "status": "CONTACTED"})
        self.assertEqual(response.status_code, 200)
        updated = response.get_json()["lead"]
        self.assertEqual(updated["notes"], "Updated context")
        self.assertEqual(updated["status"], "CONTACTED")
        self.assertEqual(updated["name"], lead["name"])

        empty = self.client.patch(f"/api/leads/{lead['id']}", json={})
        self.assertEqual(empty.status_code, 400)
        self.assertIn("at least one", empty.get_json()["error"])

        missing = self.client.patch("/api/leads/does-not-exist", json={"status": "CLOSED"})
        self.assertEqual(missing.status_code, 404)

    def test_delete_route_removes_lead_and_handles_missing_id(self) -> None:
        lead = self.get_leads()[0]
        response = self.client.delete(f"/api/leads/{lead['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertNotIn(lead["id"], {item["id"] for item in self.get_leads()})

        missing = self.client.delete(f"/api/leads/{lead['id']}")
        self.assertEqual(missing.status_code, 404)

    def test_csv_export_contains_headers_and_filtered_rows(self) -> None:
        response = self.client.get("/export.csv?status=NEW")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["Name", "Company", "Email", "Event", "Follow-up status", "Notes"])
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row[4] == "New" for row in rows[1:]))

    def test_database_operations_survive_new_connection(self) -> None:
        payload = {"name": "Persistent Person", "company": "Persist Co", "email": "persist@example.com", "event": "Persistence Test"}
        created = self.client.post("/api/leads", json=payload).get_json()["lead"]
        # Close/reopen an actual SQLite connection to verify persistence is not process-memory state.
        connection = app_module.db()
        row = connection.execute("SELECT name, event FROM leads WHERE id = ?", (created["id"],)).fetchone()
        connection.close()
        self.assertEqual(tuple(row), ("Persistent Person", "Persistence Test"))


if __name__ == "__main__":
    unittest.main()
