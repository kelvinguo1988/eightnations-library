"""日本国立公文書館デジタルアーカイブ适配器（direct 策略，站点对脚本友好）。

要点（沿用已验证的两个脚本 official_pdf_by_cids / digital_archives_to_pdf）:
  * 收割: fonds 列表页 HTML 抽 /img/<id>；IIIF manifest 取件名(label)与 cid 列表
  * 下载: 官方 contentDownload POST（cid 分块 ≤100/请求）→ pypdf 合并；
    兜底: IIIF JP2 原生 JPEG（/full/max/0/native.jpg）→ 本地无损组 PDF
  * 站点限流: 连续约 58 次请求触发临时封禁（403/502）——每域 2.5s 间隔 + 退避
    重试已内置（core/http.py），大册之间册间停顿 2s。
"""
import glob
import json
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional

from core.http import HttpClient, sha256_of
from core.limiter import Progress
from core.models import BookMeta, DownloadResult
from core import pdfbuild

BASE = "https://www.digital.archives.go.jp"
CHUNK = 100
MAX_ATTEMPTS = 8


def _manifest(http: HttpClient, vid: str) -> Optional[dict]:
    return http.get(f"{BASE}/api/iiif/{vid}/manifest.json", as_json=True)


def _label_cids(manifest: dict):
    label = (manifest.get("label") or "").strip()
    cids = []
    for seq in manifest.get("sequences", []):
        for canvas in seq.get("canvases", []):
            for im in canvas.get("images", []):
                m = re.search(r"(da\d+/C\d+)",
                              im.get("resource", {}).get("@id", ""))
                if m:
                    cids.append(m.group(1))
    return label, cids


