"""LoC 目录快照采集器（半自动过盾）。

www.loc.gov 在 Cloudflare 盾后，元数据无法纯脚本获取；本工具把"发现"做成
低频人工辅助步骤（专藏一年更新几次），图像/PDF 下载不受影响（tile 直连）。

用法:
  # 模式 A: 弹出真实浏览器窗口，如遇人机验证请手动点一次复选框，之后全自动翻页
  python3 tools/loc_snapshot.py --headed \
      --collections yongle-da-dian [--with-items --item-limit 50]

  # 模式 B: 你在浏览器里把 ?fo=json 页面另存为 .json 后，交给工具归一化
  python3 tools/loc_snapshot.py --from-dir ~/Downloads/loc_saved

产出（snapshots/loc/<时间戳>/）:
  collection_<slug>.json   集合页完整快照（含 results）
  item_<lccn>.json         各条目详情（含官方 pdf 直链与逐页文件表）

依赖: playwright（仅 --headed 模式需要）；--from-dir 模式纯标准库。
"""
import argparse
import json
import os
import re
import sys
import time

BASE = "https://www.loc.gov/collections/{slug}/?fo=json&c={c}&sp={sp}"
ITEM = "https://www.loc.gov/item/{lccn}/?fo=json"
_SLUG_RE = re.compile(r"^[\w-]+$")


def slugify_collection(payload: dict, fallback: str = "") -> str:
    for p in payload.get("results", [{}])[0].get("partof") or []:
        title = p.get("title") if isinstance(p, dict) else str(p)
        if not title:
            continue
        t = title.lower()
        if "yongle" in t:
            return "yongle-da-dian"
        if "chinese rare book" in t:
            return "chinese-rare-books"
    return fallback or "collection"


def lccn_of(item_url: str) -> str:
    m = re.search(r"/item/([^/?]+)", item_url or "")
    return m.group(1) if m else ""


# ---------------------------------------------------------------- 模式 A
def fetch_headed(slug: str, out_dir: str, per_page: int, with_items: bool,
                 item_limit: int, headless: bool, max_pages: int) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("缺少 playwright: pip install playwright && playwright install chromium")
    profile = os.path.expanduser("~/.eightnations/browser-profile")
    os.makedirs(profile, exist_ok=True)

    def wait_json(page, timeout_s: int = 300) -> str:
        """等待页面 body 变成 JSON（过盾期间提示人工点击）。"""
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
                print("  !! 检测到人机验证，请在浏览器窗口中点击复选框…", flush=True)
                hinted = True
            time.sleep(2)
        sys.exit("  超时：未拿到 JSON（过盾未完成）")

    saved = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            locale="en-US", storage_state=None)
        page = ctx.new_page()
        print(f"[{slug}] 打开集合页（每页 {per_page} 条）…", flush=True)
        page.goto(BASE.format(slug=slug, c=per_page, sp=1), timeout=90_000)
        txt = wait_json(page)
        col = json.loads(txt)
        total_pages = int(col.get("pagination", {}).get("totalpages") or 1)
        out_path = os.path.join(out_dir, f"collection_{slug}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(txt)
        saved += 1
        print(f"  保存 {out_path}（results={len(col.get('results', []))}, "
              f"共 {total_pages} 页）", flush=True)

        item_urls = [r["id"] for r in col.get("results", [])
                     if "/item/" in str(r.get("id", ""))]
        for sp in range(2, min(total_pages, max_pages) + 1):
            time.sleep(2)
            page.goto(BASE.format(slug=slug, c=per_page, sp=sp), timeout=90_000)
            txt = wait_json(page)
            col = json.loads(txt)
            with open(os.path.join(out_dir, f"collection_{slug}_p{sp}.json"),
                      "w", encoding="utf-8") as f:
                f.write(txt)
            saved += 1
            item_urls += [r["id"] for r in col.get("results", [])
                          if "/item/" in str(r.get("id", ""))]
            print(f"  第 {sp}/{total_pages} 页完成", flush=True)

        if with_items:
            if item_limit:
                item_urls = item_urls[:item_limit]
            print(f"抓取 {len(item_urls)} 个条目详情…", flush=True)
            for i, u in enumerate(item_urls, 1):
                lccn = lccn_of(u)
                dest = os.path.join(out_dir, f"item_{lccn}.json")
                if os.path.exists(dest):
                    continue
                time.sleep(1.5)
                page.goto(re.sub(r"^http://", "https://", u) + "?fo=json",
                          timeout=90_000)
                txt = wait_json(page)
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(txt)
                if i % 20 == 0:
                    print(f"  条目 {i}/{len(item_urls)}", flush=True)
            saved += len(item_urls)
        browser.close()
    return saved


# ---------------------------------------------------------------- 模式 B
def normalize_from_dir(src_dir: str, out_dir: str) -> int:
    saved = 0
    for name in sorted(os.listdir(src_dir)):
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(src_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            print(f"[跳过] {name}: {e}")
            continue
        if isinstance(payload, dict) and "results" in payload:
            slug = slugify_collection(payload)
            dest = os.path.join(out_dir, f"collection_{slug}.json")
        elif isinstance(payload, dict) and "item" in payload:
            lccn = lccn_of(payload.get("id") or "")
            dest = os.path.join(out_dir, f"item_{lccn or name}.json")
        else:
            print(f"[跳过] {name}: 不像 LoC 快照（无 results/item 键）")
            continue
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print(f"[归档] {name} -> {os.path.relpath(dest)}")
        saved += 1
    return saved


def main() -> None:
    ap = argparse.ArgumentParser(description="LoC 目录快照采集器")
    ap.add_argument("--headed", action="store_true",
                    help="弹出浏览器自动抓（遇盾需人工点一次复选框）")
    ap.add_argument("--headless", action="store_true",
                    help="--headed 时改为无头（通常过不了盾，仅试跑用）")
    ap.add_argument("--from-dir", help="模式 B：解析手工另存的 .json 目录")
    ap.add_argument("--collections", default="yongle-da-dian",
                    help="模式 A：专藏 slug，逗号分隔（如 yongle-da-dian,chinese-rare-books）")
    ap.add_argument("--out", default=None, help="输出目录（默认 data/snapshots/loc/<ts>）")
    ap.add_argument("--c", type=int, default=100, help="每页条数（默认 100）")
    ap.add_argument("--max-pages", type=int, default=50, help="最多翻页数")
    ap.add_argument("--with-items", action="store_true",
                    help="同时抓取条目详情（拿到官方 pdf 直链，推荐）")
    ap.add_argument("--item-limit", type=int, default=0, help="条目详情上限（0=全部）")
    args = ap.parse_args()

    out = args.out or os.path.join("data", "snapshots", "loc",
                                   time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)

    if args.from_dir:
        n = normalize_from_dir(args.from_dir, out)
        print(f"归一化 {n} 个文件 -> {out}")
        return
    if args.headed or args.headless:
        total = 0
        for slug in [s.strip() for s in args.collections.split(",") if _SLUG_RE.match(s.strip())]:
            total += fetch_headed(slug, out, args.c, args.with_items,
                                  args.item_limit, args.headless, args.max_pages)
        print(f"完成：{total} 个 JSON -> {out}")
        return
    ap.error("请指定 --headed 或 --from-dir（二选一）")


if __name__ == "__main__":
    main()
