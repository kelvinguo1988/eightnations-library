# 审查 C：采集适配器 + 调度器 + CLI + 框架扩展性

> 审查范围：`sites/base.py`、`sites/loc.py`、`sites/na_jp.py`、`sites/bnf.py`、`sites/__init__.py`、
> `scheduler.py`、`manage.py`、`tools/loc_snapshot.py`、`tools/loc_fill_details.py`、
> `digital_archives_to_pdf.py`、`official_pdf_by_cids.py`
> 交叉阅读（只作证据，不重复审查）：`core/pipeline.py`、`core/http.py`、`core/db.py`、
> `entrypoint.sh`、`web/app.py`（仅采集相关路由）、`README.md`、`docs/DESIGN.md`
> 基线：Python 3.9.9 / pypdf 6.16.2 / requests 已装；`tests/test_regressions.py` 13 组通过。
> 方法：全文精读 + **实测**（/tmp 下 18 个桩/复现脚本，用 FakeHttp 与假 pypdf 模拟，业务代码零改动）。
   本次有 3 次受控的只读线上探测（仅 GET 公开页面/公开 IIIF manifest，已遵守 UA 与间隔），
   用于验证 `na_jp` 的解析假设；未触发限流、未下载任何图像或 PDF。

---

## 0. 结论速览

| 级别 | 条数 | 摘要 |
|---|---|---|
| **P0（阻塞/数据事故）** | **5** | na_jp 多卷分块下载跨卷串页与"页已下载却不登记"；三处 `parse_*` 无异常边界可炸穿整轮；`exhausted` 语义错配导致目录"提前 7 天封存"；UI 允许启用未注册的 `ndl_jp` |
| **P1（重要）** | **13** | 心跳漂移、SIGTERM 硬砍、冷却内存态、`max_pages` 未透传、限流账本不闭环、upsert 覆盖标量、封面丢失、缓存/克隆件爆炸、CLI 破坏性命令、契约无强制、注释说谎等 |
| **P2（优化）** | **11** | 重复实现、循环依赖、死代码、魔数、可测试性等 |

**三条最关键的结论**

1. **na_jp 的官方分块下载在"部分块失败"时没有任何事务语义**：`parts` 列表跨卷复用、
   失败分支 `return 0, []` 丢弃已完成卷、卷 PDF 先落盘再被当作"无产出"。实测三种可复现事故
   （见 P0-1/P0-2），且都会让 `ok` 判定与磁盘真实状态脱节。
2. **调度器的"逐源隔离"只隔离了一半**：`_source_round` 外层有 `except Exception`，但
   `run_source_heartbeat → fetch_one → adapter.download_item` 里的解析代码（`loc._merge_resources`
   的 `resources[0].get`）和 DB 的 `ValueError` 都在隔离圈内**外**传播路径上，会让整轮心跳跳过后面的
   馆并**跳过 `reap_stuck()`**；而 `seed_catalog` 的 `dict["key"]` 缺键会让收割结果永远判"未完成"，
   每个心跳重扫同一页目录（P0-3）。
3. **框架扩展性并不好，但问题不在"要改几个文件"而在"没有任何机制强制你改对"**：
   新增一馆要手工改 **9 处**（其中 4 处是"改错了也不报错"的软耦合）；`SourceAdapter` 是
   `Protocol` 却既非 `runtime_checkable` 也没被任何代码检查，`_REGISTRY: Dict[str, Type[SourceAdapter]]`
   对静态检查完全无效（实测 `loc` 没有 `harvest_step` 也照样能注册、能跑）。详见 §4。

---

## 1. 分级问题清单

### P0

#### P0-1 ｜ `sites/na_jp.py:377,400-443,444` ｜ 分块下载的 `parts` 跨卷复用，部分失败时**卷间串页**

```python
377:        parts: List[str] = []
401:                for ci, ch in enumerate(chunks, 1):
418:                    if not ok:
419:                        errors.append(f"卷{vi}({vid}) 块{ci}/{len(chunks)} 下载失败")
420:                        return 0, []          # ← ① 已完成的卷被丢弃；② parts 没清空就返回
...
424:                if parts:                     # ← ③ 判断的是"全局 parts"，不是"本卷 parts"
425:                    w = PdfWriter()
427:                    for p in parts:           # ← ④ 可能包含上一卷残留的块
```

- **问题**：`parts` 是函数级变量，只在成功合并后清空（`443: parts = []`）。失败分支直接
  `return 0, []`，残留块只在 `finally` 里被删（临时文件不泄漏），但**本条路径上已经把卷 1 的 PDF
  写进了 `dest_dir`**，而返回值声称"0 页、无产出"。
- **实测**（`/tmp/rv/test_vol_leak6.py`）：卷 1 成功、卷 2 块 1 失败 →
  `_download_official` 返回 `(0, [])`，但 `dest/cover.pdf` 真实存在 1 页，`errors` 只有一条
  "卷2 块1/2 下载失败"。`download_item` 随后 `pages==0 → outputs==[]`（na_jp.py:319-323 逻辑反推），
  接着 `_download_iiif` 又从第 0 卷重来。
- **影响**：磁盘上出现**不属于任何 job 产出清单**的孤儿 PDF；下一次重试时 `_download_official`
  会因 `os.path.exists(out)` 命中而把该孤儿当作"已完成卷"（`385-393`），**跳过真实下载**；
  更糟的是若上一轮卷 1 的块残留到卷 2 的合并里（`427`），就会产出**卷 2 的 PDF 里混着卷 1 的页**——
  对古籍而言这是不可发现的错版。
- **建议**：
  1. `parts` 改为**每卷局部**，卷首 `parts = []`；
  2. 失败分支不要 `return 0, []`，改为 `break` 出外层循环 + 返回 `(total, outputs)`（已完成卷照实上报），
     并在 `errors` 里标 `fatal`；
  3. 卷 PDF 写 temp（`out + ".part"`）再 `os.replace`，写成功后才 append 到 `outputs`；
  4. 修正 `if parts:` → `if vol_parts and len(vol_parts) == len(chunks):` 再合并。

#### P0-2 ｜ `sites/na_jp.py:301-304, 336-350` ｜ 多卷书**首张图被静默丢弃**（`cover.pdf` 不进任何图集）

- **问题**：`_volume_names` 约定"多卷时父记录仅 1 页 → `cover.pdf`"（336-350），
  `_download_official` 确实把它写出。但 `pipeline.fetch_one:158-159` 与
  `pipeline.create_title_links:84` 只认 `book.pdf` / `book_NN.pdf`：

  ```python
  # core/pipeline.py:83-84
  pdfs = sorted(f for f in os.listdir(dest)
                if f == "book.pdf" or re.fullmatch(r"book_\d+\.pdf", f))
  ```

  `_title_link` 甚至显式跳过 `cover.pdf`（`core/pipeline.py:97-98`）。
- **影响**：多卷书的**卷首（通常是书名叶/封面/序）在阅读器里根本看不到**——阅读器按
  `book*.pdf` 拼卷，`cover.pdf` 既不在 `result.outputs`（na_jp 会把它 append 进 outputs，但
  `fetch_one` 的 `_title_link` 跳过、阅读器 URL 生成也按 `book*` 规则）也不在书名链接里。
  同一次扫描里我穷举了 `_volume_names` 所有输出（`['book.pdf','book_01..04','cover.pdf']`），
  确认 `cover.pdf` 是唯一一个"写进 outputs 但下游不认"的名字。
