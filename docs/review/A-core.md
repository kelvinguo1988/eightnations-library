# 审查 A：`core/` 存储与下载管线

> 审查范围：`core/db.py`(602) · `core/pipeline.py`(196) · `core/http.py`(226) · `core/pdfbuild.py`(155) ·
> `core/doctor.py`(150) · `core/limiter.py`(24) · `core/importer.py`(93) · `core/models.py`(38) ·
> `core/text.py`(49) · `core/mounts.py`(59)，以及为厘清边界而查阅的调用方
> `scheduler.py` / `manage.py` / `web/app.py` / `sites/{base,loc,na_jp}.py`。
>
> 审查方式：逐行静态审查 + 在 `/tmp/review-a/` 写最小复现脚本（18 个，全部离线，不联网、不改仓库、不 commit）+
> 对本机真实库 `data/db/library.db`（2128 册）做**只读**交叉验证。
>
> 基线：`python3 tests/test_regressions.py` → **13 组全过**；Python 3.9.9。
>
> 图例：**P0 阻塞**（数据损坏/数据丢失/安全）· **P1 重要**（功能语义错误、可靠性隐患）· **P2 优化**（性能/可维护性/一致性）。

---

## 0. 结论摘要

| 级别 | 条数 | 关键项 |
|---|---|---|
| P0 | **3** | ①下载 skip-if-exists 信任半成品导致**永久损坏文件被判成功**；②`full_size` 跨 attempt 泄漏可**拼出长度正确但内容错误**的文件；③`attempt` 计数被写死为"5 次"却实际**无上限重试**（死循环 + 日志说谎） |
| P1 | 12 | 稳定错误被重试到 dead（配置型失败永久丢失）；`ok=bool(outputs)` 弱成功；`set_status` 无迁移校验；`upsert_book` 字段覆盖；`_immediate` 二次异常与长写锁；`reap_stuck` 60 分钟租约与容器重启；节流非串行语义；`fetch_one` 崩溃留下无界 `running`；路径校验三处重复实现 |
| P2 | 10 | 连接池/PRAGMA 开销（实测可接受）、`facets()` 全表读（实测 15ms/5k 行）、UTC 读日键、死代码、异常吞掉、类型标注等 |

**先说清楚一个关键判断**：Lead 给的已知可疑点 #1 的**机制描述不完全准确**（不是"5 次变 2.5 次"，见 §2.1），
但**结论更严重**：`dead` 状态可以被无限重新认领，配合 Web「重试」可以打 25 次以上且 `attempt` 无上限。
已知可疑点 #3（跳过半成品）**在真实数据里已经发生过一次**（`jobs` 表有 `卷1: PDF 校验失败 Stream has ended unexpectedly` 的历史记录），
说明"半截文件"曾真实落到 `dest` 路径上——这正是 #3 与 #2 的共同前置条件。

---

## 1. P0 — 阻塞级（安全 / 数据损坏）

### P0-1 下载层 `skip-if-exists` 只比体积，半截文件被**永久**判成功

| 项 | 内容 |
|---|---|
| 位置 | `core/http.py:163-164`（跳过逻辑）；`sites/loc.py:236`（`min_bytes=100_000`）；`sites/base.py:108`（页图 `>min_bytes` 预检） |
| 级别 | **P0** |

**代码证据**

```python
# core/http.py:163-164
if os.path.exists(dest) and os.path.getsize(dest) >= max(min_bytes, 1):
    return True
```

**问题**：`dest` 是"已提交"的最终文件（`.part` 才是下载中文件）。一旦有任何路径把**不完整的内容**
写到 `dest`，此后所有重试（含换时刻、换进程）都会在**零 HTTP 请求**的情况下直接返回 `True`。
`min_bytes` 对官方 PDF 是 `100_000`（`loc.py:236`），而一本 100 页的善本约 20MB，
阈值只占 0.5%——"半截但 >100KB"是**常态**而非边界情况。

**真实影响链路**（三条独立入口都汇到同一个洞）：

1. **`pdf_page_count` 用的是 pypdf，pypdf 对截断 PDF 可能返回页数而不抛异常**
   → `loc.py:236-241`：`download` 返回 True → `pdf_page_count` 没抛 → `outputs.append` →
   `pages_done += n` → 整本记为 `done`。`doctor` 的"已归档 PDF 对账"只检查"目录里有 .pdf"（`doctor.py:44-49`），
   照样通过；只有手工 `doctor --verify` 的 sha256 才能发现（且它在 Web 的 `/api/doctor` 里被硬编码为 `verify_n=0`，见 `web/app.py:734`）。
2. **真实发生过**：本机库 `jobs` 表现存失败记录 `卷1: PDF 校验失败 Stream has ended unexpectedly`（`book_id=39`）。
   该消息来自 `loc.py:242-248` 的 `except`，说明"下载返回 True 但 pypdf 抛异常"确实发生过；
   而该分支只 `os.remove(out_pdf)` —— 如果 pypdf 那次**没**抛异常，就落成 done 了。
3. **页级组图**：`assemble_pages_to_pdf` 里 `if not (os.path.exists(path) and os.path.getsize(path) > min_bytes): download(...)`
   （`sites/base.py:108`）与 `download` 内部的同一个判断形成"双重信任"，残页无法自愈。

**复现（`/tmp/review-a/t2_exists.py`）**

```
1) dest 已有 200KB 半截文件，服务端本有 400KB：download 返回 True，
   HTTP 请求次数 0，实际大小 200018 —— 半成品被当成成功
```

**建议修法（最小 diff）**：把"已存在即成功"降级为**仅在能证明完整性时**才跳过，并给目标文件留一个可核对的总长。

```python
# core/http.py —— 用 sidecar 记录"上次成功提交时的总长"，取代裸体积判断
def _meta_path(dest: str) -> str:
    return dest + ".size"

def _verified_exists(dest: str, min_bytes: int) -> bool:
    try:
        size = os.path.getsize(dest)
    except OSError:
        return False
    if size < max(min_bytes, 1):
        return False
    try:
        with open(_meta_path(dest), "r", encoding="ascii") as f:
            expect = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return False          # 没有总长凭证 → 宁可重下（页级续传成本低）
    return expect == size

def download(self, url, dest, *, min_bytes=1024, expected_bytes=None) -> bool:
    if _verified_exists(dest, min_bytes):
        return True
    ...
            if size == full_size and size >= min_bytes:
                os.replace(part, dest)
                with open(_meta_path(dest), "w", encoding="ascii") as f:
                    f.write(str(full_size))       # 只有长度可证时才写凭证
                return True
```

配套（重要，比上面的补丁更能兜底）：`loc.py:236` 这类"官方 PDF 直链"调用应把
`expected_bytes` 传入（HEAD 拿 `Content-Length`，一次请求，成本可忽略），
并在 `pdfbuild.pdf_page_count` 之外再校验 **PDF 尾部 `%%EOF`**（纯字节检查，零依赖）：

```python
def looks_complete_pdf(path: str) -> bool:
    with open(path, "rb") as f:
        f.seek(0, 2); n = f.tell()
        if n < 1024: return False
        f.seek(max(0, n - 4096)); return b"%%EOF" in f.read()
```

---

### P0-2 `full_size` 跨 attempt 泄漏：可拼出"长度正确、内容错位"的 PDF 并判成功

| 项 | 内容 |
|---|---|
| 位置 | `core/http.py:167`（`full_size = expected_bytes` 在循环**外**）· `:183-219` |
| 级别 | **P0**（数据损坏，且校验手段只看长度） |

**代码证据**

