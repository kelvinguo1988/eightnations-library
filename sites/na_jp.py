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
    def harvest_step(self, catalog_url: str, known_uids=None, budget: int = 40,
                     max_pages: int = 50, on_meta=None) -> Dict[str, Any]:
        """增量收割一个心跳批次（防封禁设计）。

        站点对"连续约 58 次请求"临时限流，因此：
          * 每次调用最多抓 budget 个条目（默认 40 < 58），剩余留给下个心跳续传；
          * 已在库的条目直接跳过（每周巡检时几乎零请求）；
          * 连续 3 个条目失败视为被限流，立即中止并上报 blocked。

        on_meta(meta) 逐条回调（调用方即时入库，中断不丢进度）。
        返回 {ids, fetched, skipped, blocked, exhausted}。
        """
        known = known_uids or set()
        ids: List[str] = []
        m = re.search(r"/fonds/(\d+)", catalog_url)
        fonds_id = m.group(1) if m else ""
        blocked = False
        for page in range(1, max_pages + 1):
            url = catalog_url if (max_pages == 1 or not fonds_id) else \
                f"{BASE}/fonds/{fonds_id}?page={page}"
            html = self.http.get(url)
            if html is None:
                blocked = True          # 列表页都拿不到：已被限流
                break
            new = [i for i in re.findall(r"/img/(\d+)", html) if i not in ids]
            if not new:
                break
            ids.extend(new)
        todo = [i for i in ids if i not in known]
        fetched, fail_streak = 0, 0
        for vid in todo:
            mf = _manifest(self.http, vid)
            if not mf or not mf.get("sequences"):
                fail_streak += 1
                if fail_streak >= 3:    # 连续失败 = 被限流，本轮到此为止
                    blocked = True
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
            meta = BookMeta(
                source_uid=vid,
                title=label or f"item_{vid}",
                collection=f"fonds-{fonds_id}" if fonds_id else "",
                volume_count=1,
                page_count=len(cids),
                item_url=f"{BASE}/img/{vid}",
                cover_url=str(cover or ""),
                page_files=[[]],
                raw=mf)
            if on_meta:
                on_meta(meta)
            fetched += 1
            if budget and fetched >= budget:
                break
        return {"ids": len(ids), "fetched": fetched,
                "skipped": len([i for i in ids if i in known]),
                "blocked": blocked,
                "exhausted": (not blocked) and fetched < (budget or 10 ** 9)}

    def harvest_fonds(self, fonds_url: str, max_pages: int = 1,
                      progress: Optional[Progress] = None) -> List[BookMeta]:
        """一次性全量收割（CLI 用；调度器请用 harvest_step 防封禁）。"""
        out: List[BookMeta] = []
        stats = self.harvest_step(
            fonds_url, known_uids=None, budget=0, max_pages=max_pages,
            on_meta=(lambda m: (out.append(m),
                                progress.tick(1, 0) if progress else None)))
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
    def _volume_ids(self, http: HttpClient, vid: str) -> List[str]:
        """多卷展开：从条目页 viewer 翻页链收集兄弟分卷 vid。

        站点对多卷书只在 fonds 列表放父记录，父 manifest 仅含封面 1 canvas；
        各分卷（近思録１〜Ｎ…）是兄弟条目，只能由详情页
        viewer-header__nav-btn--next 的 data-href 翻页链发现（末卷按钮
        is-disabled）。返回 [卷1, 卷2, ...]（不含父记录本身）；单卷书 []。
        """
        out: List[str] = []
        cur = vid
        for _ in range(60):                     # 安全上限
            html = http.get(f"{BASE}/img/{cur}")
            if not html:
                break
            m = re.search(r'<button[^>]*nav-btn--next[^>]*>', html)
            if not m or "is-disabled" in m.group(0):
                break
            nxt = re.search(r'data-href="/img/(\d+)"', m.group(0))
            if not nxt or nxt.group(1) in out or nxt.group(1) == vid:
                break
            cur = nxt.group(1)
            out.append(cur)
        return out

    def download_item(self, meta: BookMeta, dest_dir: str, http: HttpClient,
                      quality: str = "auto",
                      progress: Optional[Progress] = None) -> DownloadResult:
        os.makedirs(dest_dir, exist_ok=True)
        started = time.time()
        pages, errors = 0, []

        manifest = meta.raw if meta.raw.get("sequences") \
            else _manifest(http, meta.source_uid) or {}
        label, cids = _label_cids(manifest) if manifest \
            else (meta.title, [])

        # 多卷书：父记录 manifest 通常只有封面 1 页，分卷从翻页链展开，
        # 每卷单独成一个 PDF（合并单文件体积可达数 GB，无法打开）
        vols: List[tuple] = []      # (vid, cids, manifest)
        if cids:
            vols.append((meta.source_uid, cids, manifest))
        if 0 < len(cids) <= 1:
            for svid in self._volume_ids(http, meta.source_uid):
                mf = _manifest(http, svid)
                if not mf:
                    continue
                _, mc = _label_cids(mf)
                if mc:
                    vols.append((svid, mc, mf))
        outputs: List[str] = []
        if not vols:
            errors.append("manifest 无 cid（站点可能限流）")
        else:
            pages, outputs = self._download_official(vols, dest_dir, http,
                                                     errors, progress)
            if pages == 0:
                pages, outputs = self._download_iiif(vols, dest_dir, http,
                                                     errors, progress)

        result = DownloadResult(ok=bool(outputs) and not errors,
                                outputs=outputs, pages=pages,
                                bytes_done=sum(os.path.getsize(p)
                                               for p in outputs),
                                errors=errors)
        self._write_meta(dest_dir, meta, result, started)
        return result

    @staticmethod
    def _volume_names(vols: List[tuple]) -> List[str]:
        """每卷一个 PDF 的命名：单卷沿用 book.pdf；多卷时父记录若仅 1 页
        视为封面 cover.pdf，其余按序 book_01.pdf、book_02.pdf…"""
        if len(vols) == 1:
            return ["book.pdf"]
        names, seq = [], 0
        for i, v in enumerate(vols):
            cids = v[1]
            if i == 0 and len(cids) == 1:
                names.append("cover.pdf")
            else:
                seq += 1
                names.append(f"book_{seq:02d}.pdf")
        return names

    @staticmethod
    def _cleanup_stale(dest_dir: str, names: List[str]) -> None:
        """清理旧方案/失败重试残留的 PDF（book.pdf、book_NN.pdf、cover.pdf
        中不在本次产出名单里的）。"""
        for n in os.listdir(dest_dir):
            if (n.endswith(".pdf") and
                    (n == "book.pdf" or n == "cover.pdf" or
                     re.fullmatch(r"book_\d+\.pdf", n)) and
                    n not in names):
                try:
                    os.remove(os.path.join(dest_dir, n))
                except OSError:
                    pass

    def _download_official(self, vols: List[tuple], dest_dir: str,
                           http: HttpClient, errors: List[str],
                           progress: Optional[Progress]):
        """官方 contentDownload 分块下载，每卷合并写入独立 PDF。

        vols = [(vid, cids, manifest), ...]；返回 (总页数, 产出路径)，页数 0=失败。
        """
        from pypdf import PdfReader, PdfWriter
        names = self._volume_names(vols)
        total_cids = sum(len(c) for _, c, _ in vols)
        tmpdir = tempfile.mkdtemp(prefix="najp_")
        parts: List[str] = []
        outputs: List[str] = []
        total = 0
        try:
            pi = 0
            for vi, ((vid, cids, _mf), name) in enumerate(zip(vols, names), 1):
                chunks = [cids[i:i + CHUNK] for i in range(0, len(cids), CHUNK)]
                for ci, ch in enumerate(chunks, 1):
                    ok = False
                    for att in range(1, MAX_ATTEMPTS + 1):
                        data = self._post_chunk(http, vid, ch)
                        if data and data[:4] == b"%PDF":
                            pi += 1
                            tp = os.path.join(tmpdir, f"chunk_{pi}.pdf")
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
                        errors.append(f"卷{vi}({vid}) 块{ci}/{len(chunks)} 下载失败")
                        return 0, []
                    if progress:
                        progress.tick(len(ch), total_cids)
                # 本卷各块齐了 → 写独立卷 PDF
                if parts:
                    w = PdfWriter()
                    vol_total = 0
                    for p in parts:
                        rd = PdfReader(p)
                        for pg in rd.pages:
                            w.add_page(pg)
                        vol_total += len(rd.pages)
                    out = os.path.join(dest_dir, name)
                    with open(out, "wb") as f:
                        w.write(f)
                    outputs.append(out)
                    total += vol_total
                    if vol_total != len(cids):
                        errors.append(f"warn: 卷{vi} 合并页数 {vol_total} != cid数 {len(cids)}")
                    for p in parts:
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                    parts = []
            self._cleanup_stale(dest_dir, names)
            return total, outputs
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

    def _download_iiif(self, vols: List[tuple], dest_dir: str,
                       http: HttpClient, errors: List[str],
                       progress: Optional[Progress]):
        """兜底: 各卷 canvases -> 原生 JPEG -> 每卷独立组 PDF（页级断点）。"""
        names = self._volume_names(vols)
        grand_total = sum(len(v[1]) for v in vols)
        outputs: List[str] = []
        total = 0
        for vi, ((_vid, _cids, manifest), name) in enumerate(zip(vols, names), 1):
            urls = []
            for seq in manifest.get("sequences", []):
                for canvas in seq.get("canvases", []):
                    try:
                        svc = canvas["images"][0]["resource"]["service"]["@id"]
                        urls.append(svc + "/full/max/0/native.jpg")
                    except (KeyError, IndexError):
                        pass
            if not urls:
                continue
            pages_dir = os.path.join(dest_dir, f"_pages_{vi:02d}")
            os.makedirs(pages_dir, exist_ok=True)
            paths = []
            for idx, url in enumerate(urls, 1):
                path = os.path.join(pages_dir, f"page_{idx:04d}.jpg")
                if not (os.path.exists(path) and os.path.getsize(path) > 10_000):
                    if not http.download(url, path, min_bytes=10_000):
                        continue
                paths.append(path)
                if progress:
                    progress.tick(1, grand_total)
            if not paths:
                continue
            out = os.path.join(dest_dir, name)
            pdfbuild.build_pdf(paths, out)
            outputs.append(out)
            total += len(paths)
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
        if outputs:
            self._cleanup_stale(dest_dir, names)
        return total, outputs

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
