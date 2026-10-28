# 审查 D：测试质量 / CI / 容器部署 / 运维脚本 / 文档一致性

> 审查人：ops-reviewer（代码审查 D）
> 审查时间：2026-10-27
> 审查范围：`tests/`、`fixtures/`、`requirements.txt`、`Dockerfile`、`docker-compose.yml`、
> `entrypoint.sh`、`migrate-nas.sh`、`.github/workflows/docker-publish.yml`、
> `.dockerignore`、`.gitignore`、`README.md`、`docs/DESIGN.md`、`docs/READER-DESIGN.md`、
> `docs/PLAN-20260919.md`
> 结论一律基于**实际运行/变异实验**，不做臆测。所有实验在 `/tmp` 或 `/tmp` 下的仓库副本中进行，
> 未修改任何业务代码，未 `git commit`，`pip install` 只装进 `/tmp/_d_venv`（不污染系统环境）。

---

## 0. 执行摘要（先看这段）

| 维度 | 结论 | 评分 |
|------|------|------|
| 测试 | 13 组全过（0.37s），但对**用户可见的功能面零覆盖**；且断言比它自己声称的弱 —— 8 次关键变异中 5 次**测不出** | 55 / 100 |
| CI | 结构正确（test → build-push 依赖链、双架构 manifest、buildx 缓存、concurrency 防 latest 回退），但**PR 完全不跑**、actions 未 pin SHA、test job 断言比 build 环境宽 | 68 / 100 |
| 容器 | Dockerfile 干净（无 VOLUME 是有理由的决策），但 root 运行 + healthcheck 恒真 + `web.log` 轮转是死代码 | 62 / 100 |
| 运维脚本 | `migrate-nas.sh` 的**破坏性操作路径本身有防护**（先拷后删、校验 PDF 数量），但**`DEST` 参数无白名单校验**、**无 `--dry-run`**，且 `rm -rf`/`tar -x` 的目标由未校验的 `DEST` 拼出（实测 `DEST=/` → `/ .migrate-staging`）；比它更严重的是 **README 教用户 `curl -O` 后直接 `sh` 执行这个以 root 运行的脚本，却不 pin 版本、不校验 sha256** | 50 / 100 |
| 文档 | 抽查 15 处，**6 处与代码不一致**（含 1 处会直接误导故障恢复的关键错误、1 处 API 清单整段是未实现的旧设计） | 52 / 100 |

**最关键的三条结论**

1. **「13 组回归全过」给了虚假安全感。** 变异实验证明：把 `core/importer.py` 里合并 item 详情的那一行删掉（官方 PDF 直链、逐页清单**全部丢失**）、把持久配额窗口从 1 小时缩成 1 分钟、把硬链接幂等判定改成永远"新建"、把 `download()` 的"已存在文件须达 min_bytes"改成"存在即算完成"——**测试依然全绿**。现有测试守不住"数据正确性"这条最贵的回归线。近期连续修 XSS / 双写 / 截断残片完全符合这个预测：这些 bug 落在测试的盲区里。
2. **CI 在执行层面可被绕过。** ① 没有 `pull_request` 触发，PR 红灯只能在本地发现（P1-D8）；② `latest` 的语义是"最后一次成功构建"而非"最新 tag"，tag 构建与 main 构建之间没有顺序保证，于是 **NAS 的 `restart: unless-stopped` 会在无人确认的情况下被拉到未经人工验证的版本**（P1-D9）；③ action 只 pin 到 tag，`tj-actions/changed-files` 那类"tag 被改写"的供应链事故对它直接有效（P1-D10）。
3. **部署侧真正的单点风险不在 Dockerfile，而在文档里的 `curl -O` + `sh`。** `migrate-nas.sh` 会：停容器、`docker rm -f` 容器（级联删匿名卷）、以 root 写宿主 `/share`。而 README:236-243 让用户 `curl -O` 后直接 `sh migrate-nas.sh`，脚本本身既不支持 `--dry-run` 也不校验 `DEST` 参数。只要把「从 `main` 改成 pin tag/commit + sha256 校验」+「脚本内加 DEST 白名单校验 + `--dry-run`」两件事做掉，风险就降一个数量级 —— 加固 diff 见 §5.2。

---

## 1. 测试质量（重点）

### 1.1 「测了什么」清单（逐条核对 `tests/test_regressions.py`）

实测：`python3 tests/test_regressions.py` → **13 组全过，`real 0.37s`，退出码 0**；
一组运行**泄漏 3 个临时目录，约 724KB（每次运行）**（`tempfile.mkdtemp()` 无 `finally` 清理）。

| # | 行号 | 覆盖点 | 严重性价值 | 备注 |
|---|------|--------|-----------|------|
| 1 | 37-42 | 快照导入 + era/subjects 多值筛选 | 高 | 用真实 1.0MB fixture（**已核实不是空文件**），`new=41` ✓ |
| 2 | 45-51 | `jp2t` 日文新字体转换 | 中 | 4 条断言 |
| 3 | 54-63 | na_jp 分类富化 | 中 | 手工构造 HTML |
| 4 | 66-90 | na_jp 收割器预算/限流中止 | 高 | FakeHttp，离线，设计良好 |
| 5 | 93-102 | BnF SRU 解析 | 中 | **带 `if os.path.exists` 跳过分支 → CI 里缺 fixture 会静默通过** |
| 6 | 105-109 | 挂载检测 + 迁移指引 | 低 | 断言 `data_mount()["kind"] in (...)`，三个取值全覆盖 → **恒真断言** |
| 7 | 112-128 | 流式 PDF（连续/非连续页） | 高 | 真实 PIL 生成 JPEG |
| 8 | 131-149 | jobs 台账小时配额 + 原子认领 | 高 | 断言质量好（含"满额不扣不脏写"） |
| 9 | 152-187 | 租约回收 reap_stuck | 高 | 用真实已退出 PID，思路正确 |
| 10 | 190-215 | 落盘路径段校验（穿越防御） | 高 | 含 `\x00`、`a\b`、`..` |
| 11 | 218-265 | 下载完整性（chunked/Content-Length） | 高 | 用真实 `requests` 异常类 |
| 12 | 268-278 | build_pdf 原子性 | 高 | 断言 `.part` 也被清理 |
| 13 | 281-287 | 书名硬链接幂等 | **名义高、实际低** | **空库上跑，`created` 恒 0 → 断言退化为 `0 == 0`**（见 1.3-E） |

### 1.2 「没测什么」清单（按风险排序）

| 缺口 | 证据 | 风险 |
|------|------|------|
| **web 路由 / 模板渲染 —— 零测试** | `grep -cE "TestClient\|web\.app\|templates\|jinja" tests/…` → **0** | 高。XSS 收口、`/settings` 渲染、模板变量缺失全无防护 |
| **API 输入校验 —— 零测试** | 同上 | 高。近期还在修"进度 API 不存在 book_id 返回 404""输入白名单" |
| **前端 JS / reader.js —— 零测试** | `grep -c "reader\.js\|pdfjs"` → 0 | 中高。`reader.js` 19KB，是用户停留最久的界面 |
| **并发 / 多进程 —— 零测试** | 无 `multiprocessing`/`concurrent.futures`/多连接 | 高。项目架构核心就是"3 个进程共用一个 SQLite"，是 bug 高发区 |
| **`create_title_links` 在非空库上的行为** | 测试库是空库，`done` 书目为 0 | 高。这个函数唯一的目的是处理**已有**数据 |
| **`core/doctor.py` 断言** | 无 | 中。doctor 是 NAS 运维唯一自检手段 |
| **`manage.py` CLI 退出码 / 参数校验** | 无 | 中。脚本化运维依赖退出码 |
| **`importer.merge_item_details` 的正确性** | 只间接经过（而且间接路径也不校验结果） | **高**：官方 PDF 直链、逐页清单全靠它 |
| **`_sanitize_filename` 边界** | 只测 1 条 | 中。Windows/NAS 文件名、超长名 |
| **跨进程节流 `DomainThrottle` 真实 flock** | 被 `_NoWait` 桩替换 | 中。防封禁的核心机制 |
| **Adapter 协议一致性**（3 个适配器签名一致） | 无 | 中。新增第 4 馆时无护栏 |

### 1.3 变异测试实验结果（这是本报告最硬的证据）

方法：把仓库复制到 `/tmp/_mut`，用 Python 脚本对源码做**最小语义等价类破坏**，然后跑
`python3 tests/test_regressions.py`，看退出码。**「exit=0」= 测试漏掉了这个回归。**

| # | 变异内容（模拟真实回归 bug） | 测试结果 | 判定 |
|---|------------------------------|---------|------|
| M1 | `download()` 的"已存在文件须达 `min_bytes`"改成"存在即算完成" | ✅ 全绿 | **漏** —— 正是"错信半成品"这一类 bug，测试抓不到 |
| M2 | `claim_and_start` 配额窗口 `timedelta(hours=1)` → `timedelta(minutes=1)` | ✅ 全绿 | **漏** —— 每小时配额名存实亡，无测试 |
| M3 | `_title_link` 的 `return "exists"` → `return "new"`（非幂等） | ✅ 全绿 | **漏**，而且输出还印着"✓ 书名硬链接幂等" |
| M4 | `core/importer.py` 删掉 `LocAdapter._merge_resources(...)`（PDF 直链/逐页清单全丢） | ✅ 全绿 | **漏** —— 影响最大的数据丢失路径 |
| M5 | `count_books` 的 `subjects` 分支恒 false | ❌ 抓到了 | 有效 |
| M6 | `_dest_dir` 放宽反斜杠/NUL 校验 | ❌ 抓到了（`应拒绝 uid='a\\b'`） | 有效 |
| M7 | `sites/loc.py` 的 era 识别彻底失效 | ❌ 抓到了 | 有效 |
| M8 | `pdfbuild` 去掉 `os.replace` 原子提交 | ❌ 抓到了（NameError，属间接暴露） | 半有效 |

**命中率 3/8（37.5%）**，而这 8 个变异全部是我从"近期修过的 bug 类型"倒推出来的
——也就是说，**这 8 个 bug 都可以在测试全绿的情况下重新出现。**

### 1.4 问题清单

#### P1-D1｜测试是脚本式而非 pytest，`pytest` 收集到 0 个测试却退出码 0

- **位置**：`tests/test_regressions.py:1-290`（全文无 `def test_*` / 无 class）；实测
  `python3 -m pytest tests/ -q` → `no tests ran in 0.29s`，**exit=0**
