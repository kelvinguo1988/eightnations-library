"""下载管线：从队列取书 → 适配器下载 → 状态机流转。

manage.py（手动）与 scheduler.py（守护心跳）共用；进程内 HourQuota 实现
"每源每小时至多 N 册"——重启清零可接受（NAS 容器常驻时即长期有效）。
"""
import json
import os
import re
from typing import Optional

from core.http import HttpClient
from core.text import jp2t
from core.limiter import HourQuota
from core.models import BookMeta, DownloadResult
from sites import get_adapter

MAX_ATTEMPTS = 5
BOOKS_DIR = os.path.join(
    os.environ.get("EIGHTNATIONS_DATA",
                   os.path.join(os.path.dirname(os.path.dirname(
                       os.path.abspath(__file__))), "data")),
    "books")


def row_to_meta(row) -> BookMeta:
    return BookMeta(
        source_uid=row["source_uid"], title=row["title"], alt_title=row["alt_title"],
        author=row["author"], era=row["era"], year_start=row["year_start"],
        year_end=row["year_end"], language=row["language"], item_url=row["item_url"],
        cover_url=row["cover_url"], collection=row["collection"],
        volume_count=row["volume_count"], page_count=row["page_count"],
        rights=row["rights"], shelf_id=row["shelf_id"],
        subjects=json.loads(row["subjects"] or "[]"),
        pdf_urls=json.loads(row["pdf_urls"] or "[]"),
        page_files=json.loads(row["files_json"] or "[]"),
        raw=json.loads(row["raw_json"] or "{}"))


def _dest_dir(source_id: str, collection: str, uid: str) -> str:
    return os.path.join(BOOKS_DIR, source_id, collection or "misc", uid)


def _sanitize_filename(s: str, cap: int = 60) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("._")
    return s[:cap]


def create_title_links(d) -> tuple:
    """为全部已归档书补建书名硬链接；返回 (新建, 已存在)。幂等。

    只处理实体 PDF（book*.pdf），排除已有的书名链接文件（<编号>_*.pdf），
    避免对链接再做链接与重复计数。
    """
    created = skipped = 0
    for r in d.list_books(status="done", limit=100000):
        meta = row_to_meta(r)
        dest = _dest_dir(r["source_id"], r["collection"], r["source_uid"])
        if not os.path.isdir(dest):
            continue
        pdfs = sorted(f for f in os.listdir(dest)
                      if f.endswith(".pdf") and not f.endswith(".part")
                      and f != "cover.pdf"
                      and not re.match(r"^\d+_.+\.pdf$", f))
        for i, p in enumerate(pdfs, 1):
            state = _title_link(dest, os.path.join(dest, p), meta, i, len(pdfs))
            if state == "new":
                created += 1
            elif state == "exists":
                skipped += 1
    return created, skipped


def _title_link(dest_dir: str, output: str, meta: BookMeta,
                idx: int, total: int) -> Optional[str]:
    """为 PDF 建立书名硬链接。返回 "new"（新建）/ "exists"（已存在）/ None（失败）。"""
    if os.path.basename(output) == "cover.pdf":
        return None
    t = _sanitize_filename(jp2t(meta.alt_title or meta.title))
    if not t:
        return None
    suffix = "" if total == 1 else f"_{idx:02d}"
    link = os.path.join(dest_dir, f"{meta.source_uid}_{t}{suffix}.pdf")
    if os.path.exists(link):
        return "exists"
    try:
        os.link(output, link)
        return "new"
    except OSError:
        return None


def fetch_one(d, row, quality: str, quota: HourQuota) -> bool:
    """下载单本。返回 False 表示配额用尽，调用方应结束本轮。"""
    src = row["source_id"]
    if not quota.allow(src):
        d.log("达到每小时配额，停止本轮", source=src)
        return False
    if not d.claim_book(row["id"]):
        # 已被其他执行方（调度器/手动）认领：跳过且不消耗配额
        return True
    adapter = get_adapter(src, HttpClient())
    http = adapter.http if hasattr(adapter, "http") else HttpClient()
    meta = row_to_meta(row)
    dest = _dest_dir(src, row["collection"], row["source_uid"])
    job = d.start_job(row["id"], quality)
    d.log(f"开始下载: {meta.alt_title or meta.title}", source=src, book_id=row["id"])
    try:
        result = adapter.download_item(meta, dest, http, quality)
    except Exception as e:
        result = DownloadResult(ok=False, errors=[f"适配器异常: {e}"])
    if result.ok:
        d.finish_job(job, "done", result.pages,
                     max(meta.page_count, result.pages),
                     result.bytes_done, result.outputs)
        d.update_download_info(row["id"], os.path.join(dest, "cover.jpg"),
                               result.pages)
        for i, out in enumerate(result.outputs, 1):
            _title_link(dest, out, meta, i, len(result.outputs))
        d.set_status(row["id"], "done")
        d.log(f"完成: {result.pages} 页 / {result.bytes_done / 1e6:.1f} MB "
              f"-> {dest}", source=src, book_id=row["id"])
        return True
    msg = "; ".join(result.errors) or "unknown"
    attempts = row["attempt"] + 1
    new_status = "dead" if attempts >= MAX_ATTEMPTS else "failed"
    d.finish_job(job, "failed", result.pages,
                 max(meta.page_count, result.pages),
                 result.bytes_done, result.outputs, msg)
    d.set_status(row["id"], new_status, error=msg[:500])
    d.log(f"失败({attempts}/{MAX_ATTEMPTS}): {msg}", level="warn",
          source=src, book_id=row["id"])
    return True


def run_source_heartbeat(d, source_id: str, quota_n: int,
                         quality: str = "auto",
                         quota: Optional[HourQuota] = None) -> tuple:
    """对单个源跑一轮心跳：取 queued 至多 quota_n 册逐本下载。

    quota 传入常驻实例（scheduler 持有）→ 真正实现"每小时 ≤ quota_n 册"
    的滑动窗口；不传则每次新建（CLI 单轮手动语义）。

    返回 (尝试册数, 成功册数, 是否因配额提前停止)。
    """
    rows = d.queued_books(source_id, limit=quota_n)
    if not rows:
        return 0, 0, False
    quota = quota or HourQuota(default_quota=quota_n)
    tried = ok = 0
    for row in rows:
        cont = fetch_one(d, row, quality, quota)
        tried += 1
        if d.get_book(row["id"])["status"] == "done":
            ok += 1
        if not cont:
            return tried, ok, True
    return tried, ok, False