```python
165:        part = dest + ".part"
166:        os.makedirs(...)
167:        full_size = expected_bytes          # ← 在 for 循环外，被后续 attempt 复用
168:
174:        for _ in range(1, self.attempts + 1):
...
183:                    if r.status_code == 206:
184:                        m = re.search(r"/(\d+)\s*$", r.headers.get("Content-Range", ""))
185:                        if m:
186:                            full_size = int(m.group(1))   # ← 只在能解析时更新
187:                        with open(part, "ab") as f:        # ← "ab" 追加
...
205:            if full_size is not None:
206:                if size == full_size and size >= min_bytes:
207:                    os.replace(part, dest)
208:                    return True
```

**问题**：`206` 分支在"拿不到 `Content-Range` 总长"时**不重置** `full_size`，而是沿用上一次 attempt
（可能来自 200 的 `Content-Length`）的值。随后只比较 `size == full_size`。
由于 `part` 是**追加**写（`"ab"`），两个不同来源/不同偏移的响应可以拼出恰好等于旧 `full_size` 的字节数。

**复现（`/tmp/review-a/t3b.py`）**

```
B_pollute: 返回=True 大小=9000 首4字节=b'aaaa'
  尝试1：200，Content-Length=9000，写到 5000 字节时断链（ChunkedEncodingError）→ full_size=9000，part=5000B
  尝试2：206，不带 Content-Range，返回 4000 字节 → 追加到 part → size=9000 == 旧 full_size → 判成功
  结果：dest 前 5000 字节来自"被中断的全量响应"，后 4000 字节来自"另一次 Range 响应"，
        长度对得上，内容错位/重复，PDF 可能仍能打开但页面错乱。
```

同一处还有两个次级缺陷：

* `:209-214` `if size > full_size: os.remove(part); full_size = expected_bytes` —— **最后一轮**把残片删掉后
  循环直接结束，白删一次（`/tmp/review-a/t3_fullsize.py` 场景可复现"返回 False 且文件不存在"）；
* `:198-199` `416` 直接把 `completed = True`，`full_size is None` 时任何 `>= min_bytes` 的残片都会被接受
  （`t2_exists.py` 场景3：`返回 True，大小 5000`）。
* `:181` `timeout=min(self.timeout*5, 600)` 同时用作 **connect/read** 超时，即"两个 chunk 间隔 600s 才算超时"，
  连接半死（对端不回 RST）时单次 `download` 可挂 600s × 4 attempts = 40 分钟，期间只能靠 `on_progress`
  心跳维持租约（而 `on_progress` 只在页/卷完成时回调，大 PDF 下载中**根本没有回调** → 见 P1-6）。

**建议修法**

```python
        full_size = expected_bytes
        for attempt in range(1, self.attempts + 1):
            attempt_total = None                 # ← 每轮独立，绝不复用
            ...
                    if r.status_code == 206:
                        m = re.search(r"/(\d+)\s*$", r.headers.get("Content-Range", ""))
                        if not m:
                            # 无法证明总长的 206：丢弃残片、按全量重下
                            try: os.remove(part)
                            except OSError: pass
                            continue
                        attempt_total = int(m.group(1))
                        if os.path.getsize(part) >= attempt_total:   # 已覆盖全量，别追加
                            attempt_total = None; continue
                        with open(part, "ab") as f: pump(f, r)
                    elif r.status_code == 200:
                        clen = r.headers.get("Content-Length", "")
                        attempt_total = int(clen) if clen.isdigit() else None
                        with open(part, "wb") as f: pump(f, r)
                    elif r.status_code == 416:
                        attempt_total = os.path.getsize(part) if os.path.exists(part) else None
                        if attempt_total is None: continue
                    ...
            size = os.path.getsize(part) if os.path.exists(part) else 0
            if attempt_total is not None and size == attempt_total and size >= min_bytes:
                os.replace(part, dest); return True
```

要点：**每轮一个变量**；206 拿不到总长就不许追加；`416` 仅在"残片确实等于远端全长"（可通过 HEAD 或 Range `bytes=-1` 探测）时接受。

---

### P0-3 `attempt` 计数失真：`MAX_ATTEMPTS=5` 实际**无上限重试**，且日志谎报次数

| 项 | 内容 |
|---|---|
| 位置 | `core/pipeline.py:165-166`（读数源错误）；`core/db.py:491,501-502`（`dead` 可被再次认领） |
| 级别 | **P0**（对站点礼貌性/配额/数据语义三重破坏；先给 P0 是因为"对外行为不可控"，若评审认为只算 P1 我接受降级，但修法必须做） |

**代码证据**

```python
# core/pipeline.py:113  row 是 fetch_one 的入参 —— 调用方在 claim 之前取的快照
125:    job, quota_full, claimed = d.claim_and_start(row["id"], quality, quota_n)
...
165:    attempts = row["attempt"] + 1
166:    new_status = "dead" if attempts >= MAX_ATTEMPTS else "failed"

# core/db.py:491  dead 也是"可认领"状态；:501 认领时 attempt+1（无上限）
491:            if not row or row["status"] not in ("queued", "failed", "dead"):
501:            cur = conn.execute(
502:                "UPDATE books SET status='running', attempt=attempt+1 "
```

**必须纠正的一点**：Lead 的假设是"claim 已 `attempt+1`，pipeline 又按旧 row +1 → 每次 +2 → 5 次变 2.5 次"。
**实际不会翻倍**：`row` 是 claim **之前**的快照，而 `claim_and_start` 只加一次 1，
所以 `row["attempt"] + 1` 恰好等于数据库里的当前值——**数值是对的**。真正坏的是：

1. **`dead` 永远不是终点**：`claim_and_start` 把 `dead` 和 `failed` 一起放行（`db.py:491`），
   `reap_stuck` 也会把 `running` 打回 `queued`。所以 `dead` 只是"标签"，下一次 `run_source_heartbeat`
   仍会取到它 —— **等等，取不到**：`queued_books` 只查 `status='queued'`（`db.py:527`），
   所以调度器不会主动重试 `dead`。**但** Web「重试」按钮接受 `dead`（`web/app.py:530` `not in ("failed","dead")`），
   用户点一次就把它送回 `queued`，此后 `claim_and_start` 每次把它 `attempt+1` 且**无上限**。
2. **日志谎报**：`d.log(f"失败({attempts}/{MAX_ATTEMPTS})")` 在 attempt 已是 5 时打印 `失败(5/5)` 但状态仍是 `failed`
   （因为 `row["attempt"]` 只到 4），然后第 5 次才 `dead`——**看起来对，其实是"少算了一次"**；
   而如果在两次失败之间有人点了「重试」把状态重置为 `queued` 而**没**重置 `attempt`，
   则出现 `失败(6/5)`、`失败(25/5)` 这种自相矛盾的日志。
3. **配额被无限消耗**：每次失败都 `INSERT INTO jobs`，而配额账本就是 jobs 的 `started_at`
   （`db.py:494-497`）→ 一本**永远下不下来**的书会持续占用该馆每小时的 10 册配额，
   把同一来源里**能下成功的书**挤出队列。

**复现（`/tmp/review-a/t1b_attempt_api.py`）**

```
A) scheduler/queued 路径：轮次 = 1  status = failed      ← 调度器不会自己重试 failed（见 db.py:524-528）
B) dead 也可重排（api_retry）：轮次 = 25  最终 attempt = 25  ← Web 重试 + fetch 循环 25 次
C) 若按 DB 实际值判定：轮次 = 5（应为 5）
```

> 补充：`tests/test_regressions.py` 第 144-147 行**明确断言**了 `claim_and_start(b1)` 在 `running` 时返回
> `again=False`、不限额仍可认领 `b2`——说明"状态机"本身没被测试覆盖到 `dead→running→max attempts` 这条链。

**建议修法（两步，都很小）**

