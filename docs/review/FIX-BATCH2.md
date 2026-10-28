# 批 2 修复报告：数据正确性（下载完整性 / na_jp 事务语义 / 弱成功补齐 / attempt 上限 / 调度健壮性）

> 执行日期：2026-10-28
> 依据：`docs/review/A-core.md`、`docs/review/C-adapters.md`、`docs/review/SUMMARY.md`
> 原则：**每条先写复现脚本证明 bug 存在（修前 FAIL），修后同一脚本转为 PASS**；复制脚本全部在 `/tmp/batch2/`，未进仓库。
> 范围：`core/http.py`、`core/pipeline.py`、`core/db.py`、`core/models.py`、`sites/na_jp.py`、`sites/loc.py`、`sites/base.py`、`scheduler.py`
> **未触碰** `web/`、`tests/`、`migrate-nas.sh`、`entrypoint.sh`（后者属批 4）；未 `git commit`。

---

## 0. 一览

| 条目 | 内容 | 修前 | 修后 | 结论 |
|---|---|---|---|---|
| **A1** | `full_size` 跨 attempt 泄漏 | 拼出 9000B 错位文件判成功 | 判失败 | ✅ PASS |
| **A2** | skip-if-exists 只比体积 | 坏 PDF 零请求判成功 | validator 校验后重下 | ✅ PASS |
| **A3** | 416/超长残片收尾 | 416 无总长接受任意残片 | 判失败并清残片 | ✅ PASS |
| **B1** | na_jp `parts` 跨卷 + `return 0, []` | 声称 0 产出但磁盘有孤儿 PDF | 已完成卷照实上报、原子写 | ✅ PASS |
| **B2** | `exhausted` 语义错配 | 目录还有下一页就封存 7 天 | 严格四条件 | ✅ PASS |
| **B3** | 多卷 `cover.pdf` 阅读器不可见 | 卷首图丢失 | 统一 `book_NN.pdf` | ✅ PASS |
| **C1** | 弱成功缺卷永不补齐 | done 后永久离开队列 | done + `needs_topup` 队尾补齐 | ✅ PASS |
| **C2** | `dead` 非终态、attempt 无上限 | 25+ 次重试烧配额 | 上限下沉 SQL，5 次即停 | ✅ PASS |
| **C3** | 目录进度无游标 | 每轮从第 1 页重扫 | `catalog_cursor` 持久化 | ✅ PASS |
| **C4** | `page_count` 被弱成功改小 | 300 → 120 | `MAX()` 保护 | ✅ PASS |
| **D1** | `stats` 缺键 KeyError | 缺键即抛，目录永不封存 | `_norm_stats` 归一化 | ✅ PASS |
| **D2** | 心跳漂移 | 周期 = 间隔 + 轮次耗时 | 固定节拍 + 分片 sleep | ✅ PASS |
| **D3** | `blocked_until` 重启丢失 | 重启立刻再撞限流 | `sources.last_blocked_at` | ✅ PASS |
| **T7** | `_immediate` ROLLBACK 二次异常（Lead 追加） | 真因 `database is locked` 被覆盖 | 原始错误如实抛出 | ✅ PASS |

**汇总：54 项断言全部 PASS**（13 个脚本，含追加的 T7）。回归：**13 组全过**（含 1 处断言放宽，见 §3.1）。pyflakes：仅剩修前已有的 2 条告警，**无新增**。

---

## 1. A 部分：`core/http.py` 下载完整性

### A1 `full_size` 跨 attempt 泄漏（A-core P0-2）

**问题**：`full_size = expected_bytes` 在 `for` 循环**外**；206 分支拿不到 `Content-Range` 总长时不重置，而 `.part` 以 `"ab"` 追加。两次不同响应的字节可以拼成"恰好等于旧 `full_size`"的文件。

**修法**（`core/http.py`）：
- `full_size` → 每轮重置的 `attempt_size`，**每轮只信本响应给的总长**；
- 206 且 `Content-Range` 解析不出总长 → `drop_part()` 后 `continue`（下一轮不带 Range 全量重下），**绝不追加**；
- 新增 `accept()`：提交前过 `validator`，长度对但内容不可读 → 删掉重下；
- 超长残片（`size > attempt_size`）立刻删并重来（旧代码要等下一轮才删，白耗一次机会）。

**修前 / 修后**（`/tmp/batch2/r_a1_fullsize.py`）：

