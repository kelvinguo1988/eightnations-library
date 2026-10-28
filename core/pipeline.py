"""下载管线：从队列取书 → 适配器下载 → 状态机流转。

manage.py（手动）与 scheduler.py（守护心跳）共用；"每源每小时至多 N 册"
以 jobs 表为账本、在 DB.claim_and_start 的写事务内原子消费（跨进程共享）。
"""
import json
import os
import re
import time
from typing import Optional

from core.db import MAX_ATTEMPTS
from core.http import HttpClient
from core.text import jp2t
from core.limiter import Progress
from core.models import BookMeta, DownloadResult
from sites import get_adapter

# 重试上限统一放在 core/db.py（claim_and_start 的 SQL 里要用它做原子判定）。
# 这里保留同名导入，便于日志与旧代码引用；不要再本地硬编码第二份数字。
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
    """落盘目录。source_id/collection/uid 均来自远端元数据（快照/收割），
    必须逐段校验，防路径穿越写出 data/books 之外。

    collection 允许为空（归入 misc）；source_id/uid 必须非空。
    """
    def _illegal(v: str) -> bool:
        return (v in (".", "..") or "/" in v or "\\" in v or "\x00" in v)
    if not source_id or _illegal(source_id):
        raise ValueError(f"非法路径段 source_id: {source_id!r}")
    if not uid or _illegal(uid):
        raise ValueError(f"非法路径段 uid: {uid!r}")
    if collection and _illegal(collection):
        raise ValueError(f"非法路径段 collection: {collection!r}")
    full = os.path.join(BOOKS_DIR, source_id, collection or "misc", uid)
    root = os.path.realpath(BOOKS_DIR)
    if os.path.commonpath([root, os.path.realpath(full)]) != root:
        raise ValueError(f"路径逃逸数据目录: {full!r}")
    return full


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
        try:
            dest = _dest_dir(r["source_id"], r["collection"], r["source_uid"])
        except ValueError:
            continue        # 非法路径段的旧数据：不补链接（fetch 侧同样拒绝）
        if not os.path.isdir(dest):
            continue
        # 只认实体文件名（book.pdf / book_NN.pdf）；书名链接 <uid>_<题名>.pdf
        # 不参与再链接，也避免 uid 恰为数字时被前缀正则误伤。
        pdfs = sorted(f for f in os.listdir(dest)
                      if f == "book.pdf" or re.fullmatch(r"book_\d+\.pdf", f))
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