- **建议**：统一命名契约：多卷一律 `book_01.pdf…book_NN.pdf`（父记录的 1 页封面就是 `book_01`），
  或让 `pipeline` 把 `cover.pdf` 当第 0 页合并进首卷。二者择一，并在 `base.py` 契约 docstring 里写死。

#### P0-3 ｜ `scheduler.py:100-103` ｜ `stats` 非约定返回时 `KeyError` → 收割永远"未完成"，每心跳重扫

```python
100:    if stats.get("exhausted"):
101:        d.set_catalog_time(s["id"])
102:    d.log(f"目录收割: 总 {stats['ids']} 条，新抓 {stats['fetched']}，"
103:          f"跳过已存在 {stats['skipped']}（新书 {new}）", source=s["id"])
```

- **问题**：`HarvestAdapter` 只是 `Protocol`（无强制），`stats` 若缺 `ids/fetched/skipped`
  或为 `None`，102-103 行抛 `KeyError`/`AttributeError`。该异常被 `_source_round` 外层捕获→
  记为"本轮异常"，**`set_catalog_time` 已经执行了**（100-101 在 102 之前），所以后果分两种：
  - 缺 `exhausted` 时：`stats.get("exhausted")` 为 falsy → **永不 `set_catalog_time`** → `_stale()`
    恒 True → **每个 900s 心跳都重扫同一目录页**（`seed_catalog` 每轮 1 次列表页 + 已入库跳过详情，
    即每 15 分钟 1 次无意义请求，持续到天荒地老）；
  - 有 `exhausted` 但缺 `ids`：已封存却记了一条异常日志，下次巡检在 7 天后。
- **实测**（`/tmp/rv/t3.py`）：`{"ids":3,"fetched":1,"skipped":0}` 能过 `get`，`None` 直接
  `AttributeError`；`{"ids":1,...}` 正常。真正危险的是**缺 `exhausted` 的组合**。
- 结合 P1-4 的 na_jp `exhausted` 语义（见下），这条的**现实触发概率很高**。
- **建议**：`stats` 读出后用 `_norm_stats()` 归一化（缺键补 0、`None` 转空统计并视为失败重试），
  并把"是否封存目录"的判断移到日志之后、且只在 `exhausted and not blocked` 时执行。

#### P0-4 ｜ `sites/na_jp.py:180,220` ｜ `exhausted` 语义错配 → **目录被提前 7 天封存**

```python
180:            if not new:
181:                break
182:        todo = [i for i in ids if i not in known]
...
220:                "exhausted": (not blocked) and fetched < (budget or 10 ** 9)}
```

- **问题**：`exhausted` 的含义是"**本轮没抓到 budget 上限**"，而不是"目录已全量处理完"。
  正确语义应是"**翻页到底且所有条目都已入库**"。`scheduler.py:57,100-101` 的注释写得很清楚
  （"exhausted=True 表示目录已全量处理完"），实现却做不到。
- **实测**（`/tmp/rv` 场景 2）：页面上 3 个 id **全部已在库**，`budget=2` →
  返回 `{"ids":3,"fetched":0,"skipped":3,"blocked":False,"exhausted":True}` →
  scheduler 调用 `set_catalog_time()` → **这个 fonds 的目录 7 天内不再扫描**。但该 fonds 真实规模是
  **5268 件**（实测 `/fonds/3611449` 页面："該当件数 5268件"），第一页只有 100 件。
- **实测**（场景 3）：3 个 id 里前 2 个 manifest 拿不到（网络抖动，未到 3 次阈值）→
  `{"fetched":1, "exhausted":True}` → 同样封存 7 天，且那 2 条**永久丢失**（下次巡检 7 天后，
  且 7 天后如果仍只扫第一页，这 2 条会命中 `known` 之外的 `todo` 被重新尝试——实际取决于翻页深度）。
- **影响**：na_jp 是目前**唯一启用的 direct 馆**，"真正被发现的书"取决于它。这条会让
  5268 件的目录被 7 天封存一次，而每 7 天只处理预算内的前 40~100 件 → 全量入库需要
  **~370 周**（5268/40 周末）。这不是"慢"，是"永远跑不完"。
- **建议**：`exhausted` 严格定义为 `不是因 budget 截断 且 翻页到最后一页 且 没有遗漏 id`；
  建议同时返回 `reached_end: bool`、`next_page: int` 供调度器续传，并把"已处理到第几页"
  持久化到 `sources` 表新列（`catalog_cursor`），而不是靠"7 天过期"这种粗粒度信号。

#### P0-5 ｜ `core/db.py:115` + `web/templates/settings.html:37` + `sites/__init__.py:12-16` ｜ UI 允许启用**未注册**的 `ndl_jp`，且上传快照会 500

- **问题**：`sources` 表种子有 `ndl_jp`（enable=0），设置页对**所有** sources 渲染"启用下载"复选框
  （`settings.html:37`）与目录 URL 输入框，用户完全可以勾上并保存。之后：
  - `scheduler.seed_catalog:66-70` 的 `except KeyError: return False` → 静默失败（**只对这一处**）；
  - `scheduler._source_round:176-179` 若该源有 `pending`，会走 `run_source_heartbeat` →
    `fetch_one` → `get_adapter()` 抛 `KeyError` → 被 `_source_round` 外层捕获，记"本轮异常"——
    **每个心跳重复一次**，日志刷屏，且 **`reap_stuck()` 被跳过**（见 P1-1）；
  - `core/importer.import_collection_payload:24` 的 `get_adapter` **没有 try**：上传 `ndl_jp` 快照 →
    `web/app.py:712-718` 只捕获 `ValueError` → `KeyError` 冒泡 → **HTTP 500**。
- **实测**：`import_collection_payload(d, "ndl_jp", payload)` → `KeyError: '未注册的适配器: ndl_jp'`；
  `"onb"` 同样；`"loc"` 正常 `(1, 0)`。
- **影响**：用户视角"启用了日本国立国会图书馆但什么都没发生/任务页全是红字"；上传快照直接白屏。
  README 写的是"待实现"，**代码没有把"待实现"这个状态表达出来**。
- **建议**：三选一并落实到代码：
  1. `sources` 加 `implemented INTEGER`（或 `adapter_status`），设置页对未实现项渲染为 disabled；
  2. `get_adapter` 改为 `KeyError` → 自定义 `AdapterNotRegistered`，并在
     `seed_catalog`/`fetch_one`/`import_collection_payload` **三处**统一捕获落日志后跳过；
  3. 干脆不种 `ndl_jp` 进 sources 表（等有 adapter 再 INSERT），前端就不会显示它。

### P1

#### P1-1 ｜ `scheduler.py:127-163` ｜ 心跳**间隔漂移**、`sleep` 不可中断、SIGTERM 硬砍