- **问题**：全部逻辑写在模块顶层，无函数、无 `if __name__ == "__main__"` 守卫。
- **影响**：
  1. **无法选择性运行**（改 `pdfbuild` 想只跑 PDF 那组？做不到，只能全跑，且排名不分先后）；
  2. **失败定位差**：只有裸 `AssertionError` + 行号，没有"哪组失败/为什么失败/期望 vs 实际"；
  3. **最危险的一条**：任何 CI 或本地习惯了 `pytest tests/` 的人**看到绿灯，但一个测试都没跑**。
     这是一个"假绿灯"装置 —— 未来有人把 CI 从 `python tests/test_regressions.py` 改成
     `pytest`（很自然的"现代化"重构），测试会**静默失效**。
  4. pytest 不可用时还能用 `unittest`，但当前连这一点都拿不到。
- **建议**（渐进式，不必一次重构）：

  ```python
  # 最小改法：给每组包一层函数 + main 里显式调用，兼顾"按文件跑"和"能选跑"
  def test_download_integrity():  ...
  def test_claim_and_quota():     ...

  GROUPS = {  # 组名 → 函数；支持 python3 tests/test_regressions.py download
      "download": test_download_integrity,
      "quota": test_claim_and_quota,
  }

  if __name__ == "__main__":
      only = sys.argv[1:]
      failed = []
      for name, fn in GROUPS.items():
          if only and name not in only:
              continue
          try:
              fn(); ok(name)
          except Exception:
              failed.append(name)
              traceback.print_exc()          # 关键：保留堆栈
      print(f"\n== {len(GROUPS)-len(failed)}/{len(GROUPS)} 组通过 ==")
      sys.exit(1 if failed else 0)
  ```

  并在 CI 里加一道"防假绿灯"守卫：

  ```bash
  # 若将来改用 pytest，至少保证至少收集到 N 个测试
  pytest tests/ -q --co | grep -qE '^[0-9]+ (tests? )?collected' || { echo "pytest 收集到 0 个测试"; exit 1; }
  ```

#### P1-D2｜断言质量低于它自己声明的严格度（6 处具体实例）

- **位置与实例**：

  | 位置 | 断言 | 为什么是弱断言 |
  |------|------|----------------|
  | `tests/test_regressions.py:286` | `assert c2 == 0` | 测试库是**空库**（`done` 书目 0 本），`create_title_links` 两次都返回 `(0, 0)`。**变异 M3 证明了它对非幂等完全免疫。** 输出"✓ 书名硬链接幂等"属于自我表扬 |
  | `:107` | `assert data_mount()["kind"] in ("bind","volume","unknown")` | 三个取值覆盖了函数的**全部**返回面 → 恒真 |
  | `:39` | `assert d.count_books(status="discovered", era="明,清") >= 40` | 用 `>=` 而不是 `== 41`；且**只测"多值并集有结果"，不测"并集≠交集"**（`era=明,清` 若被错误实现成 AND，结果仍是 41，测试无感） |
  | `:40` | `assert d.count_books(..., era="unknown") >= 0` | `>= 0` 是最弱的断言（计数非负恒成立），等于没断言 |
  | `:38` | `assert new >= 40` | fixture 实际 `new=41`；若导入器**漏掉 1 条**仍绿（容差 1） |
  | `:96-102` | `if os.path.exists(fx): ... else: print("跳过")` | **fixture 缺失时静默跳过**，CI 绿 |

- **影响**：读者（包括 Lead、包括未来的自己）从输出"13 组全过"推断出的保障，比真实保障强得多。
- **建议**：把弱断言改成精确值，并补"非空库"前置：

  ```python
  # 书名硬链接幂等：先造 1 本 done 书 + 磁盘上的 book.pdf，再来
  uid = "link-probe"
  d.upsert_book("loc", {"source_uid": uid, "collection": "c", "title": "永樂大典卷六十八"})
  row = d.find_book("loc", uid)
  d.set_status(row["id"], "done")
  dest = _dest_dir("loc", "c", uid); os.makedirs(dest, exist_ok=True)
  open(os.path.join(dest, "book.pdf"), "wb").write(b"%PDF-1.4\n")   # 只需存在
  c1, _ = create_title_links(d)
  assert c1 == 1, f"首跑应新建 1 条链接，实际 {c1}"                  # 不再是 0
  c2, s2 = create_title_links(d)
  assert c2 == 0 and s2 >= 1                                        # 二跑 0 新建

  # era 多值：并集语义必须严格大于单值
  n_ming = d.count_books(status="discovered", era="明")
  n_qing = d.count_books(status="discovered", era="清")
  n_both = d.count_books(status="discovered", era="明,清")
  assert n_both == 41 and n_both >= max(n_ming, n_qing)
  # unknown 要造一个 era='' 的样本再断言精确值
  ```

  并把 `:96` 的"缺 fixture 就跳过"改成 `assert os.path.exists(fx), "fixtures/bnf_sru_sample.xml 丢失"`。

#### P1-D3｜测试无清理逻辑，每次运行泄漏临时目录

- **位置**：`tests/test_regressions.py:31`、`:116`、`:251`（三处 `tempfile.mkdtemp()`，无 `finally`）
- **实测**：单次运行新增 3 个目录共 ~724KB（`20K + 200K + 504K`），失败时退出更早、泄漏更多。
- **影响**：CI runner 是一次性的无所谓；**本机反复跑会把 `/tmp` 越堆越多**（我这次审查前后已观察到同形状的历史残留多份）。不是安全问题，是卫生问题。
- **建议**：

  ```python
  import shutil, atexit, tempfile
  _PROBE = tempfile.mkdtemp(prefix="e8n-test-")
  atexit.register(shutil.rmtree, _PROBE, ignore_errors=True)   # 正常/异常退出都清
  tmp = _PROBE          # 替换掉所有裸 mkdtemp()
  t   = os.path.join(_PROBE, "pdf"); os.makedirs(t)
  tdir= os.path.join(_PROBE, "dl");  os.makedirs(tdir)
  ```

  用 `prefix="e8n-test-"` 的好处：将来 `rm -rf /tmp/e8n-test-*` 一条命令可清全部残留，也不会误删别人的目录。

#### P2-D4｜CI 的 test job 与 build 环境不一致（Python 版本 / 依赖集合）

- **位置**：`.github/workflows/docker-publish.yml:26-36`（test 用 `python-version: "3.12"` + `pip install -r requirements.txt` + `pillow`）
  vs `Dockerfile:1,11`（`python:3.12-slim` + 同一份 requirements）
- **问题**：版本号一致 ✓，但 **依赖解析结果不保证一致**（无 lock / 无 hash，见 P1-D5）。test job 在
  ubuntu-latest 上解析到的可能是 `fastapi 0.162 / starlette 1.10 / pypdf 6.21`（我本机实测就解析出了这套），
  而镜像构建是**另一次独立解析**。两次可以不同。
- **实测补充**：我用 `python:3.12` 等价环境（本机 `/opt/homebrew/bin/python3.12` + `/tmp/_d_venv` 装最新依赖）
  跑了回归 + 起服务冒烟，**当前依赖解析确实能跑**（13 组全绿，`/`、`/explore`、`/review`、`/jobs`、
  `/settings`、`/api/*` 全部正常响应）。所以这不是**当前**的故障，而是**未受控的漂移风险**：
  哪天某个上游发大版本，test job 和镜像会各错各的。
- **建议**：见 P1-D5（加 `requirements.lock` + `pip install --require-hashes`），并在 CI 里把
  "test job 装的依赖集合"打印出来（`pip freeze > deps.txt` 作为 artifact），构建 job 复用同一份 lock。

#### P2-D5｜`requirements.txt` 无锁、无 hash，构建不可复现

- **位置**：`requirements.txt:12-22`（全部 `>=x,<y` 区间，无 hash）；仓库无 `requirements.lock`/`constraints.txt`
- **影响**：
  1. 同一 commit 在不同时间构建出**不同的镜像**（"昨天还好，今天重建就炸"）；
  2. 无 `--require-hashes` → PyPI 侧任何一次账号/包投毒都直达生产镜像（NAS 是 7×24 自启）。
- **建议**：

  ```bash
  # 生成锁（在 CI 与本机都用同一条命令，锁文件入库）
  pip install pip-tools
  pip-compile --generate-hashes --output-file=requirements.lock requirements.txt

  # Dockerfile 改成
  COPY requirements.lock ./
  RUN pip install --no-cache-dir --require-hashes -r requirements.lock
  ```

  顺带把上界收一收：`fastapi>=0.110,<1` 实际会装到 `0.162`（跨度 52 个小版本），
  `pypdf>=3.0,<7` 跨度 4 个大版本。建议按**当前验证过的版本**设窄区间（如 `fastapi>=0.110,<0.120`），
  要升级就显式升并跑回归。

#### P2-D6｜fixture 只覆盖 LoC 的"好路径"，`item_*.json` 的合并结果无人校验

- **位置**：`tests/test_regressions.py:37`（只断言条数）；`fixtures/loc/item_2018421651.json`（199KB）
- **实测**：导入后 `uid=2018421651` 的 `pdf_urls` 有官方 PDF 直链、`files_json` 有 96 页 —— **说明合并逻辑当前是工作的**，
  但测试**只数了个总数**，所以 M4 变异（删掉合并）也能通过。
- **建议**：

  ```python
  r = d.find_book("loc", "2018421651")
  assert r and json.loads(r["pdf_urls"]), "条目详情未合并：官方 PDF 直链丢失"
  pages = json.loads(r["files_json"] or "[]")
  assert pages and len(pages[0]) >= 90, f"逐页清单异常: {len(pages[0]) if pages else 0}"
  assert json.loads(r["subjects"] or "[]"), "item 详情的主题词未合并"
  ```

#### P2-D7｜`fixtures/` 体积与隐私

- `fixtures/` 共 1.2MB 已入库（`collection_yongle_full.json` 1.0MB + `item_2018421651.json` 199KB + BnF XML 21KB），
  对 git 可接受。但需注意：这是**从原馆抓的真实数据**，含原馆 URL/权利声明，属公有领域数据，无隐私问题 ✓。
  建议在 `fixtures/README.md` 里写明来源 URL 与抓取时间，便于将来判断"fixture 过期"与"解析器回归"的区别。

---

## 2. 实际运行验证

### 2.1 环境

