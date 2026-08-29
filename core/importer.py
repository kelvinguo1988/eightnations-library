"""目录快照导入：CLI（manage.py）与 Web 上传共用。

目录结构约定（与 tools/loc_snapshot.py 产出一致）:
  collection_*.json  集合快照（results 列表）
  item_<uid>.json    条目详情（补逐页清单/官方 PDF 直链）
"""
import json
import os
import re
from typing import Any, Dict, List, Tuple

from core.db import DB
from core.http import HttpClient
from core.models import BookMeta
from sites import get_adapter
from sites.loc import LocAdapter


def import_collection_payload(d: DB, source_id: str, payload: Dict[str, Any],
                              collection_slug: str = "") -> Tuple[int, int, List[BookMeta]]:
    """导入一个集合快照 payload，返回 (新增, 更新, metas)。"""
    if not (isinstance(payload, dict) and "results" in payload):
        return 0, 0, []
    adapter = get_adapter(source_id, HttpClient())
    metas = adapter.parse_snapshot(payload, collection_slug)
    new = updated = 0
    for meta in metas:
        if d.upsert_book(source_id, meta.__dict__):
            new += 1
            row = d.find_book(source_id, meta.source_uid)
            d.log(f"新书发现: {meta.alt_title or meta.title}",
                  source=source_id, book_id=row["id"] if row else None)
        else:
            updated += 1
    return new, updated, metas


def merge_item_details(d: DB, source_id: str, items_dir: str,
                       by_uid: Dict[str, BookMeta]) -> int:
    """把 item_<uid>.json 详情合并进 metas（内存对象，随后一并 upsert）。"""
    if not os.path.isdir(items_dir):
        return 0
    merged = 0
    for name in os.listdir(items_dir):
        m = re.match(r"item_(.+)\.json$", name)
        if not m or m.group(1) not in by_uid:
            continue
        try:
            with open(os.path.join(items_dir, name), "r", encoding="utf-8") as f:
                detail = json.load(f)
        except Exception:
            continue
        LocAdapter._merge_resources(by_uid[m.group(1)], detail.get("resources"))
        merged += 1
    return merged


def import_snapshot_files(d: DB, source_id: str, target: str,
                          collection_slug: str = "") -> Tuple[int, int]:
    """导入目录（或单文件）。返回 (新增, 更新)。"""
    if os.path.isdir(target):
        files = [os.path.join(target, f) for f in sorted(os.listdir(target))
                 if f.startswith("collection_") and f.endswith(".json")]
        items_dir = target
    else:
        files = [target]
        items_dir = os.path.dirname(target)
    if not files:
        return 0, 0
    new_total = update_total = 0
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            continue
        new, updated, metas = import_collection_payload(
            d, source_id, payload, collection_slug)
        by_uid = {m.source_uid: m for m in metas}
        if merge_item_details(d, source_id, items_dir, by_uid):
            # 详情合并后需重写一次（补 pdf_urls / files_json）
            for meta in metas:
                d.upsert_book(source_id, meta.__dict__)
        new_total += new
        update_total += updated
    return new_total, update_total