- **问题**：`while True: <整轮工作>; time.sleep(HEARTBEAT_SEC)`。心跳的实际周期是
  `HEARTBEAT_SEC + 本轮耗时`。一轮耗时上限估算（实测/推算）：
  - 单本下载：`na_jp` 大册 `contentDownload` 单块 `timeout=360`（na_jp.py:465）× 8 次重试（MAX_ATTEMPTS）
    → 单块最坏 48 分钟；`loc` 官方 PDF `timeout=min(60*5,600)=600`（http.py:181）；
  - 目录收割：na_jp `15 页列表 + 40 次 manifest`，同域 2.5s → **≥138 秒**（还未计重试）；
  - 每源 `hourly_quota` 本 × 上述单本耗时 → **一轮数小时**是常态。
  - 于是 `EIGHTNATIONS_HEARTBEAT=900` 名存实亡，README:143 "默认 15 分钟一轮心跳"与
    settings.html:31 "保存后调度器在下一心跳生效"都是**误导**。
- **问题（信号）**：`time.sleep` 期间收到 SIGTERM 不会中断，Python 默认处理器只置标志，
  进程要等 `sleep` 结束或当前阻塞的 `requests` 返回才退出；`entrypoint.sh` 的
  `exec uvicorn` 前台 → 容器里 `docker stop` 会在 10s 后 **SIGKILL**，scheduler 正在写的
  `.part` 只靠 `http.download` 的 Range 续传兜底（**这层兜底是对的**，但 job 会被
  `reap_stuck` 回收后 `attempt` 已经 +1，见 P1-9 的重试计数问题）。
- **建议**：
  1. 用 `threading.Event().wait(HEARTBEAT_SEC)` 代替 `sleep`，注册 `signal.signal(SIGTERM, ...)`
     置位事件 + 在 `fetch_one` 的页/卷回调里检查"退出中"标志，实现**优雅退出**（当前 job 标
     `interrupted` 而不是 `failed`，不计入 attempts）；
  2. 心跳周期改为**固定节拍**：`next_tick = max(next_tick + HEARTBEAT_SEC, now)`，并在每轮开头
     打印"本轮耗时/下一轮时刻"；
  3. 单轮设**时间预算**（如 10 分钟），超时就把剩余 queued 留给下一轮（`run_source_heartbeat`
     加 `deadline` 参数）。

#### P1-2 ｜ `scheduler.py:123, 171-174` ｜ `blocked_until` 只在内存，重启即丢；冷却逻辑与 `exhausted` 互斥错位

- **问题**：`blocked_until: Dict[str, float]` 是进程内变量。`entrypoint.sh` 有"连续崩溃 5 次后重启容器"
  与"30s 后拉起"的重试逻辑 → **一次崩溃就把 30 分钟冷酷期清零**，下一个心跳立即再去撞限流站点。
- **问题**：冷却判断在 `_stale()` 之后（169-172），而 `seed_catalog` 返回 True（被限流）时
  **不会** `set_catalog_time`；冷却 30 分钟后 `blocked_until` 过期，立刻再扫 → 若站点仍封，
  每个心跳"撞一次 + 冷却 30 分钟"，实际是**每 30 分钟撞一次**，而不是指数退避。
- **建议**：`sources` 表加 `blocked_until TEXT`（或复用 `last_catalog_at` + `backoff_s` 列）持久化，
  冷却时间**指数增长**（30m → 1h → 2h …，上限 24h），成功一次后清零。

#### P1-3 ｜ `scheduler.py:140-143` ｜ 逐源隔离不彻底：`_source_round` 抛异常会**跳过 `reap_stuck()` 与事件裁剪**

```python
135:            for s in sources:
138:                try:
139:                    _source_round(d, s, blocked_until)
140:                except Exception as e:      # ← 这里兜住了单源
141:                    d.log(...)
145:            stuck = d.reap_stuck()          # ← 但如果 129-134 的 DB 读失败，直接跳到 160
```

- **问题**：`except Exception` 在循环内**确实**能兜住单源；但**循环外**还有 `sources` 查询、
  `reap_stuck()`、`conn.execute(DELETE events...)` 三步，它们任一抛异常都会跳到 160 的
  "心跳异常(继续)"——**当轮回收与裁剪被跳过**。`reap_stuck` 里若有一条畸形 row（如
  `last_beat` 为 NULL 与 str 比较）就会整轮不再回收，running 任务堆积。
- **问题**：`except Exception` 不捕获 `KeyboardInterrupt`（`BaseException` 子类，实测
  `issubclass(KeyboardInterrupt, Exception) == False`）→ 下载过程中 Ctrl-C 会**穿透逐源隔离**
  直接终止调度器（157 行捕获后 `return`），但**Web 进程里的下载线程仍在跑**，两边对同一 job
  状态写不同结果。这个行为不一定是错，但 `scheduler.py:13` 的注释"逐源 try/except：单馆异常不影响
  其他馆"容易被误读成"任何异常"。
- **建议**：把"回收 + 裁剪"包进各自的 `try/except sqlite3.Error`；显式说明
  `KeyboardInterrupt` 属于**全局停机**语义；`reap_stuck` 内对行做防御性转换。

#### P1-4 ｜ `scheduler.py:178-179` ｜ `max_pages` **未透传**，na_jp 自带限流假设被打破

- **问题**：`run_source_heartbeat(d, s["id"], s["hourly_quota"], s["quality"])` —— 只传了下载参数，
  目录收割在 `seed_catalog` 里是 `harvest(s["catalog_url"], known_uids=known, budget=CATALOG_BUDGET, on_meta=on_meta)`，
  **没传 `max_pages`** → na_jp 用默认 15、bnf 用默认 10。
- **问题（更重）**：na_jp.py:148-154 的 docstring 自己算过账：**限流线 ≈58 次请求 / 单次调用**，
  "默认 15 + 40 = 55 < 58，留少量余量给 `core/http` 的重试"。但：
  1. `budget` 随环境变量 `EIGHTNATIONS_CATALOG_BUDGET` 可调（docker-compose 里注释就建议设 20，
     **设大一些就超线**且无任何校验）；
  2. `http.get` 对 403/429/502/503 **每次尝试 4 次**（http.py:138-151），"55 次"会变成 **最多 220 次**；
  3. `_volume_ids` 每卷 2 次 GET（na_jp.py:261-281，**上限 60 轮**）→ 一把"疑似封面"的书
     可能额外消耗 **120 次请求**，全部计入同域 2.5s 节流，**不吃 `harvest_step` 的预算**。
  实测（`/tmp/rv`）：`max_pages(15)+budget(40)=55`，加上重试的放大，**限流账本完全不闭环**。
- **建议**：把"单次调用请求预算"做成 `HttpClient` 级别的**计数器**（`http.budget(n)` 上下文管理器），
  由适配器声明本次最多 N 次请求，`http.get/download/post` 统一扣减；库里 `harvest_step` 的
  `budget` 参数语义改为"条目数"，另加 `request_budget`；`scheduler` 显式传 `max_pages`。

#### P1-5 ｜ `sites/base.py:62-67` ｜ `HarvestAdapter` 契约**未被任何代码使用**