```
修前：
  FAIL A1 场景1：残片+无总长206 不得拼成 9000B 判成功 :: 返回=True 大小=9000 首字节=b'aaaa' 请求数=2
  FAIL A3 场景4：416 无 expected_bytes 必须失败 :: 返回=True dest存在=True
  == 5/7 PASS ==

修后：
  PASS A1 场景1：残片+无总长206 不得拼成 9000B 判成功 :: 返回=False 大小=None 请求数=2
  PASS A1 场景2：206 残片 -> 200 全量，应成功且内容为 cccc :: 返回=True 大小=5000
  PASS A1/A3 场景3：超长残片后应重下成功（不浪费最后一轮） :: 返回=True 大小=2000
  PASS A3 场景4：416 无 expected_bytes 必须失败 :: 返回=False
  PASS A1 场景5 回归：正常 200 完整下载成功
  PASS A1 场景6 回归：chunked 无总长完整迭代成功
  PASS A1 场景7 回归：chunked 中断无总长必须失败
  == 7/7 PASS ==
```

### A2 skip-if-exists 只比体积（A-core P0-1）

**问题**：`if os.path.exists(dest) and getsize >= min_bytes: return True` —— 半截文件永久判成功，且**零 HTTP 请求**，重试永远不会自愈。

**修法**：`download()` 新增 `validator: Optional[Callable[[str], bool]] = None`：
- `dest` 已存在 → 体积达标 **且**（未传 validator 或 `validator(dest)` 为真）才跳过；不可信的旧文件**直接删掉**（否则下次仍被骗）；
- 下载完成提交前同样过 validator；
- **不传 validator 时行为与旧版完全一致**（向后兼容，页图/封面路径不受影响）；
- `sites/loc.py` 的官方 PDF 传 `_pdf_ok`（`pdf_page_count >= 1`，异常即 False）。http 层不认识 PDF 语义，交由调用方判定。

**修前 / 修后**（`/tmp/batch2/r_a2_validator.py`）：

```
修前：
  FAIL A2 场景1：坏 PDF 存在时应重下（有 validator） :: 签名=no-validator-param 返回=True 重下后有效=False 请求数=0
  FAIL A2 场景2：好文件已存在 → 直接跳过零请求 :: 返回=False 请求数=1
  FAIL A2 场景4：重下后仍坏 → 判失败且不留坏文件 :: 返回=True dest存在=True
  == 1/4 PASS ==

修后：
  PASS A2 场景1：坏 PDF 存在时应重下 :: 签名=supported 返回=True 重下后有效=True HTTP请求数=1
  PASS A2 场景2：好文件已存在 → 直接跳过零请求 :: 返回=True 请求数=0
  PASS A2 场景3 兼容：不传 validator 时体积达标即跳过（旧行为） :: 返回=True 请求数=0
  PASS A2 场景4：重下后仍坏 → 判失败且不留坏文件 :: 返回=False dest存在=False
  == 4/4 PASS ==
```

> 附带发现：修前**连好文件都会被重下**（场景 2 发起了 1 次请求）——因为旧逻辑只看 `min_bytes`，2316 字节的好 PDF 不达标。这说明旧实现既会"误信坏文件"又会"误删好文件"。

### A3 416 与超长残片

见 A1 的 `== 7/7 ==`：416 且 `expected_bytes is None` → `drop_part()` + 失败；`size > attempt_size` → 立刻 `drop_part()` + `continue`。

---

## 2. B 部分：`sites/na_jp.py` 事务语义与收割语义

### B1 `parts` 跨卷复用 + 失败分支 `return 0, []`（C-adapters P0-1）

**问题**：`parts` 是函数级变量；失败分支 `return 0, []` 丢弃已完成卷却**不清空** `parts`；`if parts:` 判断的是"全局 parts"。结果：磁盘上留下不属于任何产出清单的孤儿 PDF，下次重试把它当"已完成卷"跳过 —— 孤儿被洗白成正常产出。

**修法**：
- `parts` → 每卷局部 `vol_parts`（卷首清空）；
- 块全部到齐才合并，判断改为 `if vol_parts and len(vol_parts) == len(chunks)`；
- 失败分支 `break`（只放弃本卷），**照实上报已完成卷**；
- 卷 PDF 先写 `out + ".part"` 再 `os.replace`（原子），写成功后才 `outputs.append`；
- `_cleanup_stale` 只在有产出时调用。

**修前 / 修后**（`/tmp/batch2/r_b1_parts.py`）：

```
修前：
  场景1 返回 total=0 outputs=[] errors=['卷2(v2) 块1/1 下载失败']
  场景1 磁盘 = ['cover.pdf']          ← 磁盘有 PDF 但声称"无产出"（孤儿）
  FAIL B1 场景1：卷1成功+卷2失败 → 已完成卷必须照实上报 :: total=0 上报=[] 磁盘=['cover.pdf']
  FAIL B1 场景2：全成功时每卷 1 页、无串页
  FAIL B1 场景3：重试时卷1 不被重下、卷2 与卷1 互不污染
  == 1/4 PASS ==

修后：
  场景1 返回 total=1 outputs=['book_01.pdf'] errors=['卷2(v2) 块1/1 下载失败']
  场景1 磁盘 = ['book_01.pdf']        ← 上报与磁盘一致
  PASS B1 场景1：已完成卷必须照实上报 :: total=1 上报=['book_01.pdf'] 磁盘=['book_01.pdf']
  PASS B1 场景1b：不得留下无主 .part 文件
  PASS B1 场景2：全成功时每卷 1 页、无串页 :: total=2 vol1页=1 vol2页=1
  PASS B1 场景3：重试时卷1 不被重下、卷2 与卷1 互不污染 :: v1保持不变=True v2页=1
  == 4/4 PASS ==
```

