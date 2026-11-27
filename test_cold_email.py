import asyncio
import base64
import csv
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import mcp_tools
from lead_store import LEAD_CSV_COLUMNS, LeadStore, SCHEMA


CONTACT = {"date": "2026-11-27", "channel": "email"}


class ColdEmailTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "leads.sqlite3"
        self.store = LeadStore(self.path)
        self.store.ensure_schema()

    def create(self, company="株式会社サンプル", **fields):
        return self.store.update_status(company=company, status="new", **CONTACT, **fields)["lead_id"]

    def test_legacy_status_constraints_and_history(self):
        old_statuses = "('new', 'contacted', 'replied', 'meeting', 'won', 'lost')"
        for active in ("status", "contact_status"):
            with self.subTest(active=active):
                path = self.path.with_name(active + ".sqlite3")
                legacy = SCHEMA.replace(", 'bounced', 'unsubscribed')", ")")
                with closing(sqlite3.connect(path)) as db, db:
                    db.executescript(legacy)
                    if active == "contact_status":
                        db.execute(f"ALTER TABLE leads ADD COLUMN contact_status TEXT NOT NULL "
                                   f"DEFAULT 'new' CHECK (contact_status IN {old_statuses})")
                    db.execute(f"""
                        INSERT INTO leads (id, company_name, source_url, canonical_url, signal,
                            lead_type, created_at, updated_at, {active})
                        VALUES ('legacy', 'Legacy', '', 'urn:legacy', '', 'partner', '', '', 'contacted')
                    """)
                    db.execute("""
                        INSERT INTO contact_logs VALUES ('history', 'legacy', 'contacted',
                            '2026-11-26', 'email', 'first@example.com', '', '', '', '')
                    """)
                store = LeadStore(path)
                store.ensure_schema()
                store.ensure_schema()
                self.assertEqual(store.list_leads(status="contacted")[0]["sequence_step"], 0)
                for status, address in (("bounced", "first@example.com"),
                                        ("unsubscribed", "second@example.com")):
                    result = store.update_status(record_id="legacy", status=status, **CONTACT, email=address,
                                                 to_address=address, batch_id="batch")
                    self.assertEqual(result["log"]["status"], status)
                    self.assertEqual(result["log"]["batch_id"], "batch")
                    self.assertEqual(store.list_leads(status=status, include_suppressed=True)[0]["email"], address)
                self.assertEqual([log["status"] for log in store.contact_log(lead_id="legacy")],
                                 ["contacted", "bounced", "unsubscribed"])

    def test_email_and_parent_domain_suppression(self):
        lead_id = self.create(email="Hello@Sub.EXAMPLE.COM")
        for kind, value in (("email", "HELLO@SUB.EXAMPLE.COM"), ("domain", "EXAMPLE.COM")):
            with self.subTest(kind=kind):
                row = self.store.add_suppression(kind, value, "manual")
                self.assertEqual(row["value"], value.lower())
                self.assertTrue(self.store.is_suppressed("hello@sub.example.com"))
                self.assertFalse(self.store.is_suppressed("hello@anotherexample.com"))
                with self.assertRaises(ValueError):
                    self.store.update_status(record_id=lead_id, status="contacted", **CONTACT)
                with self.assertRaises(ValueError):
                    self.store.update_status(record_id=lead_id, status="new", **CONTACT, sequence_step=1)
                with self.assertRaises(ValueError):
                    self.store.update_lead(lead_id, next_followup_at="2026-12-03")
                self.assertEqual(self.store.list_leads(), [])
                self.assertTrue(self.store.list_leads(include_suppressed=True)[0]["suppressed"])
                reply = self.store.update_status(record_id=lead_id, status="replied",
                                                 date="2026-11-28", channel="inbound", note="Reply")
                self.assertEqual(reply["log"]["status"], "replied")
                self.assertIsNotNone(self.store.remove_suppression(row["id"])["removed_at"])
                self.assertEqual(self.store.list_suppressions(), [])
                self.assertFalse(self.store.is_suppressed("hello@sub.example.com"))
                self.store.update_status(record_id=lead_id, status="contacted", **CONTACT,
                                         next_followup_at="2026-12-03")
        self.assertEqual(len(self.store.list_suppressions(include_removed=True)), 2)
        self.store.add_suppression("email", "blocked@example.org", "manual")
        with self.assertRaises(ValueError):
            self.store.update_status(record_id=lead_id, status="contacted", **CONTACT,
                                     to_address="blocked@example.org")

    def test_outcomes_clear_followup_and_record_inbound(self):
        for index, status in enumerate(("bounced", "unsubscribed", "replied", "won", "lost", "meeting")):
            with self.subTest(status=status):
                email = f"lead{index}@example.com"
                lead_id = self.create(company=f"Company {index}", email=email, next_followup_at="2026-12-03")
                if status != "meeting":
                    self.store.update_lead(lead_id, do_not_contact=True)
                    with self.assertRaises(ValueError):
                        self.store.update_lead(lead_id, sequence_step=1)
                result = self.store.update_status(record_id=lead_id, status=status, date=CONTACT["date"],
                                                  channel="inbound", subject="Response", note="Details")
                lead = self.store.list_leads(status=status, include_suppressed=True)[0]
                self.assertIsNone(lead["next_followup_at"])
                self.assertEqual(result["log"]["channel"], "inbound")
                self.assertEqual((result["log"]["subject"], result["log"]["note"]), ("Response", "Details"))
                if status in ("bounced", "unsubscribed"):
                    self.assertTrue(self.store.is_suppressed(email))
                    self.assertIn((email, status), {(row["value"], row["reason"])
                                                    for row in self.store.list_suppressions()})

    def test_filters_csv_and_validation(self):
        lead_id = self.create(email="Hello@EXAMPLE.COM", email_source_url="https://example.com/contact",
                              email_checked_at="2026-11-27", email_evidence="連絡先", tags="a,b",
                              segment="one", batch_id="batch-a")
        contact = self.store.update_status(record_id=lead_id, status="contacted", **CONTACT,
                                           sequence_step=1, next_followup_at="2026-12-03",
                                           to_address="Hello@example.com", subject="Introduction")
        self.assertEqual(contact["log"]["sequence_step"], 1)
        self.assertEqual(contact["log"]["batch_id"], "batch-a")
        for company, status, segment, batch, due in (
            ("Other status", "new", "one", "batch-a", "2026-12-03"),
            ("Other segment", "contacted", "two", "batch-a", "2026-12-03"),
            ("Other batch", "contacted", "one", "batch-b", "2026-12-03"),
            ("Later due", "contacted", "one", "batch-a", "2026-12-04"),
        ):
            self.store.update_status(company=company, status=status, **CONTACT, segment=segment,
                                     batch_id=batch, next_followup_at=due)
        filters = dict(status="contacted", segment="one", batch_id="batch-a", due_on_or_before="2026-12-03")
        leads = self.store.list_leads(**filters)
        self.assertEqual([lead["id"] for lead in leads], [lead_id])
        self.assertEqual(leads[0]["last_contact"], contact["log"])
        self.assertEqual(self.store.list_leads(**filters, offset=1, limit=1), [])
        payload = self.store.export_leads_csv(**filters)
        self.assertTrue(payload.startswith(b"\xef\xbb\xbf"))
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
        self.assertEqual(tuple(reader.fieldnames), LEAD_CSV_COLUMNS)
        row, = reader
        self.assertEqual(row["company"], "株式会社サンプル")
        self.assertEqual(row["email"], "Hello@example.com")
        self.assertEqual(row["email_evidence"], "連絡先")
        self.assertEqual(row["last_contact_subject"], "Introduction")
        for invalid in ("invalid", "someone @example.com", "someone@ example.com", "someone@example..com"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.update_lead(lead_id, email=invalid)


class ColdEmailConnectorTests(unittest.TestCase):
    def test_handlers(self):
        cases = [
            ("list_leads", "GET", "/api/leads", {"include_suppressed": True}, b"[]", []),
            ("export_leads_csv", "GET", "/api/leads/export.csv", {"segment": "日本"},
             b"\xef\xbb\xbfid,email\r\n", "id,email\r\n"),
            ("add_suppression", "POST", "/api/suppressions",
             {"kind": "domain", "value": "example.com", "reason": "manual"}, b'{"id": "one"}', {"id": "one"}),
            ("check_suppression", "GET", "/api/suppressions/check", {"address": "hi+tag@example.com"},
             b'{"suppressed": true}', {"suppressed": True}),
        ]
        for name, method, route, args, raw, result in cases:
            with self.subTest(tool=name):
                def transport(request, timeout):
                    url = urlsplit(request.full_url)
                    self.assertEqual(url.path, route)
                    self.assertEqual(request.get_method(), method)
                    self.assertEqual(request.get_header("Authorization"), "Basic bWVpc2hpOnNlY3JldA==")
                    if method == "POST":
                        self.assertEqual(request.get_header("X-meishi"), "1")
                        self.assertEqual(json.loads(request.data), args)
                    else:
                        self.assertEqual(parse_qs(url.query), {
                            key: ["true" if value is True else value] for key, value in args.items()
                        })
                    return io.BytesIO(raw)

                self.assertEqual(getattr(mcp_tools, name)(args, "https://meishi.example/", "secret",
                                                        transport=transport), result)
                self.assertEqual(getattr(mcp_tools, name.upper() + "_TOOL")["name"], name)


class ColdEmailHTTPTests(unittest.TestCase):
    def test_routes(self):
        try:
            import app
        except ModuleNotFoundError as error:
            if error.name in {"fastapi", "pillow_heif", "PIL"}:
                self.skipTest(f"App dependency unavailable: {error.name}")
            raise
        directory = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(directory) / "leads.sqlite3"
        LeadStore(path).ensure_schema()
        self.enterContext(patch.object(app, "DB_PATH", path))
        self.enterContext(patch.object(app, "PASSWORD", "secret"))

        def request(method, path, body=None, *, guarded=True, auth=True, query=b""):
            messages = []
            headers = [(b"host", b"localhost"), (b"content-type", b"application/json")]
            if auth:
                headers.append((b"authorization", b"Basic " + base64.b64encode(b"meishi:secret")))
            if guarded:
                headers.append((b"x-meishi", b"1"))

            async def send(message):
                messages.append(message)

            incoming = iter([{"type": "http.request", "body": json.dumps(body).encode()}])

            async def receive():
                message = next(incoming, None)
                if message is not None:
                    return message
                await asyncio.Future()

            scope = {"type": "http", "method": method, "scheme": "http", "path": path,
                     "query_string": query, "headers": headers, "asgi": {"spec_version": "2.4"}}
            asyncio.run(app.app(scope, receive, send))
            data = b"".join(message.get("body", b"") for message in messages)
            return messages[0]["status"], data if data.startswith(b"\xef\xbb\xbf") else json.loads(data)

        status_path = "/api/leads/status"
        suppressions = "/api/suppressions"
        self.assertEqual(request("GET", "/api/leads", auth=False)[0], 401)
        code, created = request("POST", status_path, dict(CONTACT, company="Example", status="new"))
        self.assertEqual(code, 200)
        lead_path = "/api/leads/" + created["lead_id"]
        self.assertEqual(request("PATCH", lead_path, {"email": "hi@example.com"}, guarded=False)[0], 403)
        self.assertEqual(request("PATCH", lead_path, {"email": "bad"})[0], 400)
        self.assertEqual(request("PATCH", lead_path, {"email": "hi@EXAMPLE.COM", "segment": "a"})[0], 200)

        code, row = request("POST", suppressions, {"kind": "domain", "value": "Example.com", "reason": "manual"})
        self.assertEqual(code, 200)
        self.assertEqual(request("GET", suppressions)[1][0]["value"], "example.com")
        self.assertEqual(request("GET", suppressions + "/check", query=b"address=hi%40example.com"),
                         (200, {"suppressed": True}))
        self.assertEqual(request("POST", status_path, dict(CONTACT, status="contacted",
                                                          record_id=created["lead_id"]))[0], 409)
        self.assertEqual(request("PATCH", lead_path, {"next_followup_at": "2026-12-03"})[0], 409)
        self.assertEqual(request("GET", "/api/leads"), (200, []))
        self.assertEqual(request("DELETE", suppressions + "/" + row["id"], guarded=False)[0], 403)
        self.assertEqual(request("DELETE", suppressions + "/" + row["id"])[0], 200)
        self.assertEqual(request("GET", suppressions), (200, []))
        self.assertIsNotNone(request("GET", suppressions, query=b"include_removed=true")[1][0]["removed_at"])
        self.assertEqual(request("GET", "/api/leads", query=b"segment=a")[1][0]["email"], "hi@example.com")
        code, content = request("GET", "/api/leads/export.csv", query=b"segment=a")
        self.assertEqual(code, 200)
        self.assertIn(b"hi@example.com", content)
