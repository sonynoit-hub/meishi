"""Lead storage for Meishi.

Submissions dedupe in two steps:

* Canonical URL. One lead and, unless the latest run is ``fetch_error``,
  one run. ``http``/``https``, a leading ``www.``, default ports, fragments,
  trailing slashes, host case, and tracking query parameters collapse.
  The path keeps its case. A different path or a meaningful query is a
  different lead.
* Organization. The company name is matched after Unicode NFKC, case
  folding, and stripping of legal forms (株式会社, 合同会社, Inc., …).
  A second page for the same organization creates another lead and run
  on the existing company. It does not drop the new URL.

A run that ended in ``fetch_error`` is not sticky: submitting that URL
again, or calling retry, starts a new run and fetches again. Successful
and in-progress runs are reused.

Pages are fetched with a browser User-Agent, redirects, and the charset
declared by the response. The page body is not stored.
"""

from __future__ import annotations

import csv
import gzip
import ipaddress
import logging
import posixpath
import re
import socket
import sqlite3
import unicodedata
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

logger = logging.getLogger(__name__)

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_BODY_BYTES = 3 * 1024 * 1024
RUN_STATUSES = ("processing", "review", "added", "fetch_error")
STATUS_RANK = {"added": 4, "review": 3, "processing": 2, "fetch_error": 1}
TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}
TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "gclsrc", "dclid", "yclid", "msclkid",
    "mc_cid", "mc_eid", "igshid", "_ga", "fb_action_ids", "fb_action_types",
    "ref", "ref_src",
}
JAPANESE_LEGAL_FORMS = (
    "一般社団法人", "公益社団法人", "一般財団法人", "公益財団法人",
    "社会福祉法人", "医療法人", "学校法人",
    "株式会社", "合同会社", "有限会社", "合名会社", "合資会社",
)
ENGLISH_LEGAL_FORMS = (
    "incorporated", "corporation", "limited", "company",
    "inc.", "ltd.", "corp.", "co.",
    "inc", "ltd", "llc", "corp", "gk", "kk", "co",
)
CHARSET_ALIASES = {
    "shift_jis": "shift_jis", "shift-jis": "shift_jis", "x-sjis": "shift_jis",
    "sjis": "shift_jis", "windows-31j": "cp932", "cp932": "cp932",
    "euc-jp": "euc_jp", "eucjp": "euc_jp", "utf8": "utf-8", "utf-8": "utf-8",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    company_id TEXT REFERENCES companies(id),
    company_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL UNIQUE,
    signal TEXT NOT NULL,
    lead_type TEXT NOT NULL CHECK (lead_type IN ('buyer', 'partner')),
    status TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('new','verified','contacted','in_discussion','won','lost')),
    latest_run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL REFERENCES leads(id),
    source_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    fetch_url TEXT NOT NULL,
    processing_status TEXT NOT NULL
        CHECK (processing_status IN ('processing','review','added','fetch_error')),
    error TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_lead ON runs(lead_id, submitted_at);