- **问题**：`base.py:45-67` 定义了 `HarvestAdapter` Protocol，注释说"scheduler 通过
  `getattr(adapter, "harvest_step")` 鸭子类型发现，**不 import 本 Protocol**"。也就是说这个契约
  **只活在文档里**，运行期零校验（实测 `loc` 没有 `harvest_step` 仍能注册，`getattr` 返回 None 后
  `seed_catalog` 直接 `return False`，**没有任何提示说明"这个馆不支持自动收割"**）。
- **影响**：新增一馆时，忘记实现 `harvest_step` → 目录永不收割，日志安静，用户以为"这个馆没书"。
- **建议**：见 §4 的"能力声明 + 启动期自检"方案。

#### P1-6 ｜ `sites/__init__.py:10-12` ｜ `Dict[str, Type[SourceAdapter]]` 类型注解对静态检查**无效**

```python
_REGISTRY: Dict[str, Type[SourceAdapter]] = {"loc": LocAdapter, ...}
```

- **问题**：`SourceAdapter` 是 `Protocol`，`Type[Protocol]` 在 mypy/pyright 下**不是**结构化检查
  （`type[...]` 位置不参与协议匹配），而且项目**没有 mypy/pyright 配置、CI 也不跑**（见 D 报告）。
  实测：`LocAdapter` 缺 `harvest_step`、`parse_snapshot` 参数名/默认值都对，但仍能注册并通过
  `get_adapter` 返回；`isinstance(adapter, SourceAdapter)` 直接 `TypeError: Instance and class checks
  can only be used with @runtime_checkable protocols`。
- **建议**：`@runtime_checkable` + 启动期 `assert isinstance(cls, HarvestAdapter)`（仅对 direct 策略）；
  或在 `get_adapter` 里按 `meta_strategy` 做一次 `_validate_adapter(cls, strategy)`。

#### P1-7 ｜ `sites/bnf.py:151, 194-199` + `sites/na_jp.py:222-234` ｜ `parse_snapshot` 是**摆设**且返回类型不一致

- **问题**：bnf `parse_snapshot` 恒返回 `[]`（194-199）；na_jp 只认 `{"items": [...]}` 结构（222-234）；
  loc 认 `{"results": [...]}`。而 `core/importer.import_collection_payload:25` 对**任何** source
  都调 `adapter.parse_snapshot(payload)`，且 `import_snapshot_files` 的目录约定（`collection_*.json`）
  是 LoC 专属。
- **实测**：给 `bnf` 上传 `collection_xxx.json` → `parse_snapshot` 返回 `[]` → 导入 0 条，
  **不报错**（`manage.py:49-51` 只打印"跳过"）。用户会以为"快照格式不对"，实际上这条路径
  **设计上就不支持 bnf**。
- **建议**：把 `parse_snapshot` 从"SourceAdapter 必选"降级为"SnapshotCapable 可选能力"，
  并在 `import_snapshot_files` 里按能力分派；bnf/na_jp 明确报 "该馆不支持快照导入"。

#### P1-8 ｜ `core/db.py:330-335`（经 `scheduler.seed_catalog:81` 触发）｜ upsert 会用**空值覆盖**标量字段

- **问题**：`upsert_book` 的"合并保护"只覆盖 `subjects/pdf_urls/files_json` 三个 JSON 列。
  `title/era/author/shelf_id/page_count/volume_count` 等标量**一律覆盖**。
- **实测**（`/tmp/rv`）：先 upsert `page_count=500, volume_count=3`，再 upsert
  `page_count=0, volume_count=0` → 库里变成 `0, 0`；title/era/shelf_id 同样被清空。
- **触发路径**：每周巡检 `seed_catalog` 用 `_meta_from_manifest` 重新解析（bnf.py:154-192 的
  `page_count=len(pages)`，manifest 结构变化→0 页），或 `manage.py enrich-na-jp` 用
  `row_to_meta` 往返（这条是安全的，因为带着原值）。
- **建议**：把保护扩为"新值为空/0 且旧值非空 → 保留旧值"的统一规则（对 `title` 除外，标题可能真改名）。

#### P1-9 ｜ `core/pipeline.py:165-166` + `core/db.py:500-503` ｜ 重试计数**双自增**，5 次变 ~2.5 次；失败后 book 永远留在 `failed` 不再被调度

```python
# db.claim_and_start
500:            cur = conn.execute(
501:                "UPDATE books SET status='running', attempt=attempt+1 " ...
# pipeline.fetch_one
165:    attempts = row["attempt"] + 1          # row 是认领前快照 → 少算 1
166:    new_status = "dead" if attempts >= MAX_ATTEMPTS else "failed"
```

- **问题**：`attempt` 认领时 +1，失败判定又用**认领前**的 `row["attempt"] + 1` → 日志说
  `失败(N/5)` 但实际 DB 已 `N+2`。`queued_books` 只取 `status='queued'`（db.py:527），
  而失败后是 `failed` → **scheduler 再也不会重试它**，只有 `manage.py retry` / Web 按钮手动重排。
  `claim_and_start` 明明允许认领 `failed/dead`（db.py:491），但没人把它们再喂回来。
- **影响**：一次网络抖动就把书永久踢出队列，需要人工"任务面板点重试"。这与
  README:143-147"从 queued 取书"的语义相符，但与"scheduler 自动跑完一个馆"的目标不符。
- **建议**：失败后 `failed` 且 `attempt < MAX` 的书，由 `run_source_heartbeat` 在队列枯竭时
  **补位**（`queued_books` 加 `OR (status='failed' AND attempt<?)`）；`attempts` 直接用
  `d.get_book(...)["attempt"]` 或让 `claim_and_start` 返回新 attempt。

#### P1-10 ｜ `sites/loc.py:203, 236, 272-284` ｜ 官方 PDF `min_bytes=100_000` 使小册/单叶**永远失败**；封面被"缓存"成半成品

- **问题**：`http.download(pdf_url, out_pdf, min_bytes=100_000)`。LoC 官方 PDF 约 200KB/页
  （DESIGN 事实 #7），但**1–2 页的散叶/单页公文只有 1–3KB 量级**，会稳定判失败；`auto` 模式会退回
  IIIF 组图（有 `page_files` 时才可行），`quality=pdf` 则**永远失败**。
- **问题**：封面下载 `self._download_cover(...).min_bytes=2_000`（286-288）。LoC 的
  `?fo=json` 里 `image_url` 分两种：`tile.loc.gov/.../default.jpg`（可能只有几百字节的小缩略图）
  与 `tile.loc.gov/.../full/pct:100.0/...`。前者 `min_bytes=2000` 直接失败 → `cover.jpg` 缺失 →
  书库网格无封面。代码没有区分。
- **建议**：`min_bytes` 改为按 `resources[].pdf` 的 `size`/`page_count` 推算（至少 1KB/页，
  下界 8KB），或干脆用"`%PDF` magic + pypdf 页数校验"代替体积阈值（代码里已有 `pdfbuild.pdf_page_count`）。

#### P1-11 ｜ `sites/base.py:107-127` + `sites/na_jp.py:300-314` ｜ 页图**永不清理**，NAS 磁盘被"缓存"吃空

