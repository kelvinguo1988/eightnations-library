"""JPEG 无损嵌入 PDF（纯标准库，DCTDecode）——自 digital_archives_to_pdf.py 提炼。

用于 IIIF 逐页下载后的本地组装（官方 PDF 直链不可用时的兜底路径）。
"""
import glob
import os
import struct
from typing import Dict, List, Optional

LONG_EDGE_PT = 1190.0  # 每页 MediaBox 长边（约 A4 长边，单位 pt）


def _next_marker(data: bytes, i: int):
    """跳过 0xFF 填充字节定位标记；返回 (marker, 段头起点) 或 None。"""
    n = len(data)
    if i + 1 >= n:
        return None
    while i + 1 < n and data[i + 1] == 0xFF:   # 0xFF 填充串
        i += 1
    if i + 1 >= n:
        return None
    return data[i + 1], i


def jpeg_size(data: bytes):
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    i, n = 2, len(data)
    while i < n:
        if data[i] != 0xFF:
            i += 1
            continue
        step = _next_marker(data, i)
        if step is None:
            break
        marker, i = step
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 9 > n:
                raise ValueError("truncated JPEG header")
            height = struct.unpack(">H", data[i + 5:i + 7])[0]
            width = struct.unpack(">H", data[i + 7:i + 9])[0]
            return width, height
        if marker in (0xD9, 0xDA):
            break
        if i + 4 > n:
            raise ValueError("truncated JPEG header")
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    raise ValueError("no SOF marker")


def build_pdf(image_paths: List[str], out_path: str,
              long_edge_pt: Optional[float] = LONG_EDGE_PT) -> None:
    """JPEG 逐页写入 PDF（流式：内存中同时只保留一页图像）。

    先写 out_path.part 再原子 rename：中途异常绝不留下"看起来完整"的
    半成品 PDF（skip-if-exists/doctor 对账都会被半成品骗过）。
    对象布局: 1=Catalog, 2=Pages, 第 i 页(0基) = 3+3i(图像)/4+3i(内容)/5+3i(页面)。
    """
    n = len(image_paths)
    if n == 0:
        raise ValueError("no images")
    max_obj = 2 + 3 * n
    tmp = out_path + ".part"
    try:
        _write_pdf(tmp, image_paths, n, max_obj, long_edge_pt)
        os.replace(tmp, out_path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _write_pdf(out_path: str, image_paths: List[str], n: int, max_obj: int,
               long_edge_pt: Optional[float]) -> None:
    with open(out_path, "wb") as f:
        f.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: Dict[int, int] = {}

        def write_obj(num: int, body: bytes) -> None:
            offsets[num] = f.tell()
            f.write(("%d 0 obj\n" % num).encode() + body + b"\nendobj\n")

        write_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        kids = " ".join("%d 0 R" % (5 + 3 * i) for i in range(n))
        write_obj(2, ("<< /Type /Pages /Count %d /Kids [%s] >>" % (n, kids)).encode())

        for i, p in enumerate(image_paths):
            with open(p, "rb") as fh:
                data = fh.read()
            w, h = jpeg_size(data)
            comps = _jpeg_components(data)
            cs = {3: "/DeviceRGB", 1: "/DeviceGray",
                  4: "/DeviceCMYK"}.get(comps, "/DeviceRGB")
            head = ("<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                    "/ColorSpace %s /BitsPerComponent 8 /Filter /DCTDecode "
                    "/Length %d >>\nstream\n" % (w, h, cs, len(data))
                    ).encode("latin-1")
            img_num = 3 + 3 * i
            write_obj(img_num, head + data + b"\nendstream")
            del data
            s = (long_edge_pt / max(w, h)) if long_edge_pt else 1.0
            # /Contents 必须是 stream 对象（PDF 规范），裸内容串会被严格解析器拒绝
            content = ("q %.3f 0 0 %.3f 0 0 cm /Im%d Do Q\n"
                       % (w * s, h * s, i)).encode()
            write_obj(img_num + 1,
                      ("<< /Length %d >>\nstream\n" % len(content)).encode()
                      + content + b"endstream")
            write_obj(img_num + 2, (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.3f %.3f] "
                "/Resources << /XObject << /Im%d %d 0 R >> >> "
                "/Contents %d 0 R >>" % (w * s, h * s, i, img_num, img_num + 1)
            ).encode())

        xref_pos = f.tell()
        f.write(("xref\n0 %d\n" % (max_obj + 1)).encode())
        f.write(b"0000000000 65535 f \n")
        for num in range(1, max_obj + 1):
            f.write(("%010d 00000 n \n" % offsets[num]).encode())
        f.write(("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n"
                 "%%%%EOF\n" % (max_obj + 1, xref_pos)).encode())


def _jpeg_components(data: bytes) -> int:
    i, n = 2, len(data)
    while i < n:
        if data[i] != 0xFF:
            i += 1
            continue
        step = _next_marker(data, i)
        if step is None:
            break
        marker, i = step
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            if i + 10 > n:
                break
            return data[i + 9]
        if marker in (0xD9, 0xDA):
            break
        if i + 4 > n:
            break
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return 3


def pdf_page_count(path: str) -> int:
    """pypdf 页数；坏文件抛异常。"""
    from pypdf import PdfReader
    return len(PdfReader(path).pages)


def find_pages(pages_dir: str) -> List[str]:
    return sorted(glob.glob(os.path.join(pages_dir, "page_*.jpg")))
