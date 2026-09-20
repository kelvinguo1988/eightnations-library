"""数据自检：CLI（manage.py doctor）与 Web 页面（/api/doctor）共用。"""
import json
import os
from typing import List, Tuple


def run_checks(d, books_dir: str, data_dir: str,
               verify_n: int = 0) -> Tuple[List[dict], int]:
    """执行全部检查，返回 (检查项列表, 问题数)。

    verify_n>0 时额外深度抽检最近 N 册 done 书：按 meta.json 台账
    复核每个产出 PDF 的 sha256 与页数（写侧曾有无总长截断判成功的
    漏洞，坏文件只有哈希/页数校验才能查出）。
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

    # 5) 数据目录挂载来源（匿名卷 → 迁移警告）
    from core.mounts import data_mount, migration_commands
    m = data_mount()
    if m["kind"] == "bind":
        add("数据目录挂载", True, f"已绑定宿主机固定路径：{m['source']}")
    elif m["kind"] == "volume":
        add("数据目录挂载", False,
            "⚠️ 数据在 Docker 匿名卷内（删容器/清理卷有丢失风险），"
            "请按设置页指引迁移到固定路径。原卷位置：" + (m["source"] or "?"))
    else:
        add("数据目录挂载", True, "开发环境（非 Linux 容器），跳过")

    # 6) 节流锁目录可写
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

    # 7) 深度抽检（可选）：按 meta.json 台账复核 sha256 与页数
    if verify_n > 0:
        from core.http import sha256_of
        from core import pdfbuild
        bad = []
        checked = 0
        rows = d.list_books(status="done", limit=100000)
        for r in rows[-verify_n:]:
            leaf = os.path.join(books_dir, r["source_id"],
                                r["collection"] or "misc", r["source_uid"])
            mpath = os.path.join(leaf, "meta.json")
            if not os.path.isfile(mpath):
                continue          # 无台账的历史产出物由"对账"项覆盖
            checked += 1
            tag = f"{r['source_id']}/{r['source_uid']}"
            try:
                with open(mpath, "r", encoding="utf-8") as f:
                    rec = json.load(f)
                for ent in rec.get("files") or []:
                    p = os.path.join(leaf, ent.get("path") or "")
                    if not os.path.isfile(p):
                        bad.append(f"{tag}: 台账文件缺失 {ent.get('path')}")
                        continue
                    if ent.get("sha256") and sha256_of(p) != ent["sha256"]:
                        bad.append(f"{tag}: sha256 不符 {ent.get('path')}")
                        continue
                    if ent.get("pages"):
                        try:
                            n = pdfbuild.pdf_page_count(p)
                        except Exception as e:
                            bad.append(f"{tag}: PDF 不可读 {ent.get('path')} ({e})")
                        else:
                            if n != ent["pages"]:
                                bad.append(
                                    f"{tag}: 页数不符 {ent.get('path')}"
                                    f"（台账 {ent['pages']} 实际 {n}）")
            except (OSError, ValueError) as e:
                bad.append(f"{tag}: 台账解析失败 {e}")
        add("产出物深度抽检", not bad,
            f"抽检 {checked} 册（sha256+页数），全部一致"
            if not bad else
            f"{len(bad)} 处异常: {bad[:5]}")

    return checks, issues