```python
# 1) db.py:claim_and_start —— 让数据库自己守住上限，并在超过时拒绝认领
        if row["status"] not in ("queued", "failed") and not (
                row["status"] == "dead" and force):        # force 仅"人工重试"入口使用
            return None, False, False
        ...
        cur = conn.execute(
            "UPDATE books SET status='running', attempt=attempt+1 "
            "WHERE id=? AND status IN ('queued','failed') AND attempt < ?",
            (book_id, MAX_ATTEMPTS))                       # 上限下沉到 SQL，跨进程原子
        if not cur.rowcount:
            return None, False, False                      # 已到上限：不再烧配额
        # 返回认领后的真实 attempt（与 job_id 一起），供 pipeline 记录
        attempt_now = conn.execute("SELECT attempt FROM books WHERE id=?",
                                   (book_id,)).fetchone()["attempt"]
        return int(job.lastrowid), False, attempt_now

# 2) pipeline.py:125 / :165 —— 用认领返回值，不用入参 row
    job, quota_full, claimed = d.claim_and_start(row["id"], quality, quota_n)
    if not claimed: ...
    ...
    attempts = claimed_attempt            # 取代 row["attempt"] + 1
    new_status = "dead" if attempts >= MAX_ATTEMPTS else "failed"
```

同时给"人工重试"一个显式的降级路径（`web/app.py:530` / `manage.py:273` 的 `retry`）：
`set_status(book_id, "queued")` 之前应 `attempt=0`（或 `force=True`），否则 `attempt<MAX_ATTEMPTS`
的 SQL 守卫会让人工重试失效——这是**故意**的、可选的语义，需在 README 里写清。

完成后 `README.md:57`「failed(重试5次)」与 `docs/DESIGN.md:182`「重试 ≥5 次转 dead」才与实现一致。

---

## 2. P1 — 重要（功能语义错误 / 可靠性隐患）

### P1-1 确定性错误也被当作"可重试"，配置型失败会一路烧到 dead

| 项 | 内容 |
|---|---|
| 位置 | `sites/loc.py:212-215,227-232`；`core/pipeline.py:164-172` |
| 级别 | **P1** |

```python
# sites/loc.py:212-215
use_pdf = pdf_url and quality in ("auto", "pdf")
if quality == "pdf" and not pdf_url:
    errors.append(f"卷{i + 1}: quality=pdf 但无官方 PDF 链接")
    continue
# sites/loc.py:229-232
    errors.append(f"卷{i + 1}: 无官方PDF直链且缺逐页清单"
                  f"——运行 tools/loc_fill_details.py 补详情后重试")
```

**问题**：这两条错误是**数据缺失**（快照里没有 `pdf_url` / 没有逐页清单），与网络无关，
重试 5 次必然同样失败。`pipeline.fetch_one` 不看错误类型，一律 `failed` 计数 → 烧掉 5 次配额 + 5 个 job 行，
最后 `dead`。本机库现有 1 册 `failed`（`book_id=267`，`卷1: IIIF 组图失败(0页); 卷2: IIIF 组图失败(0页)`）
正是这类"可判定为数据缺失"的样本。

**修法**：`DownloadResult` 增加一个可重试标志，`pipeline` 据此决定是否计数：

```python
# core/models.py
@dataclass
class DownloadResult:
    ok: bool
    outputs: list = field(default_factory=list)
    pages: int = 0
    bytes_done: int = 0
    errors: list = field(default_factory=list)
    retryable: bool = True                 # 新增：False = 确定性失败，直接 dead 不必重试

# sites/loc.py（数据缺失分支）
    errors.append(...); retryable = False
    ...
    return DownloadResult(ok=..., errors=errors, retryable=retryable)

# core/pipeline.py
    new_status = ("dead" if attempts >= MAX_ATTEMPTS
                  else "dead" if not getattr(result, "retryable", True)   # 确定性失败不再重试
                  else "failed")
```

这样 `attempt` 才真正代表"网络重试次数"，`dead` 也才有意义（"不值得再试"）。

---

### P1-2 弱成功语义：部分卷成功即 `done`，缺卷**永不补齐**；`open=True` 的 `0 页` 也能记 done

| 项 | 内容 |
|---|---|
| 位置 | `sites/loc.py:270-275`（`ok=bool(outputs)`）· `sites/bnf.py:232`（同）· `sites/na_jp.py:327`（略强：`bool(outputs) and pages > 0`）· `core/pipeline.py:152-163` |
| 级别 | **P1**（Lead 的 #2 属实；但要说清"是不是真实漏洞"） |

**代码证据**

```python
# sites/base.py:39-40 契约文档
成功语义（各馆统一）: 有产出即 ok=True；缺卷/部分失败记入
result.errors（进 meta.json 与日志），由断点续传补齐。
# sites/loc.py:270-275
# 成功语义（各馆统一）：有产出即 ok；缺卷/部分失败照常记 errors，
# 靠页级断点续传在重跑时补齐，不把整条记录打成 failed 烧配额。
result = DownloadResult(ok=bool(outputs), ...)
# core/pipeline.py:152-160
if result.ok:
    d.finish_job(job, "done", ...)
    d.set_status(row["id"], "done")         # ← errors 被完全忽略
```

**"靠断点续传补齐"是错的论证**：续传的前提是"这本书还会被再跑一次"。
但 `done` 之后它离开 `queued`（`run_source_heartbeat` 只取 `queued`，`db.py:527`），
`reap_stuck` 也只回收 `running`。**没有任何路径会再访问它**——除非用户手工点重试（而 `retry` 只允许 `failed/dead`，`web/app.py:530`）。
所以：**多卷书只要第一卷成功、其余卷失败，缺失卷就是永久缺失，且 UI 显示"已完成"。**

> 交叉验证（本机真实库 42 册 done）：**当前没有多卷书**（`volume_count>1` 的 done = 0），
> 所以今天**尚未**发生实际缺失；但这是"数量还没到"而不是"机制没问题"——
> 一旦开始跑多卷善本（永樂大典、na_jp 分卷），第 2 条多卷书就会踩中。

**另一个更现实的洞**：`ok=True` 但 `pages == 0`（loc/bnf 都可能：`outputs` 非空但 `pages_done` 为 0 时，
`pipeline:153-155` 仍写 `done`），以及 `pipeline.py:156` 无条件写 `cover_path=dest/cover.jpg`
**而不检查文件是否存在**（`loc.py:264-268` 下载封面失败时 `cover_path=""`，但 pipeline 覆盖了它）。

**修法（三处小改，语义立刻自洽）**

```python
# 1) 各馆把"期望卷数"报出来
# core/models.py
    expected_volumes: int = 0      # 新增
    missing: list = field(default_factory=list)   # 新增：缺哪几卷

# sites/loc.py
    result = DownloadResult(ok=bool(outputs) and pages_done > 0,
                            expected_volumes=n_res,
                            missing=[f"卷{i+1}" for i in range(n_res)
                                     if os.path.join(...) not in outputs], ...)

# 2) pipeline 区分"干净完成"与"部分完成"
    if result.ok and not result.missing:
        d.set_status(row["id"], "done")
    elif result.ok:
        # 部分完成：留在队列里，下次心跳继续（页级/卷级断点会自动跳过已完成部分）
        d.set_status(row["id"], "queued",
                     error="部分卷未完成: " + ",".join(result.missing[:5]))
        d.log(f"部分完成 {result.pages} 页，缺卷 {result.missing}；已回队列续补", ...)
    ...

# 3) 封面不要假装存在
    cover = os.path.join(dest, "cover.jpg")
    d.update_download_info(row["id"], cover if os.path.exists(cover) else "",
                           result.pages)
```

注意：`queued` 回退会让"每次都失败的数据缺失卷"循环，所以必须**配合 P1-1 的 `retryable=False`**：
确定性缺卷 → `dead` + 明确错误文案（"缺卷 2/3，需补详情后重试"），而不是无限排队。
两处配合后，状态机才真正表达四种结局：**done / 部分完成待补 / failed 待重试 / dead 需人工**。

---

### P1-3 `set_status` 无状态迁移校验；`decided_at` / `finished_at` 会被反复覆盖

| 项 | 内容 |
|---|---|
| 位置 | `core/db.py:451-465` |
| 级别 | **P1** |

