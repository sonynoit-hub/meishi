import asyncio
import base64
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from lead_store import FetchOutcome, LeadStore, SCHEMA
from mcp_tools import UPDATE_LEAD_STATUS_TOOL, update_lead_status

CONTACT = {"status": "contacted", "date": "2026-11-26", "channel": "email",
           "to_address": "hello@example.com", "note": "First contact"}


class LeadStatusTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "test.sqlite3"
        self.fetcher = self.enterContext(patch("lead_store.fetch_page"))
        self.fetcher.return_value = FetchOutcome(True)
        self.store = LeadStore(self.path)
        self.store.ensure_schema()

    def test_create_and_link_by_name(self):
        created = self.store.update_status(company="株式会社GOOYA", **CONTACT)
        linked = self.store.update_status(company="GOOYA", **CONTACT)
        self.assertTrue(created["created"])
        self.assertFalse(linked["created"])
        self.assertTrue(linked["deduped"])
        self.assertEqual(created["lead_id"], linked["lead_id"])
        self.assertEqual(created["log"], linked["log"])
        self.assertEqual(self.store.contact_log(company="GOOYA"), [created["log"]])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT created_via FROM leads").fetchone(), ("status_update",))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone(), (0,))
        self.assertTrue(self.store.plan(read_only=True).empty)
        self.fetcher.assert_not_called()

    def test_link_by_url_and_id_and_keep_history(self):
        url = "https://example.com/contact"
        submitted = self.store.submit("Gooya", url, "Contact page", "partner")
        first = self.store.update_status(source_url="http://www.example.com/contact/?utm_source=t", **CONTACT)
        second_contact = dict(CONTACT, date="2026-11-27", status="meeting")
        second = self.store.update_status(record_id=submitted["lead_id"], **second_contact)
        repeat = self.store.update_status(record_id=submitted["run_id"], **dict(CONTACT, status="replied"))
        self.assertEqual(first["lead_id"], submitted["lead_id"])
        self.assertEqual(second["lead_id"], first["lead_id"])
        self.assertTrue(repeat["deduped"])
        self.assertEqual(repeat["log"], first["log"])
        self.assertEqual(self.store.contact_log(lead_id=first["lead_id"]), [first["log"], second["log"]])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute(
                "SELECT status, last_contacted_at, created_via FROM leads"
            ).fetchone(), ("replied", "2026-11-27", "submit"))

    def test_submit_after_url_only_status_creates_run_on_same_lead(self):
        url = "https://example.com/contact"
        created = self.store.update_status(source_url=url, **CONTACT)
        self.fetcher.assert_not_called()
        submitted = self.store.submit("Example", url, "Contact page", "partner")
        self.assertEqual(created["lead_id"], submitted["lead_id"])
        self.assertEqual(submitted["status"], "review")
        self.fetcher.assert_called_once_with(url)

    def test_invalid_updates(self):
        for overrides in ({"status": "verified"}, {"date": "2026-02-30"},
                          {"date": "20261126"}, {"channel": " "}, {"company": "株式会社"},
                          {"source_url": "file:///test"}, {"company": None}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.store.update_status(**(dict(CONTACT, company="GOOYA") | overrides))
        with self.assertRaises(KeyError):
            self.store.update_status(record_id="unknown", **CONTACT)
        with self.assertRaises(KeyError):
            self.store.contact_log(company="unknown")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leads").fetchone(), (0,))

    def test_additive_migration_of_both_old_status_layouts(self):
        for legacy_status in (False, True):
            with self.subTest(legacy_status=legacy_status):
                path = self.path.with_name(f"legacy-{legacy_status}.sqlite3")
                schema = SCHEMA[:SCHEMA.index("CREATE TABLE IF NOT EXISTS contact_logs")]
                status_column = next(line for line in schema.splitlines() if line.startswith("    status "))
                old_status = ("    status TEXT NOT NULL DEFAULT 'new' "
                              "CHECK (status IN ('new','verified','contacted','in_discussion','won','lost')),\n")
                with closing(sqlite3.connect(path)) as db, db:
                    db.executescript(schema.replace(status_column + "\n", old_status if legacy_status else ""))
                    db.execute("""
                        INSERT INTO leads (id, company_name, source_url, canonical_url, signal,
                            lead_type, created_at, updated_at)
                        VALUES ('old', 'Old', 'https://example.com', 'https://example.com', '', 'partner', '', '')
                    """)
                    if legacy_status:
                        db.execute("UPDATE leads SET status = 'in_discussion'")
                store = LeadStore(path)
                store.ensure_schema()
                store.ensure_schema()
                current = "contact_status" if legacy_status else "status"
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute(
                        f"SELECT {current}, created_via, last_contacted_at FROM leads"
                    ).fetchone(), ("replied" if legacy_status else "new", "submit", None))
                result = store.update_status(record_id="old", **dict(CONTACT, status="meeting"))
                self.assertEqual(result["status"], "meeting")
                self.assertEqual(store.contact_log(lead_id="old"), [result["log"]])
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute(f"SELECT {current} FROM leads").fetchone(), ("meeting",))


