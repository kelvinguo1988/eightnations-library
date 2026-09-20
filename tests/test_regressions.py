#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""整合回归测试（全部离线，零联网）。

运行: python3 tests/test_regressions.py
退出码 0=全部通过。覆盖：数据筛选 · 繁体转换 · 分类富化 · SRU 解析 ·
挂载检测 · 流式 PDF（含原子写出） · jobs 台账配额/原子认领 · 租约回收 ·
路径穿越校验 · 下载完整性（chunked/Content-Length） · 书名链接。
"""
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "fixtures")
PASS = []


def ok(name):
    PASS.append(name)
    print(f"  ✓ {name}")


# ---------- 数据筛选 ----------
from core.db import DB

tmp = tempfile.mkdtemp()
os.makedirs(f"{tmp}/db", exist_ok=True)
d = DB(f"{tmp}/db/library.db")
d.init()
from core.importer import import_snapshot_files  # noqa: E402

new, upd = import_snapshot_files(d, "loc", os.path.join(FIXTURES, "loc"))
assert new >= 40, (new, upd)
assert d.count_books(status="discovered", era="明,清") >= 40
assert d.count_books(status="discovered", era="unknown") >= 0
assert d.count_books(status="discovered", subjects="不存在的分类") == 0
ok("快照导入 + era/subjects 多值筛选")

# ---------- 繁体转换 ----------
from core.text import jp2t  # noqa: E402

assert jp2t("欽定古今図書集成") == "欽定古今圖書集成"
assert jp2t("太平御覧") == "太平御覽"
assert jp2t("Yuan ye") == "Yuan ye"
assert jp2t(None) is None
ok("日文新字体→繁体转换")

# ---------- 分类富化 ----------
from core.models import BookMeta  # noqa: E402
from sites.na_jp import NaJpAdapter  # noqa: E402

m = BookMeta(source_uid="x", title="朱子語類")
html = ('<dt>旧蔵者</dt><dd>昌平坂学問所</dd>'
        '<dt>書誌事項</dt><dd>刊本:朝鮮:::</dd>'
        '<dt>言語</dt><dd>中国語</dd>')
NaJpAdapter.enrich_categories(m, NaJpAdapter.parse_item_page(html))
assert m.subjects == ["漢籍", "昌平坂学問所旧蔵", "朝鮮刊本", "中文"], m.subjects
ok("日本馆分类富化")

# ---------- na_jp 收割器（假 HTTP） ----------
class FakeHttp:
    def __init__(self, ids, fail_vids=()):
        self.ids = ids; self.n = 0; self.fail_vids = set(map(str, fail_vids))
    def get(self, url, as_json=False, **kw):
        self.n += 1
        if "/fonds/" in url:
            return "".join(f'<a href="/img/{i}">' for i in self.ids)
        vid = re.search(r"/api/iiif/(\d+)/", url).group(1)
        if vid in self.fail_vids:
            return None
        mf = {"sequences": [{"canvases": [{"images": [{"resource": {
            "@id": f"https://x/da12/C{vid}001"}}]}]}], "label": f"件名{vid}"}
        return mf if as_json else json.dumps(mf)

a = NaJpAdapter(FakeHttp(["101", "102", "103", "104", "105"]))
s = a.harvest_step("https://x/fonds/99", budget=2)
assert s == {"ids": 5, "fetched": 2, "skipped": 0, "blocked": False,
             "exhausted": False}, s
a = NaJpAdapter(FakeHttp(["201", "202", "203", "204", "205"],
                         fail_vids={"203", "204", "205"}))
got = []
s = a.harvest_step("https://x/fonds/99", budget=0,
                   on_meta=lambda m: got.append(m.source_uid))
assert s["blocked"] and got == ["201", "202"], (s, got)
ok("收割器预算/限流中止")

# ---------- BnF SRU 解析 ----------
from sites.bnf import _parse_sru  # noqa: E402

fx = os.path.join(FIXTURES, "bnf_sru_sample.xml")
if os.path.exists(fx):
    r = _parse_sru(open(fx).read())
    assert r["total"] >= 1000
    assert r["records"][0]["ark"].startswith("btv1")
    ok("BnF SRU 解析（fixture）")
else:
    print("  - BnF fixture 不存在，跳过")

# ---------- 挂载检测 ----------
from core.mounts import data_mount, migration_commands  # noqa: E402

assert data_mount()["kind"] in ("bind", "volume", "unknown")
assert "migrate-nas.sh" in migration_commands("x")
ok("挂载检测与迁移指引")

# ---------- 流式 PDF ----------
from core import pdfbuild  # noqa: E402
from pypdf import PdfReader  # noqa: E402
from PIL import Image  # noqa: E402

t = tempfile.mkdtemp()
paths = []
for i in range(6):
    p = f"{t}/p{i}.jpg"
    Image.new("RGB", (700 + i, 1000), (90 + i * 20, 60, 40)).save(
        p, "JPEG", quality=88)
    paths.append(p)
out = f"{t}/t.pdf"
pdfbuild.build_pdf(paths, out)
assert len(PdfReader(out).pages) == 6
pdfbuild.build_pdf(paths[:2] + paths[4:], f"{t}/g.pdf")
assert len(PdfReader(f"{t}/g.pdf").pages) == 4
ok("流式 PDF 构建（连续/非连续页）")

# ---------- jobs 台账配额 + 原子认领 ----------
def _mk_queued(uid):
    d.upsert_book("loc", {"source_uid": uid, "collection": "quota-set",
                          "title": uid})
    row = d.find_book("loc", uid)
    d.set_status(row["id"], "queued")
    return row["id"]

b1, b2 = _mk_queued("quota-uid-1"), _mk_queued("quota-uid-2")
job1, full1, claimed1 = d.claim_and_start(b1, "auto", quota_limit=1)
assert claimed1 and not full1 and job1
job2, full2, claimed2 = d.claim_and_start(b2, "auto", quota_limit=1)
assert not claimed2 and full2          # 本小时 loc 已启动 1 册 = 配额用尽
assert d.get_book(b2)["status"] == "queued"   # 认领失败不消耗配额、不改状态
_, _, again = d.claim_and_start(b1, "auto")
assert not again                       # 已 running：不可重复认领
job3, _, claimed3 = d.claim_and_start(b2, "auto")   # 不限额仍可认领
assert claimed3
d.job_heartbeat(job1)                  # 续约不报错
ok("jobs 台账小时配额 + 认领原子性（满额不扣不脏写）")

# ---------- 租约回收 reap_stuck ----------
import subprocess  # noqa: E402

proc = subprocess.Popen([sys.executable, "-c", "pass"])
proc.wait()
dead_pid = proc.pid                    # 已退出的真实 PID

def _add_job(book_id, pid, started, heart=""):
    with d._immediate() as conn:
        cur = conn.execute(
            "INSERT INTO jobs(book_id,state,quality,started_at,pid,heartbeat_at) "
            "VALUES(?,'running','auto',?,?,?)", (book_id, started, pid, heart))
        return int(cur.lastrowid)

b3 = _mk_queued("reap-dead-pid")
d.claim_and_start(b3, "auto")          # running + 活 job
with d._immediate() as conn:           # 换成"已死执行方"
    conn.execute("UPDATE jobs SET pid=? WHERE book_id=?", (dead_pid, b3))
b4 = _mk_queued("reap-stale")
sj = _add_job(b4, os.getpid(), "2020-01-01T00:00:00Z")   # 活 PID 但超时
with d._immediate() as conn:
    conn.execute("UPDATE books SET status='running' WHERE id=?", (b4,))
b5 = _mk_queued("reap-healthy")
with d._immediate() as conn:
    conn.execute("UPDATE books SET status='running' WHERE id=?", (b5,))
hj = _add_job(b5, os.getpid(), "2099-01-01T00:00:00Z")  # 活租约，绝不可回收
n = d.reap_stuck(stale_minutes=60)
assert n >= 2, n
assert d.get_book(b3)["status"] == "queued"
assert d.get_book(b4)["status"] == "queued"
assert d.get_book(b5)["status"] == "running"      # 在途下载不动
with d.connect() as conn:
    assert conn.execute("SELECT state FROM jobs WHERE id=?", (sj,)).fetchone(
        )["state"] == "failed"         # 回收同时关闭 job 行
    assert conn.execute("SELECT state FROM jobs WHERE id=?", (hj,)).fetchone(
        )["state"] == "running"
ok("租约回收：死 PID 立即回收 / 心跳超时回收 / 在途不动，job 行同事务关闭")

# ---------- 路径校验（穿越防御） ----------
from core.pipeline import _dest_dir  # noqa: E402

for bad_uid in ("", ".", "..", "../etc", "a/b", "a\\b", "a\x00b"):
    try:
        _dest_dir("loc", "coll", bad_uid)
        raise AssertionError(f"应拒绝 uid={bad_uid!r}")
    except ValueError:
        pass
try:
    _dest_dir("loc", "../../etc", "u")
    raise AssertionError("应拒绝穿越 collection")
except ValueError:
    pass
p_ok = _dest_dir("loc", "", "u1")      # 空专藏归入 misc（与新校验一致）
assert p_ok.endswith(os.path.join("misc", "u1")), p_ok
for meta_bad in ({"source_uid": "../x", "collection": "c"},
                 {"source_uid": "", "collection": "c"},
                 {"source_uid": "u", "collection": "a/b"}):
    try:
        d.upsert_book("loc", meta_bad)
        raise AssertionError(f"应拒绝 {meta_bad}")
    except ValueError:
        pass
assert d.upsert_book("loc", {"source_uid": "empty-coll-ok",
                             "collection": ""})    # 空 collection 合法
ok("落盘路径段校验：_dest_dir/upsert_book 拒绝穿越、放行空专藏")

# ---------- 下载完整性（假 HTTP，零联网） ----------
import requests  # noqa: E402
from core.http import HttpClient, DomainThrottle  # noqa: E402

class _NoWait(DomainThrottle):
    def wait(self, url):
        pass

class _Resp:
    def __init__(self, status, chunks, headers=None, exc=None):
        self.status_code = status
        self._chunks = chunks
        self.headers = headers or {}
        self._exc = exc
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def iter_content(self, chunk_size=1):
        yield from self._chunks
        if self._exc:
            raise self._exc

class _Session:
    def __init__(self, factory):
        self._factory = factory
    def get(self, url, **kw):
        return self._factory()

def _dl(resp_factory, dest):
    hc = HttpClient(throttle=_NoWait(), attempts=2)
    hc.session = _Session(resp_factory)
    return hc.download("https://x/y.jpg", dest, min_bytes=1000)

tdir = tempfile.mkdtemp()
# chunked 中断（无 Content-Length、迭代抛异常）→ 必须判失败
r = _dl(lambda: _Resp(200, [b"a" * 2000],
                      exc=requests.exceptions.ChunkedEncodingError()),
        f"{tdir}/cut.jpg")
assert not r and not os.path.exists(f"{tdir}/cut.jpg")
# chunked 正常收完 → 成功
r = _dl(lambda: _Resp(200, [b"b" * 6000, b"c" * 5000]), f"{tdir}/full.jpg")
assert r and os.path.getsize(f"{tdir}/full.jpg") == 11000
# 声称 Content-Length 却只给一半 → 失败
r = _dl(lambda: _Resp(200, [b"d" * 1000],
                      headers={"Content-Length": "9000"}),
        f"{tdir}/short.jpg")
assert not r and not os.path.exists(f"{tdir}/short.jpg")
ok("下载完整性：chunked 中断拒收 / 完整迭代接受 / Content-Length 不符拒收")

# ---------- PDF 原子写出 ----------
bad = f"{t}/bad.jpg"
with open(bad, "wb") as f:
    f.write(b"\xff\xd8\xff\xfe not-a-complete-jpeg-data")
half = f"{t}/half.pdf"
try:
    pdfbuild.build_pdf([paths[0], bad], half)
    raise AssertionError("坏 JPEG 应抛异常")
except ValueError:
    pass
assert not os.path.exists(half) and not os.path.exists(half + ".part")
ok("build_pdf 原子性：中途异常不留半成品 PDF")

# ---------- 书名链接计数语义 ----------
from core.pipeline import create_title_links, _sanitize_filename  # noqa: E402

assert _sanitize_filename('永樂大典/卷六十八:十八陽') == "永樂大典_卷六十八_十八陽"
c1, s1 = create_title_links(d)
c2, s2 = create_title_links(d)
assert c2 == 0, (c1, c2)          # 第二次必须全部"已存在"
ok(f"书名硬链接幂等（首跑 +{c1}，二跑 +0/{s2}）")

# ---------- 汇总 ----------
print(f"\n== 全部 {len(PASS)} 组回归通过 ==")