```python
451:    def set_status(self, book_id: int, status: str, error: str = "",
...
455:        if status in ("queued", "ignored"): sets.append("decided_at=?"); ...
458:        if status == "done": sets.append("finished_at=?"); ...
465:            conn.execute(f"UPDATE books SET {','.join(sets)} WHERE id=?", args)
```

**问题**：`set_status` 可以 `done → discovered`、`ignored → running`，把 `finished_at`、`decided_at` 覆盖成本次时间，
且**不校验 `status` 取值是否在枚举内**。它绕开了 `claim_and_start` 的原子认领，是"第二个写状态的入口"。

**复现（`/tmp/review-a/t13_status.py`）**

```
1) set_status 任意跳转： dead （依次 ignored→done→queued→running→discovered→dead 全部成功）
   decided_at/finished_at 被反复覆盖： {... 'decided_at': '...', 'finished_at': '...', 'added_at': '...'}
2) attempt=99 的书仍可被 claim：True，attempt 变为 100
3) finish_job 可覆盖已 done 的 job： failed     ← finish_job 同样不校验 job 状态
```

**修法**

```python
_ALLOWED = {
    "discovered": {"queued", "ignored"},
    "queued":     {"running", "ignored", "discovered"},
    "running":    {"done", "failed", "queued", "dead"},
    "failed":     {"queued", "running", "dead", "ignored"},
    "dead":       {"queued"},                 # 仅人工重试
    "done":       set(),                      # 终态（要重做走 queued 显式通道）
    "ignored":    {"discovered", "queued"},
}
SQL = ("UPDATE books SET status=?,last_error=? "
       "WHERE id=? AND status IN (%s)" % ",".join("?" * len(allowed_from)))

def set_status(self, book_id, status, error="", *, allow_from=None):
    if status not in _ALLOWED:
        raise ValueError(f"未知状态 {status!r}")
    ...
    cur = conn.execute(SQL, (status, error, book_id, *allowed_from))
    if not cur.rowcount:
        raise ConflictError(f"#{book_id} 状态迁移被拒: {current} -> {status}")
```

另外 `finish_job` 应加 `WHERE id=? AND state='running'`，并检查 `rowcount`（见 P1-10 的同一修法）。

---

### P1-4 `upsert_book` 的"合并保护"只覆盖 3 个字段，其余字段被空值**整体覆盖**

| 项 | 内容 |
|---|---|
| 位置 | `core/db.py:325-338`（只保护 `subjects/pdf_urls/files_json`） |
| 级别 | **P1**（元数据丢失；Lead 的 #6 属实） |

**复现（`/tmp/review-a/t7_upsert.py`）**：先导入完整元数据，再导入一份只有 `source_uid` 的紧凑快照：

```
  title          = ''          ← 丢失
  alt_title      = ''          ← 中文原题丢失（书名链接/PDF 文件名都会变成空）
  author         = ''          ← 丢失
  era            = ''          ← 丢失（筛选"明/清"会把这本书筛掉）
  year_start     = None
  language       = ''   item_url = ''   cover_url = ''
  collection     = ''          ← 丢失（落盘目录从 yongle-da-dian 变成 misc！）
  volume_count   = 0   page_count = 0
  rights         = ''   shelf_id = ''
  raw_json       = '{}'        ← 原始留档丢失
  subjects       = '["類書"]'  ← 唯一被保住的
  pdf_urls       = '["https://x/a.pdf"]'
  files_json     = '[[{"url": "u"}]]'
```

**最严重的是 `collection`**：`collection` 参与落盘路径（`pipeline._dest_dir` 的 `collection or "misc"`）。
一旦某次导入把 `collection` 清空，后续 `doctor` 对账、`book_urls`、`create_title_links` 全都会去 `misc/` 下找——
**已有的 PDF 目录瞬间变成"孤儿"**，而书被当作新路径重下（浪费配额）或被判缺失。

**修法**：把"空值不覆盖"从白名单改成**通用规则**（空字符串/`[]`/`{}`/`0`/`None` 不覆盖已有非空值），
只对"确实需要清零"的字段开显式 override：

```python
_NULLISH = ("", "[]", "{}", "null")

def _merge(cols, row):
    src = cols
    for k, v in list(src.items()):
        if isinstance(v, str) and v.strip(" \t\r\n") in _NULLISH:
            cur = row[k]
            if cur is not None and str(cur).strip() not in _NULLISH:
                src[k] = cur          # 空值 → 保留库里已有的
    return src
```

注意 `SELECT` 需要把全部列取出来（现在只取了 3 个，`db.py:326-329`），成本可忽略（单行）。

---

### P1-5 `_immediate` 的 `ROLLBACK` 二次异常会**覆盖真正的错误**；事务内做 `os.kill` 拉长写锁

| 项 | 内容 |
|---|---|
| 位置 | `core/db.py:147-155`（`except Exception: conn.execute("ROLLBACK")`）· `core/db.py:574-595`（`reap_stuck` 内 `os.kill`） |
| 级别 | **P1**（Lead 的 #5 前半属实，后半影响有限） |

**复现（`/tmp/review-a/t5_rollback.py`）**

```
BEGIN 失败: OperationalError database is locked
→ except 分支里的 ROLLBACK 抛二次异常: OperationalError cannot rollback - no transaction is active
  (原始异常会被这个二次异常覆盖，_immediate 的 caller 看不到 'database is locked')
```

**影响**：`scheduler.py:121` `except sqlite3.Error`、`web` 的异常日志都会显示 `cannot rollback - no transaction is active`，
运维按这个错误根本查不到真因（锁竞争）。**目前被 `timeout=30` 掩盖了**——需要 30s 内持续有写锁才会触发，
但 `reap_stuck` 恰恰在写锁内做 `os.kill`（见下），QNAP 上 CPU 弱、进程多时并非不可能。

**修法**

```python
    def _immediate(self):
        conn = self.connect()
        conn.isolation_level = None
        began = False
        try:
            conn.execute("BEGIN IMMEDIATE"); began = True
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if began:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass      # 绝不覆盖原始异常
            raise
        finally:
            conn.close()
```

**关于 `reap_stuck` 在事务内 `os.kill`**：写锁持有时间 = `N_running × os.kill` + 2×UPDATE。
当前 `N_running ≤ 每源 1 本`（`run_source_heartbeat` 串行），所以**实际可忽略**；
但可以零成本消除：把判活挪到事务外（先快照 `(id,pid,last_beat)`，再在事务内复查 `pid/last_beat` 未变）。

```python
    def reap_stuck(self, stale_minutes=60):
        cutoff = ...
        with self.connect() as conn:                     # 只读快照，不持写锁
            candidates = conn.execute(
                "SELECT id, book_id, pid, last_beat FROM (...)", ()).fetchall()
        dead = [j["id"] for j in candidates
                if not self._pid_alive(j["pid"]) or j["last_beat"] < cutoff]   # os.kill 在锁外
        if not dead: return 0
        with self._immediate() as conn:                  # 事务内只做 UPDATE
            ...
```

---

### P1-6 租约语义偏弱：容器重启后最长 60 分钟停摆；`on_progress` 大文件下载期间根本没有心跳

| 项 | 内容 |
|---|---|
| 位置 | `core/db.py:549-560`（`_pid_alive`）· `core/pipeline.py:136-149`（心跳节流 30s 但只在回调时触发）· `scheduler.py:117`（启动回收与心跳同参 `stale_minutes=60`） |
| 级别 | **P1** |

1. **`_pid_alive(pid<=0) → True`，`PermissionError → True`**：都是"宁可误判活"。
   但容器重建（`docker compose down/up`）后，`jobs.pid` 是**旧容器命名空间内的 pid**，
   在新容器里可能恰好对应一个活进程（`t11_pid.py`：`_pid_alive(1)=True`），于是"进程已死"的快速路径失效，
   只能等 `stale_minutes=60` → **该来源的下一次心跳被卡住最长 60 分钟**。
