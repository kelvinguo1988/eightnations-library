"""站点适配器契约：每馆一个文件，实现 harvest(解析快照) + download_item。"""
from typing import Any, Dict, List, Optional, Protocol

from core.http import HttpClient
from core.limiter import Progress
from core.models import BookMeta, DownloadResult


class SourceAdapter(Protocol):
    id: str            # 与 sources 表 id 一致
    name: str
    flag: str

    def parse_snapshot(self, payload: Any, collection_slug: str = "") -> List[BookMeta]:
        """发现层：把目录快照 JSON（集合页 ?fo=json）解析为书目列表。

        快照由 tools/<id>_snapshot.py 产出（半自动过盾），解析本身不联网。
        """
        ...

    def download_item(self, meta: BookMeta, dest_dir: str, http: HttpClient,
                      quality: str = "auto",
                      progress: Optional[Progress] = None) -> DownloadResult:
        """下载层：只访问公开图床/文件服务（不碰被盾的元数据站）。

        quality: auto(官方PDF优先,否则1600px组图) / pdf / orig / mid / thumb
        dest_dir 内产出: book.pdf(或 vol_NN.pdf) + cover.jpg + meta.json
        页级断点续传: 已存在且达标的目标文件跳过。
        """
        ...