| 项 | 结果 |
|----|------|
| 本机 Python | **3.9.9**（`/Library/Frameworks/Python.framework/Versions/3.9`） |
| 容器 Python | **3.12**（`Dockerfile:1`） |
| 是否有第二个解释器 | 有：`/opt/homebrew/bin/python3.12`（3.12.13） |
| pytest / pyflakes | **本机都装了**（pytest 8.4.2 / pyflakes 3.4.0）—— 任务书里"若无 pytest"的前提不成立，可以真跑 |
| 已装运行依赖 | `requests 2.32.5 / pypdf 6.16.2 / PIL 11.3.0 / fastapi 0.111.0 / uvicorn 0.30.1 / jinja2 3.1.6 / python-multipart 0.0.20` |
| **环境缺口** | 本机 3.9 环境的依赖**版本与容器不一致**（fastapi 0.111 vs CI 解析到的 0.162）；`/opt/homebrew/bin/python3.12` 无任何依赖。为验证 3.12 + 最新依赖，我在 `/tmp/_d_venv` 建了隔离 venv（**未动系统环境**），安装 `requirements.txt` + `pillow` 成功 |

### 2.2 命令与结果

| 命令 | 结果 | 耗时 |
|------|------|------|
| `python3 tests/test_regressions.py` | **13 组全过，exit 0** | `real 0.37s / user 0.21s / sys 0.11s` |
| `python3 -m pytest tests/ -q` | **`no tests ran`，exit 0** ← 假绿灯（P1-D1） | 0.29s |
| `python3 -m pytest tests/test_regressions.py -q` | **`no tests ran`，exit 0** | 0.26s |
| `python3 -m compileall -q core web sites tools scheduler.py manage.py tests` | **无输出，exit 0** ✓ 复核通过 | — |
| `/opt/homebrew/bin/python3.12 -m compileall -q …` | **exit 0** ← 3.12 语法兼容 ✓ | — |
| `python3 -m pyflakes core web sites tools scheduler.py manage.py tests *.py` | **exit 1，5 条警告**（见下） | — |
| `/tmp/_d_venv/bin/python tests/test_regressions.py`（3.12 + 最新依赖） | **13 组全过，exit 0** | — |
| 用 `/tmp/_d_venv` 实起 uvicorn 冒烟 | `/api/health` 200、`/`、`/explore`、`/review`、`/jobs`、`/settings`、`/api/stats`、`/api/doctor` 全部 200/正常 JSON；`/api/bookmarks`（缺参）422、`/read/abc` 422 —— 输入校验在，行为正常 | — |

### 2.3 pyflakes 结果（可 1 分钟修掉，建议直接进 CI）

```
core/mounts.py:9:1: 'os' imported but unused
core/doctor.py:84:5: 'core.mounts.migration_commands' imported but unused
tools/loc_fill_details.py:16:1: 're' imported but unused
official_pdf_by_cids.py:30:1: 'sys' imported but unused
official_pdf_by_cids.py:30:1: 'html' imported but unused
```

> 前两条在 `core/` 里，可能与别的审查员（A）交叉，这里只记录事实：**仓库当前不干净**，
> 因此"加 pyflakes 到 CI"必须先清掉这 5 条（否则 CI 立刻红）。

### 2.4 幂等性 / 可重复性

- **可重复**：连续跑两次结果一致 ✓（测试库每次新建，`new=41` 稳定）。
- **不污染仓库** ✓：只从 `fixtures/` **读**，`data/` 未被写（`git status --short` 空）。
- **污染 `/tmp`** ✗：每次 3 个目录 ~724KB 泄漏（P1-D3）。
- **依赖不存在的路径** ✗ 有一处：`:96` 的 fixture 缺失分支静默跳过（P1-D2 表内）。
- **一条副作用**：导入 `core.pipeline` 时会在 `EIGHTNATIONS_DATA` 下建目录吗？实测**不会**
  （`BOOKS_DIR` 只是常量拼接，`_dest_dir` 不 `makedirs`）；但 `DB(...).init()` 会建 db 目录 —— 在 `/tmp` 内，可接受 ✓。

---

## 3. CI/CD（`.github/workflows/docker-publish.yml`）

### 做对了的（先肯定）

- `test` job 前置 + `build-push: needs: test` → 测试红则不发镜像 ✓
- 双架构**在同一次 buildx 调用**里构建，产出 manifest list（注释解释得很清楚，这个设计判断是对的）✓
- `cache-from/to: type=gha,mode=max,scope=buildx-multiarch` ✓
- `concurrency: cancel-in-progress: false` 防止旧 commit 把 latest 覆盖回旧版本 ✓
- `permissions:` 显式声明且最小化（`contents: read` + `packages: write`，没有无脑 `write-all`）✓

### 问题

#### P1-D8｜无 `pull_request` 触发 —— PR 阶段零检查

- **位置**：`.github/workflows/docker-publish.yml:3-7`（只有 `push: branches:[main]`、`push: tags`、`workflow_dispatch`）
- **影响**：唯一的自动化门禁在 **main 上 push 之后**才跑。也就是说：**红灯出现时代码已经在 main 上了**，
  而 build-push 依赖 test，于是 main 会停在"有 bug 的代码 + 冻结的镜像"状态。多人协作/多分支时这是硬伤。
- **建议**：拆成 `ci.yml`（PR + push 都跑 test，不 push 镜像）+ `docker-publish.yml`（仅 main/tag）。
  最小改动（不拆文件）：

  ```yaml
  on:
    push:
      branches: [main]
      tags: ["v*"]
    pull_request:            # ← 加这一段
      branches: [main]
    workflow_dispatch:

  jobs:
    test:
      # 同现有内容
    build-push:
      needs: test
      if: github.event_name != 'pull_request'    # PR 只跑测试，不发镜像
  ```

#### P1-D9｜`latest` 是"最后一次成功构建"，而非"最新 tag" —— NAS 会被静默回退/前滚

- **位置**：`.github/workflows/docker-publish.yml:68-70`
  ```yaml
  type=raw,value=latest,enable={{is_default_branch}}
  type=ref,event=tag
  ```
- **问题**：`latest` 在**每次 main 构建**时都被重写；而 `v*` tag 构建**也**会因为
  `docker/build-push-action` 的 manifest 推送顺序而成为"当时最新的 latest"。
  两条流水线（tag 与 main）之间**没有 ordering 保证**。
  具体事故链：`手推 v0.1` → tag 构建把 latest 指向 v0.1 的镜像 → 之后随手并一个 docs commit 到 main →
  main 构建把 latest 指向 docs commit（版本号仍是"v0.1 之后"，但 `docker inspect` 看不出）→
  NAS `docker compose pull && up -d` **无提示地换掉运行版本**。反向亦然：若有人在打完 tag 后发现 bug 回滚 main，latest 仍指向含 bug 的 tag 镜像。
  再叠加 `restart: unless-stopped`，**事故自动生效、无人确认**。
- **影响**：生产（NAS 上的书库）跑着无人审查过的版本；出问题后无法从镜像 tag 反推代码版本。
- **建议**：
  1. **别把版本身份丢掉**：加 `type=sha,prefix=sha-,format=short` 与 `type=semver,pattern={{version}}`，让每个镜像都能追溯到 commit；
  2. **区分频道**：`latest` 只跟 tag（人工发布），main 推 `:edge`：

  ```yaml
  tags: |
    type=semver,pattern={{version}}                 # v0.1.0 → :0.1.0
    type=raw,value=latest,enable=${{ startsWith(github.ref, 'refs/tags/v') }}
    type=raw,value=edge,enable={{is_default_branch}}
    type=sha,prefix=sha-,format=short               # 永远带一份可追溯 tag
  ```

  3. compose 里 pin 到具体版本（`image: …library:0.1.0`）或在 README 里写明"NAS 上请用 `:0.1.0` 而非 `:latest`"，
     升级变成一次**显式动作**。

#### P1-D10｜actions 未 pin 到 commit SHA（供应链）

- **位置**：`:24,26,52,54,56,58,65,72` —— 全部 `uses: xxx@v4/@v3/@v5/@v6`
- **影响**：tag 是可变引用，上游一次 tag 重写/账号被盗即可在拥有 `packages: write` 的 job 里执行任意代码，
  进而**改写 ghcr 上的镜像**（`tj-actions/changed-files` 2025 年事故就是这个模式）。本仓库已有 `packages: write`，属高风险面。
- **建议**：pin 到 40 位 SHA + 加 dependabot 自动升级：

  ```yaml
  - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683    # v4.2.2
  ```
  ```yaml
  # .github/dependabot.yml
  version: 2
  updates:
    - package-ecosystem: github-actions
      directory: "/"
      schedule: { interval: weekly }
  ```

#### P2-D11｜多架构构建耗时与成本未受控

- **位置**：`:54` (`setup-qemu-action`) + `:80` (`platforms: linux/amd64,linux/arm64`)
- **事实**：`arm64` 在 `ubuntu-latest`（x86_64）上走 **QEMU 模拟**，`pip install` 阶段会慢 5-10 倍。
  纯 Python 依赖（无 C 扩展编译，只有 `pydantic-core` 有 wheel）所以**没有编译风险**，只是慢。
  估算：单架构 ~1.5-3 min，双架构 QEMU 下 ~8-15 min。`cache-to: mode=max` 会占 GHA cache 配额（默认 10GB/仓库），
  双架构 `mode=max` 一次约 300-600MB，**几个版本后可能把配额挤满**，导致频繁 cache 失效（表现为"偶尔特别慢"）。
- **建议**：① 若 NAS 是 x86（QNAP 多数型号是 Intel/AMD）→ **只构建 amd64**，arm64 按需手动触发，可省一大半时间；
  ② 若必须双架构，把 `cache-to` 改 `mode=min`（只缓存最终层，`pip` 层靠 Dockerfile 分层顺序已经能命中）；
  ③ 给 build 加 `timeout-minutes: 30` 防 QEMU 卡死白占 runner。

#### P2-D12｜CI 没有做"镜像自身"的冒烟

- **位置**：`.github/workflows/docker-publish.yml` 全文
- **问题**：构建成功后 **不验证镜像能跑**。一个 `import` 层面的错误（比如漏 COPY 某个模块）在
  `pip install` / `COPY` 阶段都能通过，直到 NAS 上容器起不来才发现。
- **建议**（在 build-push 里追加，仅 amd64 加载测试）：

  ```yaml
  - name: Smoke test image
    if: github.event_name != 'pull_request'
    run: |
      docker buildx build --platform linux/amd64 -t e8n:smoke --load .
      docker run -d --name smoke -p 18080:8080 e8n:smoke
      for i in $(seq 1 30); do
        curl -fsS http://127.0.0.1:18080/api/health && break || sleep 2
      done
      curl -fsS http://127.0.0.1:18080/api/health
      docker rm -f smoke
  ```

