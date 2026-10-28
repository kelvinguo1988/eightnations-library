"""pytest 公共夹具：临时数据目录、隔离的 DB、离线 HTTP 桩、样张图片。

设计约束（批 3 任务）:
  * **完全离线**：不联网；所有站点交互由 FakeHttp / FakeSession 替代。
  * **不污染仓库**：一切临时文件落在 pytest 的 tmp_path 里。
  * **零泄漏**：每个用例独立的 DB / books 目录；切换 EIGHTNATIONS_DATA 时
    同时刷新 `core.pipeline.BOOKS_DIR`（该常量在 import 时由环境变量求值，
    不刷新就会把文件写到宿主 data/ 下——这是旧脚本"泄漏 3 个临时目录"的
    同类根因）。

注意：本文件里的 `pytest_collection_modifyitems` 是**假绿灯守卫**——
`python3 -m pytest tests/ -q` 曾经收集 0 个测试却 exit 0，必须让它失败。
"""
import sys
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "fixtures"


# ---------------------------------------------------------------- 假绿灯守卫
def pytest_collection_modifyitems(config, items):
    """收集数为 0 时让整轮明确失败。

    pytest 的既有行为：没有测试时退出码是 5（不是 0），但 CI 里若用
    `pytest ... || true`、或收集阶段用 `--co` 判断、或有人把它包在脚本里，
    就很容易变成"绿"。这里把"0 个用例"钉死为 error，任何入口都一样。
    """
    if not items:
        raise pytest.UsageError(
            "测试收集数为 0：这一定是配置/导入错误（假绿灯）。"
            "请检查 pytest.ini 的 testpaths 与 tests/ 是否可导入。")


# ---------------------------------------------------------------- 数据目录
@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """隔离的 EIGHTNATIONS_DATA；同步刷新所有在 import 时求值过的路径常量。"""
    d = tmp_path / "data"
    (d / "db").mkdir(parents=True)
    (d / "books").mkdir(parents=True)
    monkeypatch.setenv("EIGHTNATIONS_DATA", str(d))

    import core.pipeline as pipeline
    monkeypatch.setattr(pipeline, "BOOKS_DIR", str(d / "books"), raising=False)
    # core.http 的 FileThrottle 目录也由环境变量决定；每个用例独立目录，
    # 避免用例之间通过锁文件互相等待。
    import core.http as http_mod
    monkeypatch.setattr(http_mod, "_data_dir", lambda: str(d), raising=True)
    return d


@pytest.fixture()
def db(data_dir):
    """空库（含 seeds）。"""
    from core.db import DB
    d = DB(str(data_dir / "db" / "library.db"))
    d.init()
    return d


@pytest.fixture()
def empty_db_seeded(data_dir):
    """别名夹具：语义上强调"只跑了 init() 的库"，供 seeds 断言使用。"""
    from core.db import DB
    d = DB(str(data_dir / "db" / "library.db"))
    d.init()
    return d


# ---------------------------------------------------------------- 造书工具
def make_queued(d, uid, source="loc", collection="test-set", title=None,
                **extra):
    """插入一本 queued 书并返回最新 row。"""
    meta = {"source_uid": uid, "collection": collection,
            "title": title or uid}
    meta.update(extra)
    d.upsert_book(source, meta)
    row = d.find_book(source, uid)
    d.set_status(row["id"], "queued")
    return d.get_book(row["id"])


@pytest.fixture()
def make_book():
    return make_queued


# ---------------------------------------------------------------- 假 HTTP
class NoWaitThrottle:
    """不起作用的节流（测试里不许 sleep）。"""

    def wait(self, url):        # noqa: D401, ARG002
        return None


class FakeResp:
    """requests.Response 的最小替身（支持 with / headers / iter_content）。"""

    def __init__(self, status=200, chunks=None, headers=None, exc=None):
        self.status_code = status
        self._chunks = list(chunks or [])
        self.headers = headers or {}
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk_size=1):     # noqa: ARG002
        yield from self._chunks
        if self._exc is not None:
            raise self._exc


class SessionStub:
    """按调用次序返回预设响应的 session 替身。"""

    def __init__(self, *resps):
        self._resps = list(resps)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw.get("headers") or {}))
        if not self._resps:
            # 响应耗尽 = 实现多发了一次请求（通常是重试）。抛真实的网络异常，
            # 让被测的 download() 走"请求失败"分支，而不是把 AssertionError
            # 漏出被测代码（那会让测试失败在错误的位置、掩盖真实行为）。
            raise requests.exceptions.ConnectionError(
                f"SessionStub: 预设响应已用完（第 {len(self.calls)} 次请求）")
        return self._resps.pop(0)

    def post(self, url, **kw):                # na_jp contentDownload 用
        self.calls.append((url, kw.get("data")))
        if not self._resps:
            raise requests.exceptions.ConnectionError(
                f"SessionStub: 预设响应已用完（第 {len(self.calls)} 次请求）")
        return self._resps.pop(0)


def make_http(monkeypatch, *resps, attempts=2):
    """构造一个用 SessionStub 顶替真实网络的 HttpClient。"""
    from core.http import HttpClient
    hc = HttpClient(throttle=NoWaitThrottle(), attempts=attempts)
    stub = SessionStub(*resps)
    hc.session = stub
    return hc, stub


@pytest.fixture()
def http_factory(monkeypatch):
    def _make(*resps, attempts=2):
        return make_http(monkeypatch, *resps, attempts=attempts)
    return _make


# ---------------------------------------------------------------- 图片 / PDF
@pytest.fixture()
def jpeg_factory(tmp_path):
    """生成真实 JPEG（PIL），供 pdfbuild / 组图测试使用。"""
    from PIL import Image

    def _make(name="p0.jpg", size=(700, 1000), color=(90, 60, 40)):
        p = tmp_path / name
        Image.new("RGB", size, color).save(str(p), "JPEG", quality=88)
        return str(p)
    return _make


def write_pdf(path, pages=1):
    """写一个结构合法（可被 pypdf 读出页数）的最小 PDF。"""
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=300)
    with open(path, "wb") as f:
        w.write(f)
    return str(path)


def is_valid_pdf(path):
    """模拟 sites/loc.py 的 `_pdf_ok`：能读出 >=1 页才算可用。"""
    from pypdf import PdfReader
    try:
        return len(PdfReader(str(path)).pages) >= 1
    except Exception:
        return False


# ---------------------------------------------------------------- Web App
@pytest.fixture()
def web_client(data_dir, monkeypatch):
    """一个指向 tmp 数据目录的 TestClient（每个用例独立进程内单例）。

    web.app 在 import 时求值 DB_PATH / BOOKS_DIR，因此在 import 之前设置
    环境变量；已 import 过的模块要 reload 才能吃到新路径。
    """
    for mod in [m for m in list(sys.modules) if m.startswith(("web.app", "web"))]:
        sys.modules.pop(mod, None)
    from fastapi.testclient import TestClient
    import web.app as app_mod
    # 清掉进程内 DB 单例：否则上一个用例的库会被复用
    app_mod._DB = None
    app_mod.DATA_DIR = str(data_dir)
    app_mod.DB_PATH = str(data_dir / "db" / "library.db")
    app_mod.BOOKS_DIR = str(data_dir / "books")
    client = TestClient(app_mod.app)
    client.app_module = app_mod
    return client
