#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""八国联军图书馆 · 管理CLI（手动闭环；常驻批量获取用 scheduler.py）

用法:
  python3 manage.py init-db
  python3 manage.py import-snapshot data/snapshots/loc/<时间戳目录> [--source loc]
  python3 manage.py books [--status discovered] [--source loc] [--kw 永樂]
  python3 manage.py approve --id 3 | --collection yongle-da-dian
  python3 manage.py ignore  --id 3 | --collection yongle-da-dian
  python3 manage.py fetch --id 3 [--quality auto]
  python3 manage.py fetch-next [--source loc] [--quota 10]   # 单轮心跳
  python3 manage.py stats
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.db import DB                               # noqa: E402
from core.http import HttpClient                     # noqa: E402
from core.importer import import_snapshot_files      # noqa: E402
from core.pipeline import fetch_one, run_source_heartbeat, row_to_meta  # noqa: E402
from sites.loc import LocAdapter                     # noqa: E402

DATA_DIR = os.environ.get("EIGHTNATIONS_DATA",
                          os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "data"))
DB_PATH = os.path.join(DATA_DIR, "db", "library.db")


def db() -> DB:
    d = DB(DB_PATH)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    d.init()
    return d


# ---------------------------------------------------------------- import
def cmd_import_snapshot(args) -> None:
    d = db()
    new, updated = import_snapshot_files(d, args.source, args.target,
                                         args.collection or "")
    if new == 0 and updated == 0:
        print(f"[跳过] 未找到可用快照: {args.target}")
        return
    counts = d.count_by_status(args.source)
    print(f"完成：新书 {new}，更新 {updated}；当前状态 {counts}")
    print("下一步: python3 manage.py approve --collection <slug> 或 --id N")


# ---------------------------------------------------------------- list/stats
def cmd_books(args) -> None:
    d = db()
    rows = d.list_books(status=args.status or "", source_id=args.source or "",
                        collection=args.collection or "", keyword=args.kw or "",
                        limit=args.limit, offset=args.offset)
    for r in rows:
        title = r["alt_title"] or r["title"]
        yr = r["year_start"] or "?"
        print(f"#{r['id']:<4} [{r['status']:<10}] {r['era'] or '—'}{yr}  "
              f"{title[:46]:<48} {r['shelf_id']}")
    counts = d.count_by_status(args.source or "")
    print(f"-- 共 {len(rows)} 行；状态合计 {counts}")


def cmd_stats(args) -> None:
    d = db()
    with d.connect() as conn:
        for r in conn.execute(
                "SELECT source_id, status, COUNT(*) n FROM books "
                "GROUP BY source_id, status ORDER BY source_id"):
            print(f"  {r['source_id']:<8} {r['status']:<12} {r['n']}")
        jobs = conn.execute(
            "SELECT state, COUNT(*) n, SUM(bytes_done) b FROM jobs "
            "GROUP BY state").fetchall()
    for j in jobs:
        gb = (j["b"] or 0) / 1e9
        print(f"  jobs {j['state']:<8} {j['n']}  ({gb:.2f} GB)")


# ---------------------------------------------------------------- approve/ignore
def _decide(args, d: DB, status: str) -> None:
    if args.id:
        ids = [args.id]
    else:
        rows = d.list_books(status="discovered", source_id=args.source or "",
                            collection=args.collection or "",
                            keyword=args.kw or "", limit=100000)
        ids = [r["id"] for r in rows]
    if not ids:
        print("没有匹配的 discovered 书目")
        return
    for i in ids:
        d.set_status(i, status)
    shown = ids if len(ids) < 20 else str(ids[:20]) + "…"
    print(f"{status}: {len(ids)} 条 -> {shown}")


def cmd_approve(args) -> None:
    _decide(args, db(), "queued")


def cmd_ignore(args) -> None:
    _decide(args, db(), "ignored")


# ---------------------------------------------------------------- fetch
def cmd_fetch(args) -> None:
    d = db()
    row = d.get_book(args.id)
    if not row:
        sys.exit(f"无此书 id={args.id}")
    from core.limiter import HourQuota
    fetch_one(d, row, args.quality, HourQuota(default_quota=10 ** 6))


def cmd_fetch_next(args) -> None:
    d = db()
    tried, ok, hit_quota = run_source_heartbeat(d, args.source, args.quota,
                                                args.quality)
    if hit_quota:
        print("达到每小时配额，本轮结束")
    counts = d.count_by_status(args.source)
    print(f"[{args.source}] 本轮尝试 {tried} 本，成功 {ok}；状态 {counts}")