2. **`os.getpid()` 在 Web 里记录的是 `uvicorn` 主进程 pid，而下载跑在 `threading.Thread`（`web/app.py:514`）**：
   只要 uvicorn 活着，`_pid_alive` 恒为 True —— 线程崩溃/挂死时只能靠心跳超时。
3. **心跳只在 `progress.tick()` 时刷新**（`pipeline.py:138-145`）
   → 官方 PDF 直链下载（几百 MB）**全程零回调**，一次请求最长可占 600s（`http.py:181`），
   超过 `stale_minutes=60`? 不会，但**超过 30 分钟**时 `on_progress` 未触发 → 心跳不续 →
   如果下载恰好跨过 60 分钟（慢速 NAS 网络 + 大文件 + 4 attempts 重试）就会被 `reap_stuck` 判死，
   而此时**原下载还在跑** → 双写同一 `dest`/`.part`（`loc.py:204-206` 注释里已经踩过"串页"的坑）。

**修法**

```python
# pipeline.py：把心跳从"回调驱动"改成"时间驱动"（一个轻量后台线程，零依赖）
def _heartbeat_loop(stop: threading.Event, d, job, every=30):
    while not stop.wait(every):
        try: d.job_heartbeat(job)
        except Exception: pass

stop = threading.Event()
threading.Thread(target=_heartbeat_loop, args=(stop, d, job), daemon=True).start()
try:
    result = adapter.download_item(...)
finally:
    stop.set()          # 无论成功失败都必须停，否则僵尸线程续约已结束的 job

# scheduler.py：启动时用更短的 stale（容器重启后残留 job 一定不再有持有者）
d.reap_stuck(stale_minutes=5)      # 在 :117
```

另一个选择（更彻底）：给 `jobs` 加 `owner`（容器/进程启动 UUID），`reap_stuck` 先按 `owner != 本进程 owner` 判定，
彻底摆脱 pid 复用的语义模糊。

---

### P1-7 `claim_and_start` 之后、`adapter.download_item` 之外的异常会留下**无界** `running`

| 项 | 内容 |
|---|---|
| 位置 | `core/pipeline.py:131-134`（`get_adapter` / `row_to_meta` / `d.log` 都在 `try` 之外） |
| 级别 | **P1** |

```python
131:    adapter = get_adapter(src, HttpClient())     # ← 未注册源 → KeyError；此时代理已在 :125 认领
132:    http = adapter.http if hasattr(adapter, "http") else HttpClient()
133:    meta = row_to_meta(row)                       # ← 坏 JSON → json.JSONDecodeError
134:    d.log(...)                                    # ← 写库异常
147:    try:
148:        result = adapter.download_item(...)       # ← 只有这里被 try 包住
```

`claim_and_start` 已经把 `status='running'` 且建了 job 行；`get_adapter` 抛 `KeyError`（例如 `sources` 表里的
`ndl_jp` 未注册，见审查 C）时，异常穿出 `fetch_one` → `scheduler._source_round` 被 `scheduler.py:140` 兜住并记日志，
但这本书**永远停在 `running`**（job 行 `running` 且 pid 就是当前活进程 → `_pid_alive=True`），
只能等 60 分钟超时回收，之后又**再次**踩同一个 `KeyError` → 无限循环，每轮烧一次配额。

**修法**：把"认领之后的全部工作"纳入统一的 `try/finally`，失败即使没有 `result` 也要收尾：

```python
    job, quota_full, claimed = d.claim_and_start(...)
    if not claimed: ...
    try:
        adapter = get_adapter(src, HttpClient())
        ...
        result = adapter.download_item(...)
    except Exception as e:
        result = DownloadResult(ok=False, errors=[f"下载器异常: {e!r}"], retryable=False)
        # retryable=False：适配器缺失/元数据坏是确定性问题，直接 dead，不要每轮重烧配额
    finally:
        ...  # 确保 job 一定被 finish（见下）
```

---

### P1-8 路径校验三处重复实现，入口覆盖**不完备**（doctor / create_title_links / sidebar）

| 项 | 内容 |
|---|---|
| 位置 | `core/db.py:288-299`（`_check_path_segments`）· `core/pipeline.py:40-58`（`_dest_dir`）· `core/doctor.py:42-43,115-116`（**手拼路径**） |
| 级别 | **P1**（Lead 的 #7：`importer → upsert_book` 与 `fetch_one/create_title_links → _dest_dir` 两条主路**已覆盖**；缺口在 doctor 与 Web 静态挂载） |

**已覆盖的**（正面结论，不必重复造轮子）：

* `core/importer.py:28,90` 走 `upsert_book` → `_check_path_segments` ✅
* `pipeline.create_title_links:76`、`fetch_one:117`、Web `book_urls`（`web/app.py:63`）走 `_dest_dir` ✅

**未覆盖的**：

```python
# core/doctor.py:42-43  直接拼路径，不做任何校验
        leaf = os.path.join(books_dir, r["source_id"],
                            r["collection"] or "misc", r["source_uid"])
```

- doctor 的对账/孤儿/深抽检三处（`doctor.py:42,115`）用**手拼**路径。如果历史库里存在非法段，
  doctor 会去 `books_dir/../../etc/...` 找文件 → `os.path.isfile` 只读，风险有限，但**"孤儿目录"检查
  （`doctor.py:60-67`）用 `os.walk` 反推 `src/col/uid` 只取三层**，遇到多一层/少一层的目录会误报或漏报；
- `web/app.py:42` `app.mount("/data", StaticFiles(directory=BOOKS_DIR))` —— 只要 FS 上有符号链接指向外部，
  `StaticFiles` 会**跟随软链**把内容吐出去（Starlette 只在 `follow_symlink=False` 时拒绝，默认 True）；
- `_dest_dir` 的 `realpath` 校验（`pipeline.py:55-57`）能挡住"目标目录是软链"的情况
  （`/tmp/review-a/t6_path.py` 实测：`拒绝: 路径逃逸数据目录`），这点做得对，**但仅限 `_dest_dir` 调用方**。

**修法**：把 `_dest_dir` 提升为 `core/paths.py` 的**唯一实现**，`doctor` 与 `web.book_urls` 都改用；
`db._check_path_segments` 删掉，`upsert_book` 里 `from core.paths import validated_dir` 复用（避免循环导入，
`_dest_dir` 本身就只依赖 `BOOKS_DIR`，把 `BOOKS_DIR` 抽成 `core/paths.py:books_dir()` 即可）：

```python
# core/paths.py（新文件，唯一真源）
def illegal_segment(v: str) -> bool:
    return (not v) or v in (".", "..") or "/" in v or "\\" in v or "\x00" in v

def dest_dir(source_id: str, collection: str, uid: str) -> str:
    ...   # 原 _dest_dir 实现，含 realpath 逃逸检查
```

顺带把真实存在的两段重复删掉：`db.py:294-295` 与 `pipeline.py:46-47` 的 `_illegal` 是**逐字重复**。

**补充（Windows 兼容性，P2 但值得一提）**：`t6_path.py` 实测 `C:` / `C:x` / `a:b` / `.hidden` / `NUL` 这类段
**全部放行**。在当前 Linux 容器部署下无害，但如果有人在 Windows 上跑（README 提到本机工具），
`os.path.join` 会把它当盘符/保留名，`_sanitize_filename` 也不会拦 `CON`/`aux`（实测 `len('CON')=3` 原样保留）。
建议在 `illegal_segment` 里加：`":" in v or v.strip(" .") != v or v.upper().split(".")[0] in _WIN_RESERVED`。

---

### P1-9 节流是"最小间隔"≠"串行"：同域并发无上限，跨进程 flock 也只保护取号

