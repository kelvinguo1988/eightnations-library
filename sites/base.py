"""站点适配器契约：每馆一个文件，实现 harvest(解析快照) + download_item。

同时承载各馆下载层的公共实现（IIIF 组图、meta.json 台账），
避免 loc/bnf/na_jp 逐行拷贝。
"""
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Protocol, Set

from core import pdfbuild
from core.http import HttpClient, sha256_of
from core.limiter import Progress
from core.models import BookMeta, DownloadResult


class SourceAdapter(Protocol):
    id: str            # 与 sources 表 id 一致
    name: str
    flag: str

    def parse_snapshot(self, payload: Any, collection_slug: str = "") -> List[BookMeta]:
        """发现层：把目录快照 JSON（集合页 ?fo=json）解析为书目列表。

        快照由 tools/<id>_snapshot.py 产出（半自动过盾），解析本身不联网。
        无快照流程的 direct 策略馆（如 bnf）返回空列表即可。
        """
        ...

    def download_item(self, meta: BookMeta, dest_dir: str, http: HttpClient,
                      quality: str = "auto",
                      progress: Optional[Progress] = None) -> DownloadResult:
        """下载层：只访问公开图床/文件服务（不碰被盾的元数据站）。

        quality: auto(官方PDF优先,否则1600px组图) / pdf / orig / mid / thumb
        dest_dir 内产出: book.pdf(或 vol_NN.pdf) + cover.jpg + meta.json
        页级断点续传: 已存在且达标的目标文件跳过。
        成功语义（各馆统一）: 有产出即 ok=True；缺卷/部分失败记入
        result.errors（进 meta.json 与日志），由断点续传补齐。
        """
        ...


class HarvestAdapter(Protocol):
    """可选能力：direct 策略馆（目录可直连）额外实现增量收割。

    scheduler 通过 getattr(adapter, "harvest_step") 鸭子类型发现，不 import
    本 Protocol；这里固化签名与返回契约，scheduler 按 sources.meta_strategy
    == "direct" 决定是否调用（字段名沿用 DB/scheduler 现有用法）。

    约定:
      * 每心跳调用一次，budget = 本轮最多新入库条目数，known_uids 内的
        条目跳过（每周巡检近乎零详情请求）；
      * 触发站点限流时置 blocked=True 立即收尾上报，调用方安排冷却续传；
      * 返回值必须含 {ids, fetched, skipped, blocked, exhausted}；
        exhausted=True 表示目录已全量处理完（scheduler 据此标记收割时间）；
      * 本轮请求总数（列表页 + 详情）必须与站点限流线自洽，假设写在各馆
        harvest_step docstring。
    """

    def harvest_step(self, catalog_url: str,
                     known_uids: Optional[Set[str]] = None,
                     budget: int = 40, max_pages: int = 10,
                     on_meta: Optional[Callable[[BookMeta], None]] = None
                     ) -> Dict[str, Any]:
        ...


# ---------------- 下载层公共实现 ----------------

def iiif_size_url(url: str, tier: str) -> str:
    """改写 IIIF 图像 URL 的 size 段：/full/<size>/... -> /full/<tier>/...

    tile.loc.gov（/full/pct:100.0/0/default.jpg）与 gallica
    （/full/full/0/native.jpg）的页 URL 同构，两处共用。
    """
    return re.sub(r"/full/[^/]+/", f"/full/{tier}/", url, count=1)


def assemble_pages_to_pdf(pages: List[List[Dict[str, Any]]], out_pdf: str,
                          pages_dir: str, http: HttpClient, tier: str,
                          progress: Optional[Progress] = None,
                          min_bytes: int = 10_000,
                          allow_missing: int = 2) -> int:
    """IIIF 逐页组图公共实现：选最优变体下载 -> 组 PDF -> 收尾清理。

    pages: 每页一个变体列表 [{url, width, ...}, ...]，取宽度最大者，
    经 iiif_size_url 改写为 tier 档；
    页级断点续传: 已存在且达标的页跳过（http.download 对已存在文件跳读）；
    pages_dir 必须每卷独立（如 _pages_01/），多卷共用会把上一卷残留页
    当成本卷已下载页 → 串页 PDF；
    成功（缺页 ≤ allow_missing）才清页图，否则保留供下次续传。
    返回成稿页数，0 = 失败。
    """
    if not pages:
        return 0
    os.makedirs(pages_dir, exist_ok=True)
    paths: List[str] = []
    for idx, variants in enumerate(pages, 1):
        if not variants:
            continue
        best = max(variants, key=lambda v: int(v.get("width") or 0))
        url = iiif_size_url(str(best.get("url") or ""), tier)
        if not url:
            continue
        path = os.path.join(pages_dir, f"page_{idx:04d}.jpg")
        if not (os.path.exists(path) and os.path.getsize(path) > min_bytes):
            if not http.download(url, path, min_bytes=min_bytes):
                continue
        paths.append(path)
        if progress:
            progress.tick(1, len(pages))
    if not paths:
        return 0
    pdfbuild.build_pdf(paths, out_pdf)
    if len(paths) >= len(pages) - allow_missing:
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(pages_dir)
        except OSError:
            pass
    return len(paths)


def write_meta_json(dest_dir: str, source: str, meta: BookMeta,
                    result: DownloadResult, started: float, *,
                    quality: str = "",
                    extra: Optional[Dict[str, Any]] = None) -> None:
    """产出物台账 meta.json（各馆 _write_meta 公共实现）。

    extra 并入各馆特有字段（era/years/collection/rights/alt_title…）。
    """
    record: Dict[str, Any] = {
        "source": source,
        "source_uid": meta.source_uid,
        "title": meta.title,
        "item_url": meta.item_url,
        "quality": quality,
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_s": round(time.time() - started, 1),
        "pages": result.pages,
        "bytes": result.bytes_done,
        "errors": result.errors,
        "files": [
            {"path": os.path.basename(p), "sha256": sha256_of(p),
             "bytes": os.path.getsize(p)}
            for p in result.outputs],
    }
    if extra:
        record.update(extra)
    with open(os.path.join(dest_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=1)