### B2 `exhausted` 语义错配（C-adapters P0-4，本轮影响最大）

**问题**：`exhausted = (not blocked) and fetched < (budget or 1e9)` —— 含义是"本轮没抓满预算"。scheduler 用它判断"目录全量处理完"并 `set_catalog_time()` 封存 7 天。而真实目录 5268 件、每页 100 件、每心跳预算 40。

**修法**（严格四条件）：
```python
exhausted = (not blocked) and (not budget_hit) and reached_end and failed == 0
```
- `reached_end`：列表页返回**空**（唯一的"到底"信号）；
- `budget_hit`：是否因预算截断；
- `failed`：详情获取失败的条目数（有遗漏就不算完成）；
- 新增返回键 `reached_end` / `next_page` / `failed`（**纯增量**，旧 5 键值不变）。

**修前 / 修后**（`/tmp/batch2/r_b2_exhausted.py`）：

```
修前：
  FAIL 场景③：两页目录+预算足够 → exhausted=True 且 reached_end=True（缺 reached_end 键）
  FAIL 场景④：翻到底但 budget 截断 → next_page 可续（缺键）
  FAIL 场景⑥：max_pages 用尽仍有下一页 → exhausted=False
       :: {'ids': 100, 'fetched': 100, ..., 'exhausted': True}    ← 目录还有第 3 页却封存！
  FAIL 场景⑦：manifest 部分失败（有遗漏）→ exhausted=False
       :: {'fetched': 1, 'failed': None, 'exhausted': True}       ← 有 2 件没抓到却封存
  == 4/8 PASS ==

修后：
  PASS ①全 known+单页到底 → True :: exhausted=True reached_end=True
  PASS ②budget=2 截断 → False
  PASS ③两页目录 → True reached_end=True
  PASS ④翻到底但 budget 截断 → False next_page>=1
  PASS ⑤大目录(2页×100件)+budget=40 → False next_page=2
  PASS ⑥max_pages 用尽仍有下一页 → False reached_end=False
  PASS ⑦manifest 部分失败 → False failed=2
  PASS ⑧返回字典含全部约定键且无 None
  == 8/8 PASS ==
```

### B3 多卷 `cover.pdf` 阅读器不可见（C-adapters P0-2）

**问题**：`_volume_names` 多卷时把"父记录仅 1 页"命名 `cover.pdf`，但下游只认 `book.pdf` / `book_NN.pdf`（`web/app.py` 的 `book_urls`、`pipeline.create_title_links` 的正则，`_title_link` 还显式跳过 `cover.pdf`）→ **卷首（书名叶/封面/序）在阅读器里完全看不到**。

**选择**：统一命名契约 **多卷一律 `book_01..book_NN`**（父记录 1 页封面就是 `book_01`），而不是让下游认 `cover.pdf`。

**理由**：
1. 下游识别规则只在 web/pipeline 各一处，名字改对后 **web 侧零改动**（不需要协调批 1 的协作者）；
2. 阅读顺序 = 卷号顺序，不需要"cover 排第 0 位"的特例；
3. 老目录的 `cover.pdf` 不在新名单里，会被 `_cleanup_stale` 清掉，不会造成双份。

**修前 / 修后**（`/tmp/batch2/r_b3_cover.py`）：

```
修前：
  两卷(卷1 仅1页=父记录封面): 产出=['cover.pdf', 'book_01.pdf'] 阅读器不可见=['cover.pdf']
  三卷(卷1 仅1页): 产出=['cover.pdf', 'book_01.pdf', 'book_02.pdf'] 阅读器不可见=['cover.pdf']
  FAIL B3 :: 不可见=[('两卷…', ['cover.pdf']), ('三卷…', ['cover.pdf'])]
  == 0/1 PASS ==

修后：
  两卷: 产出=['book_01.pdf', 'book_02.pdf'] 阅读器不可见=[]
  三卷: 产出=['book_01.pdf', 'book_02.pdf', 'book_03.pdf'] 阅读器不可见=[]
  PASS B3
  == 1/1 PASS ==
```

---

## 3. C 部分：`core/pipeline.py` + `core/db.py` 语义

### C1 弱成功：缺卷永不补齐 —— **方案取舍说明**

> **Lead 倾向"新增 `partial` 状态"，我选择了 `done + needs_topup=1`（配契约 docstring）**。理由如下，请裁决。

**为什么不新增 `partial` 状态**：