- **问题**：`assemble_pages_to_pdf` 只在 `len(paths) >= len(pages) - allow_missing` 时清页图；
  否则**保留供续传**。`na_jp._download_iiif:509-518` 更严格：只有 `len(paths) == len(urls)`
  才清。缺 1 页 → 整本页图（几百页 × 1–3MB = GB 级）永久留在 `_pages_XX/`。
  若该书最终被判"OK"（只要总数够），这些页图再也不会有机会删除。
- **影响**：`assemble_pages_to_pdf` 的 docstring 说"成功才清页图，否则保留供下次续传"，
  但**没有"续传成功后清理上一次残留"的路径**——下次调用会复用这些页并清理，但有两条路走不到：
  ① 缺页的书会被判 `ok`（`ok=bool(outputs)`）不再重跑；② `na_jp` 走的是自己的 `_download_iiif`
  （不用 `assemble_pages_to_pdf`），清理条件更严。
- **建议**：新增 `cleanup_pages_dir(dest_dir)` 在 `download_item` **成功返回前**统一清理所有
  `_pages_*`；`doctor` 增加"孤儿 `_pages_*` 目录"检查。

#### P1-12 ｜ `manage.py:88-102` ｜ 破坏性批量命令**无确认、无 dry-run、无退出码**

- **问题**：`approve --collection X` 一次性把所有匹配的 `discovered` 改成 `queued`
  （实测 5 条全改），随后 scheduler 每小时开始下载，**用户无法撤销**（只能逐条 `ignore`）。
  `retry --failed` 同理（默认 `--source loc`，见 275-278）。`ignore --collection` 更是直接打入冷宫。
  `_decide` 也没有 `--dry-run`、没有 `--limit`、没有 "确认 N 条" 的交互。
- **问题**：退出码不统一：`cmd_fetch` 失败 `sys.exit(str)`（退出码 1，OK）、`cmd_import_snapshot`
  找不到快照 **`return`（退出码 0）**（49-51）、`cmd_import_details` 目录不存在 `sys.exit(str)`（1）。
  脚本化时无法区分"成功"与"没干活"。
- **问题**：`cmd_fetch` 与 `cmd_fetch_next` 的 `--quality` 未做白名单校验（`web/app.py` 有
  `QUALITIES`），传 `--quality xxx` 会进适配器 `quality in ("auto","pdf")` 判断 → 落到 `mid` 兜底，
  **静默用错档位**。
- **建议**：`argparse` 加 `choices=QUALITIES`；批量命令加 `--dry-run`/`--yes`，非 TTY 无 `--yes` 时拒绝执行；
  统一 `sys.exit(1)`；`cmd_import_snapshot` 无匹配退出 1。

#### P1-13 ｜ `digital_archives_to_pdf.py` / `official_pdf_by_cids.py` ｜ 两个根目录脚本是**遗留原型**，与 `sites/na_jp.py` 重复

- **问题**：两脚本合计 652 行，功能与 `sites/na_jp.py` 高度重叠（manifest→cid、contentDownload 分块、
  IIIF 兜底、组 PDF）。关键差异说明它们**已经过期**：
  - `official_pdf_by_cids.py:75` 用了 `json.loads`，但 **`import json` 在文件末尾 `if __name__=="__main__":`
    块内（275 行）**，作为模块导入时 `get_manifest` 必崩；`sys`/`html` 未使用（pyflakes 报告）；
  - 两脚本用**浏览器 UA**（`Mozilla/5.0 ... Chrome/124`）且 `official_pdf_by_cids.py` 用 `curl`
    子进程（绕过 `core/http` 的节流与诚实 UA）——**与"礼貌采集"的当前设计冲突**；
  - `official_pdf_by_cids.py:150,255` 硬编码 `/tmp/_vol_*` 与 `/tmp/_man_*` → 并发跑两本会互相覆盖；
  - `digital_archives_to_pdf.py:160-204` 用 `ThreadPoolExecutor(max_workers=6)` **并发下载**，
    正是 DESIGN 事实 #4 记录的"~58 次连续请求被封"的元凶；当前 `core/http` 明确改成"每域串行"；
  - `digital_archives_to_pdf.py:57-62` 无节流、无退避策略、`fetch_binary` 把整个文件读进内存
    （大 PDF 100MB+）。
  - 两者都未被 Dockerfile/README 目录树/入口引用（README:341-346 目录树里没有它们）。
- **结论与建议**：**删除**（`git rm`），需要追溯时从 git 历史取。若想保留"手动指定 cid 列表"这个
  `official_pdf_by_cids.py` 独有的能力，把它作为 `manage.py download-cids` 子命令迁进 `sites/na_jp.py`
  （复用 `core/http` 的节流与 UA），而不是留在根目录。

### P2

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| P2-1 | `sites/na_jp.py:8` | 注释说"大册之间册间停顿 2s"，全文**没有**任何册间 `sleep`（仅 417/470 行重试退避） | 删掉这句或补实现；这类"注释说谎"比没注释更危险 |
| P2-2 | `sites/na_jp.py:13` `time` + `417/470` | `m.time = ...` 在测试里能被打桩，说明模块级 `import time` 被当测试缝用；生产上 `time.sleep(min(8*att,60))` 在**同域节流之外**再叠一层，估算节律困难 | 统一用 `http.backoff(att)`，便于集中审计 |
| P2-3 | `sites/na_jp.py:522` | 文件 522 行、单类 15 个方法，混合了"发现/详情富化/下载/多卷展开/清理"四类职责 | 拆 `NaJpHarvest`（发现）+ `NaJpDownload`（下载）+ `najp_parse.py`（纯解析函数，可单测） |
| P2-4 | `sites/loc.py:208-210` | `_images` 是闭包捕获 `pages/out_pdf/pages_dir`。**实测结论：不是 bug**（定义后立即在 222/255 行调用，没有循环迭代间隔，晚绑定不成立；`tier` 还是默认参数绑定） | 但**结构脆弱**：一旦有人把调用挪到循环外/存起来延后调用就会串卷。建议改成模块级 `_images(pages, out_pdf, pages_dir, tier, ...)` 显式传参 |
| P2-5 | `sites/loc.py:162-186` | `_merge_resources` 的 `files` 双语义（整数=页数 / 列表=逐页变体）用 `isinstance` 判定，**集合级快照的整数页数**会 `pages_total += len(pages)` = 0，然后靠 180-182 兜底。逻辑正确但可读性差，且 181 行只检查 `resources[0]` | 提成两个小函数 `_parse_files_list` / `_parse_files_int`，并在 docstring 画一张 LoC JSON 结构图 |
| P2-6 | `sites/loc.py:42, sites/na_jp.py:22` 等 | 硬编码 BASE/UA/阈值散落各适配器（`100_000`、`10_000`、`2_000`、`1_600`） | 收到 `sites/base.py` 常量区或各适配器类常量，便于统一调参 |
| P2-7 | `manage.py:26-28` | 顶部 `from sites.loc import LocAdapter` + `from sites.na_jp import NaJpAdapter`，函数内又 `from sites.na_jp import NaJpAdapter`（137、215）重复导入；`from core.pipeline import create_title_links` 也在函数内（164） | 清理成顶部导入；`cmd_fetch`/`cmd_fetch_next` 的 quality 白名单见 P1-12 |
| P2-8 | `manage.py:219-222` | `enrich-na-jp` 用 `not (r["subjects"] or "").strip("[]") or json.loads(...) == []` 双条件判断空，且 **`limit=100000` 全量拉取后再内存过滤** | 一条 SQL：`WHERE subjects IS NULL OR subjects IN ('','[]')` |
| P2-9 | `manage.py:100, 124` | `cmd_fetch` 先 `fetch_one` 再 `get_book` 判状态；`fetch_one` 内部已有完整状态机，重复查询 | 直接用 `fetch_one` 返回值 + 一次查询 |
| P2-10 | `sites/bnf.py:56, 151` | `ids` 字段返回 `total_hint`（**记录总数** 1011）而非"本页 ids 数"，与 na_jp 语义（本页 id 数）不同；`skipped` 只在 `_meta_from_manifest` 成功前 +1，manifest 失败不计 | 统一两馆 `ids` 语义并在 base.py 契约里定义清楚 |
| P2-11 | `sites/na_jp.py:139` 与 `manage.py:212-239` | `fetch_item_categories` 走详情页富化，但 **scheduler 从不调用它**；只有手动 `manage.py enrich-na-jp` 才会补全分类 | 把它做成"低优先级后台任务"（如每轮心跳最后花 ≤5 次请求补齐 N 册），否则 library.html:44 的空态提示会一直挂着 |
| P2-12 | `tools/loc_snapshot.py:57, 84-87` | 用 `~/.eightnations/browser-profile` 目录名却**从未持久化** `storage_state`（`new_context(storage_state=None)` 且结束不 save）→ 每次都要人工过盾，"复用会话"的注释意图落空 | 用 `launch_persistent_context(user_data_dir=profile)`，这是 playwright 过 Cloudflare 的常见做法 |
| P2-13 | `tools/loc_fill_details.py:16` | `import re` 未使用（pyflakes） | 删 |
| P2-14 | `sites/base.py:84` | `min_bytes=10_000` 对 IIIF 页图：LoC 的 `pct:25` 小图或日文馆的小图可能 < 10KB，会被判失败并**反复重下** | 按 `width/height` 推算合理下界，或只校验"是 JPEG 且有 SOF" |
| P2-15 | `sites/bnf.py:194-199` | `parse_snapshot` 恒返回 `[]`，注释说明"仅为满足协议存在性"——**协议存在性检查实际上并不存在**（P1-6） | 见 P1-7 的能力拆分 |