class NaJpAdapter:
    id = "na_jp"
    name = "日本国立公文書館"
    flag = "🇯🇵"

    def __init__(self, http: HttpClient):
        self.http = http

    # ---------------- 发现层（站点可直接访问，实时收割） ----------------
    def harvest_fonds(self, fonds_url: str, max_pages: int = 1,
                      progress: Optional[Progress] = None) -> List[BookMeta]:
        """fonds 列表页（如 .../fonds/3611449?page=1）→ 条目元数据。"""
        ids: List[str] = []
        m = re.search(r"/fonds/(\d+)", fonds_url)
        fonds_id = m.group(1) if m else ""
        for page in range(1, max_pages + 1):
            url = fonds_url if max_pages == 1 else \
                f"{BASE}/fonds/{fonds_id}?page={page}"
            html = self.http.get(url) or ""
            found = re.findall(r"/img/(\d+)", html)
            if not found:
                break
            new = [i for i in found if i not in ids]
            ids.extend(new)
            if progress:
                progress.tick(len(new), 0)
        out: List[BookMeta] = []
        fail_streak = 0
        for vid in ids:
            mf = _manifest(self.http, vid)
            if not mf or not mf.get("sequences"):
                # 站点对连续请求约 58 次后限流：连续失败即中止本轮，
                # 已抓到的先入库，剩余由下次巡检（每周/重启）补齐
                fail_streak += 1
                if fail_streak >= 5:
                    break
                continue
            fail_streak = 0
            label, cids = _label_cids(mf)
            cover = (mf.get("sequences", [{}])[0].get("canvases") or
                     [{}])[0].get("thumbnail", "")
            if isinstance(cover, dict):
                cover = cover.get("@id", "")
            elif isinstance(cover, list):
                cover = cover[0].get("@id", "") if cover else ""
            out.append(BookMeta(
                source_uid=vid,
                title=label or f"item_{vid}",
                collection=f"fonds-{fonds_id}" if fonds_id else "",
                volume_count=1,
                page_count=len(cids),
                item_url=f"{BASE}/img/{vid}",
                cover_url=str(cover or ""),
                page_files=[[]],
                raw=mf))
        return out

    def parse_snapshot(self, payload: Any, collection_slug: str = ""
                       ) -> List[BookMeta]:
        """兼容快照导入：{"items":[{id,label,page_count,...}]}"""
        if isinstance(payload, dict) and "items" in payload:
            out = []
            for it in payload["items"]:
                out.append(BookMeta(
                    source_uid=str(it["id"]), title=it.get("label", ""),
                    collection=collection_slug or it.get("collection", ""),
                    volume_count=1, page_count=it.get("page_count", 0),
                    item_url=f"{BASE}/img/{it['id']}"))
            return out
        return []

    # ---------------- 下载层 ----------------
    def download_item(self, meta: BookMeta, dest_dir: str, http: HttpClient,
                      quality: str = "auto",
                      progress: Optional[Progress] = None) -> DownloadResult:
        os.makedirs(dest_dir, exist_ok=True)
        started = time.time()
        out_pdf = os.path.join(dest_dir, "book.pdf")
        pages, errors = 0, []

        manifest = meta.raw if meta.raw.get("sequences") \
            else _manifest(http, meta.source_uid) or {}
        label, cids = _label_cids(manifest) if manifest \
            else (meta.title, [])
        if not cids:
            errors.append("manifest 无 cid（站点可能限流）")
        else:
            pages = self._download_official(meta.source_uid, cids, out_pdf,
                                            http, errors, progress)
            if pages == 0:
                pages = self._download_iiif(manifest, dest_dir, out_pdf,
                                            http, errors, progress)

        outputs = [out_pdf] if pages > 0 else []
        result = DownloadResult(ok=bool(outputs) and not errors,
                                outputs=outputs, pages=pages,
                                bytes_done=os.path.getsize(out_pdf)
                                if outputs else 0, errors=errors)
        self._write_meta(dest_dir, meta, result, started)
        return result

    def _download_official(self, vid: str, cids: List[str], out_pdf: str,
                           http: HttpClient, errors: List[str],
                           progress: Optional[Progress]) -> int:
        """官方 contentDownload 分块下载 + pypdf 合并（返回页数, 0=失败）。"""
        from pypdf import PdfReader, PdfWriter
        chunks = [cids[i:i + CHUNK] for i in range(0, len(cids), CHUNK)]
        parts: List[str] = []
        tmpdir = tempfile.mkdtemp(prefix="najp_")
        try:
            for ci, ch in enumerate(chunks, 1):
                ok = False
                for att in range(1, MAX_ATTEMPTS + 1):
                    data = self._post_chunk(http, vid, ch)
                    if data and data[:4] == b"%PDF":
                        tp = os.path.join(tmpdir, f"chunk_{ci}.pdf")
                        with open(tp, "wb") as f:
                            f.write(data)
                        try:
                            if len(PdfReader(tp).pages) == len(ch):
                                parts.append(tp)
                                ok = True
                                break
                        except Exception:
                            pass
                    time.sleep(min(8 * att, 60))
                if not ok:
                    errors.append(f"块{ci}/{len(chunks)} 下载失败")
                    return 0
                if progress:
                    progress.tick(len(ch), len(cids))
            if not parts:
                return 0
            w = PdfWriter()
            total = 0
            for p in parts:
                rd = PdfReader(p)
                for pg in rd.pages:
                    w.add_page(pg)
                total += len(rd.pages)
            with open(out_pdf, "wb") as f:
                w.write(f)
            if total != len(cids):
                errors.append(f"warn: 合并页数 {total} != cid数 {len(cids)}")
            return total
        finally:
            for p in parts:
                try:
                    os.remove(p)
                except OSError:
                    pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass

    @staticmethod
    def _post_chunk(http: HttpClient, vid: str, cids: List[str]) -> Optional[bytes]:
        url = f"{BASE}/contentDownload/{vid}?type=imagePdf"
        backoff = 2.0
        for att in range(1, 4):
            http.throttle.wait(url)
            try:
                r = http.session.post(url, data=[("cid", c) for c in cids],
                                      timeout=360)
                if r.status_code == 200 and r.content[:4] == b"%PDF":
                    return r.content
            except Exception:
                pass
            time.sleep(backoff * att)
        return None

    def _download_iiif(self, manifest: dict, dest_dir: str, out_pdf: str,
                       http: HttpClient, errors: List[str],
                       progress: Optional[Progress]) -> int:
        """兜底: IIIF canvases -> 原生 JPEG -> 组 PDF（页级断点）。"""
        urls = []
        for seq in manifest.get("sequences", []):
            for canvas in seq.get("canvases", []):
                try:
                    svc = canvas["images"][0]["resource"]["service"]["@id"]
                    urls.append(svc + "/full/max/0/native.jpg")
                except (KeyError, IndexError):
                    pass
        if not urls:
            return 0
        pages_dir = os.path.join(dest_dir, "_pages")
        os.makedirs(pages_dir, exist_ok=True)
        paths = []
        for idx, url in enumerate(urls, 1):
            path = os.path.join(pages_dir, f"page_{idx:04d}.jpg")
            if not (os.path.exists(path) and os.path.getsize(path) > 10_000):
                if not http.download(url, path, min_bytes=10_000):
                    continue
            paths.append(path)
            if progress:
                progress.tick(1, len(urls))
        if not paths:
            return 0
        pdfbuild.build_pdf(paths, out_pdf)
        if len(paths) == len(urls):
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
    def _write_meta(dest_dir: str, meta: BookMeta, result: DownloadResult,
                    started: float) -> None:
        record = {
            "source": "na_jp", "source_uid": meta.source_uid,
            "title": meta.title, "item_url": meta.item_url,
            "pages": result.pages, "bytes": result.bytes_done,
            "errors": result.errors,
            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "elapsed_s": round(time.time() - started, 1),
            "files": [{"path": os.path.basename(p), "sha256": sha256_of(p),
                       "bytes": os.path.getsize(p)} for p in result.outputs],
        }
        with open(os.path.join(dest_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=1)