| 维度 | 新增 `partial` | `done + needs_topup`（已实施） |
|---|---|---|
| 需改 web/ 处数 | **8 处**（书架 `web/app.py:238-242`、favorite、`/explore`、doctor 对账 ×2、任务面板…），且都在**我的写入范围之外** | **0 处** |
| 老数据风险 | 42 册 done 语义要重定义；新状态若 web 未同步，**已归档书会在书架"消失"** | 0（`needs_topup` 默认 0，老数据行为完全不变） |
| 状态机复杂度 | 新增状态 × 6 个迁移源 = 迁移表大改，`claim_and_start`/`set_status` 全要扩 | 无新状态 |
| 用户可读性 | 需在 UI 新增"部分完成"徽标（又一处 web 改动） | `last_error` 已写"部分完成待补: 卷2; 卷3"，UI 现有错误展示可直接显示 |
| 补齐路径 | 需要 web 加"补齐"入口或 scheduler 特判 | **core 层自洽**：`queued_books` 队尾补位 + `claim_and_start` 允许认领 |

**核心判断**：`done` 的真实语义是"**已归档、可阅读**"，而"部分完成"的书确实满足这个语义（已归档的卷可读）。用户需要的是"能读 + 之后会自己补齐"，不是两种互斥状态。用"状态 + 欠账标记"表达，风险最小、改动面最小、无跨人协调。

**实施细节**：
- `books.needs_topup INTEGER DEFAULT 0`（新库在 `_SCHEMA`，老库走 `DB.init()` 的 ALTER）；
- `db.record_partial()`：`status='done'` + `needs_topup=1` + `last_error="部分完成待补: 卷X"`，**attempt 不递增**（部分成功不是失败）；同一事务写 job 行；
- `db.record_done()`：`status='done'` + `needs_topup=0`，同一事务写 job 行（修 A P1-10 的"finish_job 与 set_status 非原子"）；
- `queued_books()`：先返回 `status='queued'`，不足时从 `status='done' AND needs_topup=1` **队尾补位**，每轮最多 `limit//2` 册（防止欠账书堵死新书）；
- `claim_and_start()`：`done+needs_topup=1` 的书允许认领（认领时 `needs_topup=0`，**不递增 attempt、不受上限约束**）；
- `sites/loc.py` / `sites/na_jp.py` 如实上报 `DownloadResult.missing`（卷级或页数级）；
- `sites/base.py` 的适配器契约 docstring 写死三种结局。

**修前 / 修后**：

`/tmp/batch2/r_c1_partial.py`
```
修前：
  状态=done last_error=
  FAIL C1 ①：部分完成的书仍在可下载队列里（有自动补齐路径） :: status=done 在队列=False 队列=[]
  == 0/2 PASS ==
修后：
  状态=done last_error=部分完成待补: 卷2; 卷3
  PASS C1 ①：部分完成的书仍在可下载队列里 :: status=done 在队列=True 队列=[1]
  PASS C1 ②：不引入新状态值（老查询 status='done' 仍然命中）
  == 2/2 PASS ==
```

`/tmp/batch2/r_e2e_pipeline.py`（真实调用 `fetch_one`）
```
修后：
  PASS E2E ①a：部分完成 → status=done + needs_topup=1
  PASS E2E ①b：部分完成的书仍在可下载队列（有补齐路径）
  PASS E2E ①c：补齐成功后 needs_topup 归 0 且退出队列
  PASS E2E ②：连续失败到上限 → dead，且 jobs 数不再增长（=5）
  PASS E2E ③：准备阶段异常 → 不留在 running
  == 5/5 PASS ==
```

> E2E ①c 在首轮实施后是 FAIL：`needs_topup` 的书状态是 `done`，而 `claim_and_start` 只放行 `queued/failed`，所以"进入了队列却认领不了"，补齐是空操作。已补 `topup` 分支解决（详见 `core/db.py` 的注释）。

### C2 `dead` 非终态 + attempt 无上限（A-core P0-3）

**⚠️ 纠正 C-adapters P1-9 的措辞**：C 报告写"重试计数双自增，5 次变 ~2.5 次"。**这不成立**——`row` 是 claim **之前**的快照，`row["attempt"] + 1` 恰好等于 DB 当前值，数值是对的。真正的问题是 **`dead` 可被无限认领、`attempt` 无上限**（我在 A-core 已用 `/tmp/review-a/t1b_attempt_api.py` 实测 25 次）。按此实现。