---

## 4. 容器与部署

### 4.1 Dockerfile（`Dockerfile:1-28`）

#### P2-D13｜以 root 运行 —— 评估：当前**可接受**，但有低风险改进路径

- **位置**：`Dockerfile:22-23`（注释明确了原因：NAS 挂载目录为 root 属主）
- **评估**：这个决策是**诚实且有依据的**（改动会破坏现网老部署）。不做"必须改"的结论。但要注意风险面：
  容器的攻击面包括 `uvicorn 0.0.0.0:8080` + `upload-snapshot`（写文件）+ 下载远端 PDF（解析第三方内容）+ **无鉴权局域网**。
  root 意味着任何一处路径/解析缺陷直接等于宿主机 `/share` 下 root 写权限。
- **建议（不破坏老部署的中间方案）**：
  1. 让 entrypoint 支持"**可选降权**"：若设置了 `PUID`/`PGID` 环境变量则 `chown -R` + `su-exec` 切到该用户，默认不设 → 行为与现在完全一致（零破坏）；
  2. 长期目标：用 `adjust-ownership` 的一次性 init 步骤把 `/data` 交给 uid 1000，再 `USER 1000`；
  3. **无论切不切用户**，都建议加：

     ```dockerfile
     # 降权（可选，默认禁用 = 与现网一致）
     RUN apt-get update && apt-get install -y --no-install-recommends gosu \
         && rm -rf /var/lib/apt/lists/*
     ```

     entrypoint 里：

     ```sh
     if [ -n "${PUID:-}" ] && [ -n "${PGID:-}" ]; then
       chown -R "$PUID:$PGID" /data
       exec gosu "$PUID:$PGID" "$@"
     fi
     ```

#### P1-D14｜HEALTHCHECK 恒真 —— 数据库坏了、容器半死都报 healthy

- **位置**：`Dockerfile:25-26` vs `web/app.py:746-748`
  ```python
  @app.get("/api/health")
  def health():
      return {"ok": True, "ts": utcnow(), "db": os.path.exists(DB_PATH)}
  ```
- **问题**：`/api/health` **永远返回 HTTP 200**，`db` 字段只是 `os.path.exists()`（SQLite 文件存在 ≠ 可读可写）。
  而 `HEALTHCHECK` 用 `urllib.request.urlopen(...)` —— **只要不抛异常就判健康**，即"HTTP 200 即健康"。
- **实测证据**：我用 `/tmp/_d_venv` 起服务，首次探测 `/api/health` 返回 `{"ok":true,…,"db":false}` —— **数据库还不存在时就报 ok=true**。
- **影响**：`docker ps` 永远显示 `(healthy)`；配合 `restart: unless-stopped`，一个"HTTP 层活着但数据库锁死/磁盘满/只读挂载"的容器
  **永远不会被判定为不健康**，且没有任何告警。这才是 NAS 上最可能的故障形态（磁盘满），而恰恰是 healthcheck 最该抓的。
- **建议**：healthcheck 检查**真实可用性**（读一次 DB + 看 ok 字段），并让 health 端点诚实：

  ```python
  # web/app.py
  @app.get("/api/health")
  def health():
      db_ok = False
      try:
          with get_db().connect() as conn:
              conn.execute("SELECT 1").fetchone()
          st = os.statvfs(DATA_DIR) if hasattr(os, "statvfs") else None
          disk_ok = (st is None) or (st.f_bavail / st.f_blocks > 0.01)   # 剩余 <1% 视为不健康
          db_ok = True
      except Exception:
          disk_ok = False
      ok = db_ok and disk_ok
      return JSONResponse({"ok": ok, "db": db_ok, "disk_ok": disk_ok, "ts": utcnow()},
                          status_code=200 if ok else 503)
  ```

  ```dockerfile
  HEALTHCHECK --interval=60s --timeout=10s --start-period=40s --retries=3 \
    CMD python3 -c "import json,sys,urllib.request as u; \
  d=json.load(u.urlopen('http://127.0.0.1:8080/api/health',timeout=8)); \
  sys.exit(0 if d.get('ok') and d.get('db') else 1)"
  ```

  > 注意：改 `status_code` 会影响 `migrate-nas.sh:217` 的就绪探测（它只看"不抛异常"），改后需同步 —— 这正好是想要的效果。

#### P2-D15｜无 `VOLUME` 的决策正确，但需在 `docker-compose.yml` 侧补齐护栏

- **位置**：`Dockerfile:20-21`（刻意不写 VOLUME，注释解释了根因）+ `docker-compose.yml:11-12`（显式 bind mount）
- **评估**：**这是本项目最正确的一个部署决策**。没有 VOLUME → 忘记挂载时数据直接写在容器可写层，容器一重建就丢，
  但至少**不会在多个匿名卷之间"看起来有数据"地漂移**（比匿名卷更诚实）。配合 `core/mounts.py` 的匿名卷检测 + 设置页红字告警，闭环完整 ✓。
- **但有一个残留**：`docker-compose.yml:11-12` 用的是**绝对路径 bind mount**。若宿主机路径不存在，
  Docker 会**自动创建空目录**（root 属主）→ 容器启动成功、`mkdir -p /data/logs` 成功、于是**空库正常跑起来**，
  用户只是"看不到旧书"。这个失败模式是静默的。
- **建议**（compose 层）：README 已让用户先 `mkdir -p`，但建议再加一道：

  ```yaml
  volumes:
    - type: bind
      source: /share/Container/eightnations/data
      target: /data
      bind:
        create_host_path: false      # ← 宿主路径缺失直接启动失败，而不是静默建空目录
  ```

#### P2-D16｜`.dockerignore` 与 Dockerfile 一致性 —— 复核通过 ✓

- 逐项核对：`Dockerfile` 里 COPY 的 8 个对象（`core/ sites/ web/ tools/ manage.py scheduler.py entrypoint.sh requirements.txt`）
  **都不在** `.dockerignore` 中 ✓；`data/`（717MB）、`tests/`、`fixtures/`、`docs/`、`.git/` 全部排除 ✓。
  构建上下文从 722MB 降到 ~2MB 量级（未实测 build，因本机 Docker 守护进程未运行 —— **如实记录为未验证项**）。

### 4.2 entrypoint.sh（`entrypoint.sh:1-61`）

#### P1-D17｜后台 scheduler 的子 shell **不转发信号** —— `docker stop` 留 30s 尾巴

- **位置**：`entrypoint.sh:31-56`（`( … ) &` 子 shell）+ `:61`（`exec python3 -m uvicorn …`）
- **问题**：`:61` 用 `exec` 让 uvicorn 顶替 PID 1，收到 SIGTERM 会优雅退出 ✓（这一点对了）。
  但 `:31` 的后台子 shell**没有 trap**，主脚本退出时它**不会**收到信号。
- **实测证据**（我在 `/tmp` 用等价的 sh 复现进程模型）：
  ```
  进程树： sh /tmp/_ep_test.sh  (18525)
           ├── sleep 120          (18530)   ← 模拟长时间调度/下载
           └── sleep 5            (18534)   ← 前台 uvicorn 替身
  发送 SIGTERM 给 entrypoint PID 18525 后：
    18530     1 sleep 120            ← PPID 变成 1，成为孤儿
  ```
- **影响**：
  1. 容器退出后**残留一个孤儿 python 进程**（在容器内即僵尸，PID 1 不 reap）；
  2. `docker stop` 默认 10s 超时 → SIGKILL。此时**正在下载的那一册**被硬杀，
     留下 `.part` 残片（下次靠 Range 续传，虽可恢复但会触发一次"完整性拒收"路径）；
  3. 若下载正在进行且进程未被杀（孤儿），它**仍持有宿主 `/data` 的写句柄**，而新容器已挂载同一目录 → **双写风险**（新提交刚修过"双写"问题）。
- **建议**：

  ```sh
  #!/bin/sh
  set -eu
  child=""
  shutdown() {
    [ -n "$child" ] && kill -TERM "-$child" 2>/dev/null || true   # 杀整个进程组
    wait "$child" 2>/dev/null || true
    exit 0
  }
  trap shutdown TERM INT

  # 用 setsid 起独立进程组；并让里面的 python 也收到信号
  ( … while true; do … done ) &
  child=$!
  exec python3 -m uvicorn ...     # 前台
  ```
  更稳妥的写法（不依赖 `setsid` 可用性）：把 scheduler 循环本身 `trap 'exit 0' TERM`，
  并在 `.py` 侧处理 `SIGTERM` 时"放下当前任务再退"（`scheduler.py` 目前只在 `KeyboardInterrupt` 处退出，SIGTERM 默认直接终止）。

#### P1-D18｜`web.log` 轮转是**死代码**，日志轮转只覆盖了一半进程

- **位置**：`entrypoint.sh:23-27`（轮转 `web.log`）vs `:58-61`（uvicorn **输出到 stdout**，从不写 `web.log`）
- **问题**：注释（`:58-60`）说"Web 前端日志走容器 stdout"—— 这是对的。那么 `:23-27` 的 `web.log` 轮转
  **永远不会命中任何正在写入的日志**，只会在有人手工往 `/data/logs/web.log` 写过东西时生效。
  同时 `scheduler.log` 的轮转是在**每次重启 scheduler 时**（`:34`，即 scheduler 退出后）才执行 ——
  **一个长期不崩的 scheduler 永远不轮转**。`LOG_MAX=20MB`、上限约 40MB，写满需要很久，但"永不轮转"是事实。
- **影响**：次要。① 读者会误以为轮转已覆盖两个进程（文档同步问题）；② scheduler 日志实际的增长没人管。
- **建议**：
  1. 删掉 `web.log` 轮转分支（诚实化），或改成真的把 uvicorn 输出 tee 到文件；
  2. scheduler 侧改用 `multiprocessing`/外部 logrotate 不现实 —— 最省事的办法是**让 scheduler 自己按大小轮转**
     （在循环里检查 `os.path.getsize`），或把日志也交给 stdout（与 web 一致，全靠 compose 的 `json-file` 限流）：

     ```sh
     # 推荐：两个进程都写 stdout，日志轮转统一交给 docker logging driver（compose 已限 10m×3）
     python3 scheduler.py 2>&1 | sed -u 's/^/[sched] /' &
     exec python3 -m uvicorn ... 2>&1 | sed -u 's/^/[web] /'
     ```

#### P2-D19｜`rotate_logs` 的 `[ "$sz" -gt ... ]` 依赖 `num()` 式的清洗，但 `sz` 只做了 `tr -cd '0-9'`

