"""美国国会图书馆（LoC）适配器。

要点（2026-08-29 实测）:
  * 元数据（www.loc.gov ?fo=json）在 Cloudflare 盾后 —— 由 tools/loc_snapshot.py
    半自动产出快照，本模块只做离线解析；
  * 图像与官方 PDF 在 tile.loc.gov，无盾，可长期直连下载：
      - resources[i].pdf      官方整本 PDF（约 200KB/页）—— 首选；
      - resources[i].files    每页 JPEG 变体（IIIF，pct:100/50/25/…）—— 兜底；
        IIIF 路径支持 /full/<w>,/ 任意宽度（实测 1600px 可用），用于 mid/thumb 档。
"""
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from core.http import HttpClient, sha256_of
from core.limiter import Progress
from core.models import BookMeta, DownloadResult
from core import pdfbuild

# 朝代启发式：LoC created_published/date 里的英文纪年关键词
_ERA_RULES = [
    (("song", "sung", "960", "1279"), "宋"),
    (("yuan", "1260", "1368"), "元"),
    (("ming", "1368", "1644"), "明"),
    (("qing", "ching", "1644", "1911"), "清"),
    (("republic", "ming-kuo", "1912", "1949"), "民国"),
]
# 集合级结果常只有年份（如 "1562"），按年份区间兜底
_ERA_BY_YEAR = [
    (1912, 1949, "民国"), (1644, 1911, "清"), (1368, 1644, "明"),
    (1271, 1368, "元"), (960, 1279, "宋"), (618, 907, "唐"),
]
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_TAG_RE = re.compile(r"<[^>]+>")


def _lccn_of(item_id: str) -> str:
    m = re.search(r"/item/([^/?]+)", item_id or "")
    return m.group(1) if m else ""


def _pick_cjk(entries: Any) -> str:
    for e in entries or []:
        if isinstance(e, str) and _CJK_RE.search(e):
            return e.strip().rstrip("/").strip()
    return ""


def _first_str(entries: Any) -> str:
    for e in entries or []:
        if isinstance(e, str) and e.strip():
            return e.strip()
    return ""


def _era_and_years(result: Dict[str, Any]):
    text = " ".join(str(x) for x in [
        result.get("date"), _first_str(result.get("created_published")),
        json.dumps(result.get("dates") or [], ensure_ascii=False)])
    era = next((zh for keys, zh in _ERA_RULES
                if any(k in text.lower() for k in keys)), "")
    nums = [int(y) for y in re.findall(r"\b(1[0-9]{3})\b", text)]
    year = None
    d = str(result.get("date") or "")
    m = re.match(r"^\s*(\d{4})", d)
    if m:
        year = int(m.group(1))
    elif nums:
        year = nums[0]
    year_start = year or (nums[0] if nums else None)
    year_end = max(nums) if len(nums) > 1 else year_start
    if not era and year_start:
        era = next((zh for lo, hi, zh in _ERA_BY_YEAR
                    if lo <= year_start <= hi), "")
    return era, year_start, year_end


def _collection_slug(result: Dict[str, Any], fallback: str = "") -> str:
    for p in result.get("partof") or []:
        title = p.get("title") if isinstance(p, dict) else p
        t = (title or "").lower()
        if "yongle" in t:
            return "yongle-da-dian"
        if "chinese rare book" in t:      # 馆方字段为单数 "book"
            return "chinese-rare-books"
    return fallback


