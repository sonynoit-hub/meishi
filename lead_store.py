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
import io
import ipaddress
import json
import logging
import posixpath
import re
import socket
import sqlite3
import unicodedata
import urllib.error
import urllib.request
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import date as calendar_date, datetime, timezone
from itertools import islice
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

logger = logging.getLogger(__name__)

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_BODY_BYTES = 3 * 1024 * 1024
LEAD_STATUSES = ("new", "contacted", "replied", "meeting", "won", "lost", "bounced", "unsubscribed")
LEAD_STATUS_COLUMN = f"TEXT NOT NULL DEFAULT 'new' CHECK ({{column}} IN {LEAD_STATUSES})"
AUTO_SUPPRESSION = {"bounced", "unsubscribed"}
OUTCOME_STATUSES = {"replied", "won", "lost"} | AUTO_SUPPRESSION
STOP_FOLLOWUP = OUTCOME_STATUSES | {"meeting"}
CRM_COLUMNS = {
    "email": "TEXT",
    "email_source_url": "TEXT",
    "email_checked_at": "TEXT",
    "email_evidence": "TEXT",
    "do_not_contact": "INTEGER NOT NULL DEFAULT 0",
    "sequence_step": "INTEGER NOT NULL DEFAULT 0",
    "next_followup_at": "TEXT",
    "segment": "TEXT",
    "batch_id": "TEXT",
    "tags": "TEXT",
}
LEAD_CSV_COLUMNS = (
    "id", "company_id", "company_name", "company", "source_url", "canonical_url",
    "signal", "lead_type", "status", *CRM_COLUMNS, "suppressed", "last_contacted_at",
    "last_contact_status", "last_contact_channel", "last_contact_to_address",
    "last_contact_subject", "last_contact_note",
)
RUN_STATUSES = ("processing", "review", "added", "fetch_error")
STATUS_RANK = {"added": 4, "review": 3, "processing": 2, "fetch_error": 1}
TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}
TRACKING_QUERY_KEYS = {"gclid", "fbclid", "yclid", "mc_cid", "mc_eid"}
PERCENT_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")
UNRESERVED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
LATEST_RUN_ORDER = ("submitted_at", "id")
LATEST_RUN_SQL = ", ".join(f"{column} DESC" for column in LATEST_RUN_ORDER)
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

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS companies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    company_id TEXT REFERENCES companies(id),
    company_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    signal TEXT NOT NULL,
    lead_type TEXT NOT NULL CHECK (lead_type IN ('buyer', 'partner')),
    status {LEAD_STATUS_COLUMN.format(column="status")},
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
CREATE TABLE IF NOT EXISTS contact_logs (
    id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL REFERENCES leads(id),
    status {LEAD_STATUS_COLUMN.format(column="status")},
    contacted_on TEXT NOT NULL,
    channel TEXT NOT NULL,
    to_address TEXT NOT NULL,
    from_address TEXT NOT NULL,
    subject TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (lead_id, contacted_on, channel, to_address)
);
CREATE TABLE IF NOT EXISTS suppressions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('email', 'domain')),
    value TEXT NOT NULL,
    reason TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    removed_at TEXT,
    UNIQUE (kind, value)
);
CREATE TABLE IF NOT EXISTS lead_aliases (
    canonical_url TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL REFERENCES leads(id)
);
"""

ADDITIVE_COLUMNS = {
    "companies": {"normalized_name": "TEXT"},
    "leads": {
        "company_id": "TEXT REFERENCES companies(id)",
        "canonical_url": "TEXT",
        "status": LEAD_STATUS_COLUMN.format(column="status"),
        "last_contacted_at": "TEXT",
        "created_via": "TEXT NOT NULL DEFAULT 'submit' CHECK (created_via IN ('submit', 'status_update'))",
        "latest_run_id": "TEXT",
        **CRM_COLUMNS,
    },
    "contact_logs": {"sequence_step": "INTEGER NOT NULL DEFAULT 0", "batch_id": "TEXT"},
    "runs": {
        "canonical_url": "TEXT",
        "fetch_url": "TEXT",
        "error": "TEXT NOT NULL DEFAULT ''",
        "submitted_company": "TEXT",
        "submitted_signal": "TEXT",
        "submitted_lead_type": "TEXT",
        "resubmissions": "TEXT NOT NULL DEFAULT '[]'",
        "redirect_canonical_url": "TEXT",
        "original_source_url": "TEXT",
        "original_canonical_url": "TEXT",
        "original_fetch_url": "TEXT",
    },
}


def _normalize_escapes(value):
    def replace(match):
        escape = match[0]
        character = chr(int(escape[1:], 16))
        return character if character in UNRESERVED else escape.upper()

    return PERCENT_ESCAPE.sub(replace, value)


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


class SuppressedLeadError(ValueError):
    pass


def _domain(value):
    try:
        domain = value.strip().rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("ドメインを確認してください") from None
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
               for label in domain.split(".")):
        raise ValueError("ドメインを確認してください")
    return domain


def _email(value):
    if not isinstance(value, str):
        raise ValueError("メールアドレスを確認してください")
    local, separator, domain = value.strip().partition("@")
    if (not separator or not re.fullmatch(r"[^\s@<>(),;:\\]+", local)
            or re.search(r"\s", domain) or "." not in domain.rstrip(".")):
        raise ValueError("メールアドレスを確認してください")
    return f"{local}@{_domain(domain)}"


def _iso_date(value, field):
    try:
        if calendar_date.fromisoformat(value).isoformat() == value:
            return value
    except (TypeError, ValueError):
        pass
    raise ValueError(f"{field} は YYYY-MM-DD 形式です")


def _crm_updates(fields):
    for key, value in fields.items():
        if key not in CRM_COLUMNS:
            raise ValueError(f"不明なフィールド: {key}")
        if key == "sequence_step":
            if type(value) is not int or value < 0 or value > 2**63 - 1:
                raise ValueError("sequence_step は0以上の整数です")
        elif key == "do_not_contact":
            if type(value) is not bool:
                raise ValueError("do_not_contact は真偽値です")
        elif value is not None and not isinstance(value, str):
            raise ValueError(f"{key} は文字列です")
    if "email" in fields:
        fields["email"] = _email(fields["email"]) if fields["email"] is not None else None
    if fields.get("next_followup_at") is not None:
        _iso_date(fields["next_followup_at"], "next_followup_at")
    return fields


def _suppression_keys(address):
    if not address:
        return []
    try:
        normalized = _email(address).lower()
    except ValueError:
        return []
    labels = normalized.split("@", 1)[1].split(".")
    return [("email", normalized), *(
        ("domain", ".".join(labels[index:])) for index in range(len(labels))
    )]


def _active_suppressions(db):
    return {tuple(row) for row in db.execute(
        "SELECT kind, value FROM suppressions WHERE removed_at IS NULL"
    )}


def _add_suppression(db, kind, value, reason, note, source):
    return dict(db.execute("""
        INSERT INTO suppressions (id, kind, value, reason, note, created_at, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (kind, value) DO UPDATE SET reason = excluded.reason,
            note = excluded.note, source = excluded.source, removed_at = NULL
        RETURNING *
    """, (str(uuid4()), kind, value, reason, note, _now(), source)).fetchone())


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
    scheme = parts.scheme.lower()
    default_port = 80 if scheme == "http" else 443
    port_text = "" if port in (None, default_port) else f":{port}"
    netloc = f"[{host}]{port_text}" if ":" in host else f"{host}{port_text}"
    path = posixpath.normpath(_normalize_escapes(parts.path)).rstrip("/") if parts.path else ""
    query = []
    for parameter in parts.query.split("&"):
        parameter = _normalize_escapes(parameter)
        key = parameter.partition("=")[0].lower()
        if key not in TRACKING_QUERY_KEYS and not key.startswith("utm_"):
            query.append(parameter)
    # Keep http:443 explicit so a canonical key can be canonicalized again.
    canonical_scheme = "http" if scheme == "http" and port == 443 else "https"
    canonical = urlunsplit((canonical_scheme, netloc, path, "&".join(query), ""))
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
    latest_run_id: str | None
    reason: str
    aliases: list[str]


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


def _guard_outreach(db, lead, fields, address="", status=None):
    if status in OUTCOME_STATUSES:
        return
    if status != "contacted" and fields.get("next_followup_at") is None and "sequence_step" not in fields:
        return
    if lead["do_not_contact"] or fields.get("do_not_contact"):
        raise SuppressedLeadError("このリードは連絡禁止です")
    keys = (_suppression_keys(address) + _suppression_keys(lead["email"])
            + _suppression_keys(fields.get("email")))
    if not address:
        keys += _suppression_keys(_last_recipient(db, lead["id"]))
    if _active_suppressions(db).intersection(keys):
        raise SuppressedLeadError("この宛先は抑止リストに登録されています")


def _update_lead_fields(db, lead_id, fields):
    db.execute(
        f"UPDATE leads SET {', '.join(f'{column} = ?' for column in fields)} WHERE id = ?",
        (*fields.values(), lead_id),
    )


def _last_contact(db, lead_id):
    return db.execute("""
        SELECT * FROM contact_logs WHERE lead_id = ?
        ORDER BY contacted_on DESC, created_at DESC, id DESC LIMIT 1
    """, (lead_id,)).fetchone()


def _last_recipient(db, lead_id):
    row = db.execute("""
        SELECT to_address FROM contact_logs
        WHERE lead_id = ? AND instr(to_address, '@') > 0
        ORDER BY contacted_on DESC, created_at DESC, id DESC LIMIT 1
    """, (lead_id,)).fetchone()
    return row["to_address"] if row else None


def _contact_result(row, status_column):
    result = dict(row)
    result["status"] = result.pop(status_column)
    result.pop("contact_status", None)
    return result


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
        with self._transaction() as db:
            _ensure_columns(db)
            return _backfill_keys(db)

    def update_status(self, record_id=None, company=None, source_url=None, *,
                      status, date, channel, to_address="", from_address="", subject="", note="", **fields):
        if status not in LEAD_STATUSES:
            raise ValueError("status を確認してください")
        date = _iso_date(date, "date")
        fields = _crm_updates(fields)
        channel = channel.strip().lower()
        if not channel:
            raise ValueError("channel を指定してください")
        to_address = to_address.strip()
        company = company.strip() if company is not None else None
        normalized = normalize_company_name(company) if company is not None else None
        if company is not None and (not normalized or len(company) > 255):
            raise ValueError("会社名を確認してください")
        canonical = canonical_url(source_url) if source_url is not None else None
        if not (record_id or normalized or canonical):
            raise ValueError("record_id、company、source_url のいずれかを指定してください")

        now = _now()
        with self._transaction() as db:
            lead = None
            if record_id:
                lead = db.execute("""
                    SELECT leads.* FROM leads WHERE id = ? OR id = (SELECT lead_id FROM runs WHERE id = ?)
                    ORDER BY id = ? DESC LIMIT 1
                """, (record_id, record_id, record_id)).fetchone()
                if lead is None:
                    raise KeyError(record_id)
            else:
                if canonical:
                    lead = _lead_for_url(db, canonical)
                if lead is None and normalized:
                    matches = _company_leads(db, normalized)
                    lead = matches[0] if matches else None
            created = lead is None
            if created:
                company = company or urlsplit(canonical).hostname
                company_id = _upsert_company(db, company, normalized or normalize_company_name(company) or company, now)
                lead_id = str(uuid4())
                db.execute("""
                    INSERT INTO leads (id, company_id, company_name, source_url, canonical_url,
                        signal, lead_type, status, created_at, updated_at, created_via)
                    VALUES (?, ?, ?, ?, ?, '', 'partner', 'new', ?, ?, 'status_update')
                """, (lead_id, company_id, company, canonical or "", canonical or f"urn:meishi:lead:{lead_id}", now, now))
            else:
                lead_id, company = lead["id"], lead["company_name"]

            if created:
                lead = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            _guard_outreach(db, lead, fields, to_address, status)
            if status in STOP_FOLLOWUP:
                fields["next_followup_at"] = None
            if status in AUTO_SUPPRESSION:
                keys = (_suppression_keys(to_address) or _suppression_keys(fields.get("email"))
                        or _suppression_keys(lead["email"]))
                if not keys:
                    keys = _suppression_keys(_last_recipient(db, lead_id))
                if keys:
                    _add_suppression(db, "email", keys[0][1], status, note, "status_update")

            log_id = str(uuid4())
            key = (lead_id, date, channel, to_address)
            log_status_column = _lead_status_column(db, "contact_logs")
            db.execute(f"""
                INSERT INTO contact_logs (id, lead_id, contacted_on, channel, to_address,
                    {log_status_column}, from_address, subject, note, created_at, sequence_step, batch_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (lead_id, contacted_on, channel, to_address) DO NOTHING
            """, (log_id, *key, status, from_address, subject, note, now,
                  fields.get("sequence_step", lead["sequence_step"]), fields.get("batch_id", lead["batch_id"])))
            entry = db.execute("""
                SELECT * FROM contact_logs WHERE lead_id = ? AND contacted_on = ?
                    AND channel = ? AND to_address = ?
            """, key).fetchone()
            status_column = _lead_status_column(db)
            last_contacted = max(lead["last_contacted_at"] or "", date)
            if fields or lead[status_column] != status or lead["last_contacted_at"] != last_contacted:
                _update_lead_fields(db, lead_id, dict(fields, **{
                    status_column: status, "updated_at": now, "last_contacted_at": last_contacted,
                }))
            return {"lead_id": lead_id, "company": company, "status": status, "created": created,
                    "log": _contact_result(entry, log_status_column), "deduped": entry["id"] != log_id}

    def update_lead(self, lead_id, **fields):
        fields = _crm_updates(fields)
        with self._transaction() as db:
            lead = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if lead is None:
                raise KeyError(lead_id)
            _guard_outreach(db, lead, fields)
            if fields:
                _update_lead_fields(db, lead_id, dict(fields, updated_at=_now()))
            return self._lead_result(db, lead_id, _active_suppressions(db))

    def add_suppression(self, kind, value, reason, note="", source=""):
        if not isinstance(value, str):
            raise ValueError("value を確認してください")
        if kind == "email":
            value = _email(value).lower()
        elif kind == "domain":
            value = _domain(value)
        else:
            raise ValueError("kind は email か domain です")
        reason = reason.strip()
        if not reason:
            raise ValueError("reason を指定してください")
        with self._transaction() as db:
            return _add_suppression(db, kind, value, reason, note, source)

    def list_suppressions(self, include_removed=False):
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute("""
                SELECT * FROM suppressions WHERE removed_at IS NULL OR ?
                ORDER BY created_at DESC, id
            """, (include_removed,))]

    def remove_suppression(self, suppression_id):
        with self._transaction() as db:
            row = db.execute("""
                UPDATE suppressions SET removed_at = COALESCE(removed_at, ?) WHERE id = ?
                RETURNING *
            """, (_now(), suppression_id)).fetchone()
            if row is None:
                raise KeyError(suppression_id)
            return dict(row)

    def is_suppressed(self, address):
        keys = _suppression_keys(_email(address))
        with closing(self._connect()) as db:
            return bool(_active_suppressions(db).intersection(keys))

    def _lead_result(self, db, lead_id, suppressions):
        row = db.execute("""
            SELECT leads.*, COALESCE(companies.name, leads.company_name) AS company
            FROM leads LEFT JOIN companies ON companies.id = leads.company_id WHERE leads.id = ?
        """, (lead_id,)).fetchone()
        result = _contact_result(row, _lead_status_column(db))
        result["do_not_contact"] = bool(result["do_not_contact"])
        last = _last_contact(db, lead_id)
        result["last_contact"] = _contact_result(last, _lead_status_column(db, "contact_logs")) if last else None
        result["suppressed"] = result["do_not_contact"] or bool(suppressions.intersection(
            _suppression_keys(result["email"]) + _suppression_keys(_last_recipient(db, lead_id))
        ))
        return result

    def list_leads(self, *, status=None, segment=None, batch_id=None, due_on_or_before=None,
                   include_suppressed=False, limit=None, offset=0):
        if status is not None and status not in LEAD_STATUSES:
            raise ValueError("status を確認してください")
        if offset < 0 or (limit is not None and limit < 0):
            raise ValueError("limit と offset は 0 以上です")
        if due_on_or_before is not None:
            _iso_date(due_on_or_before, "due_on_or_before")
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            filters = ((_lead_status_column(db), "=", status), ("segment", "=", segment),
                       ("batch_id", "=", batch_id), ("next_followup_at", "<=", due_on_or_before))
            conditions = [f"{column} {operator} ?" for column, operator, value in filters if value is not None]
            where = " AND ".join(conditions) or "1"
            ids = db.execute(f"SELECT id FROM leads WHERE {where} ORDER BY created_at, id",
                             [value for _, _, value in filters if value is not None])
            suppressions = _active_suppressions(db)
            leads = (self._lead_result(db, row["id"], suppressions) for row in ids)
            visible = (lead for lead in leads if include_suppressed or not lead["suppressed"])
            return list(islice(visible, offset, None if limit is None else offset + limit))

    def export_leads_csv(self, **filters):
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=LEAD_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for lead in self.list_leads(**filters):
            last = lead["last_contact"] or {}
            writer.writerow(dict(lead, **{f"last_contact_{key}": last.get(key)
                                         for key in ("status", "channel", "to_address", "subject", "note")}))
        return output.getvalue().encode("utf-8-sig")

    def contact_log(self, lead_id=None, company=None):
        with closing(self._connect()) as db:
            if lead_id:
                matches = db.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchall()
            elif company is not None and (normalized := normalize_company_name(company)):
                matches = _company_leads(db, normalized)
            else:
                raise ValueError("lead_id または company を指定してください")
            if not matches:
                raise KeyError(lead_id or company)
            status_column = _lead_status_column(db, "contact_logs")
            return [_contact_result(row, status_column) for row in db.execute(f"""
                SELECT * FROM contact_logs WHERE lead_id IN ({",".join("?" for _ in matches)})
                ORDER BY contacted_on, created_at, id
            """, [row["id"] for row in matches])]

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
            "differing_fields": reserved["differing_fields"],
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
            _insert_run(
                db, new_id, lead["id"], run["canonical_url"], run["fetch_url"], now,
                (run["submitted_company"], run["submitted_signal"], run["submitted_lead_type"]),
                source_url=run["source_url"],
            )
            _update_latest_run(db, lead["id"], now)
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
            connection = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
        else:
            connection = self._connect()
        try:
            connection.execute("BEGIN")
            if not _has_lead_tables(connection):
                return DatabasePlan(note="このデータベースにリードの表はありません。")
            return _build_plan(connection)
        finally:
            connection.close()

    def apply(self, plan=None):
        # A previously displayed plan may be stale by the time we acquire the lock.
        with self._transaction() as db:
            if not _has_lead_tables(db):
                return DatabasePlan(note="このデータベースにリードの表はありません。")
            _ensure_columns(db)
            plan = _build_plan(db)
            now = _now()
            for merge in plan.url_merges:
                for lead_id in merge.drop_lead_ids:
                    db.execute("""
                        UPDATE runs SET lead_id = ?,
                            original_source_url = COALESCE(original_source_url, source_url),
                            original_canonical_url = COALESCE(original_canonical_url, canonical_url),
                            original_fetch_url = COALESCE(original_fetch_url, fetch_url)
                        WHERE lead_id = ?
                    """, (merge.keep_lead_id, lead_id))
                    db.execute("UPDATE lead_aliases SET lead_id = ? WHERE lead_id = ?",
                               (merge.keep_lead_id, lead_id))
                    _merge_contact_logs(db, merge.keep_lead_id, lead_id)
                    db.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
                db.executemany("""
                    INSERT INTO lead_aliases (canonical_url, lead_id) VALUES (?, ?)
                    ON CONFLICT(canonical_url) DO NOTHING
                """, [(key, merge.keep_lead_id) for key in merge.aliases])
                _update_latest_run(db, merge.keep_lead_id, now)
            for merge in plan.company_merges:
                for company_id in merge.drop_company_ids:
                    db.execute(
                        "UPDATE leads SET company_id = ?, updated_at = ? WHERE company_id = ?",
                        (merge.keep_company_id, now, company_id),
                    )
                    db.execute("DELETE FROM companies WHERE id = ?", (company_id,))
            for company_id in plan.empty_company_ids:
                db.execute(
                    """DELETE FROM companies WHERE id = ?
                       AND NOT EXISTS (SELECT 1 FROM leads WHERE company_id = companies.id)""",
                    (company_id,),
                )
            _backfill_keys(db)
            return plan

    def _reserve(self, company, canonical, fetch_url, normalized, signal, lead_type):
        now = _now()
        submitted = {"company": company, "signal": signal, "lead_type": lead_type}
        with self._transaction() as db:
            lead = _lead_for_url(db, canonical)
            differing_fields = []
            if lead is None:
                company_id = _upsert_company(db, company, normalized, now)
                lead_id = str(uuid4())
                db.execute("""
                    INSERT INTO leads (id, company_id, company_name, source_url, canonical_url,
                        signal, lead_type, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
                """, (lead_id, company_id, company, fetch_url, canonical, signal, lead_type, now, now))
                latest = None
            else:
                lead_id, company_id = lead["id"], lead["company_id"]
                original = {"company": lead["company_name"], "signal": lead["signal"],
                            "lead_type": lead["lead_type"]}
                differing_fields = [key for key in submitted if submitted[key] != original[key]]
                latest = _latest_run(db, lead_id)

            fetch = latest is None or latest["processing_status"] == "fetch_error"
            if fetch:
                run_id = str(uuid4())
                _insert_run(db, run_id, lead_id, canonical, fetch_url, now, (company, signal, lead_type))
                status = "processing"
            else:
                run_id, status = latest["id"], latest["processing_status"]
                history = json.loads(latest["resubmissions"])
                history.append({**submitted, "submitted_at": now, "source_url": fetch_url})
                db.execute("UPDATE runs SET resubmissions = ? WHERE id = ?",
                           (json.dumps(history, ensure_ascii=False), run_id))
            _update_latest_run(db, lead_id, now)
            return {
                "fetch": fetch, "run_id": run_id, "lead_id": lead_id,
                "company_id": company_id, "status": status, "differing_fields": differing_fields,
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
        redirect = None
        if outcome.ok and outcome.final_url:
            try:
                redirect = canonical_url(outcome.final_url)
            except ValueError:
                pass
        with self._transaction() as db:
            run = db.execute("""
                UPDATE runs SET processing_status = ?, error = ?
                WHERE id = ? AND processing_status = 'processing'
                RETURNING lead_id, canonical_url
            """, (status, error, run_id)).fetchone()
            if run is None or not redirect or redirect == run["canonical_url"]:
                return
            db.execute("UPDATE runs SET redirect_canonical_url = ? WHERE id = ?", (redirect, run_id))
            db.execute("""
                INSERT INTO lead_aliases (canonical_url, lead_id)
                SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM leads WHERE canonical_url = ?)
                ON CONFLICT(canonical_url) DO NOTHING
            """, (redirect, run["lead_id"], redirect))


def _lead_for_url(db, canonical):
    lead = db.execute("""
        SELECT * FROM leads WHERE canonical_url = ? ORDER BY created_at, id LIMIT 1
    """, (canonical,)).fetchone()
    if lead is not None:
        return lead
    return db.execute("""
        SELECT leads.* FROM lead_aliases JOIN leads ON leads.id = lead_aliases.lead_id
        WHERE lead_aliases.canonical_url = ?
    """, (canonical,)).fetchone()


def _merge_contact_logs(db, keep_id, drop_id):
    db.execute("""
        DELETE FROM contact_logs AS dropped WHERE dropped.lead_id = ? AND EXISTS (
            SELECT 1 FROM contact_logs AS kept WHERE kept.lead_id = ?
                AND kept.contacted_on = dropped.contacted_on AND kept.channel = dropped.channel
                AND kept.to_address = dropped.to_address
        )
    """, (drop_id, keep_id))
    db.execute("UPDATE contact_logs SET lead_id = ? WHERE lead_id = ?", (keep_id, drop_id))
    status_column = _lead_status_column(db)
    db.execute(f"""
        UPDATE leads SET last_contacted_at = (
            SELECT MAX(contacted_on) FROM contact_logs WHERE lead_id = :keep
        ), ({status_column}, updated_at) = (
            SELECT {status_column}, updated_at FROM leads
            WHERE id = :keep OR (id = :drop AND last_contacted_at IS NOT NULL)
            ORDER BY last_contacted_at IS NULL, updated_at DESC LIMIT 1
        ) WHERE id = :keep
    """, {"keep": keep_id, "drop": drop_id})


def _lead_status_column(db, table="leads"):
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    for name in ("crm_status", "contact_status", "status"):
        if name in columns:
            return name


def _company_leads(db, normalized):
    db.create_function("normalize_company_name", 1, normalize_company_name)
    return db.execute("""
        SELECT leads.* FROM leads LEFT JOIN companies ON companies.id = leads.company_id
        WHERE companies.normalized_name = ? OR normalize_company_name(COALESCE(companies.name, '')) = ?
            OR normalize_company_name(leads.company_name) = ?
        ORDER BY leads.created_at, leads.id
    """, (normalized, normalized, normalized)).fetchall()


def _insert_run(db, run_id, lead_id, canonical, fetch_url, now, submitted, source_url=None):
    db.execute("""
        INSERT INTO runs (id, lead_id, source_url, canonical_url, fetch_url,
            processing_status, submitted_at, submitted_company, submitted_signal, submitted_lead_type)
        VALUES (?, ?, ?, ?, ?, 'processing', ?, ?, ?, ?)
    """, (run_id, lead_id, source_url or fetch_url, canonical, fetch_url, now, *submitted))


def _upsert_company(db, display_name, normalized, now):
    row = db.execute(
        "SELECT id FROM companies WHERE normalized_name = ? ORDER BY created_at, id LIMIT 1", (normalized,),
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
    return db.execute(f"SELECT * FROM runs WHERE lead_id = ? ORDER BY {LATEST_RUN_SQL} LIMIT 1",
                      (lead_id,)).fetchone()


def _update_latest_run(db, lead_id, now):
    latest = _latest_run(db, lead_id)
    db.execute("UPDATE leads SET latest_run_id = ?, updated_at = ? WHERE id = ?",
               (latest["id"] if latest else None, now, lead_id))


def _ensure_columns(db):
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    for table, definitions in ADDITIVE_COLUMNS.items():
        existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        for column, definition in definitions.items():
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    for table in ("leads", "contact_logs"):
        current = _lead_status_column(db, table)
        definition = db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
        check = re.search(rf'CHECK\s*\(\s*(?:"{current}"|`{current}`|\[{current}\]|{current})'
                          r'\s+IN\s*\(([^)]*)\)', definition, re.IGNORECASE)
        if check and not all(f"'{status}'" in check[1] for status in LEAD_STATUSES):
            # Preserve constrained columns and copy values to a writable status column.
            column = "contact_status" if current == "status" else "crm_status"
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} "
                       + LEAD_STATUS_COLUMN.format(column=column))
            db.execute(f"""
                UPDATE {table} SET {column} = CASE {current}
                    WHEN 'verified' THEN 'new' WHEN 'in_discussion' THEN 'replied'
                    ELSE COALESCE({current}, 'new') END
            """)


def _lead_key(lead):
    source = lead["source_url"] or lead.get("canonical_url")
    if not source:
        return None
    try:
        return canonical_url(source)
    except ValueError:
        return lead.get("canonical_url") or source


def _lead_keys(leads, runs):
    by_id = {lead["id"]: lead for lead in leads}
    keys = {lead_id: _lead_key(lead) for lead_id, lead in by_id.items()}
    # The previous store saved the canonical key as the lead and run source.
    # Only the initial run's fetch URL can restore the original spelling.
    for run in runs:
        lead = by_id.get(run["lead_id"])
        if (lead and run["submitted_at"] == lead["created_at"]
                and run["source_url"] == lead["source_url"] and run.get("fetch_url")):
            keys[lead["id"]] = _lead_key({**lead, "source_url": run["fetch_url"]})
    return keys


def _backfill_keys(db):
    warnings = []
    lead_keys = _lead_keys(
        [dict(row) for row in db.execute("SELECT * FROM leads")],
        (dict(row) for row in db.execute("SELECT * FROM runs ORDER BY id")),
    )
    for table, column, key_for_row in (
        ("companies", "normalized_name", lambda row: normalize_company_name(row["name"]) or None),
        ("leads", "canonical_url", lambda row: lead_keys[row["id"]]),
    ):
        blocked = False
        for row in db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall():
            key = key_for_row(dict(row))
            if key is None or key == row[column]:
                continue
            try:
                db.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (key, row["id"]))
            except sqlite3.IntegrityError:
                blocked = True
        try:
            db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_{column}_unique ON {table}({column})")
        except sqlite3.IntegrityError:
            blocked = True
        if blocked:
            warning = f"Duplicate {table}.{column} keys need cleanup before migration can finish"
            warnings.append(warning)
            logger.warning(warning)
    db.execute("UPDATE runs SET fetch_url = source_url WHERE fetch_url IS NULL OR fetch_url = ''")
    for row in db.execute("SELECT id, source_url, fetch_url, canonical_url FROM runs").fetchall():
        key = _lead_key({"source_url": row["fetch_url"], "canonical_url": row["canonical_url"]})
        if key != row["canonical_url"]:
            db.execute("UPDATE runs SET canonical_url = ? WHERE id = ?", (key, row["id"]))
    db.execute("CREATE INDEX IF NOT EXISTS runs_lead ON runs(lead_id, submitted_at)")
    db.execute("CREATE INDEX IF NOT EXISTS runs_canonical ON runs(canonical_url, submitted_at)")
    db.execute(f"""
        UPDATE leads SET latest_run_id = (
            SELECT id FROM runs WHERE runs.lead_id = leads.id ORDER BY {LATEST_RUN_SQL} LIMIT 1
        )
    """)
    return warnings


def _has_lead_tables(db):
    rows = db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    names = {row["name"] if isinstance(row, sqlite3.Row) else row[0] for row in rows}
    return {"companies", "leads", "runs"} <= names


def _build_plan(db):
    leads = [dict(row) for row in db.execute("SELECT * FROM leads ORDER BY created_at, id")]
    companies = [dict(row) for row in db.execute("SELECT * FROM companies ORDER BY created_at, id")]
    runs = [dict(row) for row in db.execute("SELECT * FROM runs")]
    runs_by_lead = {}
    for run in runs:
        runs_by_lead.setdefault(run["lead_id"], []).append(run)
    lead_keys = _lead_keys(leads, sorted(runs, key=lambda run: run["id"]))
    keys_by_lead = {lead_id: {key} if key else set() for lead_id, key in lead_keys.items()}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'lead_aliases'").fetchone():
        for alias in db.execute("SELECT canonical_url, lead_id FROM lead_aliases"):
            if alias["lead_id"] in keys_by_lead:
                keys_by_lead[alias["lead_id"]].add(alias["canonical_url"])
    for run in runs:
        redirect = run.get("redirect_canonical_url")
        if redirect and run["lead_id"] in keys_by_lead:
            keys_by_lead[run["lead_id"]].add(redirect)

    leads_by_key = {}
    for lead_id, keys in keys_by_lead.items():
        for key in keys:
            leads_by_key.setdefault(key, []).append(lead_id)

    leads_by_id = {lead["id"]: lead for lead in leads}
    visited = set()
    url_merges = []
    dropped = set()
    for lead_id in leads_by_id:
        if lead_id in visited:
            continue
        pending = [lead_id]
        grouped = []
        shared_keys = set()
        while pending:
            member = pending.pop()
            if member in visited:
                continue
            visited.add(member)
            grouped.append(leads_by_id[member])
            for key in keys_by_lead[member] - shared_keys:
                shared_keys.add(key)
                pending.extend(leads_by_key[key])
        if len(grouped) < 2:
            continue

        def rank(lead):
            lead_runs = runs_by_lead.get(lead["id"], [])
            best = max((STATUS_RANK.get(run["processing_status"], 0) for run in lead_runs), default=0)
            return (-best, lead["created_at"], lead["id"])

        keeper = min(grouped, key=rank)
        key = lead_keys[keeper["id"]] or min(shared_keys)
        drop_ids = sorted(lead["id"] for lead in grouped if lead["id"] != keeper["id"])
        moved = sorted(run["id"] for member in drop_ids for run in runs_by_lead.get(member, []))
        combined = [run for lead in grouped for run in runs_by_lead.get(lead["id"], [])]
        latest = max(combined, key=lambda run: tuple(run[column] for column in LATEST_RUN_ORDER), default=None)
        url_merges.append(UrlMerge(
            canonical_url=key,
            keep_lead_id=keeper["id"],
            drop_lead_ids=drop_ids,
            move_run_ids=moved,
            latest_run_id=latest["id"] if latest else None,
            reason="正規化URLまたはリダイレクト先が一致しています。",
            aliases=sorted(shared_keys - {key}),
        ))
        dropped.update(drop_ids)
    url_merges.sort(key=lambda merge: merge.canonical_url)

    surviving_company_counts = {}
    for lead in leads:
        if lead["id"] in dropped or not lead.get("company_id"):
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
            if lead["id"] not in dropped and lead.get("company_id") in drop_ids
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
        had_leads = any(lead.get("company_id") == company["id"] for lead in leads)
        survives = surviving_company_counts.get(company["id"], 0) > 0
        if had_leads and not survives:
            emptied.append(company["id"])
    emptied.sort()
    return DatabasePlan(url_merges=url_merges, company_merges=company_merges, empty_company_ids=emptied)


def render_plan(plan, *, applied=False):
    heading = "適用した整理内容です。" if applied else "既存データは変更しません。"
    lines = ["Meishi lead dedup review", plan.note or heading]
    lines.append(f"同じページとしてまとめるリード: {len(plan.url_merges)}")
    for merge in plan.url_merges:
        lines.append(f"- {merge.canonical_url}")
        lines.append(f"  残す lead {merge.keep_lead_id}（latest run {merge.latest_run_id}）")
        lines.append(f"  まとめる lead: {', '.join(merge.drop_lead_ids) or 'なし'}")
        lines.append(f"  移動する run: {', '.join(merge.move_run_ids) or 'なし'}")
        lines.append(f"  {merge.reason}")
        if merge.aliases:
            lines.append(f"  別名URL: {', '.join(merge.aliases)}")
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