- **位置**：`entrypoint.sh:20-21`、`:25-26`
- **问题**：`sz=$(wc -c < "$f" | tr -cd '0-9')` 若 `wc` 失败会得到空串，`[ "" -gt "$LOG_MAX" ]` 在 `sh` 下会**报错**
  （`set -e` 没开，所以不会中断，但会往 stderr 喷错）。已有 `[ "${sz:-0}" -gt … ]` 兜底 ✓ —— 这条实际上**做得对**，记录为"已通过"，
  只是 `:21` 的 `mv -f` 与 `: > "$f"` 之间有一个**极短窗口**会丢日志行（`mv` 后新文件创建前的写入进旧 inode）。
  可用 `cp --reflink` 思路避免，但没有必要；记录为 P2 观察项。

### 4.3 docker-compose.yml

#### P1-D20｜端口 `8080:8080` 绑 0.0.0.0 + 无任何鉴权 = 局域网内任意人可写

- **位置**：`docker-compose.yml:9-10`；`Dockerfile:23`（root）；`README.md:4`（"局域网 Web 前端"）；`docs/DESIGN.md:198`（"仅局域网访问 + 可选简单 Token"）
- **问题**：代码里**没有任何 Token/鉴权实现**（`grep -rn "token|Authorization" web/app.py` → 空）。
  compose 没有限制 bind 地址 → 容器端口映射到宿主机**所有网卡**。
  可写接口包括：`POST /api/review`、`/api/retry`、`/api/fetch`、`/settings`、`/api/links`、`/upload-snapshot`、`POST /api/progress`、`DELETE /api/annotations/{id}` …
- **影响**：**同一局域网内任何设备**（含访客 Wi-Fi、被投毒的打印机/IoT）都能：改设置、触发任意下载（消耗流量/触发原馆封禁）、
  上传任意 snapshot（写入 `/data/snapshots`，配合路径处理需看审查 B 的结论）、批量批准/忽略、删除标注。
  由于容器是 root，写缺陷的爆炸半径覆盖整个 `/share`。
  **这不是"能不能被公网打到"的问题**（NAS 通常不暴露公网），而是"内网零信任"的问题。
- **建议**（三档，按成本递增）：
  1. **立刻、零代码**：限制监听面。若只需要家里几台设备访问：

     ```yaml
     ports:
       - "127.0.0.1:8080:8080"        # 仅本机（配合 SSH 隧道）
     # 或绑定 NAS 的内网 IP，而不是 0.0.0.0：
     # - "192.168.1.10:8080:8080"
     ```
  2. **低成本鉴权**：加一个可选 `EIGHTNATIONS_TOKEN`，`middleware` 里对**非 GET** 方法校验 `X-Auth-Token`，未设置则保持现状（零破坏）。
  3. 长期：真正想做公网访问时再上反代 + basic auth。

#### P1-D21｜compose 无 healthcheck / 无资源限制 / 数据卷非只读（后者刻意）

- **位置**：`docker-compose.yml:4-25`
- **问题**：
  1. **无 `healthcheck:`** → 依赖镜像内的 HEALTHCHECK（而它是恒真的，见 P1-D14）；
     也无 `depends_on` 场景（只有一个服务，影响小）。
  2. **无 `deploy.resources.limits` / `mem_limit`** → 一个下载风暴或内存泄漏可以吃满 NAS 内存，
     **连带影响 NAS 上其他服务**（QNAP 上通常还有一堆别的容器）。这是 NAS 部署最现实的风险之一。
  3. **无只读挂载** —— 这里是**对的**：`/data` 必须可写（书库要落盘），不能改。但可以额外加：
     `read_only: true` + `tmpfs: /tmp`（根文件系统只读），因为应用只写 `/data`。这让"容器被写坏"变成不可能。
- **建议**：

  ```yaml
  services:
    app:
      # …
      read_only: true                       # 根分区只读（应用只写 /data）
      tmpfs:
        - /tmp:size=64m                     # 下载 chunk / _probe 中间产物
      healthcheck:
        test: ["CMD", "python3", "-c",
               "import json,sys,urllib.request as u;d=json.load(u.urlopen('http://127.0.0.1:8080/api/health',timeout=8));sys.exit(0 if d.get('ok') and d.get('db') else 1)"]
        interval: 60s
        timeout: 10s
        retries: 3
        start_period: 40s
      mem_limit: 2g                         # 按 NAS 总内存的 1/3 调
      pids_limit: 256
      ulimits:
        nofile: { soft: 4096, hard: 8192 }
      security_opt:
        - no-new-privileges:true
      logging:
        driver: json-file
        options: { max-size: "10m", max-file: "3" }
  ```

  > `read_only: true` 需确认没有代码往 `/app` 或 `/` 写（实测应用只写 `EIGHTNATIONS_DATA`，但 `BOOKS_DIR` 在 `/data` 下 ✓）。
  > 上生产前请先在一台测试 NAS 上跑一天确认。

---

## 5. `migrate-nas.sh` 逐条安全审查

### 5.1 逐条核对（这个脚本会以 root 运行在 NAS 上）

先给结论：**脚本的核心安全顺序设计是正确的**，注释 `:15-25` 描述了"先拷后删"的理由，代码也**确实这么做了**。
下面按"危险操作 → 校验 → 结论"逐条列。

| # | 行号 | 操作 | 是否有目标校验 | 变量为空时会怎样 | 幂等 | 结论 |
|---|------|------|---------------|-----------------|------|------|
| 1 | `:29-30` | `DEST="${1:-/share/…}"` / `DEST="${DEST%/}"` | **无白名单校验** | 传空串 → 走默认值（安全）；但传 `".."`、`"/"`、`"/share"`、`"/etc"` → **原样接受** | — | **P1**：见 5.1-a |
| 2 | `:143` | `rm -rf "$STAGE_DIR"` | `STAGE_DIR="$DEST/.migrate-staging"`，依赖 `DEST` 非空 | **`DEST="/"` → `STAGE_DIR="/.migrate-staging"`**（打到根分区）；`DEST=""` 不会发生（有 `${1:-}` 默认） | 是 | **P1**：见 5.1-a |
| 3 | `:152,156,161,164` | 失败分支里的 `rm -rf "$STAGE_DIR"` | 同上 | 同上 | 是 | 同 #2 |
| 4 | `:160` | `tar -x -f "$TARBALL" -C "$DEST"` | 无 `-P`/无路径穿越检查 | tar 包内容来自**可信源卷**（本地 Docker 卷），非外部输入 | 覆盖写 | **P2**：见 5.1-b |
| 5 | `:193-202` | `docker rm -f "$cid"` | 只匹配**精确容器名** `eightnations`（`:105` 有二次精确比对）✓ | `cid` 来自 `docker ps` 输出，非空 ✓ | 是 | **通过** ✓（注释 `:97-98` 解释得当） |
| 6 | `:170,174,177` | `docker rm -f "$HELPER"` | `HELPER="$NAME-migrate-src"` 固定名 | 固定非空 ✓ | 是 | **通过** ✓ |
| 7 | `:173` | `docker cp "$HELPER":/from/. "$DEST"` | `DEST` 同上 | 同 #1 | 覆盖 | 同 #1 |
| 8 | `:206-209` | `docker run -d --name eightnations … -v "$DEST":/data` | `DEST` 同上 | 同 #1 | 名字已释放（:193 删过） | 同 #1 |
| 9 | `:131` | `mkdir -p "$DEST"` | — | `DEST="/"` → `mkdir -p /` 无副作用；`DEST=".."` → 在 CWD 父目录建 `.migrate-staging` | 是 | 同 #1 |
| 10 | `:186-187` | PDF 数量对账 `find "$DEST/books" -name '*.pdf' \| wc -l` == `$SRC_PDF` | 有对账 ✓ 但是**只比对数量** | — | — | **P2**：见 5.1-c |
| 11 | `:183-184` | db 大小 > 0 | 有 ✓（但 0 大小之外不校验） | — | — | **P2**：见 5.1-d |
| 12 | `:148-151` | `docker run … > "$TARBALL"` 后 `tar -t` 校验 | 有 ✓（识别截断） | — | — | **通过** ✓ 设计好 |
| 13 | 全脚本 | `set -eu` | 有 ✓ | 未定义变量会立即退出 ✓ | — | **通过** ✓ |
| 14 | 全脚本 | `--dry-run` | **无** | — | — | **P1**：见 5.1-e |
| 15 | `:12-13,29` | 用法接受 `sh migrate-nas.sh [路径]`，**无 `--help`**、无参数校验、无二次确认 | 无 | 多传的参数被静默忽略 | — | **P2** |

#### 5.1-a（P1）`DEST` 无校验 → `rm -rf` 可打到根分区

实测（`/tmp/_shelltest/_probe.sh` 复刻 `:29-30,142-143`）：

```
DEST=[]                       STAGE_DIR=[/.migrate-staging]            ← 打到根分区
DEST=[..]                     STAGE_DIR=[../.migrate-staging]          ← 落到 CWD 的父目录
DEST=[/share/x/..]            STAGE_DIR=[/share/x/../.migrate-staging]
```

由于默认值 `${1:-…}` 只在**参数为空串**时生效，而 `sh migrate-nas.sh ""` 的情况被默认值救下 ——
**但 `sh migrate-nas.sh /` 和 `sh migrate-nas.sh ..` 会被完整接受**。后果：`rm -rf /.migrate-staging`
（无害，因为不存在），但 `mkdir -p` + `tar -x` 会把整个书库**解包到根分区**，随后 `:160` 的
`tar -x -f "$TARBALL" -C "/"` 会把 `books/ db/ …` 写到 `/` 下 —— 根分区（QNAP 系统分区）通常只有几百 MB，
**写满即系统异常**；且 SQLite db 被写到 `/db/`，后续容器挂载的是 `/`，行为完全不可预期。

概率低（用户不会主动传 `/`），但**这是唯一一个能造成"NAS 不可用"级别的路径**，且防护成本只有 6 行。

#### 5.1-b（P2）`tar -x` 未防路径穿越 / 未用 `--keep-old-files`

源卷内容虽然由自己的镜像写入，但**目录名来自远端元数据**（馆藏专藏/条目 uid）。`core/pipeline._dest_dir`
已做校验（审查 A 覆盖），所以正常情况安全；但迁移脚本是**读取历史数据**，历史数据可能来自校验上线之前。
`tar` 在 `-C "$DEST"` 下解包，若成员名含 `..` 会逃逸到 `$DEST` 之外。