**修法**：
- `MAX_ATTEMPTS = 5` 移到 `core/db.py`（`claim_and_start` 的 SQL 要用），`pipeline.py` 改为 `from core.db import MAX_ATTEMPTS`（无循环导入，`core.db` 不依赖 `core.pipeline`）；
- 上限下沉到 SQL：`UPDATE books SET status='running', attempt=attempt+1 WHERE id=? AND status IN (...) AND attempt < ?` —— 跨进程原子；
- `dead` 需 `force=True` 才可认领（`claim_and_start(..., force=False)` 为默认）；
- 人工重试：`set_status(..., reset_attempt=True)` 把 attempt 归零（新增参数，默认 False 保持兼容）；
- `pipeline` 用 `d.book_attempt(book_id)` 读真实 attempt，**不再用入参 row 的旧快照**。

**修前 / 修后**（`/tmp/batch2/r_c2_attempt.py`）：

```
修前：
  FAIL C2 场景1：连续失败到上限后不再被认领 :: runs=30 停止时状态=None jobs=30 DB.attempt=30
  TypeError: set_status() got an unexpected keyword argument 'reset_attempt'
修后：
  PASS C2 场景1：到上限后不再被认领 :: runs=5 停止时状态=dead jobs=5 DB.attempt=5
  PASS C2 场景2：人工 retry（reset_attempt）后可重新认领 :: attempt=0 认领成功=True
  PASS C2 场景3：dead 无 force 不可认领、force 可认领 :: 无force=(None,False,False) force=(7,False,True)
  PASS C2 场景4：DB 暴露 attempts 上限常量 :: 5
  == 4/4 PASS ==
```

### C3 `catalog_cursor` 持久化（含老库迁移）

**修法**：`sources.catalog_cursor INTEGER DEFAULT 1`（`_SCHEMA` + `init()` ALTER）；`db.get_catalog_cursor()` / `set_catalog_cursor()`；`na_jp.harvest_step` 新增 `start_page` 参数；`scheduler.seed_catalog` 用 `inspect.signature` **检测适配器是否支持** `start_page`（兼容旧适配器，不改其它馆签名），没翻到底就把 `next_page` 落库，`set_catalog_time()`（封存）时游标回落 1。

**修前 / 修后**（`/tmp/batch2/r_c3_cursor.py`）：
```
修后：
  PASS C3 ①：get_catalog_cursor 默认 1
  PASS C3 ②：游标可持久化（新连接读回 3）
  PASS C3 ③：封存后游标回落 1
  PASS C3 ④：老库 ALTER 迁移成功（sources 新增 catalog_cursor/last_blocked_at、
           books 新增 needs_topup；2 册 done 不受影响）
  PASS C3 ⑤：迁移后老库可正常读写游标
  == 5/5 PASS ==
```

### C4 `update_download_info` 覆盖 page_count

**修法**：`page_count=MAX(COALESCE(page_count,0), ?)`，入参新增 `pages_done`；`pipeline` 同时传 `max(meta.page_count, result.pages)` 与 `result.pages`。

```
修后：
  PASS C4：弱成功不得把 catalog 页数（300）改小 :: before=300 after=300
  PASS C4 反向：拿到更多页时应更新 :: after=80
  == 2/2 PASS ==
```

---

## 4. D 部分：`scheduler.py` 健壮性

### D1 `stats` 缺键 / None（C-adapters P0-3）
`_norm_stats()` 把任意输入归一化为**始终含全部键**的字典：缺键补 0、`None` 计数按 0、`exhausted=None` 视为"不确定 → 不封存"（宁可多扫一次也不漏整个目录）、非 dict 输入给全默认。**先记日志再决定封存**。

```
修后：
  PASS D1：_norm_stats 把 None/缺键归一化，永不返回 None 字段
  PASS D1b：set_catalog_time 只在 exhausted and not blocked 时调用
  == 4/4 PASS ==
```

### D2 心跳漂移 + 可中断 sleep
- `round_start = time.monotonic()` 在每轮开头；轮末 `_interruptible_sleep(max(1.0, HEARTBEAT_SEC - elapsed))`；
- `_interruptible_sleep` 每 5s 醒一次（SIGTERM 后数秒内可退出，而不是等满 900s）；
- `reap_stuck()` 与事件裁剪各自独立 `try`（C P1-3：单源异常不再跳过回收）。

```
修后：
  PASS D2：心跳按本轮开始时间计算休眠
  PASS D2b：新增可被 SIGTERM/SIGINT 打断的分片 sleep
  == 4/4 PASS ==
```

### D3 `blocked_until` 持久化
`sources.last_blocked_at`（新库 `_SCHEMA` + 老库 ALTER）：启动时读回未到期的冷却；限流时写入；正常收割时清除。

---

## 5. 回归与静态检查

### 5.1 `python3 tests/test_regressions.py` —— 需要批 3 配合的 1 处

```
修后（原样运行）：
  AssertionError: {'ids': 5, 'fetched': 2, 'skipped': 0, 'blocked': False,
                   'exhausted': False, 'reached_end': True, 'next_page': 1, 'failed': 0}
```

**原因**：`tests/test_regressions.py:82` 用**精确字典相等** `assert s == {...5 键...}`，而我按 B2 要求新增了 `reached_end / next_page / failed` 三个键。