| 项 | 内容 |
|---|---|
| 位置 | `core/http.py:41-54`（`DomainThrottle.wait`）· `:82-105`（`FileThrottle.wait`）· `:175`（`download` 只在发起前 wait 一次） |
| 级别 | **P1**（礼貌性/被封禁风险，而非正确性） |

`wait()` 记录的是"请求**发出前**的时刻"（`:52` `self._last[domain] = now`），
一个 40s 的大文件下载期间 `_last` 早已过期 → 别的线程/进程 1.5s 后就能再发。
`FileThrottle` 的 `flock` 只包住 `:89-104` 的读时刻/写时刻，**请求在锁外**。
所以文档 `http.py:70`「单靠进程内节流会把实际间隔减半。文件锁保证跨进程串行取号」是准确的（**取号**），
但 `scheduler.py:7`「每域串行」与 `sites/na_jp.py:150`「站点对连续约 58 次请求临时封禁」隐含的
"同时只有一个在途请求"假设**并不成立**：

* Web「立即下载」+ scheduler 同源时，同域可有 2 个在途请求；
* `_download_official` 的 `_post_chunk` 是**同步阻塞** POST（`na_jp.py:464`），期间 scheduler 的心跳照样会拿号发请求。

**修法**：给 `FileThrottle` 增加"在途许可"语义（保持最小间隔不变）：

```python
class DomainGate:
    """在途请求数 = 1 的跨进程闸门（flock 覆盖整个请求体）。"""
    def __enter__(self):
        self._fh = open(self._lock_path(), "a+")
        fcntl.flock(self._fh, fcntl.LOCK_EX)      # 阻塞直到前一个请求结束
        return self
    def __exit__(self, *a):
        fcntl.flock(self._fh, fcntl.LOCK_UN); self._fh.close()
# 用法：with gate(url): wait(url); r = session.get(...)
```

---

### P1-10 `finish_job` 与 `set_status` 不在同一事务；job 的 `pages_total` 语义错位

| 项 | 内容 |
|---|---|
| 位置 | `core/pipeline.py:152-172`（四次独立写）· `core/db.py:539-546` |
| 级别 | **P1**（崩溃窗口小，但状态不一致会永久留档，且 doctor 无法发现） |

```python
152:    if result.ok:
153:        d.finish_job(job, "done", ...)            # 写 #1
156:        d.update_download_info(...)               # 写 #2
159:            _title_link(...)                      # 写文件系统
160:        d.set_status(row["id"], "done")           # 写 #3
```

进程在 #1 与 #3 之间被 `docker stop`（SIGTERM 默认没有优雅退出处理，`scheduler.py:157` 只处理 `KeyboardInterrupt`）：
→ `jobs.state='done'` 但 `books.status='running'` → `reap_stuck` 60 分钟后把它打回 `queued` 并**重新下载整本**（配额再烧一次）；
更糟的是 #2 已把 `page_count` 改成新值而 `status` 还是 running。

另外 `finish_job` 的 `pages_total`（`pipeline.py:154,168` `max(meta.page_count, result.pages)`）与
`jobs.pages_total` 的直觉含义不符（"本次应下多少页"），`pages_done` 又来自 `result.pages`——
两者单位在 IIIF 兜底与官方 PDF 两条路径下并不一致（`loc.py:226` 用 `os.path.getsize` 累加 `bytes_done`，
`na_jp` 的 `bytes_done` 是 `sum(getsize(p) for p in outputs)`，重复计数风险见 P2-5）。

**修法**：把"完成"收敛到一个事务方法：

```python
# core/db.py
def finish_book(self, book_id, job_id, state, pages, total, bytes_done, outputs, error=""):
    with self._lock, self._immediate() as conn:
        conn.execute("UPDATE jobs SET state=?,pages_done=?,pages_total=?,bytes_done=?,"
                     "outputs=?,last_error=?,finished_at=? WHERE id=? AND state='running'",
                     (state, pages, total, bytes_done, json.dumps(outputs, ensure_ascii=False),
                      error, utcnow(), job_id))
        conn.execute("UPDATE books SET status=?,attempt=attempt WHERE id=? ...", (...))
```

---

## 3. P2 — 优化（性能 / 可维护性 / 一致性）

| 级别 | 位置 | 问题 | 影响 | 建议 |
|---|---|---|---|---|
| P2 | `core/db.py:134-139` | 每个方法新建连接，每次跑 `PRAGMA journal_mode=WAL` + `foreign_keys=ON` | **实测可接受**：`connect()` 含 2 次 PRAGMA 平均 **0.31ms**；`save_progress` 全流程 **0.3ms**；4 进程并发写 1600 行 **0.84s** 零失败（`t4b.py`）。真正开销在"每请求 2-4 次 connect"（`common_ctx` 里 `count_by_status` + `sources` 各一次） | 给读方法加 `PRAGMA journal_mode` 跳过的连接（`connect(light=True)`），或 `threading.local` 复用连接；WAL PRAGMA 只需在 `init()` 里设一次（它是持久的） |
| P2 | `core/db.py:418-439` | `facets()` 全表读 `subjects` JSON 并在 Python 里解析；Web 侧 60s 缓存是**模块级 dict**（`web/app.py:83`），多 worker 各一份 | **实测 5000 行 = 15ms**（`t9_facets.py`），当前 2128 行 <10ms，**不构成瓶颈**；但 100k 行时会到 ~300ms，且每个 uvicorn worker 各付一次 | 加 `subjects` 倒排表（`book_tags(book_id, tag)`），`facets` 变 `SELECT tag, COUNT(*) ... GROUP BY tag`；短期至少把缓存改成按 `MAX(id)` 失效而不是纯时间 |
| P2 | `core/db.py:222,230-235` | `reading_daily.day = utcnow()[:10]`（UTC 日），`week_minutes` 用 `date('now','-6 days')`（UTC 窗口） | 东八区用户 00:00–08:00 的阅读被记到"昨天"，"本周"在周日 16:00（UTC）后就会跳变 → 用户看到周读数突然归零/少一天 | `day` 用 `date('now','+8 hours')` 或存 tz 偏移；SQL 一并 `+8 hours`（`t8_week.py` 已演示 UTC/CST 日键） |
| P2 | `core/db.py:526-528,494-497` | `jobs` 表**没有索引**；`reap_stuck` 的 `WHERE state='running'` 与配额计数 `JOIN ... WHERE started_at>=` 都是 **`SCAN TABLE jobs`**（`t15_deadlock.py` 的 QP 输出） | jobs 行数随时间单调增长（每册每次尝试一行，永不清理）→ 每次认领都要全表扫，认领在写事务内 | `CREATE INDEX idx_jobs_state ON jobs(state)` + `idx_jobs_started(started_at)`；参考 `events` 的裁剪策略给 jobs 加保留策略 |
| P2 | `core/db.py:467-474,531-537` | `claim_book()` 与 `start_job()` **零调用**（`grep` 确认） | 死代码，且 `start_job` 会建"running job 但不改 books 状态"，是**危险的半成品 API** | 删除；或标 `@deprecated` 并在 docstring 写明"仅测试用" |
| P2 | `core/db.py:429-435,234-235` | `except Exception: pass`（facets 解析 JSON）、`core/limiter.py:20-24` `except Exception: pass`（进度回调）、`core/http.py:147-148,202-203` 全吞 `RequestException`、`core/image.py`… | 出问题时完全无痕；`http.download` 吞掉异常只留 `False`，上层只能看到"下载失败" | 至少 `logging.getLogger(__name__).debug(...)`；`download` 返回 `(bool, reason)` 或用 `errors` 回传 |
| P2 | `core/http.py:167` / `pipeline.py:161` | `full_size` 命名与注释（"响应未给总长时只有 completed 才接受"）在 `206` 分支实际不成立（见 P0-2） | 注释说谎比没注释更糟 | 注释改成实际语义或按 P0-2 修好后保持注释 |
| P2 | `core/pdfbuild.py:63,117-123` | xref 用 `%010d` 10 位固定宽度 + 无 `/Info`、无 `/ID`、无压缩 | 合规性 OK（`pypdf` 能读，回归实测 6 页/4 页），但大量文件时体积无优势；`max_obj = 2 + 3n` 与 `3n+2` 的偏移在 n=0 时靠 `raise ValueError("no images")` 兜住，逻辑正确 | 可选：加 `/Producer`；`pdf_page_count` 依赖 pypdf（`requirements.txt` 已声明），但 `build_pdf` 是"纯标准库"卖点，二者并存需在 docstring 写清 |
| P2 | `core/doctor.py:114` | `for r in rows[-verify_n:]` —— `list_books` 按 `id` **升序**，所以取的是**最旧** N 册，与 `manage.py:175`「最近 N 册」的说法相反 | 深抽检永远查不到最新下载（最容易坏的那批） | `ORDER BY id DESC LIMIT ?`，或用 `rows[-verify_n:]` + `list_books(... order="desc")` |
| P2 | `core/doctor.py:42-43,60-67` | 对账只看"目录里有 .pdf"，不校验页数/完整性；`/api/doctor` 走 `verify_n=0`（`web/app.py:734`） | **这是 P0-1 得以长期潜伏的原因**：doctor 报 ✅ 而文件已坏 | 对账时至少 `pdfbuild.looks_complete_pdf()`；Web 版开放 `verify=1`（最近 20 册）或按需后台跑 |
| P2 | `core/mounts.py:26` | `parts[4] == "/data"` 硬编码挂载点；`migration_commands` 硬编码 `/share/Container/eightnations/data` 与 GitHub 仓库 URL | QNAP 上路径不同（`/share/CACHEDEV1_DATA/...`）时误判为 unknown；指令库里出现个人仓库地址 | 从 `EIGHTNATIONS_DATA` 反推挂载点；路径改成从 `data_mount()["source"]` 渲染 |

