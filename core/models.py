"""统一数据模型：各馆适配器把站点元数据解析成 BookMeta，入库与下载共用。"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class BookMeta:
    """一个数字化条目（可含多卷）。source_uid 在馆内唯一，如 LCCN。"""

    source_uid: str
    title: str = ""            # 拼音/罗马字题名（馆方主字段）
    alt_title: str = ""        # 中文原题（other_title 中的 CJK 条目）
    author: str = ""
    era: str = ""              # 朝代/时期：明/清/…
    year_start: Optional[int] = None
    year_end: Optional[int] = None
    language: str = ""
    item_url: str = ""         # 原馆藏页面
    cover_url: str = ""        # 缩略图（tile 服务，公开）
    collection: str = ""       # 馆内子专藏 slug：yongle-da-dian / chinese-rare-books
    volume_count: int = 0      # 卷（resource）数
    page_count: int = 0        # 页数（各卷之和，快照级常为 files 整数）
    rights: str = ""           # 权利声明（纯文本，截断）
    shelf_id: str = ""         # 馆内架藏号，常含 vol. 信息
    pdf_urls: List[str] = field(default_factory=list)    # 每卷官方 PDF 直链（可为空）
    page_files: List[List[Dict[str, Any]]] = field(default_factory=list)
    # page_files[i] = 第 i 卷的页变体列表，每个变体: {url, mimetype, width, height}
    raw: Dict[str, Any] = field(default_factory=dict)    # 原始 JSON，整体留档


@dataclass
class DownloadResult:
    ok: bool
    outputs: List[str] = field(default_factory=list)     # 产出文件绝对路径
    pages: int = 0
    bytes_done: int = 0
    errors: List[str] = field(default_factory=list)
