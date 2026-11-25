import gzip
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lead_store import (
    FetchOutcome,
    LeadStore,
    canonical_url,
    decode_body,
    fetch_page,
    normalize_company_name,
    render_plan,
    report_submissions,
)

ROOT = Path(__file__).resolve().parent
SIGNAL = "自社サイトで開発パートナーを募集している。"


def ok_fetch(url):
    return FetchOutcome(True, final_url=url, status_code=200, text="<html>パートナー募集</html>")


class NamesAndUrls(unittest.TestCase):
    def test_url_variants_share_one_canonical_form(self):
        expected = "https://acrovision.jp/ai-driven-development-partner-recruitment"
        variants = [
            "https://www.acrovision.jp/ai-driven-development-partner-recruitment/",
            "http://acrovision.jp/ai-driven-development-partner-recruitment",
            "HTTPS://WWW.ACROVISION.JP/ai-driven-development-partner-recruitment/#section",
            "https://acrovision.jp/ai-driven-development-partner-recruitment/?utm_source=batch&utm_medium=email",
            "https://user:secret@www.acrovision.jp/ai-driven-development-partner-recruitment/.",
        ]
        # The dot-segment form is a separate assertion below; userinfo must not survive.
        self.assertEqual(canonical_url(variants[0]), expected)
        self.assertEqual(canonical_url(variants[1]), expected)
        self.assertEqual(canonical_url(variants[2]), expected)
        self.assertEqual(canonical_url(variants[3]), expected)
        self.assertNotIn("secret", canonical_url(variants[4]))
        self.assertEqual(canonical_url(variants[4]), expected)
        self.assertEqual(canonical_url(expected), expected)
        self.assertEqual(
            canonical_url("https://acrovision.jp/ai-driven-development-partner-recruitment/./"),
            expected,
        )

    def test_distinct_pages_and_path_case_stay_distinct(self):
        page = canonical_url("https://example.com/partner")
        self.assertEqual(page, canonical_url("https://example.com/partner/"))
        self.assertNotEqual(page, canonical_url("https://example.com/Partner"))
        self.assertNotEqual(page, canonical_url("https://example.com/partner?id=1"))
        self.assertNotEqual(
            canonical_url("https://example.com/partner?id=1"),
            canonical_url("https://example.com/partner?id=2"),
        )
        self.assertEqual(
            canonical_url("https://example.com/partner?b=2&a=1"),
            canonical_url("https://example.com/partner?a=1&b=2"),
        )
        self.assertEqual(canonical_url("https://日本.jp/a"), "https://xn--wgv71a.jp/a")

    def test_company_legal_forms_and_width(self):
        key = "アクロビジョン"
        for name in ("株式会社アクロビジョン", "アクロビジョン株式会社", "アクロビジョン（株）", "㈱アクロビジョン", "  株式会社　アクロビジョン  "):
            self.assertEqual(normalize_company_name(name), key, name)
        self.assertEqual(normalize_company_name("合同会社TokyoScaler"), "tokyoscaler")
        self.assertEqual(normalize_company_name("合同会社 Tokyo Scaler"), "tokyoscaler")
        self.assertEqual(normalize_company_name("ＴｏｋｙｏＳｃａｌｅｒ合同会社"), "tokyoscaler")
        self.assertEqual(normalize_company_name("E&Rs inc."), "e&rs")
        self.assertEqual(normalize_company_name("Ｅ＆Ｒｓ　ｉｎｃ．"), "e&rs")
        self.assertEqual(normalize_company_name("PitDock株式会社"), "pitdock")
        self.assertEqual(normalize_company_name("Foo Co., Ltd."), "foo")
        self.assertEqual(normalize_company_name("株式会社"), "")
        self.assertNotEqual(normalize_company_name("株式会社アクロビジョン"), normalize_company_name("株式会社ヘルツレーベン"))

    def test_today_sample_does_not_collapse(self):
        rows = [
            ("株式会社アクロビジョン", "https://www.acrovision.jp/ai-driven-development-partner-recruitment/"),
            ("合同会社TokyoScaler", "https://tokyo-scaler.com/partners/"),
            ("E&Rs inc.", "https://e-rs.jp/partner.html"),
            ("株式会社ピーアールアイ", "https://pr-i.co.jp/partner/"),
            ("マネーフォワード AI エージェントマーケット", "https://ai-agent-market.moneyforward.com/partner"),
            ("株式会社ヘルツレーベン", "https://herzleben.co.jp/partners/"),
            ("株式会社SORA-NEXTAI", "https://sora-nextai.com/ai-training-partner/"),
            ("PitDock株式会社", "https://www.pit-dock.com/partner"),
        ]
        self.assertEqual(len({canonical_url(url) for _name, url in rows}), len(rows))
        self.assertEqual(len({normalize_company_name(name) for name, _url in rows}), len(rows))
        report = report_submissions([
            {"company": name, "source_url": url, "run_id": str(index), "status": "added"}
            for index, (name, url) in enumerate(rows)
        ])
        self.assertIn("この一覧に、まとめる重複はありません。", report)


