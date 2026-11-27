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
        self.assertNotEqual(
            canonical_url("https://example.com/partner?b=2&a=1"),
            canonical_url("https://example.com/partner?a=1&b=2"),
        )  # query order can be meaningful, so it is preserved
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


class LeadRegressionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "leads.sqlite3"
        self.store = LeadStore(self.path, fetcher=ok_fetch)

    def test_url_preserves_meaningful_escapes_query_and_ports(self):
        key = canonical_url
        self.assertEqual(
            key("https://example.com/%7e/a%2fb?ref=&x=%2f&x=&UTM_custom=1&gclid=2"),
            "https://example.com/~/a%2Fb?ref=&x=%2F&x=",
        )
        self.assertNotEqual(key("https://example.com/a%2fb"), key("https://example.com/a/b"))
        self.assertNotEqual(key("https://example.com/?a=1&b=2"), key("https://example.com/?b=2&a=1"))
        self.assertNotEqual(key("https://example.com/?ref="), key("https://example.com/"))
        self.assertEqual(key("http://example.com:80/"), key("https://example.com:443/"))
        for url in ("http://example.com:443/", "https://example.com:80/"):
            self.assertNotEqual(key(url), key("https://example.com/"))
            self.assertEqual(key(key(url)), key(url))

    def test_redirect_alias_reuses_lead_and_conflicts_are_planned(self):
        self.store.ensure_schema()
        target = "https://example.com/target"
        first_url = "https://example.com/first"
        second_url = "https://example.com/second"
        self.store.fetcher = lambda url: ok_fetch(target)
        first = self.store.submit("First", first_url, SIGNAL, "partner")
        self.assertEqual(self.store.submit("First", target, SIGNAL, "partner")["run_id"], first["run_id"])
        second = self.store.submit("Second", second_url, SIGNAL, "partner")
        self.assertNotEqual(first["lead_id"], second["lead_id"])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 2)
        merge, = self.store.plan(read_only=True).url_merges
        self.assertEqual({merge.keep_lead_id, *merge.drop_lead_ids}, {first["lead_id"], second["lead_id"]})
        self.assertIn(target, merge.aliases)
        self.store.apply()
        for url in (first_url, second_url, target):
            self.assertEqual(self.store.submit("First", url, SIGNAL, "partner")["lead_id"], merge.keep_lead_id)

    def test_redirect_to_an_existing_primary_url_is_planned(self):
        self.store.ensure_schema()
        target = "https://example.com/target"
        first = self.store.submit("Example", target, SIGNAL, "partner")
        self.store.fetcher = lambda url: ok_fetch(target)
        second = self.store.submit("Example", "https://example.com/redirect", SIGNAL, "partner")
        self.assertNotEqual(first["lead_id"], second["lead_id"])
        merge, = self.store.plan(read_only=True).url_merges
        self.assertEqual({merge.keep_lead_id, *merge.drop_lead_ids}, {first["lead_id"], second["lead_id"]})
        self.assertEqual(self.store.submit("Example", target, SIGNAL, "partner")["lead_id"], first["lead_id"])

    def test_resubmissions_preserve_original_metadata_and_every_attempt(self):
        import json

        self.store.ensure_schema()
        url = "https://example.com/a"
        first = self.store.submit("Original", url, SIGNAL, "partner")
        for company in ("Second", "Third"):
            reused = self.store.submit(company, url, "New signal", "buyer")
            self.assertEqual(reused["run_id"], first["run_id"])
            self.assertEqual(set(reused["differing_fields"]), {"company", "signal", "lead_type"})
        with sqlite3.connect(self.path) as db:
            original = db.execute("SELECT company_name, signal, lead_type FROM leads").fetchone()
            self.assertEqual(original, ("Original", SIGNAL, "partner"))
            run = db.execute("""
                SELECT submitted_company, submitted_signal, submitted_lead_type, resubmissions FROM runs
            """).fetchone()
            self.assertEqual(run[:3], original)
            history = json.loads(run[3])
            self.assertEqual([(entry["company"], entry["signal"], entry["lead_type"]) for entry in history],
                             [("Second", "New signal", "buyer"), ("Third", "New signal", "buyer")])
            db.execute("UPDATE runs SET processing_status = 'fetch_error'")
        retried = self.store.submit("Fourth", url, "Retry signal", "buyer")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("""
                SELECT submitted_company, submitted_signal, submitted_lead_type FROM runs WHERE id = ?
            """, (retried["run_id"],)).fetchone(), ("Fourth", "Retry signal", "buyer"))
            self.assertEqual(db.execute("SELECT company_name, signal, lead_type FROM leads").fetchone(), original)

    def test_legacy_columns_backfill_and_unique_indexes_after_cleanup(self):
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                CREATE TABLE companies (id TEXT PRIMARY KEY, name TEXT, created_at TEXT, updated_at TEXT);
                CREATE TABLE leads (
                    id TEXT PRIMARY KEY, company_id TEXT, company_name TEXT, source_url TEXT,
                    signal TEXT, lead_type TEXT, status TEXT, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE runs (id TEXT PRIMARY KEY, lead_id TEXT, source_url TEXT,
                    processing_status TEXT, submitted_at TEXT);
                INSERT INTO companies VALUES ('c1', 'Example Inc.', '1', '1'), ('c2', 'Example', '2', '2');
                INSERT INTO leads VALUES
                    ('l1','c1','Example Inc.','http://www.example.com/a/','old','buyer','new','1','1'),
                    ('l2','c2','Example','https://example.com/a','new','partner','new','2','2');
                INSERT INTO runs VALUES ('r1','l1','http://www.example.com/a/','review','1');
            """)
        with self.assertLogs("lead_store", level="WARNING"):
            warnings = self.store.ensure_schema()
        self.assertEqual(len(warnings), 2)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT canonical_url, fetch_url FROM runs").fetchone(),
                             ("https://example.com/a", "http://www.example.com/a/"))
            self.assertEqual(db.execute("SELECT latest_run_id FROM leads WHERE id = 'l1'").fetchone(), ("r1",))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 2)
        self.store.apply()
        self.assertEqual(self.store.ensure_schema(), [])
        with sqlite3.connect(self.path) as db:
            for table in ("companies", "leads"):
                self.assertTrue(any(row[2] and row[3] == "c" for row in db.execute(f"PRAGMA index_list({table})")))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 1)

    def test_backfill_recovers_legacy_url_from_the_initial_fetch(self):
        self.store.ensure_schema()
        original = "http://example.com:443/a%2fb?ref=&b=2&a=1"
        created = self.store.submit("Example", original, SIGNAL, "partner")
        legacy = "https://example.com/a/b?a=1&b=2"
        with sqlite3.connect(self.path) as db:
            for table in ("leads", "runs"):
                db.execute(f"UPDATE {table} SET source_url = ?, canonical_url = ?", (legacy, legacy))
        self.assertEqual(self.store.ensure_schema(), [])
        self.assertEqual(self.store.ensure_schema(), [])
        reused = self.store.submit("Example", original, SIGNAL, "partner")
        self.assertEqual(reused["run_id"], created["run_id"])
        distinct = self.store.submit("Example", legacy, SIGNAL, "partner")
        self.assertNotEqual(distinct["lead_id"], created["lead_id"])

    def test_apply_replans_under_write_lock_and_preserves_moved_urls(self):
        from unittest.mock import patch

        import lead_store

        self.store.ensure_schema()
        first = self.store.submit("Example", "https://example.com/a", SIGNAL, "partner")
        stale = self.store.plan(read_only=True)
        self.assertTrue(stale.empty)
        self.store.fetcher = lambda url: ok_fetch("https://example.com/a")
        second = self.store.submit("Example", "https://example.com/b?ref=", SIGNAL, "partner")
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE runs SET processing_status = 'added' WHERE id = ?", (first["run_id"],))

        build_plan = lead_store._build_plan

        def check_lock(db):
            with sqlite3.connect(self.path, timeout=0) as other:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    other.execute("BEGIN IMMEDIATE")
            return build_plan(db)

        with patch.object(lead_store, "_build_plan", side_effect=check_lock):
            applied = self.store.apply(stale)
        self.assertEqual(len(applied.url_merges), 1)
        with sqlite3.connect(self.path) as db:
            run = db.execute("""
                SELECT lead_id, original_source_url, original_canonical_url, original_fetch_url
                FROM runs WHERE id = ?
            """, (second["run_id"],)).fetchone()
        self.assertEqual(run, (first["lead_id"], *("https://example.com/b?ref=",) * 3))

    def test_latest_run_uses_timestamp_then_id_across_cleanup_submit_and_retry(self):
        self.store.ensure_schema()
        first = self.store.submit("Example", "https://example.com/a", SIGNAL, "partner")
        self.store.fetcher = lambda url: ok_fetch("https://example.com/a")
        second = self.store.submit("Example", "https://example.com/b", SIGNAL, "partner")
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE runs SET id = 'z', submitted_at = '2000', processing_status = 'review' WHERE id = ?",
                       (first["run_id"],))
            db.execute("UPDATE runs SET id = 'a', submitted_at = '2000', processing_status = 'added' WHERE id = ?",
                       (second["run_id"],))
        plan = self.store.plan(read_only=True)
        self.assertEqual(plan.url_merges[0].latest_run_id, "z")
        self.store.apply(plan)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT latest_run_id FROM leads").fetchone(), ("z",))
        self.assertEqual(self.store.submit("Example", "https://example.com/a", SIGNAL, "partner")["run_id"], "z")
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE runs SET processing_status = 'fetch_error' WHERE id = 'z'")
        with self.assertRaises(ValueError):
            self.store.retry("a")
        retried = self.store.retry("z")
        reused = self.store.submit("Example", "https://example.com/a", SIGNAL, "partner")
        self.assertEqual(reused["run_id"], retried["run_id"])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT latest_run_id FROM leads").fetchone(), (retried["run_id"],))


if __name__ == "__main__":
    unittest.main()
