"""礼貌的 HTTP 客户端：诚实 UA、每域最小间隔+抖动、退避重试、断点续传下载。

约定:
  * 仅用于公开领域资源（各馆 rights 字段校验由适配器负责）；
  * 默认每域串行 + 1.2s 最小间隔 + 随机抖动，属于对公共服务的温和访问；
  * 下载支持 Range 续传（NAS 场景大 PDF 断点续跑）。
"""
import os
import random
import re
import threading
import time
import hashlib
from typing import Dict, Optional

import requests

UA = ("eightnations-archiver/0.1 "
      "(personal cultural-heritage study archive; polite; contact: local-user)")

# tile/图床类域名可以稍微紧一点；www.* 元数据域名默认更保守
_DOMAIN_MIN_INTERVAL = {
    "tile.loc.gov": 1.0,
    "www.loc.gov": 3.0,
    "www.digital.archives.go.jp": 2.5,
    "dl.ndl.go.jp": 2.0,
}
_DEFAULT_INTERVAL = 1.5


class DomainThrottle:
    """每域令牌间隔：同一域名两次请求之间至少 min_interval(1±jitter) 秒。"""

    def __init__(self, jitter: float = 0.5):
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()
        self.jitter = jitter

    def wait(self, url: str) -> None:
        domain = _domain_of(url)
        interval = next(
            (v for k, v in _DOMAIN_MIN_INTERVAL.items() if domain.endswith(k)),
            _DEFAULT_INTERVAL)
        while True:
            with self._lock:
                now = time.monotonic()
                last = self._last.get(domain, 0.0)
                sleep_for = last + interval - now
                if sleep_for <= 0:
                    self._last[domain] = now
                    return
            time.sleep(min(sleep_for, 5.0) + random.uniform(0, self.jitter))


def _domain_of(url: str) -> str:
    try:
        return requests.utils.urlparse(url).netloc.lower()
    except Exception:
        return ""


class HttpClient:
    def __init__(self, throttle: Optional[DomainThrottle] = None,
                 timeout: int = 60, attempts: int = 4):
        self.throttle = throttle or DomainThrottle()
        self.timeout = timeout
        self.attempts = attempts
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA
        self.session.headers["Accept-Language"] = "en-US,en;q=0.9,zh-CN;q=0.8"

    def get(self, url: str, *, as_json: bool = False, headers: Optional[dict] = None):
        """GET 文本/JSON；失败返回 None（调用方决定兜底）。"""
        backoff = 2.0
        for a in range(1, self.attempts + 1):
            self.throttle.wait(url)
            try:
                r = self.session.get(url, timeout=self.timeout, headers=headers)
                if r.status_code == 200:
                    return r.json() if as_json else r.text
                if r.status_code in (403, 429, 502, 503):
                    # 可能触发限流，拉长退避
                    backoff = min(backoff * 2, 60)
            except requests.RequestException:
                pass
            if a < self.attempts:
                time.sleep(backoff * a + random.uniform(0, 1))
        return None

    def download(self, url: str, dest: str, *, min_bytes: int = 1024,
                 expected_bytes: Optional[int] = None) -> bool:
        """流式下载到 dest，支持 Range 续传。成功(且体积达标)返回 True。

        完整性: 首个响应带 Content-Length / Content-Range 总长时，完成后必须
        与之相等；200 全量响应直接覆盖残片，杜绝 200/206 混拼导致的损坏。
        """
        part = dest + ".part"
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        full_size = expected_bytes

        def pump(f, r) -> None:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)

        for _ in range(1, self.attempts + 1):
            self.throttle.wait(url)
            headers: Dict[str, str] = {}
            if os.path.exists(part) and os.path.getsize(part) > 0:
                headers["Range"] = f"bytes={os.path.getsize(part)}-"
            try:
                with self.session.get(url, timeout=min(self.timeout * 5, 600),
                                      headers=headers, stream=True) as r:
                    if r.status_code == 206:
                        m = re.search(r"/(\d+)\s*$",
                                      r.headers.get("Content-Range", ""))
                        if m:
                            full_size = int(m.group(1))
                        with open(part, "ab") as f:
                            pump(f, r)
                    elif r.status_code == 200:
                        clen = r.headers.get("Content-Length", "")
                        if clen.isdigit():
                            full_size = int(clen)
                        with open(part, "wb") as f:   # 覆盖残片全量重下
                            pump(f, r)
                    elif r.status_code == 416:
                        pass                          # 残片已到全长，走下方校验
                    else:
                        raise requests.HTTPError(f"HTTP {r.status_code}")
            except requests.RequestException:
                pass
            size = os.path.getsize(part) if os.path.exists(part) else 0
            if full_size is not None:
                if size == full_size and size >= min_bytes:
                    os.replace(part, dest)
                    return True
                if size > full_size:                  # 残片异常，弃掉重来
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                    full_size = expected_bytes
            elif size >= min_bytes:                   # 响应未给总长（少见）
                os.replace(part, dest)
                return True
        return False


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