---

## 4. 逐条回应 Lead 的"已知可疑点"

| # | Lead 的假设 | 核实结果 | 依据 |
|---|---|---|---|
| 1 | `attempt` 每次 +2，5 次变 2.5 次 | ❌ **机制不成立**（`row` 是 claim 前快照，`+1` 恰好等于 DB 当前值）；✅ **但有更坏的后果**：`dead` 可被无限认领，`attempt` 无上限，实测可跑 25 次；日志会打出 `失败(25/5)` | P0-3、`t1b_attempt_api.py` |
| 2 | `ok=bool(outputs)` 弱成功 → 缺卷永不补齐 | ✅ **属实**。"靠续传补齐"的前提（书会再被跑一次）不成立：`done` 离开 `queued` 后无任何自动路径回头。b**但要修的是"部分完成应回队列 + 确定性缺卷应 dead"两件事配合**，否则会变成无限排队 | P1-2 |
| 3 | skip-if-exists 会把半截 >100KB 文件误判成功 | ✅ **属实且已在真实数据留下痕迹**（`jobs.last_error='卷1: PDF 校验失败 Stream has ended unexpectedly'`）。零请求直接 True，实测复现 | P0-1、`t2_exists.py` |
| 4 | `full_size` 跨 attempt 泄漏 | ✅ **属实**，且能拼出"长度对、内容错"的文件（不只是状态污染）；另发现最后一轮 `size>full_size` 白删残片 | P0-2、`t3b.py` |
| 5 | `_immediate` 的 ROLLBACK 二次异常；`reap_stuck` 事务内 `os.kill` 长持锁 | 前半 ✅ 属实（实测 `cannot rollback - no transaction is active` 覆盖真因）；后半 ⚠️ **影响有限**（running 行数 ≤ 源数），但可零成本消除 | P1-5 |
| 6 | `upsert_book` 只保护 3 个字段 | ✅ **属实**，且最危险的是 `collection` 被清空 → 落盘目录漂移、doctor 对账失准 | P1-4、`t7_upsert.py` |
| 7 | `_check_path_segments` / `_dest_dir` 两处重复，是否有入口绕过 | 重复 ✅ 属实（`_illegal` 逐字重复）。绕过：`importer`/`create_title_links` **已覆盖**；**doctor 手拼路径未校验**。另实测 Windows 盘符/保留名/冒号全部放行（Linux 部署下无害） | P1-8、`t6_path.py` |
| 8 | `facets()` 全表解析，缓存多 worker 各一份 | ✅ 属实，但**性能影响被高估**：实测 5000 行 15ms，当前库 2128 行 | P2 |
| 9 | UTC 日界导致"本周阅读"偏差 | ✅ 属实（东八区 00:00–08:00 记到前一天） | P2、`t8_week.py` |
| 10 | 每方法一个连接 + 每次 `PRAGMA journal_mode=WAL` | ⚠️ **实测影响很小**：`connect()` 0.31ms、写 0.3ms、4 进程 1600 写 0.84s 零失败 → **不是性能瓶颈**，列为 P2 而非 P1 | P2、`t4b.py` |

---

## 5. 框架/分层评价（对应任务第 1 条）

**做得好的地方**（应保留，Don't refactor away）：

1. **配额账本下沉到 `jobs.started_at` + `BEGIN IMMEDIATE`**：跨进程共享、重启不清零、认领与扣配额原子——这是一个**正确的关键决策**，比进程内计数器高一个数量级。`tests/test_regressions.py:130-149` 覆盖到位。
2. **租约模型替代"无条件 running→queued"**：方向正确，且 `reap_stuck` 在**同一事务内关闭旧 job 行**（`db.py:583-593`）这个细节做对了，否则会出现"永久 running 的旧 job"反复误杀新持有者。
3. **`pdfbuild` 纯标准库 + `.part` 原子 rename**（`pdfbuild.py:64-73`）：测试覆盖了异常不留半成品，是 core 里质量最高的模块。
4. **`DownloadResult` / `BookMeta` 契约集中**（`models.py` 38 行）：适配器边界清晰，新增一馆不需要碰 core 逻辑（真正的问题在 source 注册与 `get_adapter`，属审查 C 范围）。
5. **`scheduler.py` 逐源隔离 + `reap_stuck` 不在 `try` 内**：单馆异常不拖垮回收，是对的。

**结构性问题**：

| 问题 | 证据 | 建议 |
|---|---|---|
| **状态写入有两个入口**：`claim_and_start`（事务内）与 `set_status`（裸 UPDATE） | `db.py:451` vs `db.py:476` | 让 `set_status` 内部走迁移表（P1-3），或把它降级为 `_set_status_unchecked` 只给管理命令用 |
| **"成功"的判定分散在三个适配器**，而 core 只认 `ok`/`pages` | `loc.py:273`、`bnf.py:232`、`na_jp.py:327` 三种写法 | `DownloadResult` 扩 `missing`/`retryable`/`expected_volumes`，core 只读这几个字段 |
| **路径安全有 3 份实现** | `db.py:288`、`pipeline.py:40`、`doctor.py:42` | 抽 `core/paths.py`（P1-8） |
| **`DB` 同时是"连接工厂 + 仓储 + 事务管理器 + 状态机"**（602 行） | `db.py` 全文 | 拆分：`db/conn.py`（连接/PRAGMA/`_immediate`）、`db/books.py`、`db/jobs.py`、`db/reading.py`。**但不必现在拆**——先修 P0/P1，拆分留到下次重构 |
| **`core/limiter.py` 名不副实**：只剩一个 `Progress` 回调（原限速已迁走） | `limiter.py:1-24` | 改名 `core/progress.py`，或直接并进 `models.py`（24 行的一个类不值得一个模块） |
| **`core/importer.py` 依赖具体适配器** | `importer.py:16,54` `from sites.loc import LocAdapter` | 违反"core 不依赖 sites"的方向（`sites` 也依赖 `core`）→ 存在**双向依赖**。`_merge_resources` 应上移到 `BookMeta` 或 `SourceAdapter` 协议 |
| `db.py:158` `init()` 用 `with self._lock, self.connect()` —— `with` 退出**不 commit**？ | 见下 | 需注意 |

