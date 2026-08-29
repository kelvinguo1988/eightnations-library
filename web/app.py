"""八国联军图书馆 · Web 前端（FastAPI + Jinja2，无 Node 构建链）。

页面: / 书库 · /books/{id} 详情 · /review 新书审核 · /jobs 任务面板 · /settings 设置
数据: 只读 SQLite + /data 静态挂载 PDF 与封面（与 scheduler/manage 共库互不干扰, WAL）。
运行: uvicorn web.app:app --host 0.0.0.0 --port 8080
"""
import json
import os
import sys
from typing import Optional

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import DB, utcnow                   # noqa: E402
from core.pipeline import fetch_one              # noqa: E402
from core.limiter import HourQuota               # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("EIGHTNATIONS_DATA",
                          os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "db", "library.db")
BOOKS_DIR = os.path.join(DATA_DIR, "books")

app = FastAPI(title="八国联军图书馆", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "web", "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "web", "static")),
          name="static")
os.makedirs(BOOKS_DIR, exist_ok=True)
app.mount("/data", StaticFiles(directory=BOOKS_DIR), name="data")


def get_db() -> DB:
    d = DB(DB_PATH)
    d.init()
    return d


def book_urls(row) -> dict:
    rel = f"{row['source_id']}/{row['collection'] or 'misc'}/{row['source_uid']}"
    pdf = f"/data/{rel}/book.pdf"
    cover = f"/data/{rel}/cover.jpg"
    cover_exists = os.path.exists(os.path.join(BOOKS_DIR, rel, "cover.jpg"))
    pdf_exists = os.path.exists(os.path.join(BOOKS_DIR, rel, "book.pdf"))
    return {"rel": rel, "pdf": pdf if pdf_exists else "", "cover": cover,
            "cover_exists": cover_exists}


def common_ctx(request: Request, d: DB, **kw) -> dict:
    ctx = {
        "request": request,
        "counts": d.count_by_status(),
        "sources": d.connect().execute("SELECT * FROM sources ORDER BY id").fetchall(),
    }
    ctx["sources"] = list(ctx["sources"])
    ctx.update(kw)
    return ctx


def _bytes_h(n: int) -> str:
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


templates.env.filters["bytes_h"] = _bytes_h