---

## 2. 重点清单逐条核对（任务要求的 7 项）

| # | 核对项 | 结论 |
|---|---|---|
| 1 | 契约一致性 | `SourceAdapter` 三方法：loc/na_jp/bnf 都有 `parse_snapshot` + `download_item`，签名一致 ✅；`HarvestAdapter` **只活在文档**（P1-5）；返回字段 na_jp/bnf 都有五个键但语义不同（P2-10）；`Protocol` 非 `runtime_checkable`，`Dict[str, Type[SourceAdapter]]` 静态无效（P1-6）；bnf/na_jp 都实现了 `parse_snapshot/download_item/harvest_step` ✅，loc 无 `harvest_step`（符合 snapshot 策略，但无能力声明） |
| 2 | 调度可靠性 | 间隔漂移 ✅（P1-1，实测一轮可数小时）、sleep 不可中断 ✅（P1-1）、SIGTERM → Docker 10s SIGKILL ✅（P1-1，`.part`+Range 有兜底）、`blocked_until` 内存态 ✅（P1-2）、`KeyboardInterrupt` 在下载内部不会被逐源隔离捕获 ✅（P1-3，实测非 Exception 子类）、异常边界未覆盖"循环外回收/裁剪" ✅（P1-3）、单源隔离**基本彻底**（循环内 try 覆盖 `_source_round` 全部）✅ |
| 3 | 采集合规 | `FileThrottle` 跨进程**有效**（实测两线程被串成 2.5s 一次）✅；但①锁只覆盖"取号"不覆盖请求期（实测 B 在 A 取号后**立即**拿到号，两请求可重叠）；②同域不同子路径共用同一把锁 ✅（按 netloc）；③UA 诚实 ✅（`core/http.py:18`），但**两个根脚本用浏览器 UA**（P1-13）；④na_jp 限流预算与 `max_pages` 不闭环（P1-4）；⑤`known_uids` 全量入内存：5268 条 × ~60B ≈ 300KB，**规模无问题**（真正问题是每心跳全表 `SELECT` 一次，可接受）；⑥断点续传语义：`fetched/skipped` 在**中断后不可合并**（`fetched` 只统计本轮，`skipped` 只统计本页）→ 依赖 `exhausted` 判断进度，而它本身是错的（P0-4） |
| 4 | 适配器代码质量 | loc 闭包**不是 bug**（P2-4，附实测推理）；`_merge_resources` 双语义可读性差（P2-5）；na_jp 522 行/15 方法需拆分（P2-3）；bnf 243 行**可用**（实测 SRU 解析正确、manifest→BookMeta 正确）但 `ids` 语义不一致（P2-10）；魔数散落（P2-6） |
| 5 | manage.py CLI | 参数校验缺失（quality 无 choices，P1-12）、退出码不统一（P1-12）、批量破坏性命令无确认（P1-12）、重复逻辑（P2-7/8/9）、**`--pages` 确实被用上**（`cmd_import_na_jp:150-152` → `harvest_step(max_pages=args.pages)` ✅），但 `scheduler` **没传**（P1-4） |
| 6 | 两个根脚本 | **该删除**（P1-13），能力已并入 `sites/na_jp.py`，且存在 `json` 未在模块级导入、浏览器 UA、`/tmp` 竞态、并发下载等问题 |
| 7 | 文档一致性 | 见 §3（11 处"注释/文档说谎"） |

---

## 3. 文档与实现不一致（"注释说谎"清单）