建议（低成本）：

```sh
tar -t -f "$TARBALL" | grep -qE '(^/|\b\.\.(/|$))' && die "打包文件包含绝对路径或 .. 成员，已中止"
tar -x -f "$TARBALL" -C "$DEST" --no-same-owner    # NAS 上属主无意义，且避免 uid 映射问题
```

#### 5.1-c（P2）只对账 PDF **数量**，不对账"是否可读/大小"

`SRC_PDF` 在 `:68-71` 用 `find … | wc -l` 得到，`DEST` 在 `:186` 用同样方式数 —— 两边都是"数文件"。
若 `tar` 解包把一个 PDF 写成 0 字节但文件数不变，**对账通过**。这本书就永久坏了（且旧容器已被删）。

建议加一个大小总和对比（代价很低）：

```sh
src_bytes=$(docker run --rm -v "$SRC":/from:ro "$IMG" sh -c 'find /from/books -name "*.pdf" -printf "%s\n" 2>/dev/null || true' | awk '{s+=$1} END{print s+0}')
dst_bytes=$(find "$DEST/books" -name '*.pdf' -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}')
[ "$src_bytes" = "$dst_bytes" ] || die "PDF 总字节不一致：源 $src_bytes vs 目标 $dst_bytes——迁移不完整，不删旧容器"
```

> 注意 QNAP 上的 `find` 是 busybox 版，可能不支持 `-printf`；更稳妥用 `du -sb`（busybox 支持 `-s`，但 `-b` 不一定）。
> 建议改成在**容器内**两侧都跑（镜像里有 GNU tar 的 slim 镜像通常也有 coreutils），或用 `du -sk` 取近似值并允许 `±1`。

#### 5.1-d（P2）db 只校验"非 0 字节"，不校验**可打开**

`:183-184` 只 `wc -c > 0`。若复制过来的是**旧版本 schema 的 db**（比如匿名卷里的 db 比当前镜像期望的列少），
校验通过、旧容器被删、新容器起来后由 `DB.init()` 做"老库迁移" —— 这条路是设计好的 ✓。
但若 db 是**WAL 模式且 -wal 文件没拷全**（`tar` 拷整个目录应该完整，但 `:71` 的 `date -r` 说明作者意识到 db 文件本身可能落后于 WAL），
可能出现"db 文件比 WAL 新/旧"的不一致。建议直接**用镜像内的 sqlite 打开验证**：

```sh
docker run --rm -v "$DEST":/d:ro "$IMG" python3 -c "
import sqlite3,sys
c=sqlite3.connect('/d/db/library.db')
n=c.execute('select count(*) from books').fetchone()[0]
print('books =', n); sys.exit(0 if n >= 0 else 1)
" || die "迁移后的 library.db 无法打开或查询（WAL 不完整？）——不删旧容器"
```

#### 5.1-e（P1）无 `--dry-run`：脚本一跑就开始停容器

`:99-128` 会**立即执行 `docker stop`**（包括"源卷若还被别的容器挂着"的那些容器，`:122`）。
用户想先看看"会动哪些东西"没有任何途径。这与本脚本明显偏保守（先拷后删、失败即退）的风格不一致。

建议：加 `DRY=1` 开关，把所有**有副作用**的命令包一层 `run()`：

```sh
DRY="${DRY:-0}"
run() {                                   # 所有 docker/mkdir/rm/tar 都经它
  if [ "$DRY" = 1 ]; then echo "  [dry-run] $*"; return 0; fi
  "$@"
}
# 用法：run docker stop "$cid"、run mkdir -p "$DEST"、run rm -rf "$STAGE_DIR" …
# 只读探测（docker ps/ls/volume ls）不走 run，干跑也能看到发现结果
```

调用：`DRY=1 sh migrate-nas.sh`。

#### 5.1-f（P2）脚本头部**鼓励 `curl | sh`**，但没有完整性校验

`:11-13`：
```
#   curl -O https://raw.githubusercontent.com/kelvinguo1988/eightnations-library/main/migrate-nas.sh
```
问题：① 指 `main`（可变的）而非 tag/commit → 用户拿到的可能是任意时刻的脚本；
② 没有 `sha256` 校验；③ 这个脚本要**以 root 身份动宿主机的 /share 和 docker**。
配合 README:236-243 的指引，这是一个"高权限 + 无校验 + 可变来源"的组合。

建议（README 与脚本头部同步）：

```markdown
> 安全提示：脚本会以 root 动作宿主机（停容器、docker rm、写 /share）。
> 请**先下载并阅读**再执行，或用固定版本 + 校验：
> ```bash
> curl -O https://raw.githubusercontent.com/kelvinguo1988/eightnations-library/v0.1.0/migrate-nas.sh
> curl -O https://raw.githubusercontent.com/kelvinguo1988/eightnations-library/v0.1.0/migrate-nas.sh.sha256
> sha256sum -c migrate-nas.sh.sha256      # 校验后再执行
> sh migrate-nas.sh --dry-run             # 先干跑看一眼
> sh migrate-nas.sh
> ```
```

### 5.2 加固 diff（可直接落地，按优先级排列）

```diff
@@ 脚本头部 usage
-#   sh migrate-nas.sh                       # 目标路径默认 /share/Container/eightnations/data
-#   sh migrate-nas.sh /share/其他路径        # 自定义目标路径
+#   sh migrate-nas.sh --dry-run             # 干跑：只打印将要执行的动作，不停容器、不写盘
+#   sh migrate-nas.sh                       # 目标路径默认 /share/Container/eightnations/data
+#   sh migrate-nas.sh /share/其他路径        # 自定义目标路径（必须在 /share 下）

@@ set -eu 之后
 set -eu
 
-DEST="${1:-/share/Container/eightnations/data}"
+DRY=0
+args=""
+for a in "$@"; do
+  case "$a" in
+    --dry-run|-n) DRY=1 ;;
+    --help|-h) sed -n '2,30p' "$0"; exit 0 ;;
+    -*) die "未知参数: $a（支持 --dry-run / --help）" ;;
+    *) args="$a" ;;
+  esac
+done
+
+DEST="${args:-/share/Container/eightnations/data}"
 DEST="${DEST%/}"
+
+# —— 目标路径白名单校验：只允许 /share/ 下的绝对路径 → 杜绝 / 与 ..
+case "$DEST" in
+  /share/*) ;;
+  *) die "目标路径必须是 /share/ 下的绝对路径（收到: '$DEST'）。这是为了防止误删/误写系统分区。" ;;
+esac
+case "$DEST" in
+  *..*) die "目标路径不得包含 '..'（收到: '$DEST'）" ;;
+esac
+[ "$DEST" != "/share" ] && [ "$DEST" != "/share/Container" ] \
+  || die "目标路径过于宽泛（'$DEST'），请指向具体的 data 目录"
+
 IMG="${EIGHTNATIONS_IMAGE:-ghcr.io/kelvinguo1988/eightnations-library:latest}"
@@
 say() { echo "[migrate] $*"; }
 die() { echo "[migrate] ✗ $*" >&2; exit 1; }
+
+# 所有有副作用的命令都走 run()，DRY=1 时只打印
+run() {
+  if [ "$DRY" = 1 ]; then echo "  [dry-run] $*"; return 0; fi
+  "$@"
+}

@@ 停容器（:108-109）
-  docker stop "$cid" >/dev/null 2>&1 && say "  已停止容器 $cname ($cid)" \
-    || say "  容器 $cname ($cid) 无需停止（已退出）"
+  run docker stop "$cid" >/dev/null 2>&1 && say "  已停止容器 $cname ($cid)" \
+    || say "  容器 $cname ($cid) 无需停止（已退出）"

@@ 源卷上的其他容器（:122-124）
-    docker stop "$cid" >/dev/null 2>&1 \
+    run docker stop "$cid" >/dev/null 2>&1 \

@@ 暂存目录
-  rm -rf "$STAGE_DIR"                      # 清理上次中断的残留
+  run rm -rf "$STAGE_DIR"                  # 清理上次中断的残留

@@ 打包
-    if ! docker run --rm -v "$SRC":/from:ro "$IMG" tar -c -C /from . > "$TARBALL"; then
+    if [ "$DRY" = 1 ]; then
+      say "  [dry-run] 将打包卷 $SRC → $TARBALL"
+    elif ! docker run --rm -v "$SRC":/from:ro "$IMG" tar -c -C /from . > "$TARBALL"; then

@@ 解包前的穿越校验（新增）
-    say "解包到 $DEST …"
+    # 防止 tar 成员含绝对路径或 .. 逃出 DEST（历史数据可能来自校验上线之前）
+    if tar -t -f "$TARBALL" | grep -qE '(^/|(^|/)\.\.(/|$))'; then
+      run rm -rf "$STAGE_DIR"
+      die "打包文件含绝对路径或 '..' 成员，拒绝解包——旧容器只停未删，数据完好"
+    fi
+    say "解包到 $DEST …"
-    if ! tar -x -f "$TARBALL" -C "$DEST"; then
+    if ! run tar -x -f "$TARBALL" -C "$DEST" --no-same-owner; then

@@ 删除旧容器
-    docker rm -f "$cid" >/dev/null 2>&1 && say "  已移除容器 $cid" \
+    run docker rm -f "$cid" >/dev/null 2>&1 && say "  已移除容器 $cid" \

@@ 重建
-  docker run -d --name "$NAME" --restart unless-stopped \
+  run docker run -d --name "$NAME" --restart unless-stopped \

@@ 最后
+if [ "$DRY" = 1 ]; then
+  say "干跑结束：以上为将要执行的动作。确认无误后去掉 --dry-run 重跑。"
+  exit 0
+fi
 say "完成 ✅ 打开 http://<NAS_IP>:8080 —— 设置页存储面板应显示 ✓ 已绑定固定路径"
```

> 这个 diff 的关键是**不改变原有安全顺序**（先拷后删、失败即退、对账通过才删）—— 只是把一个
> 缺少输入校验的 root 脚本补齐成"输入受限 + 可预演 + 可审计"。

---

## 6. 文档一致性（抽查 15 处，6 处不一致）

