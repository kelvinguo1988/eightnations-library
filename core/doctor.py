"""数据自检：CLI（manage.py doctor）与 Web 页面（/api/doctor）共用。"""
import os
from typing import List, Tuple


def run_checks(d, books_dir: str, data_dir: str) -> Tuple[List[dict], int]:
    """执行全部检查，返回 (检查项列表, 问题数)。

    检查项: {name, ok, detail}。
    """
    checks: List[dict] = []
    issues = 0

    def add(name: str, ok: bool, detail: str) -> None:
        nonlocal issues
        checks.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            issues += 1

    # 1) 库结构与迁移列
    with d.connect() as conn:
        scols = {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
        bcols = {r[1] for r in conn.execute("PRAGMA table_info(books)")}
        pcols = {r[1] for r in conn.execute("PRAGMA table_info(reading_progress)")}
    missing = [c for c in ("catalog_url", "last_catalog_at") if c not in scols] \
        + [c for c in ("subjects", "favorite") if c not in bcols]
    if pcols and "theme" not in pcols:
        missing.append("reading_progress.theme")
    add("数据库结构", not missing,
        "全部迁移列就绪" if not missing else f"缺列: {missing}")

    # 2) done 书目 vs 磁盘 PDF 对账
    rows = d.list_books(status="done", limit=100000)
    n_ok = 0
    missing_pdf = []
    for r in rows:
        leaf = os.path.join(books_dir, r["source_id"],
                            r["collection"] or "misc", r["source_uid"])
        pdfs = []
        if os.path.isdir(leaf):
            pdfs = [f for f in os.listdir(leaf)
                    if f.endswith(".pdf") and not f.endswith(".part")]
        if pdfs:
            n_ok += 1
        else:
            missing_pdf.append(
                f"{r['source_id']}/{r['collection']}/{r['source_uid']}")
    add("已归档 PDF 对账", not missing_pdf,
        f"done {len(rows)} 册，磁盘有 PDF {n_ok} 册"
        + (f"；缺失 {len(missing_pdf)}: {missing_pdf[:3]}"
           if missing_pdf else ""))

    # 3) 磁盘孤儿目录（有 meta.json 但库里无记录）
    orphans = []
    if os.path.isdir(books_dir):
        for root, _, files in os.walk(books_dir):
            if os.path.isfile(os.path.join(root, "meta.json")):
                uid = os.path.basename(root)
                col = os.path.basename(os.path.dirname(root))
                src = os.path.basename(os.path.dirname(os.path.dirname(root)))
                if not d.find_book(src, uid):
                    orphans.append(f"{src}/{col}/{uid}")
    add("孤儿目录", not orphans,
        "无（磁盘与库一致）" if not orphans
        else f"{len(orphans)} 个目录无库记录: {orphans[:3]}（可清理或重导）")

    # 4) 站点配置健康
    bad_cfg = []
    with d.connect() as conn:
        for r in conn.execute("SELECT * FROM sources"):
            if r["enabled"] and r["meta_strategy"] == "direct" \
                    and not r["catalog_url"]:
                bad_cfg.append(r["id"])
    add("站点配置", not bad_cfg,
        "各馆配置正常" if not bad_cfg
        else f"已启用的 direct 站点缺目录 URL: {bad_cfg}")

    # 5) 节流锁目录可写
    rt = os.path.join(data_dir, "runtime", "throttle")
    try:
        os.makedirs(rt, exist_ok=True)
        probe = os.path.join(rt, ".doctor")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        add("节流锁目录", True, f"可写：{rt}")
    except OSError as e:
        add("节流锁目录", False, f"不可写：{e}")

    return checks, issues