class LocAdapter:
    id = "loc"
    name = "美国国会图书馆"
    flag = "🇺🇸"

    def __init__(self, http: HttpClient):
        self.http = http

    # ---------------- 发现层（离线解析快照） ----------------
    def parse_snapshot(self, payload: Any, collection_slug: str = ""
                       ) -> List[BookMeta]:
        if isinstance(payload, (str, bytes, os.PathLike)):
            with open(payload, "r", encoding="utf-8") as f:
                payload = json.load(f)
        out: List[BookMeta] = []
        for result in payload.get("results", []):
            item_id = str(result.get("id") or "")
            lccn = _lccn_of(item_id) or _first_str(result.get("number_lccn"))
            if not lccn:
                continue  # 集合页可能混有合集/主题等非书条目
            era, y0, y1 = _era_and_years(result)
            rights = result.get("rights")
            if isinstance(rights, list):
                rights = " ".join(str(x) for x in rights)
            rights = _TAG_RE.sub("", str(rights or "")).strip()[:300]
            seen: set = set()
            subjects: List[str] = []
            for s in result.get("subject") or []:
                if isinstance(s, str) and s.strip() and s.lower() not in seen:
                    seen.add(s.lower())
                    subjects.append(s.strip())
            meta = BookMeta(
                source_uid=lccn,
                title=str(result.get("title") or "").strip(),
                alt_title=_pick_cjk(result.get("other_title")),
                era=era, year_start=y0, year_end=y1,
                language=_first_str(result.get("language")),
                subjects=subjects[:12],
                item_url=re.sub(r"^http://", "https://", item_id).rstrip("/")
                or f"https://www.loc.gov/item/{lccn}/",
                cover_url=_first_str(result.get("image_url")),
                collection=_collection_slug(result, collection_slug),
                rights=rights,
                shelf_id=str(result.get("shelf_id") or ""),
                raw=result,
            )
            self._merge_resources(meta, result.get("resources"))
            out.append(meta)
        return out

    @staticmethod
    def _merge_resources(meta: BookMeta, resources: Any) -> None:
        """快照/详情里的 resources -> pdf_urls + page_files。

        files 在集合级是整数(页数)，在条目详情级是逐页变体列表。
        page_files[i] = 第 i 卷的"页列表"，每页是该页的 JPEG 变体列表
        （下载时按宽度选最优变体）。
        """
        if not isinstance(resources, list):
            return
        pdfs, page_files, pages_total = [], [], 0
        for res in resources:
            if not isinstance(res, dict):
                continue
            pdfs.append(res.get("pdf") or "")
            files = res.get("files")
            pages: List[List[Dict[str, Any]]] = []
            if isinstance(files, list):
                for page in files:
                    variants = []
                    entries = page if isinstance(page, list) else [page]
                    for v in entries:
                        if isinstance(v, dict) and \
                                str(v.get("mimetype", "")).endswith("jpeg"):
                            variants.append(
                                {"url": v.get("url"),
                                 "mimetype": v.get("mimetype"),
                                 "width": v.get("width"),
                                 "height": v.get("height")})
                    if variants:
                        pages.append(variants)
            page_files.append(pages)
            pages_total += len(pages)
        if not pages_total and resources and \
                isinstance(resources[0].get("files"), int):
            pages_total = int(resources[0]["files"])
        meta.pdf_urls = pdfs
        meta.page_files = page_files
        meta.volume_count = len(resources)
        meta.page_count = pages_total

    # ---------------- 下载层（只碰 tile.loc.gov） ----------------
    def download_item(self, meta: BookMeta, dest_dir: str, http: HttpClient,
                      quality: str = "auto",
                      progress: Optional[Progress] = None) -> DownloadResult:
        os.makedirs(dest_dir, exist_ok=True)
        outputs: List[str] = []
        pages_done, bytes_done = 0, 0
        errors: List[str] = []
        started = time.time()

        n_res = max(len(meta.pdf_urls), len(meta.page_files), 1)
        for i in range(n_res):
            pdf_url = meta.pdf_urls[i] if i < len(meta.pdf_urls) else ""
            pages = meta.page_files[i] if i < len(meta.page_files) else []
            suffix = "" if n_res == 1 else f"_{i + 1:02d}"
            out_pdf = os.path.join(dest_dir, f"book{suffix}.pdf")

            use_pdf = pdf_url and quality in ("auto", "pdf")
            if quality == "pdf" and not pdf_url:
                errors.append(f"卷{i + 1}: quality=pdf 但无官方 PDF 链接")
                continue
            if not use_pdf:
                # IIIF 组图兜底（orig/mid/thumb/auto-无PDF）
                tier = {"orig": "pct:100.0", "mid": "1600,",
                        "thumb": "1024,"}.get(
                            quality if quality in ("orig", "mid", "thumb")
                            else "mid", "1600,")
                n = self._assemble_from_images(pages, tier, dest_dir,
                                               out_pdf, http, progress)
                if n > 0:
                    outputs.append(out_pdf)
                    pages_done += n
                    bytes_done += os.path.getsize(out_pdf)
                elif pages:
                    errors.append(f"卷{i + 1}: IIIF 组图失败(0页)")
                else:
                    errors.append(
                        f"卷{i + 1}: 无官方PDF直链且缺逐页清单"
                        f"——运行 tools/loc_fill_details.py 补详情后重试")
                continue

            ok = http.download(pdf_url, out_pdf, min_bytes=100_000)
            if ok:
                try:
                    n = pdfbuild.pdf_page_count(out_pdf)
                    outputs.append(out_pdf)
                    pages_done += n
                    bytes_done += os.path.getsize(out_pdf)
                except Exception as e:
                    errors.append(f"卷{i + 1}: PDF 校验失败 {e}")
            else:
                errors.append(f"卷{i + 1}: 官方 PDF 下载失败")

        # 封面缩略图（网格展示用；失败不致命）
        cover_path = ""
        if meta.cover_url:
            cover_path = os.path.join(dest_dir, "cover.jpg")
            if not self._download_cover(meta.cover_url, cover_path, http):
                cover_path = ""

        result = DownloadResult(
            ok=bool(outputs) and not errors,
            outputs=outputs, pages=pages_done, bytes_done=bytes_done,
            errors=errors)
        self._write_meta_json(dest_dir, meta, result, quality, started)
        return result

    def _assemble_from_images(self, pages: List[List[Dict[str, Any]]], tier: str,
                              dest_dir: str, out_pdf: str, http: HttpClient,
                              progress: Optional[Progress]) -> int:
        """逐页下载（每页取宽度最大的 JPEG 变体，按 tier 改写 IIIF 尺寸）→ 组 PDF。"""
        if not pages:
            return 0
        pages_dir = os.path.join(dest_dir, "_pages")
        os.makedirs(pages_dir, exist_ok=True)
        paths: List[str] = []
        for idx, variants in enumerate(pages, 1):
            if not variants:
                continue
            best = max(variants,
                       key=lambda v: int(v.get("width") or 0))
            url = self._iiif_tier_url(str(best.get("url") or ""), tier)
            if not url:
                continue
            path = os.path.join(pages_dir, f"page_{idx:04d}.jpg")
            if not (os.path.exists(path) and os.path.getsize(path) > 10_000):
                if not http.download(url, path, min_bytes=10_000):
                    continue
            paths.append(path)
            if progress:
                progress.tick(1, len(pages))
        if not paths:
            return 0
        pdfbuild.build_pdf(paths, out_pdf)
        if len(paths) >= len(pages) - 2:      # 允许个别缺页，其余清理
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

    @staticmethod
    def _iiif_tier_url(url: str, tier: str) -> str:
        # https://tile.loc.gov/image-services/iiif/public:a:b/full/pct:100.0/0/default.jpg
        # -> /full/<tier>/0/default.jpg
        return re.sub(r"/full/[^/]+/", f"/full/{tier}/", url, count=1)

    def _download_cover(self, cover_url: str, dest: str, http: HttpClient) -> bool:
        url = self._iiif_tier_url(cover_url, "480,")
        return http.download(url, dest, min_bytes=2_000)

    @staticmethod
    def _write_meta_json(dest_dir: str, meta: BookMeta, result: DownloadResult,
                         quality: str, started: float) -> None:
        record = {
            "source": "loc",
            "source_uid": meta.source_uid,
            "title": meta.title,
            "alt_title": meta.alt_title,
            "era": meta.era,
            "years": [meta.year_start, meta.year_end],
            "shelf_id": meta.shelf_id,
            "collection": meta.collection,
            "item_url": meta.item_url,
            "rights": meta.rights,
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
        with open(os.path.join(dest_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=1)
