#!/usr/bin/env python3
"""变异测试：证明 tests/ 里的守卫测试真的能抓住缺陷（而不是永远绿）。

原理
----
对业务代码做一处**语义明确、必然有害**的小改动（"变异"），然后跑测试：
  * 测试变红 = 变异被"杀死"（killed）→ 说明这条防线真实存在；
  * 测试仍绿 = 变异"逃逸"（survived）→ 说明该行为**没有测试覆盖**，
    哪怕代码现在是对的，将来改坏了也不会有人知道。

审查 D 的基线是"8 个变异只有 3 个被抓"（5 个逃逸），本脚本的目标是 8/8。

用法
----
    python3 tests/mutation_check.py            # 全部变异
    python3 tests/mutation_check.py -v         # 附带每个变异的失败用例名
    python3 tests/mutation_check.py -k quota   # 只跑名字含 quota 的变异

退出码：0 = 全部杀死；1 = 有逃逸（CI 应据此失败）。

实现要点
--------
* 每个变异先 `git stash`-free 地**备份原文件到内存**，在 `finally` 里用原样
  内容回写（保证异常/中断也不留脏改动），并校验 md5 复原成功。
* 跑测试用 `-x` 提前退出（只要能红就够），并设超时，避免变异导致死循环卡住。
* 不改动 git 状态、不产生 commit；工作区在脚本结束后与运行前逐字节一致。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# 变异清单：(名字, 相对路径, 原文片段, 替换片段, 期望被哪类测试抓住)
# 每条都对应 docs/review 里的一条真实缺陷 —— 修掉它，测试就必须能守住。
# ---------------------------------------------------------------------------
MUTATIONS = [
    # ---- 下载完整性（A-core P0-1/P0-2）----
    ("A1 206 拿不到总长时继续追加残片（拼出长度对内容错的文件）",
     "core/http.py",
     # 复刻修前行为：不丢残片、继续往 .part 追加，且沿用本轮/调用方给的总长
     '                        if not m:\n'
     '                            # 拿不到远端总长还继续追加，就会拼出"长度对、内容错"的文件。\n'
     '                            # 丢残片、下一轮按全量重下（不带 Range）。\n'
     '                            drop_part()\n'
     '                            continue\n',
     '                        if not m:\n'
     '                            with open(part, "ab") as f:\n'
     '                                pump(f, r)\n'
     '                            completed = True\n',
     "download_full_size_leak_with_known"),

    ("A2 skip-if-exists 不校验内容",
     "core/http.py",
     "            trusted = validator is None\n",
     "            trusted = True\n",  # 永远信任已存在文件
     "download_validator_rejects_bad_pdf"),

    ("A3 416 无总长时接受任意残片",
     "core/http.py",
     '                        if expected_bytes is None:\n'
     '                            drop_part()\n'
     '                            continue\n',
     '                        completed = True   # 修前：416 一律当"已到全长"\n',
     "download_416_without"),

    # ---- 配额与重试（A-core P0-3）----
    ("C2 attempt 上限从 SQL 移除（dead 无限重试）",
     "core/db.py",
     "AND attempt < ?\"\n                    % \",\".join(\"?\" * len(allowed)),\n"
     "                    (book_id, *allowed, MAX_ATTEMPTS))",
     "\"\n                    % \",\".join(\"?\" * len(allowed)),\n"
     "                    (book_id, *allowed))",
     "attempt_limit"),

    # ---- 弱成功补齐（A-core P1-2）----
    ("C1 部分完成也清欠账标记（缺卷永不补齐）",
     "core/db.py",
     '                "UPDATE books SET status=\'done\',needs_topup=1,last_error=? "\n'
     '                "WHERE id=?", (note[:500], book_id))',
     '                "UPDATE books SET status=\'done\',needs_topup=0,last_error=? "\n'
     '                "WHERE id=?", (note[:500], book_id))',
     "partial_success"),

    # ---- 收割器语义（C-adapters P0-4）----
    ("B2 exhausted 退回宽松语义（没抓满预算就算完）",
     "sites/na_jp.py",
     "        exhausted = (not blocked) and (not budget_hit) and reached_end \\\n"
     "            and failed == 0\n",
     "        exhausted = (not blocked) and fetched < (budget or 10 ** 9)\n",
     "exhausted"),

    # ---- 阅读器可见性（C-adapters P0-2）----
    ("B3 多卷首卷命名为 cover.pdf（阅读器看不到卷首）",
     "sites/na_jp.py",
     '        return [f"book_{i + 1:02d}.pdf" for i in range(len(vols))]\n',
     '        return ["cover.pdf"] + [f"book_{i + 2:02d}.pdf"\n'
     '                                for i in range(len(vols) - 1)]\n',
     "volume_names"),

    # ---- 路径穿越防御（A-core P1-6）----
    ("D 允许 '..' 通过路径段校验（路径穿越）",
     "core/pipeline.py",
     '        return (v in (".", "..") or "/" in v or "\\\\" in v or "\\x00" in v)',
     '        return ("/" in v or "\\\\" in v or "\\x00" in v)',
     "dest_dir_and_upsert_reject_traversal"),

    # ---- 前端安全（B-web P1）----
    ("E 模板关闭自动转义（XSS 复活）",
     "web/app.py",
     'templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "web", "templates"))',
     'templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "web", "templates"))\n'
     'templates.env.autoescape = False',
     "autoescape_enabled"),

    # ---- Markdown 导出 XSS（B-web，本轮 Lead 修）----
    ("F Markdown 导出不转义正文（导出文件执行脚本）",
     "web/app.py",
     '    return (str(s).replace("&", "&amp;").replace("<", "&lt;")\n'
     '            .replace(">", "&gt;"))',
     "    return str(s)",
     "markdown_escapes"),

    # ---- 阅读器布局（B-web P1）----
    ("G reader.js 不写 dataset.layout（布局持久化失效）",
     "web/static/reader.js",
     "  root.dataset.layout = layout;",
     "  /* mutation: removed */",
     "reader_js_writes_dataset_layout"),

    # ---- 健康检查契约（B-web P1）----
    ("H /api/health DB 不可用仍返回 200",
     "web/app.py",
     '        return JSONResponse({"ok": False, "ts": utcnow(), "db": False,\n'
     '                             "error": f"数据库不可用: {e}"[:200]},\n'
     '                            status_code=503)',
     '        return JSONResponse({"ok": True, "ts": utcnow(), "db": False})',
     "health_503"),
]


def _digest(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _purge_pycache() -> None:
    """删掉全部 __pycache__。

    必须做：变异把 .py 改坏后若进程被中断，Python 会留下**变异版 .pyc**。
    之后即使 .py 已复原，只要 mtime/size 恰好匹配，解释器仍会加载旧字节码
    ——表现为"代码看着是对的、行为却是坏的"，极难排查（本脚本开发时就踩到：
    record_partial 源码写着 needs_topup=1，实际却写 0）。
    """
    for root, dirs, _ in os.walk(ROOT):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                dirs.remove(d)


def _run_tests(timeout: int = 300) -> tuple[int, str]:
    """跑全量 pytest；返回 (退出码, 输出)。用 -x 提前停。"""
    _purge_pycache()
    env = dict(os.environ)
    env.setdefault("EIGHTNATIONS_DATA", tempfile.mkdtemp(prefix="mut-"))
    try:
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-x", "-q", "--no-header"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT（变异可能引入死循环）"


def main() -> int:
    verbose = "-v" in sys.argv
    only = ""
    for i, a in enumerate(sys.argv):
        if a == "-k" and i + 1 < len(sys.argv):
            only = sys.argv[i + 1]

    print("=" * 72)
    print("变异测试：验证守卫测试是否真的能抓住缺陷")
    print("=" * 72)

    # 0) 前置：未变异时必须全绿，否则"红"不能归因于变异
    rc, out = _run_tests()
    if rc != 0:
        print("✗ 前置检查失败：未变异时测试就没全绿，先修好再跑变异。")
        print(out[-2000:])
        return 1
    print("✓ 前置检查：未变异时全绿\n")

    results = []
    for name, rel, old, new, expect in MUTATIONS:
        if only and only not in name:
            continue
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            print(f"✗ 跳过（文件不存在）: {rel}")
            results.append((name, "SKIP", ""))
            continue
        original = open(path, "rb").read()
        before = _digest(path)
        if old.encode() not in original:
            print(f"⚠ 跳过（原文片段已变，变异失效）: {name}")
            print(f"   在 {rel} 里找不到片段；代码可能已被后续修改。")
            results.append((name, "STALE", ""))
            continue
        try:
            open(path, "wb").write(original.replace(old.encode(), new.encode(), 1))
            rc, out = _run_tests()
            killed = rc != 0
            detail = ""
            if killed and verbose:
                for ln in out.splitlines():
                    if ln.startswith("FAILED") or "AssertionError" in ln:
                        detail = ln.strip()[:120]
                        break
            results.append((name, "KILLED" if killed else "SURVIVED", detail))
            print(f"{'✓ KILLED  ' if killed else '✗ SURVIVED'} {name}")
            if killed and detail:
                print(f"            └ {detail}")
            if not killed:
                print(f"            └ 期望被 {expect} 抓住，但测试仍全绿")
        finally:
            # 无论成功/异常/中断，都把原文逐字节写回并校验
            open(path, "wb").write(original)
            _purge_pycache()          # 别把变异版 .pyc 留给下一次运行
            if _digest(path) != before:
                print(f"!! 警告：{rel} 复原失败，请手工 git checkout -- {rel}")
        shutil.rmtree(os.environ.get("EIGHTNATIONS_DATA", ""), ignore_errors=True)

    killed_n = sum(1 for _, s, _ in results if s == "KILLED")
    total = sum(1 for _, s, _ in results if s in ("KILLED", "SURVIVED"))
    print()
    print("=" * 72)
    print(f"结果：{killed_n}/{total} 个变异被杀死")
    if total - killed_n:
        print("逃逸的变异（= 该行为缺少测试覆盖，需要补守卫）：")
        for n, s, _ in results:
            if s == "SURVIVED":
                print(f"  * {n}")
    stale = [n for n, s, _ in results if s == "STALE"]
    if stale:
        print(f"注意：{len(stale)} 个变异因代码已改动而失效，请同步更新本脚本。")
    print("=" * 72)
    return 0 if killed_n == total else 1


if __name__ == "__main__":
    sys.exit(main())