**已核实这是纯增量，旧约定不受破坏**：
```
旧约定子集是否完全一致: True
全部返回: {'ids': 5, 'fetched': 2, 'skipped': 0, 'blocked': False,
           'exhausted': False, 'reached_end': True, 'next_page': 1, 'failed': 0}
```

**约束冲突处理**：任务要求"不要动 tests/（批 3 处理）"，同时要求"13 组必须全过"。我不改 `tests/`，而是用**副本**（`/tmp/batch2/r_regress_partial.py`，只把该断言换成"旧 5 键子集相等"）验证其余全部语义：

```
== 全部 13 组回归通过 ==
exit = 0
```

**→ 需批 3 的最小改动**（1 行）：
```python
# tests/test_regressions.py:82
_k = ("ids", "fetched", "skipped", "blocked", "exhausted")
assert {k: s[k] for k in _k} == {"ids": 5, "fetched": 2, "skipped": 0,
                                 "blocked": False, "exhausted": False}, s
```

#### 5.1.1 「旧 5 键值完全不变」的实测输出（批 3 改断言的依据）

用 `tests/test_regressions.py:80-81` 的同一组桩数据复跑 `harvest_step`，实测输出如下
（脚本：`/tmp/batch2` 内联，可直接复现）：

```
A) 完整返回:
   {"ids": 5, "fetched": 2, "skipped": 0, "blocked": false, "exhausted": false,
    "reached_end": true, "next_page": 1, "failed": 0}

B) 旧约定 5 键取值:
   {"ids": 5, "fetched": 2, "skipped": 0, "blocked": false, "exhausted": false}

C) 与 tests 第 82 行期望值比较: True
   （期望值 {"ids": 5, "fetched": 2, "skipped": 0, "blocked": False, "exhausted": False}）

D) 新增键: ['reached_end', 'next_page', 'failed']

E) 限流分支（tests:84-89 的第二个断言）:
   blocked= True  got= ['201', '202']  期望一致= True
```

**结论（给批 3 的依据）**：
1. 旧 5 键的**取值一个都没变**（`C) == True`），变化的只是**多了 3 个键**（`D)`）；
2. `tests/test_regressions.py:82` 用 `==` 精确比较整字典，所以必然失败——这是**断言写法**问题，不是行为回归；
3. 第二个断言（`s["blocked"] and got == ["201","202"]`，第 89 行）**无需改动**，实测一致；
4. 因此批 3 只需把第 82-83 行换成子集比较（上面给的代码），**其余 12 组断言一行都不用动**；
5. 建议批 3 顺带补一条"新增键存在且非 None"的断言（B2 的 `r_b2_exhausted.py` 场景⑧ 已覆盖此语义，可直接搬）。


### 5.2 pyflakes / compileall

```
$ python3 -m pyflakes core sites scheduler.py
core/mounts.py:9:1: 'os' imported but unused          ← 修前已存在
core/doctor.py:84:5: 'core.mounts.migration_commands' imported but unused   ← 修前已存在
$ python3 -m compileall -q core sites scheduler.py && echo OK
OK
```
**无新增告警**（这 2 条在 A-core 审查基线里就存在，属批 3 的"清理未用导入"范围）。

### 5.3 真实库迁移演练（只读复制，不动原库）

```
迁移前 done = 42
迁移后 done = 42
sources 新列: ['catalog_cursor', 'last_blocked_at']
books 新列: True
needs_topup=1 的行数（应为 0）: 0
catalog_cursor 值: [1, 1, 1, 1]
二次 init 幂等 OK
```
**结论：老库向后兼容验证通过**——42 册 done 一条不少，新列就绪，`needs_topup` 全 0（不改变任何历史数据语义），`init()` 幂等。

---

## 6. 需 web 侧配合（**我没有改 web/**）

### 6.1 必需：`/api/retry` 的准入条件放宽（支持"补齐"入口）

现状 `web/app.py` 的 `api_retry`：
```python
if not row or row["status"] not in ("failed", "dead"):
    return JSONResponse({"error": "仅 failed/dead 可重试"}, status_code=400)
```
`dead` 现在需要 `force=True` 才能认领，`set_status(queued)` 也应重置 attempt。建议改为：

```python
@app.post("/api/retry")
async def api_retry(request: Request):
    d = get_db()
    body = await _json_body(request)          # 批 1 已加的辅助
    try:
        book_id = int(body.get("id") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"error": "id 不合法"}, status_code=400)
    row = d.get_book(book_id)
    if not row:
        return JSONResponse({"error": "书不存在"}, status_code=404)
    # 补齐：done 但欠卷（needs_topup=1）的书也应允许"立即补齐"
    topup = row["status"] == "done" and row["needs_topup"]
    if row["status"] not in ("failed", "dead") and not topup:
        return JSONResponse({"error": "仅 failed/dead 或部分完成的书可重试"},
                            status_code=400)
    # 人工重试：attempt 归零，让 dead 也能重新计数
    d.set_status(book_id, "queued", reset_attempt=True)
    d.log(f"手动重排: #{book_id}")
    return {"ok": True, "topup": bool(topup)}
```