def fetch_one(d, row, quality: str, quota_n: Optional[int] = None) -> bool:
    """下载单本。返回 False 表示配额用尽，调用方应结束本轮。

    状态语义（审查 A P1-2 / P0-3）：
      * 齐全          → done（needs_topup=0）
      * 部分完成      → done + needs_topup=1（可读、且会被队尾补位自动补齐）
      * 失败（可重试）→ failed（attempt+1；到上限转 dead）
      * 失败（已死）  → dead（需人工 force / reset_attempt 才能再跑）
    """
    src = row["source_id"]
    try:
        dest = _dest_dir(src, row["collection"], row["source_uid"])
    except ValueError as e:
        d.set_status(row["id"], "failed", error=str(e)[:500])
        d.log(f"路径段非法，拒绝下载: {e}", level="warn", source=src,
              book_id=row["id"])
        return True
    # 认领+建 job+扣配额在一个写事务内完成：认领失败不消耗配额，
    # 也不会出现"running 但无 job 行"的孤儿状态。
    job, quota_full, claimed = d.claim_and_start(row["id"], quality, quota_n)
    if not claimed:
        if quota_full:
            d.log("达到每小时配额，停止本轮", source=src)
            return False
        return True       # 已被其他执行方认领/已达重试上限：跳过
    try:
        adapter = get_adapter(src, HttpClient())
        http = adapter.http if hasattr(adapter, "http") else HttpClient()
        meta = row_to_meta(row)
    except Exception as e:
        # 认领之后、下载之前的任何异常（未注册适配器 / 坏 JSON / 写库失败）
        # 都必须收尾，否则这本书永远停在 running，只能等 60 分钟租约回收
        # （审查 A P1-7）。
        msg = f"准备阶段失败: {e!r}"
        d.finish_job(job, "failed", 0, 0, 0, [], msg[:500])
        d.set_status(row["id"], "failed", error=msg[:500])
        d.log(msg, level="error", source=src, book_id=row["id"])
        return True
    d.log(f"开始下载: {meta.alt_title or meta.title}", source=src, book_id=row["id"])

    last_beat = [0.0]

    def on_progress(done: int, total: int) -> None:
        now = time.monotonic()
        if now - last_beat[0] >= 30:      # 每页/每卷回调，30s 续约一次租约
            last_beat[0] = now
            try:
                d.job_heartbeat(job)
            except Exception:
                pass

    try:
        result = adapter.download_item(meta, dest, http, quality,
                                       Progress(callback=on_progress))
    except Exception as e:
        result = DownloadResult(ok=False, errors=[f"适配器异常: {e}"])
    total_pages = max(meta.page_count, result.pages)
    if result.ok and result.pages > 0:
        # 封面是否真的写出来了（各馆封面失败不致命，旧代码无条件写路径，
        # 会让 UI 指向不存在的文件）
        cover = os.path.join(dest, "cover.jpg")
        d.update_download_info(row["id"], cover if os.path.exists(cover) else "",
                               total_pages, result.pages)
        if result.missing:
            # 部分完成：已归档部分可读（done），同时打欠账标记等下轮补位
            d.record_partial(row["id"], job, result.pages, total_pages,
                             result.bytes_done, result.outputs, result.missing,
                             "; ".join(result.errors))
            for i, out in enumerate(result.outputs, 1):
                _title_link(dest, out, meta, i, len(result.outputs))
            d.log(f"部分完成: {result.pages}/{total_pages} 页，缺 "
                  f"{','.join(result.missing[:5])}；已排队尾自动补齐",
                  level="warn", source=src, book_id=row["id"])
            return True
        d.record_done(row["id"], job, result.pages, total_pages,
                      result.bytes_done, result.outputs)
        for i, out in enumerate(result.outputs, 1):
            _title_link(dest, out, meta, i, len(result.outputs))
        d.log(f"完成: {result.pages} 页 / {result.bytes_done / 1e6:.1f} MB "
              f"-> {dest}", source=src, book_id=row["id"])
        return True
    msg = "; ".join(result.errors) or "unknown"
    if result.ok and result.pages == 0:
        msg = "有产出但 0 页（视为失败）; " + msg
    # 用 DB 里的真实 attempt（认领时已 +1），不要用入参 row 的旧快照：
    # 旧快照在"人工重试重置过 attempt"后会给出错误的次数（审查 A P0-3）。
    attempts = d.book_attempt(row["id"])
    new_status = "dead" if attempts >= MAX_ATTEMPTS else "failed"
    d.finish_job(job, "failed", result.pages, total_pages,
                 result.bytes_done, result.outputs, msg[:500])
    d.set_status(row["id"], new_status, error=msg[:500])
    d.log(f"失败({attempts}/{MAX_ATTEMPTS}): {msg}", level="warn",
          source=src, book_id=row["id"])
    return True


def run_source_heartbeat(d, source_id: str, quota_n: int,
                         quality: str = "auto") -> tuple:
    """对单个源跑一轮心跳：取 queued 至多 quota_n 册逐本下载。

    每小时滑窗配额由 jobs 账本在 claim_and_start 内原子判定，
    跨进程（scheduler/Web 手动）共享，重启不清零。

    返回 (尝试册数, 成功册数, 是否因配额提前停止)。
    """
    rows = d.queued_books(source_id, limit=quota_n)
    if not rows:
        return 0, 0, False
    tried = ok = 0
    for row in rows:
        cont = fetch_one(d, row, quality, quota_n)
        tried += 1
        if d.get_book(row["id"])["status"] == "done":
            ok += 1
        if not cont:
            return tried, ok, True
    return tried, ok, False