| # | 文档位置 | 文档说 | 代码实际 | 判定 |
|---|---------|--------|---------|------|
| 1 | `README.md:355` | 回归测试"**9 组**，全部离线" | 实际 **13 组** | ❌ 不一致（CI 注释 `docker-publish.yml:20` 也写 9 组，同样过时） |
| 2 | `README.md:40-41` | 本机跑测试需另装 `pillow`（已写）✓ | requirements 里确实没 pillow，测试 `:114` `from PIL import Image` | ✅ 一致（做得对） |
| 3 | `README.md:247`、`:250` | "镜像的 `VOLUME /data` 会让 Docker 自动分配匿名卷" | `Dockerfile:20-21` **刻意不写 VOLUME** | ❌ **不一致且关键**：这是**历史事故解释**，但读起来像"当前镜像有 VOLUME"。用 `docker inspect` 查挂载的人会迷惑。建议改成"（历史版本曾声明，现已在 Dockerfile 移除：见 Dockerfile:20）" |
| 4 | `docs/DESIGN.md:197` | 接口 = `/api/books`、`/api/books/{id}`、`/api/queue/approve\|ignore`、`/api/jobs`、`/api/sources` | **5 个全都不存在**（实测 grep `web/app.py` 全无） | ❌ **整段是未实现的旧设计**，且和 `:247`（M3 已完成段）自相矛盾 |
| 5 | `docs/DESIGN.md:279` | 调度器 = **APScheduler 进程内** | `scheduler.py` 是纯标准库 while 循环；`requirements.txt:30` 明确"调度器为纯标准库实现" | ❌ 陈旧（`:246` M2 段落已纠正，但 `:279` 汇总表没改） |
| 6 | `docs/DESIGN.md:218` | 镜像 = `python:3.12-slim` + pypdf/**apscheduler**/fastapi/uvicorn | 无 apscheduler | ❌ 同上 |
| 7 | `docs/DESIGN.md:282` | "**当前进度**：M1 完成；下一步 M2——APScheduler 接入 + 剩余 37 册" | M1-M5 均已标 ✅（`:244-250`） | ❌ 同一文档内部矛盾 |
| 8 | `docs/DESIGN.md:258` | 前端栈 = "FastAPI + Jinja2 + **htmx**" | `grep -rc htmx web/templates web/static` → **0** | ❌ 未使用 htmx |
| 9 | `docs/DESIGN.md:198` | "仅局域网访问 + **可选简单 Token**" | 代码**无任何 Token 实现** | ❌ 承诺了不存在的安全特性（见 P1-D20） |
| 10 | `docs/DESIGN.md:190-192` | 详情页 = "在线阅读（浏览器原生 PDF，`<embed>`）"，书库页 = "五维筛选：来源/**朝代/年代区间**/关键字" | `detail.html:37` 确实 `<embed>` ✓；但另有 `/read/{id}` 自研 PDF.js 阅读器（`read.html`），而书库筛选无"年代区间" | ⚠️ **半一致**：`<embed>` 是旧路径仍在，但 DESIGN 未提自研 reader；"年代区间"筛选不存在 |
| 11 | `README.md:12` | 书城 + 阅读器含"双页/连滚/单页、标注导出" | `POST /export/annotations/{book_id}.md` 存在 ✓；reader.js 有 pdfjs ✓ | ✅ 一致 |
| 12 | `README.md:15`、`:347` | 目录结构 `core/... · text(日文新字体→繁体) · models`；`tools/ loc_snapshot.py · loc_fill_details.py` | `core/text.py`、`core/models.py`、`tools/loc_snapshot.py`、`tools/loc_fill_details.py` 均存在 ✓ | ✅ 一致 |
| 13 | `README.md:16-19` 各馆状态表 | LoC 启用 / na_jp 启用 / bnf 停用 / **ndl_jp 待实现（停用）** | `core/db.py:112-118` 种子：loc enabled=1、na_jp=1、bnf=0、ndl_jp=0；`sites/__init__.py` 未注册 `ndl_jp` | ✅ 一致（文档诚实） |
| 14 | `README.md:354` | "**四轮代码审查**累计修复 19 项…关键路径均有离线回归测试" | 本次是第五轮审查（A/B/C/D 四位）。"关键路径均有回归测试"与 §1.3 的 3/8 命中率不符 | ⚠️ 措辞过于乐观，建议改为"核心数据路径有回归测试；web/前端/并发仍无自动化覆盖" |
| 15 | `README.md:349` | `manage.py` / `scheduler.py` / `Dockerfile` 等文件存在性 | 全部存在 ✓ | ✅ 一致 |

**另外两处需要留意（不算不一致，但会误导）**：

- `README.md:236-243`：`curl -O …/main/migrate-nas.sh` + `sh migrate-nas.sh` —— 见 P1-5.1-f，**鼓励无校验地以 root 执行远程脚本**。
- `docs/PLAN-20260919.md` 通篇是"次日执行"的**已完成计划**（全部标 ✅），但它混在 `docs/` 里与设计文档并列，
  且开头表格里的数据（2,128 书 / 41 册 / 1,987 册）与 README 的（2,028 条 / 41 册）**数字不同**（不同时点快照，可解释）。
  建议移到 `docs/archive/` 并在文件名里标明"已归档，数据为 2026-09-19 快照"。

**文档层面的整体建议**：`DESIGN.md` 已经明显"多层历史叠加"——同一个事实在三处（§6 接口、§9 里程碑、§12 汇总表）说法不同。
最小成本的解法是：**在每个"事实型"段落顶部加一行状态戳**（`> 状态：2026-09-20 已与代码核对`），
并在 CI 里加一条廉价检查（见 §8 的 `doc-link-check`）—— 至少能保证文档里引用的**文件路径**真实存在。

---

## 7. 工程化健康度评分

| 维度 | 分数 | 依据 |
|------|------|------|
| **测试** | **55 / 100** | 13 组全过、0.37s、离线、fixtures 真实（这些是加分项，结构也清楚）；扣分：web/前端/并发/真实下载路径零覆盖，**变异命中率仅 3/8**，空库上的"幂等测试"自我表扬，pytest 收集 0 个却 exit 0（假绿灯），临时目录泄漏，弱断言 4 处 |
| **CI** | **68 / 100** | test→build 依赖链、manifest list、gha 缓存、concurrency、最小 permissions（做得好）；扣分：**无 PR 触发**、latest 语义危险（tag/main 互相覆盖）、actions 未 pin SHA、无 lock、无镜像冒烟、test 与 build 环境不绑定 |
| **容器** | **62 / 100** | 无 VOLUME 决策正确且注释到位、`.dockerignore` 干净、日志限流在 compose 里配了；扣分：**healthcheck 恒真**、root 无降权路径、scheduler 信号不转发（实测产生孤儿进程）、`web.log` 轮转是死代码、compose 无资源限制/无 healthcheck/绑 0.0.0.0 无鉴权 |
| **脚本（运维）** | **50 / 100** | 核心安全顺序（先拷后删、tar 校验、PDF 对账）设计**明显高于平均水平**；扣分：`DEST` 无白名单（可打到根分区）、**无 `--dry-run`**、只对账数量不对账内容、`curl\|sh` 指引无校验、无 `--help` |
| **文档** | **52 / 100** | README 的运维章节（数据路径、迁移、doctor）实用且准确（加分）；扣分：抽查 15 处 6 处不一致，DESIGN 内部自相矛盾 + 一整段 API 清单是虚构的、承诺了不存在的 Token、版本号/测试组数过时 |

**加权总分（测试 30% / CI 20% / 容器 20% / 脚本 15% / 文档 15%）：57 / 100**

一句话总结：**"脚手架搭得比内容好"** —— 流水线结构、Dockerfile 注释、迁移脚本的安全顺序都体现了清晰的工程判断；
但**测试守不住真实回归、CI 在 PR 阶段缺席、healthcheck 恒真、迁移脚本缺输入校验**，这四点让"看起来专业"和"实际可靠"之间有明显落差。

---

## 8. Top 5（按 ROI 排序，都是 1 小时内能动的）

| # | 事项 | 为什么排这个位置 | 位置 |
|---|------|-----------------|------|
| **1** | **给测试补 3 个"变异能抓到"的断言**：① 非空库上的书名链接幂等（造 1 本 done 书再测）；② `item_*.json` 合并结果校验（pdf_urls/页数非空）；③ 配额窗口边界（造一条 2 小时前的 job，断言不计入配额）。再给每组包 `def test_*` + `if __name__`，让 `pytest` 不再"收集 0 个还 exit 0" | 直接堵住 §1.3 里 5 个"测不出"中的 4 个，改动 < 60 行，无新依赖 | `tests/test_regressions.py` |
| **2** | **修 `/api/health` + HEALTHCHECK**：health 真的 `SELECT 1` 一次并检查磁盘余量，不 ok 返回 503；HEALTHCHECK 解析 JSON 的 `ok` 字段；compose 加显式 `healthcheck:` | 一句话改掉"NAS 永远显示 healthy"，这是唯一能在故障早期报警的机制 | `web/app.py:746-748`、`Dockerfile:25-26`、`docker-compose.yml` |
| **3** | **`migrate-nas.sh` 加 DEST 白名单 + `--dry-run`**：`case "$DEST" in /share/*) ;; *) die …` + 排除 `..` + `run()` 包装副作用命令。同时把 README 的 `curl -O main/` 改成 pin tag + sha256 校验 | 消除唯一能打到根分区的路径，并把"不可预演的 root 脚本"变成"可预演" | `migrate-nas.sh:29-30,99-209`、`README.md:236-243` |
| **4** | **CI 加 `pull_request` 触发 + tag/latest 分离 + images 带 `sha-` tag** | 让评审在合并前就能红；让 NAS 拉到的镜像永远可追溯到 commit；让 `latest` 不再是"最后一次成功构建" | `.github/workflows/docker-publish.yml:3-7,68-70` |
| **5** | **entrypoint 加信号转发 + 删掉 web.log 死代码**：`trap shutdown TERM INT` + 杀进程组；让两个进程都写 stdout 交给 docker 日志驱动 | 消除 `docker stop` 后的孤儿进程与"新容器 + 旧进程双写同一 /data"风险 | `entrypoint.sh:31-61` |

> 第 6 位（如果还有余力）：给 `docker-compose.yml` 加 `mem_limit` + `read_only: true` + `tmpfs /tmp` + `security_opt: no-new-privileges`。
> 第 7 位：`requirements.lock`（`pip-compile --generate-hashes`）+ Dockerfile 用 `--require-hashes`。

---

## 9. 最小可用 CI 检查清单（命令级，可直接抄进 workflow）

> 目标：**PR 阶段就能挡住"语法错 / 测试假绿 / 依赖漂移 / 文档断链 / 镜像起不来"这五类问题**。
> 全部单文件、单 job，跑完约 3-5 分钟，不需要 Docker（除最后一步可选）。