| # | 位置 | 文档/注释 | 实现事实 |
|---|---|---|---|
| 1 | `scheduler.py:3` / `README.md:143` / `settings.html:31` | "每 15 分钟一轮心跳" | 实际 = 900s + 本轮耗时（数小时），**严重漂移** |
| 2 | `scheduler.py:57-58` | "exhausted=True 表示目录已全量处理完" | na_jp 是"本轮没到 budget"，**语义错配**（P0-4） |
| 3 | `scheduler.py:9` | "每小时配额以 jobs 表为账本（DB 写事务内原子消费）" | ✅ 成立，但 `manage.py fetch` 手动单本**不限配额**（`fetch_one(..., quota_n=None)`），与"Web 立即下载也共享窗口"（web/app.py:513 注释）不一致——**Web 手动单本也不受限**（`quota_n` 传的是 sources.hourly_quota，✅ 一致；`manage.py fetch` 不限，是两个入口语义不同） |
| 4 | `sites/na_jp.py:8` | "大册之间册间停顿 2s" | 无此代码（P2-1） |
| 5 | `sites/na_jp.py:148-154` | "单次调用请求总数 = 列表翻页(≤max_pages) + 详情(≤budget)，15+40=55 < 58" | 未计 `http.get` 的 4 次重试、未计 `_volume_ids` 的 120 次、未计 `budget` 可被环境变量放大（P1-4） |
| 6 | `sites/base.py:38` | "页级断点续传：已存在且达标的页跳过" | ✅，但"达标"= `min_bytes`（10KB），坏图也会被当"达标"跳过（P2-14） |
| 7 | `sites/base.py:90-93` | "成功才清页图，否则保留供下次续传" | 缺 1 页时**永远不清**，累计吃盘（P1-11） |
| 8 | `sites/bnf.py:196-199` | "此方法仅为满足 SourceAdapter 协议存在性" | 协议存在性**从未被检查**（P1-6），这句话描述了一个不存在的事实 |
| 9 | `README.md:29` | `sites/ndl_jp.py`、待实现、停用 | 文件不存在；sources 表却有 `ndl_jp` 且 UI 可启用（P0-5） |
| 10 | `docs/DESIGN.md:178, 218, 279, 282` | "调度器：APScheduler（进程内）"、"镜像含 apscheduler" | 代码是纯标准库 `while+sleep`，`requirements.txt` 无 apscheduler（只有 246 行 M2 段落承认"替代 APScheduler"，但文档前文没改） |
| 11 | `docs/DESIGN.md:140` | `def download_item(self, meta, dest_dir, limiter)` | 实际签名是 `(self, meta, dest_dir, http, quality="auto", progress=None)`（`sites/base.py:31-33`） |
| 12 | `core/pdfbuild.py:1` | "自 digital_archives_to_pdf.py 提炼" | 提炼完成且已独立，**根脚本应删**（P1-13） |
| 13 | `README.md:148-151` | "所有对同一域名的请求共享最小间隔 … tile.loc.gov 1s" | 实际 `_DOMAIN_MIN_INTERVAL` 为 1.0/3.0/2.5/2.0/2.0（✅ 数值对），但**"所有请求"不成立**：`na_jp._post_chunk` 直接调 `http.session.post`（用同一 throttle ✅），而两个根脚本完全绕过（P1-13） |

---

## 4. 单独一节：新增一个馆（以 `ndl_jp` 日本国立国会图书馆为例）需要改哪些文件

### 4.1 清单（按"必须改"→"容易漏"排序）

| # | 文件 | 改什么 | 漏了会怎样 | 类型 |
|---|---|---|---|---|
| 1 | `sites/ndl_jp.py`（新建） | 实现 `parse_snapshot` / `download_item` / （direct 则）`harvest_step` | 无 adapter | **硬** |
| 2 | `sites/__init__.py:8-12` | `_REGISTRY` 加 `"ndl_jp": NdlJpAdapter` + import | `get_adapter` KeyError（P0-5） | **硬** |
| 3 | `core/db.py:115` | `_DEFAULT_SOURCES` 已有 `ndl_jp` 行 —— 只需确认 `hourly_quota`/`meta_strategy`/`catalog_url` 是否正确（**注意**：`INSERT OR IGNORE`，**存量库改了种子也不会生效**，还得写迁移 `UPDATE`） | 老 NAS 库里仍是旧配置 | **硬**（且隐蔽） |
| 4 | `core/http.py:22-29` `_DOMAIN_MIN_INTERVAL` | 加 `"dl.ndl.go.jp": 2.0`（**已预埋** ✅，但 `ndlsearch.ndl.go.jp`、`www.ndl.go.jp` 没加） | 走默认 1.5s，可能过快 | 软（已有默认） |
| 5 | `web/app.py:137-142` `COLLECTION_LABELS` | 加 `collection slug → 中文标签` | 前端显示成原始 slug（"ndl-chinese"） | 软（**不报错**） |
| 6 | `web/templates/settings.html` | 无需改（按 sources 循环渲染）✅ | — | 自动 |
| 7 | `README.md:25-30` 表 / `docs/DESIGN.md:155` 表、目录树、`DESIGN.md` §9 里程碑 | 状态从"待实现"改"已接入" | 文档说谎（第 9 条） | 软 |
| 8 | `tests/test_regressions.py` | 加：`parse_snapshot` 的 fixture 解析、`harvest_step` 的 stats 契约、`registry` 完整性（"sources 表里所有 enabled 的 id 都必须能在 _REGISTRY 找到"） | 回归无从谈起 | 软（**但正是这类测试能防 P0-5**） |
| 9 | 可选：`tools/ndl_jp_snapshot.py`（若走 snapshot 策略） | — | — | 视策略 |
| 10 | 可选：`manage.py` 加 `import-ndl-jp` 之类的子命令 | — | — | 视需要 |
| 11 | `fixtures/ndl_jp/*.json` | 真实响应样本 | 无法离线测 | 软 |

**合计：必须改 3 处硬耦合（adapter / registry / 种子+迁移），外加 5 处"漏了不报错"的软耦合（限流表、COLLECTION_LABELS、两处文档、测试）。**

### 4.2 扩展性评估：为什么说"能扩，但扩得危险"

**做得好的地方**
- 下载内核抽象得不错：`assemble_pages_to_pdf` + `write_meta_json` + `iiif_size_url` 三件套让
  `loc`/`bnf` 的 download 分别只有 ~95 / ~40 行，**新增一个 IIIF 馆的成本确实低**。
- `BookMeta` 是单一数据结构，`row_to_meta` 往返完备，适配器只需产出 dataclass。
- 配置在 DB（`sources` 表）而非代码，`enable/quota/quality/catalog_url` 可在 UI 调，**运维侧扩展性好**。

**做得危险的地方（按危害排序）**
1. **能力没有声明**：`parse_snapshot` 被列为必选，但 bnf 只返回 `[]`；`harvest_step` 是可选，
   但"可选"这件事既不在类型里也不在数据里 → scheduler 靠 `getattr` 猜。**能力应显式声明**
   （见 4.3）。
2. **注册表没有自检**：`_REGISTRY` 与 `sources` 表是两份独立真相，没有任何代码或测试交叉验证。
   `ndl_jp` 就是活证据：表里有、代码里没有，而且**用户能启用**。
3. **契约是注释不是代码**：`HarvestAdapter` 文档写了"返回值必须含 5 个键"，没任何东西保证它——
   P0-3/P0-4 都是这个缺口的下游后果。
4. **限流是每馆自己拿捏的"手艺"**：`_DOMAIN_MIN_INTERVAL` 是全局表（尚可），但
   "单次调用请求预算"完全由各适配器在 docstring 里心算（na_jp 15+40=55、bnf 10 页×50 条）；
   没有运行时计数器，也没有 `EIGHTNATIONS_CATALOG_BUDGET` 与它的联动校验。
5. **新增馆要碰 `core/db.py`**：`_DEFAULT_SOURCES` 硬编码在 core 里，`INSERT OR IGNORE` 还不支持
   升级已有行 → 种子数据应该由 `sites` 自己声明，`core.db` 只负责"合并"。

### 4.3 重构建议（可落地的三步）

**第 1 步：能力声明 + 启动自检（~50 行，立竿见影）**

