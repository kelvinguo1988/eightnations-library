"""补齐 LoC 条目详情（仅针对库里缺官方 PDF 直链且缺逐页清单的书）。

背景: 集合级快照对多数中国善本条目只给页数整数，不给逐页 IIIF 清单；
这些书下载前需要 item ?fo=json 详情。详情在 Cloudflare 盾后，故本工具
用真实浏览器半自动采集：弹出窗口，如遇人机验证请点一次复选框，之后
全自动逐条抓取（~1.2s/条，1977 条约 40 分钟），支持断点续跑。

用法:
  python3 tools/loc_fill_details.py            # 采集到 data/snapshots/loc/details/
  python3 manage.py import-details data/snapshots/loc/details   # 合并进库
之后对 failed 的书重试（web 任务面板"重试"按钮或 manage.py approve）。
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.db import DB  # noqa: E402

DATA_DIR = os.environ.get("EIGHTNATIONS_DATA",
                          os.path.join(os.path.dirname(os.path.dirname(
                              os.path.abspath(__file__))), "data"))
DB_PATH = os.path.join(DATA_DIR, "db", "library.db")


def pending_books(limit: int):
    d = DB(DB_PATH)
    d.init()
    rows = d.list_books(source_id="loc", limit=100000)
    out = []
    for r in rows:
        pdfs = json.loads(r["pdf_urls"] or "[]")
        files = json.loads(r["files_json"] or "[]")
        if not any(pdfs) and not any(files):
            out.append((r["id"], r["source_uid"]))
    return out[:limit] if limit else out


def fetch_details(out_dir: str, limit: int, headless: bool) -> None:
    todo = pending_books(limit)
    print(f"待补详情: {len(todo)} 条 -> {out_dir}")
    if not todo:
        return
    os.makedirs(out_dir, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("缺少 playwright: pip install playwright && playwright install chromium")

    def wait_json(page, timeout_s: int = 420) -> str:
        deadline = time.time() + timeout_s
        hinted = False
        while time.time() < deadline:
            try:
                t = page.evaluate("document.body ? document.body.innerText : ''")
            except Exception:
                t = ""
            if t.strip().startswith("{"):
                return t
            try:
                pt = page.title()
            except Exception:
                pt = ""
            if not hinted and "moment" in (pt or "").lower():
                print("  !! 请在浏览器窗口点击人机验证复选框（只需一次）…", flush=True)
                hinted = True
            time.sleep(2)
        return ""

    done = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            locale="en-US")
        page = ctx.new_page()
        page.goto(f"https://www.loc.gov/item/{todo[0][1]}/?fo=json", timeout=90_000)
        if not wait_json(page):
            sys.exit("首个条目过盾超时，请重跑")
        print("会话已通过，开始批量采集…", flush=True)
        for i, (book_id, lccn) in enumerate(todo, 1):
            dest = os.path.join(out_dir, f"item_{lccn}.json")
            if os.path.exists(dest):
                continue
            try:
                page.goto(f"https://www.loc.gov/item/{lccn}/?fo=json", timeout=60_000)
                txt = wait_json(page, timeout_s=60)
            except Exception:
                txt = ""
            if txt.strip().startswith("{"):
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(txt)
                done += 1
            else:
                print(f"  [{i}/{len(todo)}] {lccn} 失败，跳过（可重跑补齐）", flush=True)
            if i % 50 == 0:
                print(f"  进度 {i}/{len(todo)}（本次成功 {done}）", flush=True)
            time.sleep(1.2)
        browser.close()
    print(f"完成：本次新采 {done} 条。运行:"
          f" python3 manage.py import-details {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(description="补齐 LoC 条目详情")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "snapshots", "loc", "details"))
    ap.add_argument("--limit", type=int, default=0, help="本次最多采集条数（0=全部）")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--list-only", action="store_true", help="只打印待补数量")
    args = ap.parse_args()
    if args.list_only:
        print(f"待补详情: {len(pending_books(0))} 条")
        return
    fetch_details(args.out, args.limit, args.headless)


if __name__ == "__main__":
    main()
