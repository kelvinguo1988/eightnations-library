#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
digital_archives_to_pdf.py
==========================
批量把 国立公文書館デジタルアーカイブ (https://www.digital.archives.go.jp)
的图片导出为 PDF。

输入支持
--------
  * 单册阅读器页 : https://www.digital.archives.go.jp/img/<ID>
  * 多册合集页   : https://www.digital.archives.go.jp/file/<ID>  (自动展开子册)
  * 纯数字 ID    : 4412959 / 1079251  (自动探测是 img 还是 file)
  * 可一次传多个 URL/ID 做批量

工作方式
--------
  * 站点图片源是 JPEG2000(.jp2) 主文件，经 IIIF 服务输出「最高分辨率原生 JPEG」
    (最大宽 3000px)；该服务不开放原始 .jp2 直链，故下载其原生 JPEG。
  * 以 DCTDecode 把 JPEG **无损**嵌入 PDF（纯标准库，无需 Pillow/img2pdf）。
  * 文件名按件名(label)填写；多册合集会生成多个 PDF（每册一个）。

注意事项（站点限流）
--------------------
  * 连续请求约 58 次后会被临时限流(返回 403/502/超时)。脚本已内置：
    - 并发上限(默认 6) + 失败页单线程退避重试（最多 6 次，间隔递增）
    - 多册之间短暂停顿
    若仍失败，重跑同一命令即可（已下载的页会自动跳过/resume）。

依赖: 仅 Python 3 标准库。
用法:
    python3 digital_archives_to_pdf.py <URL或ID> [更多URL/ID...]
                                       [--outdir DIR] [--workers N]
                                       [--max-pages N] [--no-resume]

示例:
    python3 digital_archives_to_pdf.py 4412959
    python3 digital_archives_to_pdf.py https://www.digital.archives.go.jp/img/4412960 \
                                        https://www.digital.archives.go.jp/file/1079251 \
                                        --outdir ~/Downloads/archives
    python3 digital_archives_to_pdf.py 4412959 --max-pages 3   # 仅前3页(试跑)
"""

import argparse
import glob
import json
import os
import re
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}
BASE = "https://www.digital.archives.go.jp"
MIN_BYTES = 10000          # 小于此体积的下载视为失败/错误页
LONG_EDGE_PT = 1190.0      # PDF 每页 MediaBox 长边(约 A4 长边，单位 pt)


# --------------------------------------------------------------------------- #
# 网络
# --------------------------------------------------------------------------- #
def fetch_json(url, attempts=4):
    for a in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if a < attempts:
                time.sleep(2 * a)
    return None


def fetch_text(url, attempts=4):
    for a in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if a < attempts:
                time.sleep(2 * a)
    return ""


def fetch_binary(url, path, timeout=120, attempts=3):
    """下载二进制；成功且体积达标返回 True。带简单退避重试。"""
    for a in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            data = urllib.request.urlopen(req, timeout=timeout).read()
            if len(data) >= MIN_BYTES:
                with open(path, "wb") as f:
                    f.write(data)
                return True
        except Exception:
            pass
        time.sleep(2 * a)
    return False


# --------------------------------------------------------------------------- #
# 目标解析
# --------------------------------------------------------------------------- #
def parse_target(s):
    s = s.strip()
    m = re.search(r"(?:digital\.archives\.go\.jp/)?(?:img|file)/(\d+)", s)
    if m:
        kind = "img" if "/img/" in s else ("file" if "/file/" in s else "auto")
        return kind, m.group(1)
    if re.fullmatch(r"\d+", s):
        return "auto", s
    return None, None


def get_manifests_for_target(kind, id_):
    """返回 [(label, manifest), ...]"""
    # 多册合集优先: 若 /file/<id> 页面含子 /img/ 链接, 则展开为子册
    if kind in ("file", "auto"):
        html = fetch_text(f"{BASE}/file/{id_}")
        child_ids = sorted({c for c in re.findall(r"/img/(\d+)", html) if c != id_},
                           key=int)
        if child_ids:
            out = []
            for cid in child_ids:
                cm = fetch_json(f"{BASE}/api/iiif/{cid}/manifest.json")
                if cm and cm.get("sequences"):
                    out.append((cm.get("label", "vol_" + cid), cm))
            if out:
                return out
    # 否则当作单册 img
    if kind in ("img", "auto"):
        m = fetch_json(f"{BASE}/api/iiif/{id_}/manifest.json")
        if m and m.get("sequences"):
            return [(m.get("label", "item_" + id_), m)]
    return []


def get_canvases(manifest):
    return [c for seq in manifest.get("sequences", []) for c in seq.get("canvases", [])]


def page_url(canvas):
    svc = canvas["images"][0]["resource"]["service"]["@id"]
    return svc + "/full/max/0/native.jpg"


# --------------------------------------------------------------------------- #
# 下载单册
# --------------------------------------------------------------------------- #
def download_volume(label, manifest, outdir, workers, max_pages):
    pages_dir = os.path.join(outdir, "_pages_" + sanitize(label))
    os.makedirs(pages_dir, exist_ok=True)
    canvases = get_canvases(manifest)
    if max_pages:
        canvases = canvases[:max_pages]

    url_map = {i + 1: page_url(c) for i, c in enumerate(canvases)}
    lock = threading.Lock()
    status = {}

    def dl(idx):
        url = url_map[idx]
        path = os.path.join(pages_dir, f"page_{idx:03d}.jpg")
        ok = False
        if os.path.exists(path) and os.path.getsize(path) >= MIN_BYTES:
            ok = True
        else:
            ok = fetch_binary(url, path)
        with lock:
            status[idx] = ok
        return idx, ok

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(dl, idx) for idx in url_map]
        for f in as_completed(futs):
            f.result()

    # 失败页：单线程退避重试（降低再次触发限流的概率）
    missing = sorted(idx for idx, ok in status.items() if not ok)
    for idx in missing:
        url = url_map[idx]
        path = os.path.join(pages_dir, f"page_{idx:03d}.jpg")
        for attempt in range(1, 7):
            if fetch_binary(url, path):
                status[idx] = True
                break
            time.sleep(2 * attempt)

    still_missing = [
        idx for idx in sorted(status)
        if not (os.path.exists(os.path.join(pages_dir, f"page_{idx:03d}.jpg"))
                and os.path.getsize(os.path.join(pages_dir, f"page_{idx:03d}.jpg")) >= MIN_BYTES)
    ]
    return pages_dir, still_missing


# --------------------------------------------------------------------------- #
# 无损 JPEG -> PDF (纯标准库)
# --------------------------------------------------------------------------- #
def _jpeg_info(data):
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    i = 2
    n = len(data)
    while i < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height = struct.unpack(">H", data[i + 5:i + 7])[0]
            width = struct.unpack(">H", data[i + 7:i + 9])[0]
            comps = data[i + 9]
            return width, height, comps
        if marker in (0xD9, 0xDA):
            break
        seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seglen
    raise ValueError("no SOF marker")


def build_pdf(image_paths, out_path, long_edge_pt=LONG_EDGE_PT):
    n = len(image_paths)
    if n == 0:
        raise ValueError("no images")

    objs = {}
    img_ids = [3 + i for i in range(n)]
    base = 3 + n
    content_ids = [base + 2 * i for i in range(n)]
    page_ids = [base + 2 * i + 1 for i in range(n)]

    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs[2] = ("<< /Type /Pages /Count %d /Kids [%s] >>" % (n, kids)).encode()

    dims = []
    for i, p in enumerate(image_paths):
        data = open(p, "rb").read()
        w, h, comp = _jpeg_info(data)
        dims.append((w, h))
        cs = "/DeviceRGB" if comp == 3 else "/DeviceGray"
        head = ("<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                "/ColorSpace %s /BitsPerComponent 8 /Filter /DCTDecode "
                "/Length %d >>\nstream\n" % (w, h, cs, len(data))).encode("latin-1")
        objs[img_ids[i]] = head + data + b"\nendstream"

    for i in range(n):
        w, h = dims[i]
        if long_edge_pt:
            s = long_edge_pt / max(w, h)
            pw, ph = w * s, h * s
        else:
            pw, ph = float(w), float(h)
        content = ("q %.3f 0 0 %.3f 0 0 cm /Im%d Do Q" % (pw, ph, i)).encode()
        objs[content_ids[i]] = content
        objs[page_ids[i]] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.3f %.3f] "
            "/Resources << /XObject << /Im%d %d 0 R >> >> "
            "/Contents %d 0 R >>" % (pw, ph, i, img_ids[i], content_ids[i])
        ).encode()

    out = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    offsets = {}
    for num in sorted(objs):
        offsets[num] = sum(len(x) for x in out)
        out.append(("%d 0 obj\n" % num).encode() + objs[num] + b"\nendobj\n")
    xref_pos = sum(len(x) for x in out)
    max_obj = max(objs)
    xref = [b"xref\n", ("0 %d\n" % (max_obj + 1)).encode(),
            b"0000000000 65535 f \n"]
    for num in range(1, max_obj + 1):
        if num in offsets:
            xref.append(("%010d 00000 n \n" % offsets[num]).encode())
        else:
            xref.append(b"0000000000 65535 f \n")
    trailer = ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
               % (max_obj + 1, xref_pos)).encode()
    with open(out_path, "wb") as f:
        f.write(b"".join(out + xref + [trailer]))


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def sanitize(name):
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip().strip(".")
    return name or "untitled"


# --------------------------------------------------------------------------- #
# 处理单个目标(可能是 1 册或多册)
# --------------------------------------------------------------------------- #
def process_target(raw, outdir, workers, max_pages, resume):
    kind, id_ = parse_target(raw)
    if not id_:
        print(f"[跳过] 无法解析目标: {raw}")
        return 0, 0
    manifests = get_manifests_for_target(kind, id_)
    if not manifests:
        print(f"[跳过] 未找到内容: {raw}")
        return 0, 0

    print(f"\n=== 处理 {raw} => 发现 {len(manifests)} 个册 ===")
    for label, m in manifests:
        print(f"  件名: {label}  (页数 {len(get_canvases(m))})")

    built = 0
    failed = 0
    for label, m in manifests:
        safe = sanitize(label)
        pdf_path = os.path.join(outdir, safe + ".pdf")
        if resume and os.path.exists(pdf_path):
            print(f"  [已存在,跳过] {pdf_path}")
            built += 1
            continue
        pages_dir, missing = download_volume(label, m, outdir, workers, max_pages)
        img_paths = sorted(glob.glob(os.path.join(pages_dir, "page_*.jpg")))
        if missing:
            print(f"  [警告] 《{label}》仍有 {len(missing)} 页缺失: {missing}")
        if not img_paths:
            print(f"  [失败] 《{label}》无图片可合成")
            failed += 1
            continue
        try:
            build_pdf(img_paths, pdf_path)
            print(f"  [完成] {pdf_path}  ({len(img_paths)} 页)")
            built += 1
        except Exception as e:
            print(f"  [失败] 合成 PDF 出错: {e}")
            failed += 1
        time.sleep(2)  # 册间停顿，降低限流概率
    return built, failed


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="国立公文書館 JP2 页面 -> 按件名命名 PDF (批量)")
    ap.add_argument("targets", nargs="+", help="URL 或数字 ID，可多个")
    ap.add_argument("--outdir", default=".", help="输出目录 (默认当前目录)")
    ap.add_argument("--workers", type=int, default=6, help="下载并发数 (默认6)")
    ap.add_argument("--max-pages", type=int, default=0,
                    help="每册最多下载页数 (0=全部; 仅供试跑)")
    ap.add_argument("--no-resume", action="store_true",
                    help="忽略已存在的 PDF，强制重新生成")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    total_built = total_failed = 0
    for raw in args.targets:
        b, f_ = process_target(raw, args.outdir, args.workers,
                               args.max_pages or 0, not args.no_resume)
        total_built += b
        total_failed += f_
    print(f"\n==== 全部完成: 生成 {total_built} 个 PDF, 失败 {total_failed} 个 ====")
    print(f"输出目录: {os.path.abspath(args.outdir)}")
    sys.exit(1 if total_failed else 0)


if __name__ == "__main__":
    main()