```yaml
# .github/workflows/ci.yml  —— PR 与 push 都跑；不发镜像
name: ci
on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true        # CI 可以取消，与 publish 的排队策略相反

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@<PIN-SHA>          # ← 换成 40 位 SHA
      - uses: actions/setup-python@<PIN-SHA>
        with: { python-version: "3.12" }

      # ① 语法：所有 .py 可编译（快，最先跑）
      - name: Syntax (compileall)
        run: python -m compileall -q core web sites tools tests scheduler.py manage.py

      # ② 静态检查：未使用导入/未定义名（需先清掉仓库现有 5 条，见 §2.3）
      - name: Lint (pyflakes)
        run: |
          pip install "pyflakes==3.4.0"
          python -m pyflakes core web sites tools tests scheduler.py manage.py \
            digital_archives_to_pdf.py official_pdf_by_cids.py

      # ③ 依赖：优先用 lock（配 --require-hashes）；无 lock 时退化为 requirements.txt
      - name: Install deps
        run: |
          python -m pip install --upgrade pip
          if [ -f requirements.lock ]; then
            pip install --require-hashes -r requirements.lock
          else
            pip install -r requirements.txt
          fi
          pip install "pillow>=9.0"
          pip freeze > deps.txt

      # ④ 回归：显式跑脚本（不要用裸 pytest —— 它会"收集 0 个还 exit 0"）
      - name: Regression (script)
        run: python tests/test_regressions.py

      # ⑤ 回归：如果已改成 def test_*，再用 pytest 跑一遍并**断言确实收集到测试**
      - name: Regression (pytest, must collect > 0)
        run: |
          pip install "pytest==8.4.2"
          python -m pytest tests/ -q --ignore-glob='*/__pycache__/*' | tee /tmp/pt.txt
          grep -qE '[1-9][0-9]* (passed|test)' /tmp/pt.txt || { echo "::error::pytest 未收集到任何测试"; exit 1; }

      # ⑥ 文档断链：文档里提到的仓库内相对路径必须真实存在（廉价、能治文档漂移）
      - name: Doc link check
        run: |
          python - <<'PY'
          import os, re, sys
          bad = []
          for doc in ("README.md", "docs/DESIGN.md", "docs/READER-DESIGN.md",
                      "docs/PLAN-20260919.md"):
              if not os.path.exists(doc): continue
              src = open(doc, encoding="utf-8").read()
              # 反引号里形如 a/b.py 或 README.md 的，且不含通配/尖括号的，检查是否存在
              for m in re.finditer(r"`([\w./-]+\.(?:py|md|yml|yaml|sh|js|css|html|txt))`", src):
                  p = m.group(1).lstrip("./")
                  if any(c in p for c in "*<>") or p.startswith("http"): continue
                  if not os.path.exists(p):
                      bad.append(f"{doc}: 引用了不存在的文件 `{p}`")
          print("\n".join(bad) or "doc links OK")
          sys.exit(1 if bad else 0)
          PY

      # ⑦ 部署护栏：compose 可解析 + migrate-nas.sh 语法正确 + 危险操作清单
      - name: Deploy guard
        run: |
          python -c "import yaml,sys; yaml.safe_load(open('docker-compose.yml')); print('compose OK')"
          sh -n migrate-nas.sh && sh -n entrypoint.sh && echo "shell syntax OK"
          # 迁移脚本必须带目标路径白名单与 --dry-run（缺则失败，防止回退）
          grep -q -- '--dry-run' migrate-nas.sh   || { echo "::error::migrate-nas.sh 缺少 --dry-run"; exit 1; }
          grep -q 'case "\$DEST"' migrate-nas.sh  || { echo "::error::migrate-nas.sh 缺少 DEST 白名单校验"; exit 1; }
          # README 不得指导用户无校验地执行 main 分支脚本
          ! grep -qE 'raw\.githubusercontent\.com/.*/main/migrate-nas\.sh' README.md \
            || { echo "::error::README 仍指导从 main 拉取并执行迁移脚本"; exit 1; }

      - uses: actions/upload-artifact@<PIN-SHA>
        if: always()
        with: { name: deps, path: deps.txt }
```

**另外两条不属于 CI 但同样廉价、建议同批做掉**：

```bash
# 本地 pre-commit 可用的单行检查（30 秒）
python -m compileall -q core web sites tools tests scheduler.py manage.py \
  && python -m pyflakes core web sites tools tests scheduler.py manage.py \
  && python tests/test_regressions.py
```

```yaml
# .github/dependabot.yml  —— actions 与 pip 都自动升级（配合 SHA pin 才安全）
version: 2
updates:
  - { package-ecosystem: github-actions, directory: "/", schedule: { interval: weekly } }
  - { package-ecosystem: pip,            directory: "/", schedule: { interval: weekly } }
```

---

## 附录 A：本次审查的实测记录（可复现）

```text
环境：macOS / Python 3.9.9；/opt/homebrew/bin/python3.12 = 3.12.13；/tmp/_d_venv（3.12 隔离环境）
Docker 守护进程：不可用（本机未启动）→ 镜像构建/容器运行相关结论基于静态分析+C 级复现，已逐条标注

1) python3 tests/test_regressions.py
   → 13 组全过，real 0.37s，exit 0；/tmp 新增 3 个目录共 ~724KB

2) python3 -m pytest tests/ -q          → "no tests ran in 0.29s"，exit 0    ← 假绿灯
   python3 -m pytest tests/test_regressions.py -q → "no tests ran"，exit 0

3) python3 -m compileall -q core web sites tools scheduler.py manage.py tests
   → 无输出 exit 0
   /opt/homebrew/bin/python3.12 -m compileall -q …  → exit 0（3.12 语法兼容）

4) python3 -m pyflakes <全部 py>  → exit 1，5 条（core/mounts.py:9、core/doctor.py:84、
   tools/loc_fill_details.py:16、official_pdf_by_cids.py:30 ×2）

5) 变异测试（/tmp/_mut 仓库副本，8 个变异）
   M1 download 去掉 min_bytes 判定                  → exit 0（漏）
   M2 配额窗口 hours=1 → minutes=1                  → exit 0（漏）
   M3 _title_link 恒返回 "new"（非幂等）            → exit 0（漏）
   M4 importer 不再合并 item 详情                   → exit 0（漏）
   M5 count_books subjects 恒 false                 → exit 1（抓到）
   M6 _dest_dir 放宽 \ 与 NUL                       → exit 1（抓到）
   M7 loc era 识别失效                              → exit 1（抓到）
   M8 pdfbuild 去掉 os.replace                      → exit 1（间接）

6) 依赖解析验证（/tmp/_d_venv，Python 3.12.13，最新允许版本）
   fastapi 0.162.0 / starlette 1.10.0 / pypdf 6.21.3 / jinja2 3.1.6 / pillow 13.0.0
   → python tests/test_regressions.py：13 组全过 exit 0
   → 起 uvicorn 冒烟：/api/health 200、/ /explore /review /jobs /settings 200、
     /api/stats /api/doctor 200、/api/bookmarks(缺参) 422、/read/abc 422

7) entrypoint 信号复现（/tmp 等价 sh 脚本）
   进程树：sh(18525) ├─ sleep 120(18530) └─ sleep 5(18534)
   kill -TERM 18525 后：18530 PPID 变为 1（孤儿）← 后台 scheduler 子 shell 不转发信号

8) migrate-nas.sh 参数探测（/tmp/_shelltest/_probe.sh 复刻 :29-30,142-143）
   DEST="/"     → STAGE_DIR="/.migrate-staging"
   DEST=".."    → STAGE_DIR="../.migrate-staging"
   DEST="/share/x/.." → STAGE_DIR="/share/x/../.migrate-staging"
   脚本中 --dry-run / DRY：0 处（不支持）
   sh -n migrate-nas.sh：语法 OK

9) 文档核对：DESIGN.md:197 列出的 5 个 API 在 web/app.py 中全部不存在（grep 0 命中）；
   htmx 0 命中；apscheduler 0 命中；web/app.py 中 token/Authorization 0 命中

清理：/tmp/_mut、/tmp/_d_venv、/tmp/_shelltest、/tmp/_pv、/tmp/_appdata 及测试残留临时目录
      （本机 tempfile.gettempdir() 为 /var/folders/…/T，不在 /tmp 下，已逐个按内容确认后删除）已全部删除；
      git status 仅剩 `?? docs/review/`，未 commit。
```

## 附录 B：与其它审查员的交界（避免重复劳动）

| 交界点 | 我的结论 | 建议归属 |
|--------|---------|---------|
| `/upload-snapshot` 的路径处理与体积上限 | 我只从"无鉴权 + root"角度记为 P1-D20；具体路径校验/zip 炸弹请以审查 B 为准 | B |
| `_dest_dir` / `upsert_book` 的校验强度 | 测试覆盖了 `..`/`\`/NUL ✓（变异 M6 能抓到），但 `importer`/`doctor` 是否绕过未测（P2-D6） | A |
| `scheduler.py` 收到 SIGTERM 的行为 | 我从容器角度记录了"信号不转发 → 孤儿进程"（P1-D17）；scheduler 内部是否需要 SIGTERM handler / 断点保存请以审查 C 为准 | C |
| pyflakes 的 5 条警告 | `core/mounts.py:9`、`core/doctor.py:84` 落在 A 的范围 | A |

## 附录 C：本次审查**未能验证**的项（诚实声明）

| 项 | 原因 | 报告中的处理 |
|----|------|-------------|
| `docker build` 实际耗时、镜像大小、QEMU 多架构真实时长 | **本机 Docker 守护进程未运行**（`failed to connect to the docker API`），且我作为子代理无权启动它 | P2-D11 的时长估算标注为"估算"，并给了可验证的优化建议而非结论 |
| `entrypoint.sh` 在真实容器里的信号行为 | 同上（无 Docker） | 用 `/tmp` 等价 sh 脚本复现了进程模型与孤儿现象（附录 A-7），结论有依据但**未在真容器内验证** |
| `read_only: true` 是否会导致应用写失败 | 需真容器验证 | P1-D21 的建议里明确写了"上生产前先在一台测试 NAS 上跑一天确认" |
| `migrate-nas.sh` 端到端执行 | 需 QNAP + 真数据卷，且脚本会动宿主机 | 只做参数层/语法层/逻辑层审查；加固 diff 未在真机上跑过，建议先在 `--dry-run` 下验证 |
| CI 是否真能通过 | 无法触发 GitHub Actions | 用本机等价命令逐条跑过（附录 A 1-4、6），pin SHA 处留了 `<PIN-SHA>` 占位 |