class ConnectorTests(unittest.TestCase):
    def test_tool_handler_posts_json_with_optional_authentication(self):
        args = dict(CONTACT, company="株式会社GOOYA")
        for password in (None, "secret"):
            with self.subTest(password=password):
                def transport(request, timeout):
                    self.assertEqual(request.full_url, "https://meishi.example/api/leads/status")
                    self.assertEqual(request.get_method(), "POST")
                    self.assertEqual(request.get_header("X-meishi"), "1")
                    self.assertEqual(request.get_header("Content-type"), "application/json")
                    self.assertEqual(request.get_header("Authorization"),
                                     "Basic bWVpc2hpOnNlY3JldA==" if password else None)
                    self.assertEqual(json.loads(request.data), args)
                    return io.BytesIO(b'{"created": true}')

                self.assertEqual(update_lead_status(args, "https://meishi.example/", password,
                                                   transport=transport), {"created": True})
        self.assertEqual(UPDATE_LEAD_STATUS_TOOL["name"], "update_lead_status")


class LeadStatusHTTPTests(unittest.TestCase):
    def test_status_and_history_routes(self):
        try:
            import app
        except ModuleNotFoundError as error:
            if error.name in {"fastapi", "pillow_heif", "PIL"}:
                self.skipTest(f"App dependency unavailable: {error.name}")
            raise
        directory = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(directory) / "test.sqlite3"
        LeadStore(path).ensure_schema()
        self.enterContext(patch.object(app, "DB_PATH", path))
        self.enterContext(patch.object(app, "PASSWORD", "secret"))

        def request(method, path, body=None, write=True, auth=True, query=b""):
            headers = [(b"host", b"localhost"), (b"content-type", b"application/json")]
            if auth:
                headers.append((b"authorization", b"Basic " + base64.b64encode(b"meishi:secret")))
            if write:
                headers.append((b"x-meishi", b"1"))
            scope = {"type": "http", "method": method, "scheme": "http", "path": path,
                     "query_string": query, "headers": headers, "asgi": {"spec_version": "2.4"}}
            messages = []

            async def send(message):
                messages.append(message)

            incoming = iter([{"type": "http.request", "body": json.dumps(body).encode()}])

            async def receive():
                message = next(incoming, None)
                if message is not None:
                    return message
                await asyncio.Future()

            asyncio.run(app.app(scope, receive, send))
            response = b"".join(message.get("body", b"") for message in messages)
            return messages[0]["status"], json.loads(response)

        endpoint = "/api/leads/status"
        payload = dict(CONTACT, company="株式会社GOOYA")
        self.assertEqual(request("POST", endpoint, payload, auth=False)[0], 401)
        self.assertEqual(request("POST", endpoint, payload, write=False)[0], 403)
        self.assertEqual(request("POST", endpoint, dict(payload, status="invalid"))[0], 400)
        self.assertEqual(request("POST", endpoint, dict(CONTACT, record_id="unknown"))[0], 404)
        code, result = request("POST", endpoint, payload)
        self.assertEqual(code, 200)
        history = f"/api/leads/{result['lead_id']}/contact-log"
        self.assertEqual(request("GET", history, write=False), (200, [result["log"]]))
        by_company = "/api/leads/contact-log"
        self.assertEqual(request("GET", by_company, query=b"company=GOOYA"), (200, [result["log"]]))
        self.assertEqual(request("GET", by_company)[0], 400)
        self.assertEqual(request("GET", "/api/leads/unknown/contact-log")[0], 404)