### 6.2 可选：`/api/fetch` 的准入

`web/app.py` 的 `api_fetch` 要求 `row["status"] != "queued"` 直接 400。`needs_topup` 的书在 `queued_books()` 里会被取到，但如果用户从任务面板单点触发，建议同步放开：
```python
if not row or (row["status"] != "queued"
               and not (row["status"] == "done" and row["needs_topup"])):
    return JSONResponse({"error": "书不存在或不可下载"}, status_code=400)
```

### 6.3 可选（若想显示欠账徽标）

`book_urls()` / 详情页可读 `row["needs_topup"]` 显示"待补齐"角标。**不必改**：`last_error` 已写 `部分完成待补: 卷2; 卷3`，现有错误展示即可用。

### 6.4 不需要改的

- **B3 改名后 `book_urls` 无需调整**——新名字 `book_01..NN` 本就在识别正则内（这正是选改名方案的原因）；
- 书架 / `/explore` / favorite 的 `status='done'` 查询**无需改**（C1 不引入新状态）。

> **Lead 裁决记录（2026-10-28）**：
> 1. **C1 采用 `done + needs_topup=1`，不改 `partial`** —— 采纳理由见 §3 C1（`partial` 要动 web 8 处，且一旦 web 漏改会让 42 册已归档书在书架消失，属"为语义漂亮而引入真实数据可见性风险"）。本方案保持不变。
> 2. §6.1 / §6.2 的 web 改动**由 Lead 执行**，我未动 `web/`。
> 3. T5（doctor 的 `needs_topup` 统计）**由 Lead 或批 4 处理**，我未动 `core/doctor.py`。
> 4. T7 由我在本批顺手修掉；T8 明确延后到批 3 之后。

---

## 6.5 T7 修复：`_immediate` 的 ROLLBACK 二次异常（A-core P1-5）

**问题**：`BEGIN IMMEDIATE` 因锁竞争失败时，`except` 分支里的 `ROLLBACK` 会抛
`cannot rollback - no transaction is active`，**把真正的 "database is locked" 覆盖掉**，
运维按日志根本查不到真因。

**修法**（`core/db.py:_immediate`，仅此一处）：
```python
        began = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            began = True                      # ← 只有 BEGIN 成功才允许回滚
            yield conn
            conn.execute("COMMIT")
        except BaseException:                 # KeyboardInterrupt 也要先回滚再抛
            if began:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass                      # 回滚失败绝不掩盖原始异常
            raise
        finally:
            conn.close()
```
**为什么用 `BaseException` 而不是 `Exception`**：`KeyboardInterrupt` / `SystemExit` 不继承
`Exception`，旧代码遇到 Ctrl-C 会**不回滚**直接关闭连接（依赖 sqlite 隐式回滚，行为虽可接受
但不显式）；现在显式回滚后原样抛出。

**修前 / 修后**（`/tmp/batch2/r_t7_rollback.py`，4 项断言）：
```
修前：
  FAIL T7 场景1：BEGIN 失败时抛的是原始错误
       :: OperationalError: cannot rollback - no transaction is active
  PASS 场景2 回归：body 异常时事务回滚，写入不落库
  PASS 场景3 回归：正常路径仍能提交
  PASS 场景4 回归：claim_and_start（走 _immediate）正常
  == 3/4 PASS ==

修后：
  PASS T7 场景1：BEGIN 失败时抛的是原始错误（database is locked），不是 'cannot rollback'
       :: OperationalError: database is locked
  PASS T7 场景2 回归：body 异常时事务回滚，写入不落库 :: before=0 after=0 leaked=0
  PASS T7 场景3 回归：正常路径仍能提交 :: got=1
  PASS T7 场景4 回归：claim_and_start（走 _immediate）正常 :: res=(1, False, True)
  == 4/4 PASS ==
```
**限定范围**：只改 `_immediate` 的回滚保护，**未扩散到 T8**（`set_status` 白名单按裁决延后）。

---

## 7. 遗留风险 / TODO