> ⚠️ 关于 `db.py:158`：`sqlite3.Connection.__exit__` 的行为是 **commit on success**（PEP 249 / CPython 实现），
> 所以 `with self.connect() as conn:` 在正常路径会提交、异常路径回滚——**这是对的**，
> 但它在 `close()` 之外隐含一次 `commit`，与 `_immediate` 的显式事务语义不同源，容易误读。
> 建议在 `db.py` 顶部注释里写一行"`with self.connect()` = 自动 commit/rollback；`_immediate` = 显式事务"，
> 避免后续维护者把两者混用（`scheduler.py:153-155` 的 DELETE 就依赖这个隐式 commit）。

---

## 6. 实际验证记录（复现脚本：`/tmp/review-a/`）

| 脚本 | 验证内容 | 结果 |
|---|---|---|
| `t1_attempt.py` / `t1b_attempt_api.py` | attempt 自增语义与上限 | 11 轮 / 25 轮无上限；反证：按 DB 值判定 = 5 轮 |
| `t2_exists.py` | skip-if-exists 信任半成品；206 无 total；416 接受残片 | 全部 `return True`，零 HTTP 请求 |
| `t3_fullsize.py` / `t3b.py` | full_size 跨 attempt 泄漏 | 拼出 9000 字节、首字节来自另一响应的文件判成功 |
| `t4b.py` | 4 进程 × 400 次并发写 | 1600/1600 成功，0.84s，零 `database is locked` |
| `t5_rollback.py` | `_immediate` 二次异常 | `cannot rollback - no transaction is active` 覆盖真因 |
| `t6_path.py` | 路径段校验边界 + 软链逃逸 | 软链逃逸被挡 ✅；`C:`/`CON`/冒号/全角空格放行 ⚠️ |
| `t7_upsert.py` | upsert 字段覆盖 | 16 个字段中 13 个被空值覆盖（含 `collection`） |
| `t8_week.py` | UTC 日键 | 日键以 UTC 为准，东八区 0–8 点错记 |
| `t9_facets.py` | facets 5000 行成本 | 15ms（非瓶颈） |
| `t10_quota.py` | 配额窗口边界 | `started_at>=cutoff` 语义正确；失败的 job 也占配额 |
| `t11_pid.py` | `_pid_alive` 语义 | `pid<=0`→True、`pid=1`→True（容器重启误判活） |
| `t12_orphan.py` | 部分成功语义 / MediaBox | `ok=True` 且 `outputs=1/3`、`errors=2`；MediaBox 793×1190 正确 |
| `t13_status.py` | 状态迁移无校验 | 任意跳转成功；`attempt=99` 仍可认领；`finish_job` 可覆盖 done |
| `t14_http.py` | 节流/HttpClient 生命周期 | 节流是"最小间隔"语义；每次 `fetch_one` 新建 2 个 Session |
| `t15_deadlock.py` | 锁竞争 + 查询计划 | 无死锁；`jobs` 两处全表扫 |
| `tests/test_regressions.py` | 基线 | 13 组全过（`EXIT=0`） |

**对真实库的只读交叉验证**（`data/db/library.db`，2128 册）：
`done 42 / failed 1 / discovered 2085`；`jobs done 42 / failed 2`；**无多卷 done 书**（故 P1-2 尚未造成实际缺失）；
`books.attempt` 最大 <5；`jobs.pages_done` 与 `books.page_count` 42/42 一致 → **当前生产数据是健康的**，
所有 P0 都是"下一次遇到就会坏"的潜在缺陷，不是已发生的损坏。

---

## 7. 模块健康度评分

| 维度 | 分数 | 说明 |
|---|---|---|
| 分层与模块边界 | **7 / 10** | 契约（models）清晰、适配器隔离好；但 `core → sites` 反向依赖（`importer`）、`limiter` 名不副实、`db.py` 职责过载 |
| 数据一致性 / 状态机 | **5 / 10** | 原子认领与租约是亮点；但 `set_status` 无守卫、`dead` 非终态、完成写不原子、弱成功 |
| 下载完整性 | **4 / 10** | Range/断点框架在，但 skip-if-exists 与 `full_size` 两个洞直接威胁"归档文件正确性"，且 doctor 查不出来 |
| 并发 / 跨进程 | **7 / 10** | `BEGIN IMMEDIATE` + jobs 账本用对了；`_lock` 是实例锁但实际不承担跨进程职责（依赖 SQLite 自身锁），注释已说明；pid 语义有模糊地带 |
| 安全（路径/输入） | **7 / 10** | 主路径校验完备且测试覆盖；doctor 手拼路径、Windows 保留名、静态挂载跟随软链是缺口（内网部署下风险低） |
| 可观测性 / 自检 | **5 / 10** | `meta.json` 台账 + `doctor --verify` 设计好；但 Web 版 `verify_n=0`、doctor 只数文件、`except: pass` 多处 |
| 性能 | **8 / 10** | 实测全部达标（连接 0.3ms、facets 15ms/5k、并发写零失败）；jobs 缺索引是唯一真实隐患 |
| 代码质量 / 可维护性 | **6 / 10** | 中文注释质量高、关键决策有"为什么"；但 `_illegal` 重复、死代码、注释与实现不一致（成功语义/串行） |
| 测试覆盖（core 部分） | **6 / 10** | 13 组覆盖了配额/租约/路径/下载完整性/PDF 原子性——比多数项目好；但**恰好没覆盖** P0-1/2/3 这三个洞，且是脚本式断言（失败即中断，无选择性运行） |
| **综合** | **6.1 / 10** | 架构判断（账本配额、租约、原子 rename）明显高于平均水平；**短板集中在"成功/失败判定的证据链"**——下载层相信体积、pipeline 相信 `ok`、doctor 相信目录里有文件，三层都是"弱证据" |

---

## 8. Top 5 必须改

| # | 事项 | 位置 | 预估工作量 |
|---|---|---|---|
| **1** | **给"下载成功"建立证据链**：`download()` 只在"长度可证"时才跳过/提交（sidecar `.size` + `looks_complete_pdf`），并禁止 206 无总长时追加 | `core/http.py:163-219`、`sites/loc.py:236` | 0.5 天 |
| **2** | **`full_size` 每轮独立 + 206 无 `Content-Range` 即丢弃残片 + 416 需探测真实总长** | `core/http.py:167,183-219` | 0.5 天（与 #1 同一 PR） |
| **3** | **attempt 上限下沉到 SQL，`dead` 成为真正的终态**（`WHERE attempt < MAX`），pipeline 用认领返回的真实 attempt；人工重试显式重置 | `core/db.py:476-510`、`core/pipeline.py:165-166`、`web/app.py:530` | 0.5 天 |
| **4** | **成功语义三分**：`done`（齐全）/ `queued`（部分完成，回队续补）/ `dead`（确定性失败需人工），配套 `DownloadResult.missing/retryable` | `core/models.py`、`sites/loc.py:270-275`、`sites/bnf.py:232`、`sites/na_jp.py:327`、`core/pipeline.py:152-172` | 1 天 |
| **5** | **`set_status` 加迁移白名单 + `finish_job` 加 `state='running'` 守卫 + 完成写收敛为一个事务** | `core/db.py:451-465,539-546`、新增 `finish_book` | 0.5 天 |

> 附带一条"零成本、建议同批做"的：`doctor` 对账加 PDF 完整性检查、Web `/api/doctor` 打开 `verify`（至少最近 20 册）——
> 这是发现 #1/#2/#4 造成的坏文件的**唯一自动手段**（`core/doctor.py:42-56,114`、`web/app.py:734`）。

---

*报告完。所有结论均附代码行号与复现路径；仓库内除本文件外未做任何修改（`git status` 干净，除本报告外无变更）。*
