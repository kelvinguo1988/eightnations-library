"""下载管线：从队列取书 → 适配器下载 → 状态机流转。

manage.py（手动）与 scheduler.py（守护心跳）共用；进程内 HourQuota 实现
"每源每小时至多 N 册"——重启清零可接受（NAS 容器常驻时即长期有效）。
"""
import json
import os
from typing import Optional

from core.http import HttpClient
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


def fetch_one(d, row, quality: str, quota: HourQuota) -> bool:
    """下载单本。返回 False 表示配额用尽，调用方应结束本轮。"""
    src = row["source_id"]
    if not quota.allow(src):
        d.log("达到每小时配额，停止本轮", source=src)
        return False
    adapter = get_adapter(src, HttpClient())
    meta = row_to_meta(row)
    dest = _dest_dir(src, row["collection"], row["source_uid"])
    job = d.start_job(row["id"], quality)
    d.set_status(row["id"], "running", bump_attempt=True)
    d.log(f"开始下载: {meta.alt_title or meta.title}", source=src, book_id=row["id"])
    try:
        result = adapter.download_item(meta, dest, HttpClient(), quality)
    except Exception as e:
        result = DownloadResult(ok=False, errors=[f"适配器异常: {e}"])
    if result.ok:
        d.finish_job(job, "done", result.pages, meta.page_count,
                     result.bytes_done, result.outputs)
        d.update_download_info(row["id"], os.path.join(dest, "cover.jpg"),
                               result.pages)
        d.set_status(row["id"], "done")
        d.log(f"完成: {result.pages} 页 / {result.bytes_done / 1e6:.1f} MB "
              f"-> {dest}", source=src, book_id=row["id"])
        return True
    msg = "; ".join(result.errors) or "unknown"
    attempts = row["attempt"] + 1
    new_status = "dead" if attempts >= MAX_ATTEMPTS else "failed"
    d.finish_job(job, "failed", result.pages, meta.page_count,
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
