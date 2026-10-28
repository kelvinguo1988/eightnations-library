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
from typing import Callable, Dict, Optional

import requests

UA = ("eightnations-archiver/0.1 "
      "(personal cultural-heritage study archive; polite; contact: local-user)")

# tile/图床类域名可以稍微紧一点；www.* 元数据域名默认更保守
_DOMAIN_MIN_INTERVAL = {
    "tile.loc.gov": 1.0,
    "www.loc.gov": 3.0,
    "www.digital.archives.go.jp": 2.5,
    "dl.ndl.go.jp": 2.0,
    "catalogue.bnf.fr": 2.0,
    "gallica.bnf.fr": 2.0,
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


def _data_dir() -> str:
    d = os.environ.get("EIGHTNATIONS_DATA")
    if not d:
        d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data")
    return d


class FileThrottle(DomainThrottle):
    """跨进程节流：每域一个 flock 锁文件存上次请求时刻（墙钟）。

    场景: Web「立即下载」线程与 scheduler 是两个进程，各自命中同一站点时
    单靠进程内节流会把实际间隔减半。文件锁保证跨进程串行取号。
    """

    def __init__(self, jitter: float = 0.5, runtime_dir: Optional[str] = None):
        super().__init__(jitter)
        import fcntl
        self._fcntl = fcntl
        self.dir = runtime_dir or os.path.join(_data_dir(), "runtime", "throttle")
        os.makedirs(self.dir, exist_ok=True)

    def _lock_path(self, domain: str) -> str:
        return os.path.join(self.dir, re.sub(r"[^A-Za-z0-9.-]", "_", domain) + ".lock")

    def wait(self, url: str) -> None:
        domain = _domain_of(url)
        interval = next(
            (v for k, v in _DOMAIN_MIN_INTERVAL.items() if domain.endswith(k)),
            _DEFAULT_INTERVAL)
        path = self._lock_path(domain)
        while True:
            with open(path, "a+") as fh:
                self._fcntl.flock(fh, self._fcntl.LOCK_EX)
                try:
                    fh.seek(0)
                    raw = fh.read().strip()
                    last = float(raw) if raw else 0.0
                    now = time.time()
                    sleep_for = last + interval - now
                    if sleep_for <= 0:
                        fh.seek(0)
                        fh.truncate()
                        fh.write(str(now))
                        fh.flush()   # 落盘先于解锁，否则他进程可能读到旧时刻
                        return
                finally:
                    self._fcntl.flock(fh, self._fcntl.LOCK_UN)
            time.sleep(min(sleep_for, 5.0) + random.uniform(0, self.jitter))


def _default_throttle() -> DomainThrottle:
    try:
        return FileThrottle()
    except OSError:
        return DomainThrottle()


SHARED_THROTTLE = _default_throttle()


def _domain_of(url: str) -> str:
    try:
        return requests.utils.urlparse(url).netloc.lower()
    except Exception:
        return ""


class HttpClient:
    def __init__(self, throttle: Optional[DomainThrottle] = None,
                 timeout: int = 60, attempts: int = 4):
        self.throttle = throttle or SHARED_THROTTLE
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
                 expected_bytes: Optional[int] = None,
                 validator: Optional[Callable[[str], bool]] = None) -> bool:
        """流式下载到 dest，支持 Range 续传。成功(且体积达标)返回 True。

        完整性证据链（三层，缺一层就不许判成功）:
          1. 总长：响应带 Content-Length / Content-Range 时，完成后必须与之相等。
             **每一轮重试都重新取总长（attempt_size），绝不复用上一轮的值**——
             上一轮的值来自另一个响应，复用就会把两次响应的字节拼成
             "长度刚好对得上"的错位文件（审查 A P0-2 实测：5000+4000 == 旧 9000）。
          2. 迭代：响应未给总长（chunked）时，只有本轮迭代正常结束（completed）才接受。
          3. 语义：validator（可选）在"已存在文件"与"下载完成"两处调用，由调用方判断
             内容是否可读（如 PDF 能读出页数）。http 层不猜 PDF 语义。

        dest 已存在时：体积达标 **且**（未传 validator 或 validator(dest) 为真）才跳过。
        未传 validator 时与旧版行为一致（体积达标即跳过），保持向后兼容；
        官方 PDF 这类"体积远小于整本"的目标必须传 validator（审查 A P0-1）。
        """
        if os.path.exists(dest) and os.path.getsize(dest) >= max(min_bytes, 1):
            # 体积达标 != 内容完整。validator 抛异常一律视为不可信（宁可重下），
            # 不把异常抛给调用方——下载层的契约是"返回 bool"。
            trusted = validator is None
            if not trusted:
                try:
                    trusted = bool(validator(dest))
                except Exception:
                    trusted = False
            if trusted:
                return True
            try:
                os.remove(dest)      # 不可信的旧文件必须删，否则下次仍被它骗
            except OSError:
                pass
        part = dest + ".part"
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)

        def pump(f, r) -> None:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)

        def drop_part() -> None:
            try:
                os.remove(part)
            except OSError:
                pass

        def accept(tmp: str) -> bool:
            """提交前过 validator：长度对但内容不可读的，删掉重下而不是入库。"""
            if validator is not None:
                try:
                    if not validator(tmp):
                        return False
                except Exception:
                    return False
            os.replace(tmp, dest)
            return True

        for _ in range(1, self.attempts + 1):
            self.throttle.wait(url)
            headers: Dict[str, str] = {}
            if os.path.exists(part) and os.path.getsize(part) > 0:
                headers["Range"] = f"bytes={os.path.getsize(part)}-"
            completed = False
            # 本轮"可证明的总长"：None = 这一轮拿不到，不能据此判定完整性。
            attempt_size: Optional[int] = expected_bytes
            try:
                with self.session.get(url, timeout=min(self.timeout * 5, 600),
                                      headers=headers, stream=True) as r:
                    if r.status_code == 206:
                        m = re.search(r"/(\d+)\s*$",
                                      r.headers.get("Content-Range", ""))
                        if not m:
                            # 拿不到远端总长还继续追加，就会拼出"长度对、内容错"的文件。
                            # 丢残片、下一轮按全量重下（不带 Range）。
                            drop_part()
                            continue
                        attempt_size = int(m.group(1))
                        if os.path.exists(part) and os.path.getsize(part) >= attempt_size:
                            drop_part()          # 残片已达该总长：不能再追加
                            continue
                        with open(part, "ab") as f:
                            pump(f, r)
                        completed = True
                    elif r.status_code == 200:
                        clen = r.headers.get("Content-Length", "")
                        attempt_size = int(clen) if clen.isdigit() else None
                        with open(part, "wb") as f:   # 覆盖残片全量重下
                            pump(f, r)
                        completed = True
                    elif r.status_code == 416:
                        # 416 只说"Range 越界"，不告诉总长。只有调用方给了 expected_bytes
                        # 才能据此认账；否则任何 >= min_bytes 的残片都会被当成完整文件。
                        if expected_bytes is None:
                            drop_part()
                            continue
                        attempt_size = expected_bytes
                    else:
                        raise requests.HTTPError(f"HTTP {r.status_code}")
            except requests.RequestException:
                pass
            size = os.path.getsize(part) if os.path.exists(part) else 0
            if attempt_size is not None:
                if size == attempt_size and size >= min_bytes:
                    if accept(part):
                        return True
                    drop_part()
                    continue
                if size > attempt_size:
                    # 超长残片：立刻清掉重来（旧版要等到下一轮才删，白耗一次机会）
                    drop_part()
                    continue
            elif completed and size >= min_bytes:      # 无总长：仅收完整迭代
                if accept(part):
                    return True
                drop_part()
                continue
        return False


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
