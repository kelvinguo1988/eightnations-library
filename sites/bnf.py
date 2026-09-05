"""法国国家图书馆 BnF / Gallica 适配器（direct 策略）。

可用性（2026-08-30 实测，严格限速逐项验证）:
  * catalogue.bnf.fr SRU 开放：`bib.digitized all "freeAccess"` 可筛出 Gallica
    自由访问文献（数字化中文语种文献 1,011 条），UNMARC 记录含 Gallica 文档
    ark（btv1b*）—— 发现层走这里，绕开 gallica.bnf.fr 检索接口的 DataDome 盾；
  * gallica.bnf.fr/iiif/ 的 manifest 与图像对脚本开放（HTTP 200）—— 下载层直连，
    逐页 JPEG → 本地无损组 PDF；
  * 注意: 写本特藏（archivesetmanuscrits，含伯希和敦煌写卷本体）的 SRU 返回
    403，暂不覆盖；通用目录可按关键词/语种自由配置目录 URL。

礼貌性: SRU 每页 50 条；IIIF 图像复用 core/http 每域节流（gallica.bnf.fr 2s）。
"""
import json
import os
import re
import time
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

from core.http import HttpClient, sha256_of
from core.limiter import Progress
from core.models import BookMeta, DownloadResult
from core import pdfbuild

SRU_BASE = "https://catalogue.bnf.fr/api/SRU"
IIIF_BASE = "https://gallica.bnf.fr/iiif"

# 朝代年份区间（法国馆条目多为早期写本/刻本）
_ERA_BY_YEAR = [
    (1912, 1949, "民国"), (1644, 1911, "清"), (1368, 1644, "明"),
    (1271, 1368, "元"), (960, 1279, "宋"), (618, 907, "唐"),
]
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_NSR = "{http://www.loc.gov/zing/srw/}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _era_from_year(y: Optional[int]) -> str:
    if not y:
        return ""
    return next((zh for lo, hi, zh in _ERA_BY_YEAR if lo <= y <= hi), "")


def _sru_url(query: str, start: int = 1, maximum: int = 50) -> str:
    from urllib.parse import quote
    return (f"{SRU_BASE}?version=1.2&operation=searchRetrieve"
            f"&query={quote(query)}&startRecord={start}"
            f"&maximumRecords={maximum}")


def _parse_sru(xml_text: str) -> Dict[str, Any]:
    """解析 catalogue.bnf.fr SRU（默认 UNMARC/marcxchange，命名空间无关）。"""
    out: Dict[str, Any] = {"total": 0, "records": []}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    n = next((e for e in root.iter() if _local(e.tag) == "numberOfRecords"), None)
    if n is not None and n.text:
        out["total"] = int(n.text)
    for rec in root.iter(f"{_NSR}record"):
        text = ET.tostring(rec, encoding="unicode")
        m = re.search(r"ark:/12148/(btv1[a-z0-9]+)", text)
        if not m:
            continue
        ark = m.group(1)
        title = creator = ""
        year = None
        for df in (e for e in rec.iter() if _local(e.tag) == "datafield"):
            tag = df.get("tag", "")
            subs = {sf.get("code"): (sf.text or "").strip()
                    for sf in df.iter() if _local(sf.tag) == "subfield"}
            if tag == "200":
                if subs.get("a") and not title:
                    title = subs["a"]
                creator = creator or subs.get("f", "")
            elif tag == "100":
                m2 = re.search(r"d(1[0-9]{3})", subs.get("a", ""))
                if m2 and year is None:
                    year = int(m2.group(1))
        out["records"].append(
            {"ark": ark, "title": title, "creator": creator, "year": year})
    return out