# ---------------------------------------------------------------- 书库
@app.get("/", response_class=HTMLResponse)
def library(request: Request, q: str = "", source: str = "",
            collection: str = "", era: str = "", status: str = "done",
            page: int = 1):
    d = get_db()
    per = 24
    rows = d.list_books(status=status, source_id=source, collection=collection,
                        keyword=q, era=era, limit=per,
                        offset=(max(page, 1) - 1) * per)
    total = d.count_books(status=status, source_id=source, collection=collection,
                          keyword=q, era=era)
    cards = []
    for r in rows:
        u = book_urls(r)
        cards.append({"row": r, "cover": u["cover"] if u["cover_exists"] else "",
                      "has_pdf": bool(u["pdf"])})
    facets = d.facets(source)
    pages = max(1, (total + per - 1) // per)
    return templates.TemplateResponse(request, "library.html", common_ctx(
        request, d, cards=cards, total=total, page=page, pages=pages,
        q=q, f_source=source, f_collection=collection, f_era=era, f_status=status,
        facets=facets))


# ---------------------------------------------------------------- 详情
@app.get("/books/{book_id}", response_class=HTMLResponse)
def detail(book_id: int, request: Request):
    d = get_db()
    row = d.get_book(book_id)
    if not row:
        return HTMLResponse("未找到该书", status_code=404)
    u = book_urls(row)
    meta = {}
    meta_path = os.path.join(BOOKS_DIR, u["rel"], "meta.json")
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    with d.connect() as conn:
        jobs = conn.execute(
            "SELECT * FROM jobs WHERE book_id=? ORDER BY id DESC LIMIT 5",
            (book_id,)).fetchall()
    return templates.TemplateResponse(request, "detail.html", common_ctx(
        request, d, b=row, urls=u, meta=meta,
        jobs=[dict(j) for j in jobs]))


# ---------------------------------------------------------------- 新书审核
@app.get("/review", response_class=HTMLResponse)
def review(request: Request, q: str = "", source: str = "",
           collection: str = "", era: str = "", limit: int = 60):
    d = get_db()
    rows = d.list_books(status="discovered", source_id=source,
                        collection=collection, keyword=q, era=era, limit=limit)
    total = d.count_books(status="discovered", source_id=source)
    facets = d.facets(source)
    return templates.TemplateResponse(request, "review.html", common_ctx(
        request, d, rows=rows, total=total, q=q, f_source=source,
        f_collection=collection, f_era=era, facets=facets, limit=limit))


@app.post("/api/review")
async def api_review(request: Request):
    d = get_db()
    body = await request.json()
    action = body.get("action")
    ids = [int(i) for i in (body.get("ids") or [])]
    if action not in ("approve", "ignore") or not ids:
        return JSONResponse({"error": "action/ids 不合法"}, status_code=400)
    status = "queued" if action == "approve" else "ignored"
    for i in ids:
        d.set_status(i, status)
    d.log(f"人工审核: {status} x{len(ids)}")
    return {"updated": len(ids), "action": action}


@app.post("/api/retry")
async def api_retry(request: Request):
    d = get_db()
    body = await request.json()
    book_id = int(body.get("id") or 0)
    row = d.get_book(book_id)
    if not row or row["status"] not in ("failed", "dead"):
        return JSONResponse({"error": "仅 failed/dead 可重试"}, status_code=400)
    d.set_status(book_id, "queued")
    d.log(f"手动重排: #{book_id}")
    return {"ok": True}


@app.post("/api/fetch")
async def api_fetch(request: Request):
    """从任务面板手动触发单本下载（与调度器共用管线/配额）。"""
    d = get_db()
    body = await request.json()
    row = d.get_book(int(body.get("id") or 0))
    if not row or row["status"] != "queued":
        return JSONResponse({"error": "书不存在或不在 queued"}, status_code=400)
    src = row["source_id"]
    quota_row = d.connect().execute(
        "SELECT hourly_quota, quality FROM sources WHERE id=?", (src,)).fetchone()
    quota_n = int(quota_row["hourly_quota"]) if quota_row else 10
    quality = quota_row["quality"] if quota_row else "auto"
    ok = fetch_one(d, row, quality, HourQuota(default_quota=quota_n))
    return {"ok": bool(ok), "status": d.get_book(row["id"])["status"]}


# ---------------------------------------------------------------- 任务面板
@app.get("/jobs", response_class=HTMLResponse)
def jobs(request: Request):
    d = get_db()
    with d.connect() as conn:
        recent = conn.execute(
            "SELECT j.*, b.source_id, b.collection, b.source_uid, b.title, "
            "b.alt_title, b.era, b.year_start FROM jobs j "
            "JOIN books b ON b.id=j.book_id ORDER BY j.id DESC LIMIT 60").fetchall()
        src_stats = conn.execute(
            "SELECT source_id, status, COUNT(*) n FROM books "
            "GROUP BY source_id, status").fetchall()
        events = conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT 40").fetchall()
        agg = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(bytes_done),0) b FROM jobs "
            "WHERE state='done'").fetchone()
    by_source = {}
    for r in src_stats:
        by_source.setdefault(r["source_id"], {})[r["status"]] = r["n"]
    return templates.TemplateResponse(request, "jobs.html", common_ctx(
        request, d, recent=[dict(j) | {"urls": None} for j in recent],
        by_source=by_source, events=[dict(e) for e in events],
        total_bytes=agg["b"], total_done=agg["n"]))


# ---------------------------------------------------------------- 设置
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    d = get_db()
    return templates.TemplateResponse(request, "settings.html", common_ctx(request, d))


@app.post("/settings")
async def settings_save(request: Request):
    d = get_db()
    form = await request.form()
    with d.connect() as conn:
        rows = conn.execute("SELECT id FROM sources").fetchall()
    for r in rows:
        sid = r["id"]
        enabled = 1 if form.get(f"enabled_{sid}") else 0
        quota = max(1, int(form.get(f"quota_{sid}") or 10))
        quality = form.get(f"quality_{sid}") or "auto"
        with d.connect() as conn:
            conn.execute(
                "UPDATE sources SET enabled=?, hourly_quota=?, quality=? WHERE id=?",
                (enabled, quota, quality, sid))
    d.log("更新站点设置")
    return RedirectResponse("/settings", status_code=303)


# ---------------------------------------------------------------- API
@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/api/health")
def health():
    return {"ok": True, "ts": utcnow(), "db": os.path.exists(DB_PATH)}


@app.get("/api/stats")
def stats():
    d = get_db()
    with d.connect() as conn:
        books = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM books GROUP BY status")}
        job = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(bytes_done),0) b FROM jobs "
            "WHERE state='done'").fetchone()
    return {"books": books, "jobs_done": job["n"], "bytes_done": job["b"]}
