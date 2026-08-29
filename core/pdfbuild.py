"""JPEG 无损嵌入 PDF（纯标准库，DCTDecode）——自 digital_archives_to_pdf.py 提炼。

用于 IIIF 逐页下载后的本地组装（官方 PDF 直链不可用时的兜底路径）。
"""
import glob
import os
import struct
from typing import List, Optional

LONG_EDGE_PT = 1190.0  # 每页 MediaBox 长边（约 A4 长边，单位 pt）


def jpeg_size(data: bytes):
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    i, n = 2, len(data)
    while i < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height = struct.unpack(">H", data[i + 5:i + 7])[0]
            width = struct.unpack(">H", data[i + 7:i + 9])[0]
            return width, height
        if marker in (0xD9, 0xDA):
            break
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    raise ValueError("no SOF marker")


def build_pdf(image_paths: List[str], out_path: str,
              long_edge_pt: Optional[float] = LONG_EDGE_PT) -> None:
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
        with open(p, "rb") as f:
            data = f.read()
        w, h = jpeg_size(data)
        dims.append((w, h))
        cs = b"/DeviceRGB" if _jpeg_components(data) == 3 else b"/DeviceGray"
        head = ("<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                "/ColorSpace %s /BitsPerComponent 8 /Filter /DCTDecode "
                "/Length %d >>\nstream\n" % (w, h, cs.decode(), len(data))
                ).encode("latin-1")
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


def _jpeg_components(data: bytes) -> int:
    i, n = 2, len(data)
    while i < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            return data[i + 9]
        if marker in (0xD9, 0xDA):
            break
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return 3


def pdf_page_count(path: str) -> int:
    """pypdf 页数；坏文件抛异常。"""
    from pypdf import PdfReader
    return len(PdfReader(path).pages)


def find_pages(pages_dir: str) -> List[str]:
    return sorted(glob.glob(os.path.join(pages_dir, "page_*.jpg")))