class BnfAdapter:
    id = "bnf"
    name = "法国国家图书馆"
    flag = "🇫🇷"

    def __init__(self, http: HttpClient):
        self.http = http

    # ---------------- 发现层（SRU 开放接口，预算制增量） ----------------
    def harvest_step(self, catalog_url: str, known_uids=None, budget: int = 40,
                     max_pages: int = 10, on_meta=None) -> Dict[str, Any]:
        """catalog_url 为完整 SRU 检索 URL（query 自定义，如 语种=chi）。

        与 na_jp 同语义：budget 条/心跳、跳过已入库、连续 3 失败即中止上报。
        """
        known = known_uids or set()
        # 规范化: 去掉调用方 URL 里的分页参数
        base = re.sub(r"&?(startRecord|maximumRecords)=\d+", "",
                      catalog_url.split("#")[0])
        base = re.sub(r"\?$", "", base) + (
            "&" if "?" in base else "?")
        fetched = skipped = 0
        blocked = False
        exhausted = False
        total_hint = 0
        start = 1
        page = 0
        fail_streak = 0
        while page < max_pages:
            url = f"{base}startRecord={start}&maximumRecords=50"
            xml_text = self.http.get(url)
            page += 1
            if xml_text is None:
                blocked = True
                break
            parsed = _parse_sru(xml_text)
            total_hint = parsed["total"]
            if not parsed["records"]:
                exhausted = True
                break
            for rec in parsed["records"]:
                ark = rec["ark"]
                if ark in known:
                    skipped += 1
                    continue
                meta = self._meta_from_manifest(rec)
                if meta is None:
                    fail_streak += 1
                    if fail_streak >= 3:
                        blocked = True
                        break
                    continue
                fail_streak = 0
                if on_meta:
                    on_meta(meta)
                fetched += 1
                if budget and fetched >= budget:
                    break
            if blocked or (budget and fetched >= budget):
                break
            start += 50
            if start > total_hint:
                exhausted = True
                break
        return {"ids": total_hint, "fetched": fetched, "skipped": skipped,
                "blocked": blocked, "exhausted": exhausted}

    def _meta_from_manifest(self, rec: Dict[str, Any]) -> Optional[BookMeta]:
        """SRU 记录 → Gallica IIIF manifest → BookMeta（1 请求/条）。"""
        mf = self.http.get(f"{IIIF_BASE}/ark:/12148/{rec['ark']}/manifest.json",
                           as_json=True)
        if not isinstance(mf, dict):
            return None
        canvases = (mf.get("sequences") or [{}])[0].get("canvases") or []
        pages: List[List[Dict[str, Any]]] = []
        for c in canvases:
            try:
                svc = c["images"][0]["resource"]["@id"]  # .../full/full/0/native.jpg
                svc = re.sub(r"/full/[^/]+/0/native\.jpg$", "", svc)
            except (KeyError, IndexError, TypeError):
                continue
            pages.append([{"url": f"{svc}/full/full/0/native.jpg",
                           "mimetype": "image/jpeg",
                           "width": c.get("width"), "height": c.get("height")}])
        if not pages:
            return None
        label = str(mf.get("label") or rec["title"] or rec["ark"]).strip()
        year = rec.get("year")
        return BookMeta(
            source_uid=rec["ark"],
            title=label,
            alt_title="",
            era=_era_from_year(year) if year else "",
            year_start=year, year_end=year,
            language="french",
            subjects=[],
            item_url=f"https://gallica.bnf.fr/ark:/12148/{rec['ark']}",
            cover_url=f"{IIIF_BASE}/ark:/12148/{rec['ark']}/f1/full/300,/0/native.jpg"
            if pages else "",
            collection="gallica-chinois",
            volume_count=1,
            page_count=len(pages),
            rights=str(mf.get("attribution") or "")[:200],
            pdf_urls=[],
            page_files=[pages] if pages else [],   # 单卷: [ [页1变体, 页2变体, ...] ]
            raw={"manifest_label": label, "creator": rec.get("creator", "")})

    def parse_snapshot(self, payload: Any, collection_slug: str = ""):
        return []   # BnF 走实时收割，无快照流程

    # ---------------- 下载层（gallica IIIF 直连） ----------------
    def download_item(self, meta: BookMeta, dest_dir: str, http: HttpClient,
                      quality: str = "auto",
                      progress: Optional[Progress] = None) -> DownloadResult:
        os.makedirs(dest_dir, exist_ok=True)
        started = time.time()
        tier = {"orig": "full", "mid": "1400,", "thumb": "900,"}.get(
            quality if quality in ("orig", "mid", "thumb") else "mid", "1400,")
        outputs, errors = [], []
        pages_done = 0
        for vi, pages in enumerate(meta.page_files or []):
            suffix = "" if len(meta.page_files) == 1 else f"_{vi + 1:02d}"
            out_pdf = os.path.join(dest_dir, f"book{suffix}.pdf")
            pages_dir = os.path.join(dest_dir, "_pages")
            os.makedirs(pages_dir, exist_ok=True)
            paths = []
            for idx, page in enumerate(pages, 1):
                if not page:
                    continue
                best = max(page, key=lambda v: int(v.get("width") or 0))
                url = re.sub(r"/full/[^/]+/0/native\.jpg$",
                             f"/full/{tier}/0/native.jpg", str(best.get("url") or ""))
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
                errors.append(f"卷{vi + 1}: 0 页下载成功")
                continue
            pdfbuild.build_pdf(paths, out_pdf)
            outputs.append(out_pdf)
            pages_done += len(paths)
            if len(paths) >= len(pages) - 2:
                for p in paths:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                try:
                    os.rmdir(pages_dir)
                except OSError:
                    pass
        cover_path = ""
        if meta.cover_url:
            cover_path = os.path.join(dest_dir, "cover.jpg")
            if not http.download(meta.cover_url, cover_path, min_bytes=2_000):
                cover_path = ""
        result = DownloadResult(
            ok=bool(outputs) and not errors,
            outputs=outputs, pages=pages_done,
            bytes_done=sum(os.path.getsize(p) for p in outputs),
            errors=errors)
        self._write_meta(dest_dir, meta, result, quality, started)
        return result

    @staticmethod
    def _write_meta(dest_dir: str, meta: BookMeta, result: DownloadResult,
                    quality: str, started: float) -> None:
        record = {
            "source": "bnf", "source_uid": meta.source_uid,
            "title": meta.title, "era": meta.era,
            "years": [meta.year_start, meta.year_end],
            "collection": meta.collection, "item_url": meta.item_url,
            "rights": meta.rights, "quality": quality,
            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "elapsed_s": round(time.time() - started, 1),
            "pages": result.pages, "bytes": result.bytes_done,
            "errors": result.errors,
            "files": [{"path": os.path.basename(p), "sha256": sha256_of(p),
                       "bytes": os.path.getsize(p)} for p in result.outputs],
        }
        with open(os.path.join(dest_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=1)
