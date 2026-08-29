"""站点适配器注册表。"""
from typing import Dict, Type

from core.http import HttpClient
from sites.base import SourceAdapter
from sites.loc import LocAdapter
from sites.na_jp import NaJpAdapter

_REGISTRY: Dict[str, Type[SourceAdapter]] = {
    "loc": LocAdapter,
    "na_jp": NaJpAdapter,
}


def get_adapter(source_id: str, http: HttpClient) -> SourceAdapter:
    cls = _REGISTRY.get(source_id)
    if not cls:
        raise KeyError(f"未注册的适配器: {source_id}")
    return cls(http)