def cmd_import_na_jp(args) -> None:
    """国立公文書館 fonds 实时收割入库（预算制增量，防封禁；中断可重跑续传）。"""
    from sites.na_jp import NaJpAdapter
    d = db()
    adapter = NaJpAdapter(HttpClient())
    with d.connect() as conn:
        known = {r["source_uid"] for r in conn.execute(
            "SELECT source_uid FROM books WHERE source_id='na_jp'")}
    new = 0

    def on_meta(meta):
        nonlocal new
        if d.upsert_book("na_jp", meta.__dict__):
            new += 1

    stats = adapter.harvest_step(args.fonds, known_uids=known,
                                 budget=args.budget, max_pages=args.pages,
                                 on_meta=on_meta)
    counts = d.count_by_status("na_jp")
    warn = "；⚠️ 遇站点限流，稍后重跑同一命令自动续传" if stats["blocked"] else ""
    print(f"总 {stats['ids']} 条，新抓 {stats['fetched']}（新书 {new}），"
          f"跳过已存在 {stats['skipped']}{warn}")
    print(f"状态 {counts}")
    if stats["fetched"] or stats["blocked"]:
        print("下一步: python3 manage.py approve --source na_jp")


def cmd_import_details(args) -> None:
    """把 tools/loc_fill_details.py 采到的 item_<lccn>.json 合并进已有书目。"""
    d = db()
    if not os.path.isdir(args.target):
        sys.exit(f"目录不存在: {args.target}")
    merged = gained_pdf = still_missing = 0
    for name in sorted(os.listdir(args.target)):
        m = re.match(r"item_(.+)\.json$", name)
        if not m:
            continue
        row = d.find_book("loc", m.group(1))
        if not row:
            continue
        try:
            with open(os.path.join(args.target, name), "r", encoding="utf-8") as f:
                detail = json.load(f)
        except Exception:
            continue
        meta = row_to_meta(row)
        LocAdapter._merge_resources(meta, detail.get("resources"))
        d.upsert_book("loc", meta.__dict__)
        merged += 1
        if any(meta.pdf_urls):
            gained_pdf += 1
        elif not any(meta.page_files):
            still_missing += 1
    print(f"合并 {merged} 条（新获官方PDF {gained_pdf} 条，"
          f"仍无可用图像 {still_missing} 条）。"
          f"对 failed 书目执行: python3 manage.py retry --failed 或在任务面板点重试")


def cmd_retry(args) -> None:
    d = db()
    if args.failed:
        rows = d.list_books(status="failed", source_id=args.source or "loc",
                            limit=100000)
        ids = [r["id"] for r in rows]
    elif args.id:
        ids = [args.id]
    else:
        sys.exit("需要 --id N 或 --failed")
    for i in ids:
        d.set_status(i, "queued")
    print(f"重排 {len(ids)} 条 -> queued")


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="八国联军图书馆 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db").set_defaults(func=lambda a: (
        db(), print(f"数据库已初始化: {DB_PATH}")))

    p = sub.add_parser("import-snapshot")
    p.add_argument("target")
    p.add_argument("--source", default="loc")
    p.add_argument("--collection", default="")
    p.set_defaults(func=cmd_import_snapshot)

    p = sub.add_parser("books")
    p.add_argument("--status", default="")
    p.add_argument("--source", default="")
    p.add_argument("--collection", default="")
    p.add_argument("--kw", default="")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_books)

    p = sub.add_parser("approve")
    p.add_argument("--id", type=int)
    p.add_argument("--source", default="")
    p.add_argument("--collection", default="")
    p.add_argument("--kw", default="")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("ignore")
    p.add_argument("--id", type=int)
    p.add_argument("--source", default="")
    p.add_argument("--collection", default="")
    p.add_argument("--kw", default="")
    p.set_defaults(func=cmd_ignore)

    p = sub.add_parser("fetch")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--quality", default="auto")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("fetch-next")
    p.add_argument("--source", default="loc")
    p.add_argument("--quota", type=int, default=10)
    p.add_argument("--quality", default="auto")
    p.set_defaults(func=cmd_fetch_next)

    p = sub.add_parser("import-details")
    p.add_argument("target")
    p.set_defaults(func=cmd_import_details)

    p = sub.add_parser("import-na-jp")
    p.add_argument("--fonds", required=True,
                   help="fonds 列表页 URL，如 .../fonds/3611449?page=1")
    p.add_argument("--pages", type=int, default=50, help="最多扫的列表页数")
    p.add_argument("--budget", type=int, default=0,
                   help="单次最多抓取条目数（0=不限；默认不限，被限流时重跑续传）")
    p.set_defaults(func=cmd_import_na_jp)

    p = sub.add_parser("retry")
    p.add_argument("--id", type=int)
    p.add_argument("--failed", action="store_true")
    p.add_argument("--source", default="loc")
    p.set_defaults(func=cmd_retry)

    sub.add_parser("stats").set_defaults(func=cmd_stats)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