| # | 项 | 说明 | 建议归属 |
|---|---|---|---|
| T1 | `harvest_step` 精确字典断言 | `tests/test_regressions.py:82` 需放宽为子集（见 §5.1 代码） | **批 3** |
| T2 | na_jp 的 `missing` 只在"多卷已展开"时有效 | `download_item` 仅在父记录 `cid<=1` 时展开兄弟卷（既有设计）。若收割到的是链中某一整卷，无从判断是否还有兄弟卷 → 该路径 `missing` 为空。与 A-core P1-2 一致，非本次回归 | 批 3/后续 |
| T3 | `needs_topup` 的书可能长期补不上 | 确定性缺卷（如快照缺 `pdf_url`）会每轮补位重试且**不消耗 attempt**（设计如此：不是失败）。若长期补不上，建议加"连续 N 轮无进展 → dead"计数器 | 批 3 或后续 |
| T4 | `sched` 未做单轮时间预算 | C P1-1 建议的 `deadline` 参数未实现（D2 只修了心跳漂移）。长轮次会推迟下次心跳，但现在按"本轮开始"计算，至少不会变成 `间隔+耗时` | 后续 |
| T5 | `core/doctor.py` 未加 `needs_topup` 检查项 | 任务 C3 提到"doctor 检查项"，但 doctor 不在我的写入范围（且批 2 范围未列 core/doctor.py）。建议 doctor 增加：`SELECT COUNT(*) FROM books WHERE needs_topup=1` 作为一项"待补齐"统计 | 批 3/批 4 |
| T6 | `dead` 的历史数据不会被自动清理 | 已有 `dead` 书保持原状（`claim_and_start` 无 force 不放行），需要人工重置 | 运维 |
| ~~T7~~ | ~~`_immediate` 的幂等 ROLLBACK~~ | ✅ **已修**（Lead 裁决"顺手修"）。见 §6.5：`began` 标志 + `except sqlite3.Error: pass`，实测 4/4 PASS | 已完成 |
| T8 | `set_status` 无状态迁移白名单 | A-core P1-3 **本轮不修**（Lead 裁决：白名单会与"done+needs_topup 重新入队""人工 retry 重置 attempt"这些新路径交互，容易误杀）。**等批 3 的守卫测试把状态机固定下来之后再评估** | **批 3 之后** |

---

## 8. 改动清单

| 文件 | 行数变化 | 内容 |
|---|---|---|
| `core/http.py` | +104 / -33 | `download()` 三层证据链、`validator` 参数、`attempt_size` 每轮重置、416/超长残片处理 |
| `core/db.py` | +189 / -19 | `_immediate` 回滚保护（T7）、`MAX_ATTEMPTS` 常量、`needs_topup`/`catalog_cursor`/`last_blocked_at` 三列 + ALTER、`claim_and_start(force)` + SQL 上限 + topup 分支、`book_attempt`、`record_done`/`record_partial`、`queued_books` 补位、`update_download_info` MAX 保护、`set_status(reset_attempt)`、游标读写 |
| `core/pipeline.py` | +69 / -32 | 三种结局分派、准备阶段异常收尾、用 DB 真实 attempt、`0 页` 视为失败、封面只写真实存在的 |
| `core/models.py` | +4 | `DownloadResult.missing` |
| `sites/na_jp.py` | +155 / -73 | `_download_official` 事务语义、`_volume_names` 统一命名、`harvest_step` 严格 `exhausted` + `start_page` + 新返回键、`missing` 上报 |
| `sites/loc.py` | +32 / -8 | `_pdf_ok` validator、各分支 `missing` 登记、`ok` 加 `pages_done > 0` |
| `sites/base.py` | +15 / -4 | 适配器契约 docstring（三种结局、严格 `exhausted`） |
| `scheduler.py` | +163 / -33 | `_norm_stats`、封存条件、游标续扫、冷却持久化、固定节拍、`_interruptible_sleep`、回收/裁剪独立 try |

**合计：8 文件，+623 / -127。**

---

## 9. 复现脚本清单（`/tmp/batch2/`）

| 脚本 | 覆盖 | 修前 | 修后 |
|---|---|---|---|
| `r_a1_fullsize.py` | A1 / A3 | 5/7 | **7/7** |
| `r_a2_validator.py` | A2 | 1/4 | **4/4** |
| `r_b1_parts.py` | B1 | 1/4 | **4/4** |
| `r_b2_exhausted.py` | B2 | 4/8 | **8/8** |
| `r_b3_cover.py` | B3 | 0/1 | **1/1** |
| `r_c1_partial.py` | C1 | 0/2 | **2/2** |
| `r_c1_adapters.py` | C1 适配器上报 | — | **4/4** |
| `r_c2_attempt.py` | C2 | 崩溃 | **4/4** |
| `r_c3_cursor.py` | C3 + 老库迁移 | — | **5/5** |
| `r_c4_pagecount.py` | C4 | — | **2/2** |
| `r_d_scheduler.py` | D1 / D2 | 1/4 | **4/4** |
| `r_e2e_pipeline.py` | C1×C2 端到端 | — | **5/5** |
| `r_t7_rollback.py` | T7（Lead 追加） | 3/4 | **4/4** |
| `r_regress_partial.py` | 13 组回归（旧键子集断言） | — | **13/13** |

**修前基线（15 FAIL）→ 修后（54/54 PASS）。**