CREATE INDEX IF NOT EXISTS runs_canonical ON runs(canonical_url, submitted_at);
"""


class FetchOutcome:
    def __init__(self, ok, error="", final_url="", status_code=None, text="", transient=False):
        self.ok = bool(ok)
        self.error = error
        self.final_url = final_url
        self.status_code = status_code
        self.text = text
        self.transient = transient


class _BlockedURL(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_url(value):
    """Return the dedup key for a public http(s) URL.

    Path case is preserved because origin servers can treat ``/A`` and
    ``/a`` as different pages. Host case is not meaningful and is folded.
    """
    raw = value.strip()
    if not raw or len(raw) > 2048 or any(character.isspace() for character in raw):
        raise ValueError("URLは1〜2048文字です")
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("URLを確認してください")
    try:
        host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as error:
        raise ValueError("URLを確認してください") from error
    if host.startswith("www."):
        host = host[4:]
    if not host:
        raise ValueError("URLを確認してください")
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("URLを確認してください") from error
    if port in (None, 80, 443):
        port_text = ""
    else:
        port_text = f":{port}"
    netloc = f"[{host}]{port_text}" if ":" in host else f"{host}{port_text}"
    path = unquote(parts.path or "")
    if path in {"", "/"}:
        path = ""
    else:
        if not path.startswith("/"):
            path = "/" + path
        path = posixpath.normpath(path)
        path = "" if path == "/" else quote(path, safe="/%:@")
    query_pairs = [
        (key, item) for key, item in parse_qsl(parts.query, keep_blank_values=False)
        if key.lower() not in TRACKING_QUERY_KEYS
    ]
    query_pairs.sort()
    canonical = urlunsplit(("https", netloc, path, urlencode(query_pairs, doseq=True), ""))
    if len(canonical) > 2048:
        raise ValueError("URLは1〜2048文字です")
    return canonical


def fetchable_url(value):
    """URL to request: original scheme, host, path, and query, without secrets or a fragment."""
    raw = value.strip()
    parts = urlsplit(raw)
    host = parts.hostname
    if host is None:
        raise ValueError("URLを確認してください")
    try:
        host = host.encode("idna").decode("ascii")
        port = parts.port
    except (UnicodeError, ValueError) as error:
        raise ValueError("URLを確認してください") from error
    if (parts.scheme.lower() == "https" and port == 443) or (parts.scheme.lower() == "http" and port == 80):
        port = None
    if ":" in host:
        netloc = f"[{host}]" if port is None else f"[{host}]:{port}"
    else:
        netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def _strip_japanese_legal_form(text):
    tokens = sorted(JAPANESE_LEGAL_FORMS, key=len, reverse=True)
    changed = True
    while changed and text:
        changed = False
        text = text.strip(" .,・")
        for token in tokens:
            if text.startswith(token):
                text = text[len(token):].strip()
                changed = True
                break
            if text.endswith(token):
                text = text[:-len(token)].strip()
                changed = True
                break
    return text


def _strip_english_legal_form(text):
    tokens = sorted(ENGLISH_LEGAL_FORMS, key=len, reverse=True)
    while text:
        cleaned = text.strip(" .,")
        if cleaned != text:
            text = cleaned
            continue
        stripped = None
        for token in tokens:
            if text == token:
                return ""
            prefix = token + " "
            suffix = " " + token
            if text.startswith(prefix):
                stripped = text[len(prefix):].strip()
                break
            if text.endswith(suffix):
                stripped = text[:-len(suffix)].strip()
                break
        if stripped is None:
            return text
        text = stripped
    return text


def normalize_company_name(name):
    """Exact-match key for an organization name. Empty when nothing remains."""
    text = unicodedata.normalize("NFKC", name).casefold()
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[(（]\s*(?:株|同|有|社|財)\s*[)）]", "", text)
    text = _strip_japanese_legal_form(text)
    text = _strip_english_legal_form(text)
    text = re.sub(r"\s+", "", text)
    return text.strip(".,・·")


def _obviously_nonpublic(url):
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if not host or host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not _public_address(address)


def _public_address(address):
    return not (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_multicast or address.is_reserved or address.is_unspecified
    )


def _blocked_host_message(url):
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return "公開のWebページのURLを指定してください。"
    host = parts.hostname
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return "URLを確認してください。"
    if _obviously_nonpublic(url):
        return "公開のWebページのURLを指定してください。"
    try:
        resolved = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return "ページの名前を解決できませんでした。"
    for info in resolved:
        address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not _public_address(address):
            return "公開のWebページのURLを指定してください。"
    return None


class _PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        problem = _blocked_host_message(newurl)
        if problem:
            raise _BlockedURL(problem)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _charset_name(content_type, data):
    sources = []
    if content_type:
        sources.append(content_type)
    sources.append(data[:4096].decode("ascii", errors="ignore"))
    for source in sources:
        match = re.search(r"charset=\s*['\"]?([\w.-]+)", source, re.IGNORECASE)
        if match:
            name = match.group(1).lower()
            return CHARSET_ALIASES.get(name, name)
    return "utf-8"


def decode_body(data, content_type):
    charset = _charset_name(content_type, data)
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _looks_like_html(content_type, data):
    if content_type:
        media = content_type.split(";", 1)[0].strip().lower()
        if media in {"text/html", "application/xhtml+xml", "text/plain", "application/octet-stream"}:
            return True
        if media.startswith("text/"):
            return True
        return False
    head = data[:200].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<html" in head


def _fetch_once(url, timeout, allow_private):
    if not allow_private:
        problem = _blocked_host_message(url)
        if problem:
            return FetchOutcome(False, problem)
    request = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en;q=0.8",
    }, method="GET")
    handler = urllib.request.HTTPRedirectHandler() if allow_private else _PublicRedirect()
    opener = urllib.request.build_opener(handler)
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type")
            encoding = (response.headers.get("Content-Encoding") or "").lower()
            data = response.read(MAX_BODY_BYTES + 1)
    except _BlockedURL as error:
        return FetchOutcome(False, str(error))
    except urllib.error.HTTPError as error:
        transient = error.code in TRANSIENT_HTTP
        return FetchOutcome(
            False, f"ページを取得できませんでした（HTTP {error.code}）。",
            status_code=error.code, transient=transient,
        )
    except TimeoutError:
        return FetchOutcome(False, "ページの取得がタイムアウトしました。", transient=True)
    except urllib.error.URLError as error:
        transient = isinstance(error.reason, (TimeoutError, socket.timeout))
        message = "ページの取得がタイムアウトしました。" if transient else "ページを取得できませんでした。"
        return FetchOutcome(False, message, transient=transient)
    except OSError:
        return FetchOutcome(False, "ページを取得できませんでした。", transient=True)

    if encoding == "gzip":
        try:
            data = gzip.decompress(data)
        except OSError:
            return FetchOutcome(False, "ページの圧縮を読めませんでした。", final_url=final_url, status_code=status)
    elif encoding and encoding != "identity":
        return FetchOutcome(False, "ページの圧縮形式を読めませんでした。", final_url=final_url, status_code=status)
    if len(data) > MAX_BODY_BYTES:
        data = data[:MAX_BODY_BYTES]
    if not data or not _looks_like_html(content_type, data):
        return FetchOutcome(False, "HTMLのページを取得できませんでした。", final_url=final_url, status_code=status)
    text = decode_body(data, content_type)
    if not text.strip():
        return FetchOutcome(False, "ページが空でした。", final_url=final_url, status_code=status)
    return FetchOutcome(True, final_url=final_url, status_code=status, text=text)


def fetch_page(url, timeout=20, allow_private=False):
    """GET a public page. Retries once on a timeout or a transient HTTP status.

    Sends a browser User-Agent. Some Japanese sites (Cloudflare in front of
    herzleben.co.jp, for example) answer 403 to the default Python client and
    200 to a browser. The response body is decoded from the declared charset
    and is not stored by the lead store.
    """
    outcome = FetchOutcome(False, "ページを取得できませんでした。")
    for _attempt in range(2):
        outcome = _fetch_once(url, timeout, allow_private)
        if outcome.ok or not outcome.transient:
            return outcome
    return outcome


def _validate(company, source_url, signal, lead_type):
    if lead_type not in {"buyer", "partner"}:
        raise ValueError("lead_type は buyer か partner です")
    if not company or len(company) > 255:
        raise ValueError("会社名は1〜255文字です")
    if not source_url or len(source_url) > 2048:
        raise ValueError("URLは1〜2048文字です")
    if not signal or len(signal) > 500:
        raise ValueError("根拠は1〜500文字です")


@dataclass
class UrlMerge:
    canonical_url: str
    keep_lead_id: str
    drop_lead_ids: list[str]
    move_run_ids: list[str]
    latest_run_id: str
    reason: str


@dataclass
class CompanyMerge:
    normalized_name: str
    keep_company_id: str
    keep_name: str
    drop_company_ids: list[str]
    repoint_lead_ids: list[str]


@dataclass
class DatabasePlan:
    url_merges: list[UrlMerge] = field(default_factory=list)
    company_merges: list[CompanyMerge] = field(default_factory=list)
    empty_company_ids: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def empty(self):
        return not self.url_merges and not self.company_merges and not self.empty_company_ids


class LeadStore:
    def __init__(self, db_path, fetcher=None):
        self.db_path = Path(db_path)
        self.fetcher = fetcher or fetch_page

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _transaction(self):
        connection = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

    def ensure_schema(self):
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.executescript(SCHEMA)
            connection.commit()
        finally:
            connection.close()

    def recover_interrupted(self):
        with self._transaction() as db:
            if not _has_lead_tables(db):
                return
            db.execute("""
                UPDATE runs SET processing_status = 'fetch_error',
                    error = '取得が中断されました。もう一度試せます。'
                WHERE processing_status = 'processing'
            """)

    def submit(self, company, source_url, signal, lead_type):
        company = company.strip()
        source_url = source_url.strip()
        signal = signal.strip()
        lead_type = lead_type.strip()
        _validate(company, source_url, signal, lead_type)
        canonical = canonical_url(source_url)
        fetch_url = fetchable_url(source_url)
        if _obviously_nonpublic(fetch_url):
            raise ValueError("公開のWebページのURLを指定してください。")
        normalized = normalize_company_name(company)
        if not normalized:
            raise ValueError("会社名を確認してください")
        reserved = None
        for _attempt in range(3):
            try:
                reserved = self._reserve(company, canonical, fetch_url, normalized, signal, lead_type)
                break
            except sqlite3.IntegrityError:
                continue
        if reserved is None:
            raise ValueError("保存できませんでした。もう一度試してください")
        status = reserved["status"]
        if reserved["fetch"]:
            self._complete(reserved["run_id"], self._run_fetch(fetch_url))
            status = self.status(reserved["run_id"])["status"]
        return {
            "run_id": reserved["run_id"],
            "lead_id": reserved["lead_id"],
            "company_id": reserved["company_id"],
            "reused": not reserved["fetch"],
            "status": status,
            "canonical_url": canonical,
        }

    def retry(self, run_id):
        with self._transaction() as db:
            run = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            latest = _latest_run(db, run["lead_id"])
            if latest["id"] != run["id"] or run["processing_status"] != "fetch_error":
                raise ValueError("この処理は再試行できません。")
            lead = db.execute("SELECT * FROM leads WHERE id = ?", (run["lead_id"],)).fetchone()
            new_id = str(uuid4())
            now = _now()
            db.execute("""
                INSERT INTO runs (id, lead_id, source_url, canonical_url, fetch_url,
                    processing_status, error, submitted_at)
                VALUES (?, ?, ?, ?, ?, 'processing', '', ?)
            """, (new_id, lead["id"], lead["canonical_url"], lead["canonical_url"], run["fetch_url"], now))
            db.execute(
                "UPDATE leads SET latest_run_id = ?, updated_at = ? WHERE id = ?",
                (new_id, now, lead["id"]),
            )
            fetch_url = run["fetch_url"]
            lead_id = lead["id"]
            company_id = lead["company_id"]
            canonical = lead["canonical_url"]
        self._complete(new_id, self._run_fetch(fetch_url))
        return {
            "run_id": new_id,
            "lead_id": lead_id,
            "company_id": company_id,
            "reused": False,
            "status": self.status(new_id)["status"],
            "canonical_url": canonical,
        }

    def status(self, run_id):
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT id, processing_status FROM runs WHERE id = ?", (run_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError(run_id)
        return {"run_id": row["id"], "status": row["processing_status"]}

    def plan(self, read_only=False):
        if read_only:
            connection = sqlite3.connect(f"file:{self.db_path.resolve().as_posix()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
        else:
            connection = self._connect()
        try:
            if not _has_lead_tables(connection):
                return DatabasePlan(note="このデータベースにリードの表はありません。")
            return _build_plan(connection)
        finally:
            connection.close()

    def apply(self, plan):
        if plan.empty:
            return
        now = _now()
        with self._transaction() as db:
            for merge in plan.url_merges:
                for lead_id in merge.drop_lead_ids:
                    db.execute("UPDATE runs SET lead_id = ? WHERE lead_id = ?", (merge.keep_lead_id, lead_id))
                    db.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
                db.execute(
                    """UPDATE leads SET source_url = ?, canonical_url = ?, latest_run_id = ?, updated_at = ?
                       WHERE id = ?""",
                    (merge.canonical_url, merge.canonical_url, merge.latest_run_id, now, merge.keep_lead_id),
                )
            for merge in plan.company_merges:
                for company_id in merge.drop_company_ids:
                    db.execute(
                        "UPDATE leads SET company_id = ?, updated_at = ? WHERE company_id = ?",
                        (merge.keep_company_id, now, company_id),
                    )
                    db.execute("DELETE FROM companies WHERE id = ?", (company_id,))
                db.execute(
                    "UPDATE companies SET normalized_name = ?, updated_at = ? WHERE id = ?",
                    (merge.normalized_name, now, merge.keep_company_id),
                )
            for company_id in plan.empty_company_ids:
                db.execute(
                    """DELETE FROM companies WHERE id = ?
                       AND NOT EXISTS (SELECT 1 FROM leads WHERE company_id = companies.id)""",
                    (company_id,),
                )

    def _reserve(self, company, canonical, fetch_url, normalized, signal, lead_type):
        now = _now()
        with self._transaction() as db:
            lead = db.execute("SELECT * FROM leads WHERE canonical_url = ?", (canonical,)).fetchone()
            if lead is not None:
                latest = _latest_run(db, lead["id"])
                if latest is not None and latest["processing_status"] != "fetch_error":
                    db.execute(
                        "UPDATE leads SET signal = ?, lead_type = ?, updated_at = ? WHERE id = ?",
                        (signal, lead_type, now, lead["id"]),
                    )
                    return {
                        "fetch": False, "run_id": latest["id"], "lead_id": lead["id"],
                        "company_id": lead["company_id"], "status": latest["processing_status"],
                    }
                run_id = str(uuid4())
                _insert_run(db, run_id, lead["id"], canonical, fetch_url, now)
                db.execute(
                    """UPDATE leads SET latest_run_id = ?, signal = ?, lead_type = ?, updated_at = ?
                       WHERE id = ?""",
                    (run_id, signal, lead_type, now, lead["id"]),
                )
                return {
                    "fetch": True, "run_id": run_id, "lead_id": lead["id"],
                    "company_id": lead["company_id"], "status": "processing",
                }
            company_id = _upsert_company(db, company, normalized, now)
            lead_id = str(uuid4())
            run_id = str(uuid4())
            db.execute("""
                INSERT INTO leads (id, company_id, company_name, source_url, canonical_url,
                    signal, lead_type, status, latest_run_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)
            """, (lead_id, company_id, company, canonical, canonical, signal, lead_type, run_id, now, now))
            _insert_run(db, run_id, lead_id, canonical, fetch_url, now)
            return {
                "fetch": True, "run_id": run_id, "lead_id": lead_id,
                "company_id": company_id, "status": "processing",
            }

    def _run_fetch(self, url):
        try:
            return self.fetcher(url)
        except Exception as error:
            logger.warning("Lead fetch failed (%s)", type(error).__name__)
            return FetchOutcome(False, "ページを取得できませんでした。")

    def _complete(self, run_id, outcome):
        status = "review" if outcome.ok else "fetch_error"
        error = "" if outcome.ok else (outcome.error or "ページを取得できませんでした。")[:300]
        with self._transaction() as db:
            db.execute(
                """UPDATE runs SET processing_status = ?, error = ?
                   WHERE id = ? AND processing_status = 'processing'""",
                (status, error, run_id),
            )


def _insert_run(db, run_id, lead_id, canonical, fetch_url, now):
    db.execute("""
        INSERT INTO runs (id, lead_id, source_url, canonical_url, fetch_url,
            processing_status, error, submitted_at)
        VALUES (?, ?, ?, ?, ?, 'processing', '', ?)
    """, (run_id, lead_id, canonical, canonical, fetch_url, now))


def _upsert_company(db, display_name, normalized, now):
    row = db.execute(
        "SELECT id FROM companies WHERE normalized_name = ?", (normalized,),
    ).fetchone()
    if row is not None:
        db.execute("UPDATE companies SET updated_at = ? WHERE id = ?", (now, row["id"]))
        return row["id"]
    company_id = str(uuid4())
    db.execute(
        "INSERT INTO companies (id, name, normalized_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (company_id, display_name, normalized, now, now),
    )
    return company_id


def _latest_run(db, lead_id):
    return db.execute(
        """SELECT * FROM runs WHERE lead_id = ?
           ORDER BY submitted_at DESC, rowid DESC LIMIT 1""",
        (lead_id,),
    ).fetchone()


def _has_lead_tables(db):
    rows = db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    names = {row["name"] if isinstance(row, sqlite3.Row) else row[0] for row in rows}
    return {"companies", "leads", "runs"} <= names


def _best_run(runs):
    return max(runs, key=lambda run: (
        STATUS_RANK.get(run["processing_status"], 0), run["submitted_at"], run["rowid"],
    ))


def _build_plan(db):
    leads = [dict(row) for row in db.execute("SELECT * FROM leads ORDER BY created_at, id")]
    companies = [dict(row) for row in db.execute("SELECT * FROM companies ORDER BY created_at, id")]
    runs = [dict(row) for row in db.execute("SELECT rowid AS rowid, * FROM runs")]
    runs_by_lead = {}
    for run in runs:
        runs_by_lead.setdefault(run["lead_id"], []).append(run)
    groups = {}
    for lead in leads:
        try:
            key = canonical_url(lead["source_url"])
        except ValueError:
            key = lead["canonical_url"]
        groups.setdefault(key, []).append(lead)

    url_merges = []
    dropped = set()
    for key in sorted(groups):
        grouped = groups[key]
        if len(grouped) < 2:
            continue
        def rank(lead):
            lead_runs = runs_by_lead.get(lead["id"], [])
            best = STATUS_RANK.get(_best_run(lead_runs)["processing_status"], 0) if lead_runs else 0
            return (-best, lead["created_at"], lead["id"])
        keeper = min(grouped, key=rank)
        drop_ids = [lead["id"] for lead in grouped if lead["id"] != keeper["id"]]
        moved = [run["id"] for lead_id in drop_ids for run in runs_by_lead.get(lead_id, [])]
        combined = [run for lead in grouped for run in runs_by_lead.get(lead["id"], [])]
        latest = _best_run(combined)["id"] if combined else keeper.get("latest_run_id")
        best_status = _best_run(combined)["processing_status"] if combined else "なし"
        url_merges.append(UrlMerge(
            canonical_url=key,
            keep_lead_id=keeper["id"],
            drop_lead_ids=drop_ids,
            move_run_ids=moved,
            latest_run_id=latest,
            reason=f"同じ正規化URL。残すリードは状態 {best_status} の最も古いレコード。",
        ))
        dropped.update(drop_ids)

    surviving_company_counts = {}
    for lead in leads:
        if lead["id"] in dropped or not lead["company_id"]:
            continue
        surviving_company_counts[lead["company_id"]] = surviving_company_counts.get(lead["company_id"], 0) + 1
    by_name = {}
    for company in companies:
        key = normalize_company_name(company["name"])
        if not key:
            continue
        by_name.setdefault(key, []).append(company)
    company_merges = []
    merged_away = set()
    for key in sorted(by_name):
        grouped = by_name[key]
        if len(grouped) < 2:
            continue
        with_leads = [company for company in grouped if surviving_company_counts.get(company["id"])]
        pool = with_leads or grouped
        keeper = min(pool, key=lambda company: (company["created_at"], company["id"]))
        drop_ids = [company["id"] for company in grouped if company["id"] != keeper["id"]]
        repoint = [
            lead["id"] for lead in leads
            if lead["id"] not in dropped and lead["company_id"] in drop_ids
        ]
        company_merges.append(CompanyMerge(
            normalized_name=key, keep_company_id=keeper["id"], keep_name=keeper["name"],
            drop_company_ids=drop_ids, repoint_lead_ids=repoint,
        ))
        merged_away.update(drop_ids)

    emptied = []
    for company in companies:
        if company["id"] in merged_away:
            continue
        had_leads = any(lead["company_id"] == company["id"] for lead in leads)
        survives = surviving_company_counts.get(company["id"], 0) > 0
        if had_leads and not survives:
            emptied.append(company["id"])
    emptied.sort()
    return DatabasePlan(url_merges=url_merges, company_merges=company_merges, empty_company_ids=emptied)


def render_plan(plan):
    lines = ["Meishi lead dedup review", plan.note or "既存データは変更しません。"]
    lines.append(f"同じページとしてまとめるリード: {len(plan.url_merges)}")
    for merge in plan.url_merges:
        lines.append(f"- {merge.canonical_url}")
        lines.append(f"  残す lead {merge.keep_lead_id}（latest run {merge.latest_run_id}）")
        lines.append(f"  まとめる lead: {', '.join(merge.drop_lead_ids) or 'なし'}")
        lines.append(f"  移動する run: {', '.join(merge.move_run_ids) or 'なし'}")
        lines.append(f"  {merge.reason}")
    lines.append(f"同じ組織としてまとめる会社: {len(plan.company_merges)}")
    for merge in plan.company_merges:
        lines.append(f"- {merge.normalized_name}: 残す {merge.keep_name} ({merge.keep_company_id})")
        lines.append(f"  まとめる company: {', '.join(merge.drop_company_ids)}")
        lines.append(
            "  リードは削除しない。company_id を付け替える lead: "
            + (", ".join(merge.repoint_lead_ids) or "なし")
        )
    lines.append(f"リードがなくなる会社: {len(plan.empty_company_ids)}")
    for company_id in plan.empty_company_ids:
        lines.append(f"- {company_id}")
    if plan.empty and not plan.note:
        lines.append("重複はありません。")
    return "\n".join(lines)


def report_submissions(rows):
    """Review an export. Does not decide to drop a distinct page for the same organization."""
    url_groups = {}
    org_groups = {}
    skipped = 0
    for index, row in enumerate(rows):
        source = (row.get("source_url") or "").strip()
        company = (row.get("company") or "").strip()
        if not source or not company:
            skipped += 1
            continue
        try:
            canonical = canonical_url(source)
        except ValueError:
            skipped += 1
            continue
        item = dict(row)
        item["_index"] = index
        url_groups.setdefault(canonical, []).append(item)
        normalized = normalize_company_name(company)
        if normalized:
            org_groups.setdefault(normalized, []).append((canonical, item))
    duplicate_urls = {key: group for key, group in url_groups.items() if len(group) > 1}
    shared_orgs = {}
    for key, entries in org_groups.items():
        pages = {canonical for canonical, _row in entries}
        names = {(row.get("company") or "").strip() for _canonical, row in entries}
        if len(pages) > 1 or len(names) > 1 or len(entries) > 1:
            shared_orgs[key] = entries
    lines = [
        "Meishi submission review (no data modified)",
        f"同じページの重複: {len(duplicate_urls)}",
        f"同じ組織（ページは残す）: {sum(1 for key, entries in shared_orgs.items() if len({c for c, _ in entries}) > 1)}",
        f"読み飛ばした行: {skipped}",
    ]
    for canonical in sorted(duplicate_urls):
        group = duplicate_urls[canonical]
        def sort_key(row):
            return (-STATUS_RANK.get((row.get("status") or "").strip(), 0), row.get("_index", 0))
        keeper = min(group, key=sort_key)
        lines.append(f"- ページ {canonical} を1件にまとめる。残す run: {keeper.get('run_id') or '（先頭）'} 状態 {keeper.get('status') or '不明'}")
        for row in group:
            lines.append(
                f"  run {row.get('run_id') or '-'} status={row.get('status') or '-'} "
                f"company={row.get('company')} url={row.get('source_url')}"
            )
    for key in sorted(shared_orgs):
        entries = shared_orgs[key]
        pages = sorted({canonical for canonical, _row in entries})
        if len(pages) < 2:
            continue
        names = sorted({(row.get("company") or "").strip() for _canonical, row in entries})
        lines.append(f"- 組織 {key}: 会社は1件にまとめ、ページは{len(pages)}件とも残す")
        lines.append("  名前: " + " | ".join(names))
        for page in pages:
            lines.append(f"  ページ: {page}")
    if not duplicate_urls and not any(len({c for c, _r in entries}) > 1 for entries in shared_orgs.values()):
        lines.append("この一覧に、まとめる重複はありません。")
    return "\n".join(lines)


def load_submission_csv(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))