```python
# sites/base.py
class AdapterCapability:          # 用 enum/Enum 都行
    SNAPSHOT = "snapshot"         # parse_snapshot 可用
    HARVEST  = "harvest"          # harvest_step 可用

class BaseAdapter:                # 真 ABC/基类（不再是 Protocol）
    id: ClassVar[str]
    name: ClassVar[str]
    flag: ClassVar[str]
    capabilities: ClassVar[frozenset] = frozenset()
    seed: ClassVar[dict] = {}     # sources 表的默认行（id/name/flag/enabled/quota/quality/strategy/catalog_url）
    request_budget_per_call: ClassVar[int] = 40   # 声明式限流预算

    def __init__(self, http): self.http = http
    def __init_subclass__(cls, **kw):
        _REGISTRY_CLASSES[cls.id] = cls       # 自动注册，不用手改 __init__.py
```

```python
# sites/__init__.py —— 只留一行 import 扫描
from sites import loc, na_jp, bnf, ndl_jp    # noqa: F401  各模块末尾 BaseAdapter 自动注册
def validate_registry(d: DB) -> list[str]:
    """启动期/CI 断言：sources 里 enabled=1 的源必须有 adapter；capability 与 meta_strategy 匹配。"""
```

```python
# scheduler.main() 开头
for warn in validate_registry(d):
    print(f"[scheduler] ⚠️ {warn}"); d.log(warn, level="warn", source="scheduler")
```

**第 2 步：`sources` 种子由适配器提供，core.db 只做合并**

```python
# core/db.py init():  for cls in BaseAdapter.registry().values():  _merge_source_row(conn, cls.seed)
# _merge_source_row 用 INSERT ... ON CONFLICT(id) DO UPDATE SET （只更新"缺失/空"字段），
# 这样存量库也能拿到新馆与新默认 catalog_url，不用再写一次性 UPDATE 补丁（现有 db.py:186-189 就是这种补丁）
```

**第 3 步：契约可验证 + 测试兜底**

- `@runtime_checkable`，并在 `get_adapter` 里 `assert isinstance(adapter, source_protocol_for(strategy))`；
- `tests/test_regressions.py` 增加 3 条：
  1. `set(sources.enabled ids) ⊆ set(_REGISTRY)`（防 P0-5）；
  2. 每个 adapter 的 `harvest_step` 必须返回 `{ids,fetched,skipped,blocked,exhausted}` **且类型正确**（防 P0-3）；
  3. `harvest_step` 的 `exhausted=True` 必须伴随"翻到最后一页"证据（用 FakeHttp 构造只有 1 页但 100 条的场景）——防 P0-4。

**第 4 步（可选，收益最大）：把"发现层"抽成独立可测组件**

当前所有发现逻辑（HTML 正则抽 id、SRU 解析、翻页游标）都埋在 adapter 的 `harvest_step` 里，
无法单测语义。建议引入：

```python
@dataclass
class HarvestItem: vid: str; label: str; extra: dict
class CatalogSource(Protocol):        # 只做"列条目 + 翻页"
    def iter_items(self, url, cursor) -> Iterator[HarvestItem]
    def next_cursor(self) -> Optional[str]
```

`harvest_step` 变成 20 行的**通用编排**（取 items → 过滤 known → 详情 → budget → 返回 stats），
每馆只实现 `iter_items`。这样 `exhausted/budget/blocked` 的语义**全局一致**，P0-4 这类错误结构上不可能再出现。

---

## 5. 采集层整体评价（分维度打分，10 分制）

| 维度 | 分数 | 依据 |
|---|---|---|
| **接口设计（可扩展性）** | 6.0 | `assemble_pages_to_pdf`/`write_meta_json` 复用到位；但 Protocol 形同虚设、能力无声明、注册表与 sources 双真相（§4.2） |
| **调度可靠性** | 4.5 | 心跳漂移/不可中断/冷却内存态/回收被跳过/失败不再入队；好在有 WAL + 租约 + 页级断点三层兜底，崩溃不丢已完成工作 |
| **下载正确性** | 4.0 | na_jp 分块无事务语义（P0-1/2）、`ok=bool(outputs)` 弱成功、孤儿文件；LoC/BnF 走公共组图路径相对健壮 |
| **采集合规** | 7.0 | 诚实 UA、每域间隔+抖动、flock 跨进程、限流后冷却、两级预算设计意识好；但预算账本不闭环、两脚本用浏览器 UA、缺 robots 参考 |
| **错误处理与可观测性** | 5.5 | 隔离粒度对（单源），但边界位置错（回收在圈外）、异常文本一律 `{e}` 无堆栈、`KeyError` 未分类 |
| **CLI 可用性** | 5.5 | 子命令齐全、`--help` 友好；但破坏性操作无确认、退出码不统一、quality 无白名单 |
| **代码质量/可维护性** | 5.0 | na_jp 522 行单类、硬编码散落、两脚本遗留、注释多处与实现不符（13 处） |
| **文档一致性** | 3.5 | README 与代码整体对得上，但 DESIGN 仍写 APScheduler、签名过时、na_jp 限流账本自述与实现不符 |

**综合：约 5.4 / 10** —— 设计意图清晰、公共内核抽得好，但**采集层最核心的两件事（分块下载的事务性、目录进度的正确性）都没有闭环**，
且扩展性依赖"记得改 9 个地方"。

---

## 6. Top 5 必须改

| 序 | 问题 | 位置 | 一句话修法 | 预估工作量 |
|---|---|---|---|---|
| **1** | na_jp 分块下载跨卷串页 / 已完成卷被丢弃 / 孤儿 PDF | `sites/na_jp.py:377,400-443` | `parts` 每卷局部；失败不 `return 0,[]` 而是 `break` 并返回已完成卷；`out.part` + `os.replace`；`if parts` 加 `len == chunks` | 0.5 天 |
| **2** | `exhausted` 语义错配 → 5268 件的目录被 7 天封存，全量入库需 ~370 周 | `sites/na_jp.py:180,220` + `scheduler.py:100-103` | `exhausted` = "翻到最后一页且无遗漏"；引入持久化 `catalog_cursor`；`stats` 归一化后再判断 | 0.5 天 |
| **3** | 未注册的 `ndl_jp` 可被 UI 启用 → 每心跳刷异常日志 + 跳过 `reap_stuck` + 上传快照 500 | `core/db.py:115`、`settings.html:37`、`scheduler.py:140`、`core/importer.py:24` | 能力声明 + `validate_registry()` 启动自检 + 三处统一捕获 `AdapterNotRegistered`；或暂不种 `ndl_jp` | 0.5 天 |
| **4** | 调度器心跳漂移 + 无优雅退出 + 冷却不持久 | `scheduler.py:127-163, 123` | `threading.Event().wait()` 固定节拍 + SIGTERM 优雅退出（job 标 interrupted 不计 attempt）+ `sources.blocked_until` 持久化指数退避 | 1 天 |
| **5** | 限流预算不闭环（na_jp 自述 55<58，实际最多 220+120 次/轮） | `sites/na_jp.py:148-154,261-281`、`core/http.py:138`、`scheduler.py:178` | `HttpClient` 加请求计数器/预算上下文；`harvest_step` 增加 `request_budget`；`scheduler` 传 `max_pages` 并对 `CATALOG_BUDGET` 做上限校验 | 1 天 |

> 其余 P1（upsert 空值覆盖、封面丢失、页图不清理、失败书不再入队、CLI 破坏性命令）建议紧随其后，
> 与 Top5 一起构成"采集层可用性"的一个迭代。