class SubmissionDedup(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = LeadStore(Path(self.temporary.name) / "meishi.sqlite3", fetcher=ok_fetch)
        self.store.ensure_schema()

    def tearDown(self):
        self.temporary.cleanup()

    def counts(self):
        connection = sqlite3.connect(self.store.db_path)
        try:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("companies", "leads", "runs")
            )
        finally:
            connection.close()

    def test_same_page_reuses_the_run_and_does_not_add_a_company(self):
        seen = []

        def fetcher(url):
            seen.append(url)
            return ok_fetch(url)

        self.store.fetcher = fetcher
        first = self.store.submit(
            "株式会社アクロビジョン",
            "https://www.Example.com/partners/?utm_source=nl#top",
            SIGNAL,
            "partner",
        )
        second = self.store.submit(
            "全然別の会社",
            "http://example.com/partners",
            "別の公開根拠。",
            "buyer",
        )
        self.assertEqual(seen, ["https://www.example.com/partners/?utm_source=nl"])
        self.assertFalse(second["reused"] is False)
        self.assertTrue(second["reused"])
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(second["lead_id"], first["lead_id"])
        self.assertEqual(second["company_id"], first["company_id"])
        self.assertEqual(second["status"], "review")
        self.assertEqual(self.counts(), (1, 1, 1))
        connection = sqlite3.connect(self.store.db_path)
        try:
            name = connection.execute("SELECT name FROM companies").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(name, "株式会社アクロビジョン")

    def test_same_organization_keeps_both_pages(self):
        first = self.store.submit("株式会社アクロビジョン", "https://acrovision.jp/a", SIGNAL, "partner")
        second = self.store.submit("アクロビジョン(株)", "https://acrovision.jp/b", SIGNAL, "partner")
        other = self.store.submit("合同会社TokyoScaler", "https://tokyo-scaler.com/partners/", SIGNAL, "partner")
        self.assertEqual(first["company_id"], second["company_id"])
        self.assertNotEqual(first["lead_id"], second["lead_id"])
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(other["company_id"], first["company_id"])
        self.assertEqual(self.counts(), (2, 3, 3))

    def test_fetch_error_can_be_resubmitted(self):
        urls = []

        def fetcher(url):
            urls.append(url)
            if len(urls) == 1:
                return FetchOutcome(False, error="ページの取得がタイムアウトしました。", transient=False)
            return ok_fetch(url)

        self.store.fetcher = fetcher
        failed = self.store.submit("E&Rs inc.", "https://e-rs.jp/partner.html", SIGNAL, "partner")
        self.assertEqual(failed["status"], "fetch_error")
        self.assertEqual(self.store.status(failed["run_id"])["status"], "fetch_error")
        retried = self.store.submit("E&Rs inc.", "https://www.e-rs.jp/partner.html", SIGNAL, "partner")
        self.assertFalse(retried["reused"])
        self.assertNotEqual(retried["run_id"], failed["run_id"])
        self.assertEqual(retried["lead_id"], failed["lead_id"])
        self.assertEqual(retried["status"], "review")
        self.assertEqual(urls, ["https://e-rs.jp/partner.html", "https://www.e-rs.jp/partner.html"])
        self.assertEqual(self.counts(), (1, 1, 2))
        with self.assertRaises(ValueError):
            self.store.retry(failed["run_id"])

    def test_explicit_retry_of_the_failed_run(self):
        self.store.fetcher = lambda url: FetchOutcome(False, error="ページを取得できませんでした（HTTP 403）。")
        failed = self.store.submit(
            "株式会社アクロビジョン",
            "https://www.acrovision.jp/ai-driven-development-partner-recruitment/",
            SIGNAL,
            "partner",
        )
        self.store.fetcher = ok_fetch
        retried = self.store.retry(failed["run_id"])
        self.assertEqual(retried["status"], "review")
        self.assertEqual(retried["lead_id"], failed["lead_id"])
        self.assertNotEqual(retried["run_id"], failed["run_id"])
        self.assertEqual(self.store.status(failed["run_id"])["status"], "fetch_error")
        with self.assertRaises(ValueError):
            self.store.retry(retried["run_id"])

    def test_in_progress_run_is_reused(self):
        started = threading.Event()
        release = threading.Event()

        def fetcher(url):
            started.set()
            self.assertTrue(release.wait(2))
            return ok_fetch(url)

        self.store.fetcher = fetcher
        found = []

        def first():
            found.append(self.store.submit("PitDock株式会社", "https://www.pit-dock.com/partner", SIGNAL, "partner"))

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(started.wait(2))
        second = self.store.submit("PitDock", "https://pit-dock.com/partner/", SIGNAL, "partner")
        release.set()
        thread.join(3)
        self.assertTrue(second["reused"])
        self.assertEqual(second["run_id"], found[0]["run_id"])
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_concurrent_submits_create_one_lead(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker():
            try:
                barrier.wait(2)
                results.append(self.store.submit("株式会社ヘルツレーベン", "https://herzleben.co.jp/partners/", SIGNAL, "partner"))
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(len({item["lead_id"] for item in results}), 1)
        self.assertEqual(len({item["run_id"] for item in results}), 1)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_rejects_bad_input_without_writing(self):
        with self.assertRaises(ValueError):
            self.store.submit("株式会社", "https://example.com/a", SIGNAL, "partner")
        with self.assertRaises(ValueError):
            self.store.submit("例", "javascript:alert(1)", SIGNAL, "partner")
        with self.assertRaises(ValueError):
            self.store.submit("例", "http://127.0.0.1/admin", SIGNAL, "partner")
        with self.assertRaises(ValueError):
            self.store.submit("例", "https://example.com/a", SIGNAL, "vendor")
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_interrupted_processing_can_be_retried(self):
        created = self.store.submit("株式会社SORA-NEXTAI", "https://sora-nextai.com/ai-training-partner/", SIGNAL, "partner")
        connection = sqlite3.connect(self.store.db_path)
        try:
            connection.execute(
                "UPDATE runs SET processing_status = 'processing', error = '' WHERE id = ?",
                (created["run_id"],),
            )
            connection.commit()
        finally:
            connection.close()
        self.store.recover_interrupted()
        self.assertEqual(self.store.status(created["run_id"])["status"], "fetch_error")
        calls = []
        self.store.fetcher = lambda url: calls.append(url) or ok_fetch(url)
        retried = self.store.retry(created["run_id"])
        self.assertEqual(calls, ["https://sora-nextai.com/ai-training-partner/"])
        self.assertEqual(retried["status"], "review")


class FetchBehavior(unittest.TestCase):
    def setUp(self):
        self.state = {"flaky": 0, "agents": []}

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.state["agents"].append(self.headers.get("User-Agent"))
                if self.path == "/go":
                    port = self.server.server_address[1]
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{port}/need-ua")
                    self.end_headers()
                    return
                if self.path == "/need-ua" and "Mozilla/" not in (self.headers.get("User-Agent") or ""):
                    self.send_response(403)
                    self.end_headers()
                    return
                if self.path == "/flaky":
                    outer.state["flaky"] += 1
                    if outer.state["flaky"] == 1:
                        self.send_response(503)
                        self.end_headers()
                        return
                if self.path == "/missing":
                    self.send_response(404)
                    self.end_headers()
                    return
                if self.path == "/sjis":
                    body = "<html>パートナー</html>".encode("shift_jis")
                    content_type = "text/html; charset=Shift_JIS"
                elif self.path == "/gzip":
                    body = gzip.compress("<html>gzip-ok</html>".encode())
                    content_type = "text/html; charset=utf-8"
                else:
                    body = b"<html>ok</html>"
                    content_type = "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                if self.path == "/gzip":
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_browser_user_agent_redirect_charset_gzip_and_retry(self):
        redirected = fetch_page(self.base + "/go", timeout=3, allow_private=True)
        self.assertTrue(redirected.ok)
        self.assertIn("Mozilla/5.0", self.state["agents"][0])
        self.assertTrue(all("Mozilla/" in agent for agent in self.state["agents"]))
        decoded = fetch_page(self.base + "/sjis", timeout=3, allow_private=True)
        self.assertIn("パートナー", decoded.text)
        packed = fetch_page(self.base + "/gzip", timeout=3, allow_private=True)
        self.assertIn("gzip-ok", packed.text)
        flaky = fetch_page(self.base + "/flaky", timeout=3, allow_private=True)
        self.assertTrue(flaky.ok)
        self.assertEqual(self.state["flaky"], 2)
        missing = fetch_page(self.base + "/missing", timeout=3, allow_private=True)
        self.assertFalse(missing.ok)
        self.assertIn("404", missing.error)

    def test_private_targets_are_not_requested(self):
        blocked = fetch_page("http://127.0.0.1:9/secret", timeout=1)
        self.assertFalse(blocked.ok)
        self.assertIn("公開", blocked.error)

    def test_meta_charset(self):
        data = b'<meta charset="shift_jis"><html>' + "パートナー".encode("shift_jis")
        self.assertIn("パートナー", decode_body(data, "text/html"))


class CleanupPlan(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "meishi.sqlite3"
        self.store = LeadStore(self.path, fetcher=ok_fetch)
        self.store.ensure_schema()
        connection = sqlite3.connect(self.path)
        try:
            connection.executemany(
                "INSERT INTO companies (id, name, normalized_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                [
                    ("c1", "株式会社アクロビジョン", "株式会社アクロビジョン", "2026-11-01T00:00:00.000000Z", "2026-11-01T00:00:00.000000Z"),
                    ("c2", "アクロビジョン（株）", "アクロビジョン（株）", "2026-11-02T00:00:00.000000Z", "2026-11-02T00:00:00.000000Z"),
                ],
            )
            connection.executemany(
                """INSERT INTO leads (id, company_id, company_name, source_url, canonical_url, signal,
                       lead_type, status, latest_run_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'partner', 'new', ?, ?, ?)""",
                [
                    ("l1", "c1", "株式会社アクロビジョン",
                     "https://www.acrovision.jp/ai-driven-development-partner-recruitment/",
                     "https://www.acrovision.jp/ai-driven-development-partner-recruitment/",
                     SIGNAL, "r1", "2026-11-01T00:00:00.000000Z", "2026-11-01T00:00:00.000000Z"),
                    ("l2", "c2", "アクロビジョン（株）",
                     "https://acrovision.jp/ai-driven-development-partner-recruitment",
                     "https://acrovision.jp/ai-driven-development-partner-recruitment",
                     SIGNAL, "r2", "2026-11-02T00:00:00.000000Z", "2026-11-02T00:00:00.000000Z"),
                    ("l3", "c2", "株式会社アクロビジョン",
                     "https://acrovision.jp/other",
                     "https://acrovision.jp/other",
                     SIGNAL, "r3", "2026-11-03T00:00:00.000000Z", "2026-11-03T00:00:00.000000Z"),
                ],
            )
            connection.executemany(
                """INSERT INTO runs (id, lead_id, source_url, canonical_url, fetch_url, processing_status, error, submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?, '', ?)""",
                [
                    ("r1", "l1", "https://www.acrovision.jp/ai-driven-development-partner-recruitment/",
                     "https://www.acrovision.jp/ai-driven-development-partner-recruitment/",
                     "https://www.acrovision.jp/ai-driven-development-partner-recruitment/",
                     "added", "2026-11-01T00:00:00.000000Z"),
                    ("r2", "l2", "https://acrovision.jp/ai-driven-development-partner-recruitment",
                     "https://acrovision.jp/ai-driven-development-partner-recruitment",
                     "https://acrovision.jp/ai-driven-development-partner-recruitment",
                     "fetch_error", "2026-11-02T00:00:00.000000Z"),
                    ("r3", "l3", "https://acrovision.jp/other", "https://acrovision.jp/other",
                     "https://acrovision.jp/other", "review", "2026-11-03T00:00:00.000000Z"),
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def tearDown(self):
        self.temporary.cleanup()

    def test_plan_is_read_only_and_apply_merges_without_dropping_the_other_page(self):
        before = self._counts()
        plan = self.store.plan(read_only=True)
        self.assertEqual(self._counts(), before)
        text = render_plan(plan)
        self.assertIn("https://acrovision.jp/ai-driven-development-partner-recruitment", text)
        self.assertIn("リードは削除しない", text)
        self.assertEqual(len(plan.url_merges), 1)
        self.assertEqual(plan.url_merges[0].keep_lead_id, "l1")
        self.assertEqual(plan.url_merges[0].drop_lead_ids, ["l2"])
        self.assertEqual(plan.company_merges[0].keep_company_id, "c1")
        self.assertEqual(plan.company_merges[0].repoint_lead_ids, ["l3"])
        self.store.apply(plan)
        connection = sqlite3.connect(self.path)
        try:
            leads = {row[0]: row[1] for row in connection.execute("SELECT id, company_id FROM leads")}
            runs = {row[0]: row[1] for row in connection.execute("SELECT id, lead_id FROM runs")}
            companies = list(connection.execute("SELECT id, normalized_name FROM companies"))
        finally:
            connection.close()
        self.assertEqual(set(leads), {"l1", "l3"})
        self.assertEqual(leads["l1"], "c1")
        self.assertEqual(leads["l3"], "c1")
        self.assertEqual(runs, {"r1": "l1", "r2": "l1", "r3": "l3"})
        self.assertEqual(companies, [("c1", "アクロビジョン")])

    def _counts(self):
        connection = sqlite3.connect(self.path)
        try:
            return tuple(connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in ("companies", "leads", "runs"))
        finally:
            connection.close()

    def test_script_dry_run_does_not_write_and_apply_is_refused_for_csv(self):
        report = report_submissions([
            {"company": "株式会社アクロビジョン", "source_url": "https://www.acrovision.jp/ai-driven-development-partner-recruitment/", "run_id": "a", "status": "fetch_error"},
            {"company": "アクロビジョン（株）", "source_url": "https://acrovision.jp/ai-driven-development-partner-recruitment", "run_id": "b", "status": "added"},
            {"company": "株式会社アクロビジョン", "source_url": "https://acrovision.jp/other", "run_id": "c", "status": "review"},
        ])
        self.assertIn("同じページの重複: 1", report)
        self.assertIn("残す run: b", report)
        self.assertIn("ページは2件とも残す", report)
        csv_path = Path(self.temporary.name) / "runs.csv"
        csv_path.write_text(
            "company,source_url,run_id,status\n"
            "株式会社アクロビジョン,https://www.acrovision.jp/a,a,added\n"
            "アクロビジョン株式会社,https://acrovision.jp/b,b,review\n",
            encoding="utf-8",
        )
        dry = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "dedup_leads.py"), "--db", str(self.path)],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("データは変更していません", dry.stdout)
        self.assertEqual(self._counts(), (2, 3, 3))
        refused = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "dedup_leads.py"), "--csv", str(csv_path), "--apply"],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(refused.returncode, 2)
        reviewed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "dedup_leads.py"), "--csv", str(csv_path)],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        self.assertIn("ページは2件とも残す", reviewed.stdout)
        self.assertEqual(self._counts(), (2, 3, 3))


if __name__ == "__main__":
    unittest.main()
