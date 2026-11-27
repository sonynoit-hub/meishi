import asyncio
import io
import logging
import os
import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener
from pydantic import BaseModel, ConfigDict, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from codex_reader import ReadError, recognize
from lead_store import LeadStore

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("MEISHI_DB", ROOT / "data" / "meishi.sqlite3"))
PASSWORD = os.environ.get("MEISHI_PASSWORD", "")
MAX_BYTES = 12 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 25_000_000
register_heif_opener()
logger = logging.getLogger(__name__)

FIELDS = ("name", "company", "department_title", "company_address", "email", "phone", "notes")
SUMMARY = "id, name, company, department_title, company_address, status, created_at, codex_requested, read_error"


@contextmanager
def database():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize():
    DB_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with database() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS cards (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK (status IN ('queued','processing','review','saved','failed')),
                name TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                department_title TEXT NOT NULL DEFAULT '',
                company_address TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                raw_text TEXT NOT NULL DEFAULT '',
                codex_requested INTEGER NOT NULL DEFAULT 0,
                read_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS photos (
                card_id TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
                jpeg BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS cards_status_date ON cards(status, created_at);
        """)
        db.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(cards)")}
        if "codex_requested" not in columns:
            db.execute("ALTER TABLE cards ADD COLUMN codex_requested INTEGER NOT NULL DEFAULT 0")
        if "read_error" not in columns:
            db.execute("ALTER TABLE cards ADD COLUMN read_error TEXT NOT NULL DEFAULT ''")
        if "department_title" not in columns:
            db.execute("ALTER TABLE cards ADD COLUMN department_title TEXT NOT NULL DEFAULT ''")
            db.execute("""
                UPDATE cards SET department_title = CASE
                    WHEN department = '' THEN title
                    WHEN title = '' THEN department
                    ELSE department || ' / ' || title
                END
            """)
        if "company_address" not in columns:
            db.execute("ALTER TABLE cards ADD COLUMN company_address TEXT NOT NULL DEFAULT ''")
        db.execute("""
            UPDATE cards SET status = 'failed', codex_requested = 0,
                read_error = '読み取りが中断されました。もう一度読み取れます。'
            WHERE status = 'processing'
        """)
    LeadStore(DB_PATH).ensure_schema()
    LeadStore(DB_PATH).recover_interrupted()


def process_queue(stop, wake):
    while not stop.is_set():
        wake.clear()
        with database() as db:
            job = db.execute("""
                UPDATE cards SET status = 'processing' WHERE id = (
                    SELECT id FROM cards WHERE status = 'queued' AND codex_requested = 1
                    ORDER BY created_at, rowid LIMIT 1
                ) RETURNING id
            """).fetchone()
            if job:
                photo = db.execute("SELECT jpeg FROM photos WHERE card_id = ?", (job["id"],)).fetchone()
        if not job:
            wake.wait(2)
            continue
        try:
            raw, suggestions = recognize(photo["jpeg"])
            with database() as db:
                db.execute("""
                    UPDATE cards SET status = 'review', codex_requested = 0, read_error = '',
                        raw_text = ?, name = ?, company = ?, department_title = ?, company_address = ?,
                        email = ?, phone = ?
                    WHERE id = ? AND status = 'processing'
                """, (raw, *(suggestions.get(key, "") for key in FIELDS[:-1]), job["id"]))
        except Exception as error:
            message = str(error) if isinstance(error, ReadError) else "読み取りに失敗しました。"
            logger.warning("Card reading failed (%s)", type(error).__name__)
            with database() as db:
                db.execute("""
                    UPDATE cards SET status = 'failed', codex_requested = 0, read_error = ?
                    WHERE id = ? AND status = 'processing'
                """, (message, job["id"]))


@asynccontextmanager
async def lifespan(app):
    initialize()
    stop, wake = threading.Event(), threading.Event()
    app.state.wake = wake
    worker = threading.Thread(target=process_queue, args=(stop, wake), daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        wake.set()
        await asyncio.to_thread(worker.join, 5)


security = HTTPBasic(auto_error=False)


def authenticate(credentials: Annotated[HTTPBasicCredentials | None, Depends(security)]):
    if PASSWORD and (
        credentials is None
        or credentials.username != "meishi"
        or not secrets.compare_digest(credentials.password.encode(), PASSWORD.encode())
    ):
        raise HTTPException(401, "ログインしてください", headers={"WWW-Authenticate": 'Basic realm="Meishi"'})


def require_app_request(x_meishi: Annotated[str | None, Header()] = None):
    if x_meishi != "1":
        raise HTTPException(403, "この操作を実行できません")


app = FastAPI(lifespan=lifespan, dependencies=[Depends(authenticate)],
              docs_url=None, redoc_url=None, openapi_url=None)
if not PASSWORD:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
write = [Depends(require_app_request)]


@app.middleware("http")
async def private_response(request, call_next):
    if request.method == "POST" and request.url.path.startswith("/api/cards/"):
        length = request.headers.get("content-length", "0")
        if length.isdecimal() and int(length) > MAX_BYTES + 64 * 1024:
            return JSONResponse({"detail": "画像は12MB以下にしてください"}, status_code=413)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob:; media-src 'self' blob:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/app.js")
def javascript():
    return FileResponse(ROOT / "web" / "app.js", media_type="text/javascript")


@app.get("/style.css")
def stylesheet():
    return FileResponse(ROOT / "web" / "style.css", media_type="text/css")


@app.get("/api/cards")
def list_cards():
    with database() as db:
        return [dict(row) for row in db.execute(f"SELECT {SUMMARY} FROM cards ORDER BY created_at DESC, rowid DESC")]


def get_card(db, card_id):
    row = db.execute(f"""
        SELECT {SUMMARY}, email, phone, notes, raw_text
        FROM cards WHERE id = ?
    """, (str(card_id),)).fetchone()
    if not row:
        raise HTTPException(404, "名刺が見つかりません")
    return dict(row)


@app.get("/api/cards/{card_id}")
def card_detail(card_id: UUID):
    with database() as db:
        return get_card(db, card_id)


@app.get("/api/cards/{card_id}/photo")
def card_photo(card_id: UUID):
    with database() as db:
        row = db.execute("SELECT jpeg FROM photos WHERE card_id = ?", (str(card_id),)).fetchone()
    if not row:
        raise HTTPException(404, "画像が見つかりません")
    return Response(row["jpeg"], media_type="image/jpeg")


def normalize_photo(data):
    try:
        with Image.open(io.BytesIO(data), formats=["JPEG", "PNG", "WEBP", "HEIF"]) as source:
            if source.width * source.height > Image.MAX_IMAGE_PIXELS:
                raise HTTPException(413, "画像の解像度が大きすぎます")
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((2400, 2400))
            output = io.BytesIO()
            image.save(output, "JPEG", quality=92)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise HTTPException(422, "JPEG・PNG・WebP・HEICの画像を選んでください") from error


@app.post("/api/cards/{card_id}", dependencies=write, status_code=202)
def upload(card_id: UUID, photo: Annotated[UploadFile, File()]):
    key = str(card_id)
    with database() as db:
        if db.execute("SELECT 1 FROM cards WHERE id = ?", (key,)).fetchone():
            return {"id": key}
    data = photo.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "画像は12MB以下にしてください")
    jpeg = normalize_photo(data)
    with database() as db:
        inserted = db.execute("""
            INSERT INTO cards (id, status, codex_requested) VALUES (?, 'queued', 1)
            ON CONFLICT(id) DO NOTHING
        """, (key,))
        if inserted.rowcount:
            db.execute("INSERT INTO photos VALUES (?, ?)", (key, jpeg))
    if inserted.rowcount:
        app.state.wake.set()
    return {"id": key}


class Edits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = ""
    company: str = ""
    department_title: str = ""
    company_address: str = ""
    email: str = ""
    phone: str = ""
    notes: str = ""

    @field_validator("*")
    @classmethod
    def trim(cls, value, info):
        value = value.strip()
        limit = {"notes": 2000, "department_title": 513, "company_address": 500}.get(info.field_name, 255)
        if len(value) > limit:
            raise ValueError("入力が長すぎます")
        return value


@app.put("/api/cards/{card_id}", dependencies=write)
def save(card_id: UUID, edits: Edits):
    if not (edits.name or edits.company):
        raise HTTPException(422, "氏名か会社名を入力してください")
    with database() as db:
        result = db.execute("""
            UPDATE cards SET name=?, company=?, department_title=?, company_address=?, email=?, phone=?, notes=?,
                status='saved', read_error=''
            WHERE id=? AND codex_requested=0 AND status IN ('queued', 'review', 'failed', 'saved')
        """, (*(getattr(edits, key) for key in FIELDS), str(card_id)))
        item = get_card(db, card_id)
        if not result.rowcount:
            raise HTTPException(409, "読み取り完了後に保存してください")
        return item


@app.delete("/api/cards/{card_id}", dependencies=write, status_code=204)
def delete(card_id: UUID):
    with database() as db:
        db.execute("DELETE FROM cards WHERE id=?", (str(card_id),))


@app.post("/api/cards/{card_id}/reread", dependencies=write, status_code=202)
def reread(card_id: UUID):
    with database() as db:
        result = db.execute("""
            UPDATE cards SET status = 'queued', codex_requested = 1, read_error = ''
            WHERE id = ? AND codex_requested = 0 AND status IN ('queued', 'review', 'failed')
        """, (str(card_id),))
        if not result.rowcount:
            get_card(db, card_id)
            raise HTTPException(409, "この名刺は現在読み取りをやり直せません。")
        card = get_card(db, card_id)
    app.state.wake.set()
    return card


class LeadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str
    source_url: str
    signal: str
    lead_type: Literal["buyer", "partner"]

    @field_validator("company", "source_url", "signal")
    @classmethod
    def trim(cls, value):
        return value.strip()


@app.post("/api/leads", dependencies=write)
def submit_lead(body: LeadIn):
    try:
        return LeadStore(DB_PATH).submit(body.company, body.source_url, body.signal, body.lead_type)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@app.get("/api/leads/runs/{run_id}")
def lead_run_status(run_id: UUID):
    try:
        return LeadStore(DB_PATH).status(str(run_id))
    except KeyError:
        raise HTTPException(404, "処理が見つかりません") from None


@app.post("/api/leads/runs/{run_id}/retry", dependencies=write)
def retry_lead_run(run_id: UUID):
    try:
        return LeadStore(DB_PATH).retry(str(run_id))
    except KeyError:
        raise HTTPException(404, "処理が見つかりません") from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("MEISHI_PORT", "8000")))
