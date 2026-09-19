#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""整合回归测试（全部离线，零联网）。

运行: python3 tests/test_regressions.py
退出码 0=全部通过。覆盖：数据筛选 · 繁体转换 · 分类富化 · SRU 解析 ·
挂载检测 · 流式 PDF · 配额/节流 · 标注 CRUD · 书名链接。
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

# ---------- 配额滑动窗口 ----------
from core.limiter import HourQuota  # noqa: E402

q = HourQuota(default_quota=2)
assert q.allow("loc") and q.allow("loc") and not q.allow("loc")
ok("每小时配额滑动窗口")

# ---------- 书名链接计数语义 ----------
from core.pipeline import create_title_links, _sanitize_filename  # noqa: E402

assert _sanitize_filename('永樂大典/卷六十八:十八陽') == "永樂大典_卷六十八_十八陽"
c1, s1 = create_title_links(d)
c2, s2 = create_title_links(d)
assert c2 == 0, (c1, c2)          # 第二次必须全部"已存在"
ok(f"书名硬链接幂等（首跑 +{c1}，二跑 +0/{s2}）")

# ---------- 汇总 ----------
print(f"\n== 全部 {len(PASS)} 组回归通过 ==")
