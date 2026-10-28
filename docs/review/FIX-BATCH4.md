# 批 4 修复报告：工程化（entrypoint 信号 / compose / migrate-nas 加固 / Dockerfile / 文档对齐）

> 执行日期：2026-10-28
> 依据：`docs/review/D-testdeploy.md`、`docs/review/SUMMARY.md` §1 P1 表 #15/#16/#20、`docs/review/C-adapters.md` §3
> 范围：`entrypoint.sh`、`docker-compose.yml`、`Dockerfile`、`migrate-nas.sh`、`README.md`、`docs/DESIGN.md`、`docs/READER-DESIGN.md`、`.dockerignore`
> **未触碰** `core/`、`web/`、`sites/`、`scheduler.py`、`tests/`（批 1/2/3 的写入范围）
> 未 `git commit`；所有自测脚本在 `/tmp`，未进仓库

---

## 0. 一览

| # | 条目 | 对应审查项 | 状态 |
|---|------|-----------|------|
| 1 | `migrate-nas.sh` 加固（DEST 白名单 / `${VAR:?}` / `--dry-run`+`--yes` / tar 穿越校验） | D P1 (§5)、SUMMARY P1#16 | ✅ 13 个自测用例全过 |
| 2 | `entrypoint.sh` 信号转发与进程监督（消除孤儿进程） | D P1-D17、SUMMARY P1#15 | ✅ 4 场景全过（含 TERM 优雅退出） |
| 3 | `docker-compose.yml`：healthcheck（503 语义）、资源限制、只读根、安全注释 | D P1-D14/D20/D21 | ✅ 3 场景验证 |
| 4 | `Dockerfile`：健康检查按 JSON 判定、root/依赖两项评估结论落注释 | D P1-D14、P2-D13/D5 | ✅ |
| 5 | `docs/DESIGN.md` 与实现对齐（API 清单 / APScheduler / htmx / Token / 签名 / 模块名） | D §6、C §3 | ✅ 逐条可核 |
| 6 | `README.md`：curl pin 版本+sha256、已知限制与安全边界、部署前置、latest 风险 | D P1-5.1-f、P1-D9 | ✅ |
| 7 | `docs/READER-DESIGN.md`：R1–R3 已完成 / **R4 未实现**、里程碑加状态列 | D §6 | ✅ |
| 8 | `.dockerignore`：遗留脚本排除理由 | C §2 | ✅ |

**语法检查**：`entrypoint.sh`、`migrate-nas.sh` 均 `bash -n` / `dash -n` 双通过（可执行位保留）。
**回归**：未改动业务代码；`python3 tests/test_regressions.py` 当前因**批 2 正在改 `na_jp.harvest_step` 返回值**（新增 `reached_end`/`next_page`）而红，属批 3 待跟进项，与本批无关（详见 §7）。

---

## 1. `migrate-nas.sh` 加固

### 1.1 问题（D P1，SUMMARY P1#16）

实测原脚本：`DEST` 直接来自 `$1`，无任何校验，而它被拼进 `rm -rf` / `mkdir -p` / `tar -x -C`：

```
DEST="/"            → STAGE_DIR="/.migrate-staging"     → tar -x -C / 可写满 QNAP 系统分区
DEST=".."           → STAGE_DIR="../.migrate-staging"
DEST="/share/x/.."  → STAGE_DIR="/share/x/../.migrate-staging"
--dry-run：0 处（不支持）
```

### 1.2 改动

| 项 | 改动 |
|----|------|
| 参数解析 | 新增 `--dry-run/-n`、`--yes/-y`、`--help/-h`；未知参数 `exit 2`；位置参数多于 1 个报错 |
| **默认干跑** | `DRY_RUN=1` 为默认，必须显式 `--yes` 才动手（"改个参数就毁数据的脚本，默认不执行"） |
| DEST 白名单 | ① 先判 `DEST="/"` 再剥尾部斜杠（否则 `/` 会被 `${DEST%/}` 剥成空串，报错信息变成"收到: ''"）；② 必须绝对路径；③ 禁 `..`；④ 必须 `/share/*`；⑤ 排除 `/share`、`/share/Container`、`/share/Container/eightnations` 这类过宽目标；⑥ 已存在但非目录则拒绝 |
| `${VAR:?}` 保护 | `STAGE_DIR` 用 `: "${STAGE_DIR:?…}"` 断言非空，并断言以 `"$DEST"/` 开头；`HELPER` 同样断言 |
| 安全清理 | 新增 `cleanup_stage()`：只允许删 `"$DEST"/.migrate-staging`，路径不符就打印"保护性跳过"，绝不裸 `rm -rf` |
| 副作用收口 | 所有 `docker pull/stop/rm/run -d`、`mkdir -p`、`tar -x`、`rm -rf` 统一经 `run()`，干跑只打印 |
| tar 穿越校验 | 解包前 `tar -t` 扫描成员，命中 `^(/|\.\./|.*(^|/)\.\.(/|$))` 即拒绝并清理暂存目录 |
| 解包加固 | `tar -x … --no-same-owner`（NAS 上属主无意义，避免 uid 映射带来的权限问题） |
| 幂等 | 保留原有"`$DEST/db/library.db` 不比源卷新则跳过拷贝"；暂存目录每次先清 |
| 文案 | 头部用法改为"下载→校验→阅读→干跑→真跑"五步；heredoc/注释说明每条护栏的原因 |

### 1.3 自测（13 用例，`/tmp/_mnstest`，假 `docker` 桩 + 沙箱目录）

> 说明：本机无 Docker daemon、非 root，无法建 `/share`。**路径校验用例用真脚本**；
> **执行路径用例**用脚本副本把 `/share/*` 白名单前缀临时替换为可写沙箱（副本仅在 `/tmp`，仓库内脚本未变）。

| # | 用例 | 期望 | 实测 |
|---|------|------|------|
| 1 | `--yes /` | 拒绝 | ✅ `✗ 目标路径不能是根目录 '/'`，退出 1 |
| 2 | `--yes ..` | 拒绝 | ✅ `✗ 必须是绝对路径（收到: '..'）` |
| 3 | `--yes /tmp/x` | 拒绝 | ✅ `✗ 必须位于 /share/ 之下` |
| 4 | `--yes /share` | 拒绝 | ✅ 同上（不命中 `/share/*`） |
| 5 | `--yes /share/Container` | 拒绝 | ✅ `✗ 目标路径过于宽泛` |
| 6 | `--yes /share/x/../y` | 拒绝 | ✅ `✗ 不得包含 '..'` |
| 7 | `--yes share/x`（相对路径） | 拒绝 | ✅ `✗ 必须是绝对路径` |
| 8 | 不带 `--yes` | 只干跑 | ✅ 无任何写类 docker 调用 |
| 9 | 合法路径 + 干跑 | 打印计划 | ✅ 完整打印 `mkdir/rm/tar -x/docker run -d`，退出 0，盘上无变化 |
| 10 | tar 含 `../../../_ESCAPE_PROBE.txt` | 拒绝解包 | ✅ `✗ 打包文件含绝对路径或 '..' 成员`，**逃逸文件未生成** |
| 11 | 正常真跑 | 成功 | ✅ db + 3 个 PDF 落地，`✓ PDF 数量一致（3）`，暂存目录已清 |
| 12 | 幂等重跑 | 跳过拷贝 | ✅ `固定路径数据不比源卷新…跳过拷贝` |
| 13 | PDF 数量不一致（源 99 / 落地 3） | 拒绝删容器 | ✅ `✗ 数量不一致…不删旧容器`，且 **`docker rm -f` 零调用** |

附加：所有拒绝用例的 `docker` 调用日志为空 —— **校验发生在任何 docker 操作之前**。

---

## 2. `entrypoint.sh` 信号转发（D P1-D17）

### 2.1 问题

原实现：后台 `( … ) &` 起 scheduler 循环（无 trap），前台 `exec uvicorn`。
`exec` 让 uvicorn 顶替 PID 1，**没人再负责给后台 scheduler 转发信号**。

实测（上一轮审查）：`kill -TERM` 主进程后，`sleep 120` 的 PPID 变 1 → 孤儿进程继续写 `/data`，
与重启后的新容器**可能双写同一目录**。

### 2.2 改动（重写为"监督者"模型）

```
entrypoint.sh (PID 1)
  ├─ scheduler_loop（后台子 shell）→ python3 scheduler.py
  └─ python3 -m uvicorn（后台）+ 主脚本轮询监督
```

| 项 | 改动 |
|----|------|
| 不再 `exec uvicorn` | 主脚本常驻做监督者，才有人收 SIGTERM |
| trap | `TERM → shutdown_all; exit 143`、`INT → … exit 130`、`HUP` 忽略；trap 用单引号避免变量在注册时被展开 |
| 优雅停机 | 先 TERM **真正的 `python3 scheduler.py`** → 等 8s → 再 TERM web → 等 5s → 超时 KILL；预算贴合 `docker stop` 默认 10s |
| 无孤儿 | `kill_tree()` 递归收后代（先自己、再子进程，见 §2.3 踩坑 1）；`SCHED_PY` 通过文件传给主脚本（踩坑 2） |
| 任一进程崩溃 → 容器退出 | uvicorn 退出 → 用它自己的退出码退出；scheduler 连续 5 次快速崩溃 → 退出 1。交给 `restart: unless-stopped` |
| 等待方式 | 主脚本 1s 轮询 `kill -0`，需要退出码时在 main 里直接 `wait`（踩坑 3） |
| 数据目录 | 用 `EIGHTNATIONS_DATA`（默认 `/data`）而非硬编码，便于本机自测 |
| 注释 | 每个反直觉决定都写了"为什么"（含三条实测踩坑） |

### 2.3 实施中实测发现并修掉的 3 个坑（否则会"看起来修好了其实没有"）

| # | 现象 | 根因 | 修法 |
|---|------|------|------|
| 1 | scheduler 的 trap 不执行，只收到 KILL | `kill_tree` 先递归子进程、最后用**负 PGID 打整组**，SIGTERM 到达两次；dash 在 trap 执行前就被第二个 TERM 终止（probe 文件为空） | 改为**先发自己、再递归子进程**，且不再用负 PGID |
| 2 | 停机时 `SCHED_PY` 恒为空，python3 只能被硬杀 | `scheduler_loop &` 跑在**子 shell**，内部变量传不回主脚本；dash 也不支持"`set -m` 后后台函数在当前 shell 执行"（实测变量仍不可见） | 循环把 PID 写进 `SCHED_PY_FILE`，主脚本 `current_sched_py()` 读取并校验存活 |
| 3 | uvicorn 退出码丢失（恒为 127） | dash 里"桩子 shell 内的 `wait <父 shell 的作业>`"拿不到，静默返回 127 | 改为**主脚本自己轮询 + main 里直接 `wait "$WEB_PID"`**（实测能拿到真实码） |

另修 2 个顺序 bug：`SCHED_PY_FILE` 在赋值前被使用（`set -u` 直接退出）、`scheduler_loop &` 启动行在一次编辑中被误删。

### 2.4 自测（4 场景，`/tmp/_eptest3`，假 `python3` 桩 + TMPDIR 隔离）

> 本机无 Docker，用等价的 sh 进程模型复现；**容器内行为标注为"静态验证/待真机验证"**。

| # | 场景 | 期望 | 实测 |
|---|------|------|------|
| 1 | 外部 `kill -TERM`（模拟 `docker stop`） | 退出 143；scheduler 的 `python3` **收到 TERM 并优雅退出**；无残留 | ✅ `退出码=143`、`scheduler 收到 TERM = [GOT-TERM]`、`残留进程=0` |
| 2 | uvicorn 自行崩溃（exit 7） | 整体退出 7；scheduler 不残留 | ✅ `退出码=7`、`残留=0`，日志 `✗ uvicorn 意外退出（code=7）` |
| 3 | scheduler 连续 5 次快速崩溃 | 整体退出 1 | ✅ `退出码=1`、`残留=0`，日志 `✗ scheduler 监督循环已退出` |
| 4 | 全新部署（`/data` 不存在） | 自动建 `logs/` 与 `db/` | ✅ 两目录均创建 |

---

## 3. `docker-compose.yml`

| 项 | 改动 | 原因 |
|----|------|------|
| healthcheck | 新增；`CMD python3 -c` 解析 `/api/health` 的 **`ok` 与 `db` 字段**；`interval 60s / timeout 10s / retries 3 / start_period 60s` | 配合批 1 的 503 语义；旧写法"HTTP 200 即健康"会让 DB 坏掉的容器显示 `(healthy)` |
| 资源限制 | `mem_limit: 2g`、`memswap_limit: 2g`、`cpus: 2.0`、`pids_limit: 512`、`nofile 4096/8192` | NAS 上通常还有别的容器，一次大批量下载就能拖垮整机 |
| 加固 | `read_only: true`、`tmpfs /tmp:128m`、`security_opt: no-new-privileges` | 应用只写 `/data` 与 `/tmp`；被拿下也无法改写程序文件 |
| 安全注释 | 文件头加"无鉴权/仅局域网/禁止端口映射/SSRF 注意/root 原因"；端口处加"可改 127.0.0.1 或内网 IP"；image 处加"建议钉版本" | 让风险在部署现场可见 |
| 挂载注释 | 说明"宿主机路径不存在时 Docker 会建空目录 → 容器正常跑但看不到旧书" | 这是最静默的失败模式 |

### 3.1 自测

- YAML 解析通过；`healthcheck/mem_limit/read_only/cpus/pids_limit/tmpfs/security_opt/ulimits` 齐备。
- **healthcheck 命令三场景实测**（从 compose 里把命令抠出来跑，本地起桩服务器，换端口 18099）：

| 场景 | 期望 | 实测 |
|------|------|------|
| HTTP 200 + `{ok:true,db:true}` | 0 | ✅ 0 |
| HTTP 503 + `{ok:false,db:false}` | 1 | ✅ 1 |
| 服务不可达（连接拒绝） | 1 | ✅ 1 |

- **踩坑记录**：第一版把命令写成多行 Python，compose 的 YAML 把换行**折叠成空格** →
  `import … try: …` 变成语法错误 → 而"语法错误恰好退出 1"，会让健康检查看起来"永远失败/永远 pass"。
  现已改为**单行**，并在注释里写明原因。

---

## 4. `Dockerfile`

| 项 | 改动 |
|----|------|
| HEALTHCHECK | 从"urlopen 不抛异常即健康"改为**解析 JSON 的 ok/db**；`start-period` 20s → **60s**（首次建库 + 老库 ALTER 迁移需要时间）；`--retries=3` |
| root 运行 | **不改默认行为**（会破坏老部署），但把评估结论写进注释：保持 root 的理由、真实风险面（上传/解析第三方 PDF/无鉴权）、以及两步低风险改进路径（可选 `gosu` + PUID/PGID；长期 uid 1000） |
| 依赖未锁定 | 注释记录"无 lock/hash → 不可复现、无供应链防护"，并给出缓解约定（升级前跑回归 + 冒烟）与 TODO（`requirements.lock` + `--require-hashes`） |
| 无 VOLUME | 保留并扩展说明（匿名卷级联删除是 NAS 事故根因；不声明的失败模式更诚实） |
| 进程模型 | 注释说明"不要把 CMD 改成 `exec uvicorn`"（会重新引入孤儿进程） |
| `ENV TMPDIR=/tmp` | 显式声明临时目录，配合 compose 的 `tmpfs /tmp` |

自测：HEALTHCHECK 命令提取后 `compile()` 通过；`.dockerignore` 与 6 条 `COPY` 逐条比对无冲突；
构建上下文估算 ~2.0 MB（`data/` 717MB 已被排除）。
**`docker build` 未执行** —— 本机 Docker daemon 不可用，标注为"待真机验证"。

---

## 5. 文档对齐（改前 → 改后）

### 5.1 `docs/DESIGN.md`

| # | 位置 | 改前 | 改后 |
|---|------|------|------|
| 1 | §6 接口（原 :197） | `/api/books`、`/api/books/{id}`、`/api/queue/approve\|ignore`、`/api/jobs`、`/api/sources` —— **5 个全不存在** | 新增 **§6.1 实际路由清单**：7 个页面 + 18 组 API，逐条与 `web/app.py` 对照；并保留一行警告"早期设计稿列的这些都不存在"（防止有人从旧文档摘抄） |
| 2 | §6 §鉴权（原 :198） | "仅局域网访问 + **可选简单 Token**" | 明确"**没有任何鉴权，也没有可选 Token**；任何能访问 8080 的设备＝管理员" |
| 3 | §5 调度器（原 :178） | "APScheduler（进程内）" | "**纯标准库 `scheduler.py`**（while 心跳 + sleep）"，附早期计划说明；`requirements.txt` 无 apscheduler |
| 4 | §6 标题（原 :192） | "FastAPI + Jinja2 + **htmx**" | "FastAPI + Jinja2 + **原生 JS**"，附"未使用 htmx"说明 |
| 5 | §11 汇总表 | 前端栈含 htmx / 调度器 APScheduler | 两行均改为实际实现 |
| 6 | 架构图（:38） | `APScheduler` | `scheduler.py` |
| 7 | 架构图（:47） | `sites/ndl_jp.py ...`（未实现） | `sites/bnf.py` |
| 8 | §3 契约（:140） | `download_item(self, meta, dest_dir, limiter)` | 实际签名 `(self, meta, dest_dir, http, quality="auto", progress=None)`，注明以 `sites/base.py` 为准 |
| 9 | §3 公共内核（:148） | `fetch.py`（不存在） | 改为**真实模块表**（http/pdfbuild/limiter/pipeline/db/importer/text/mounts/doctor），注明 `fetch.py` 已改名 `http.py`，并声明"活跃时段未实现" |
| 10 | §3 适配器表 | `ndl_jp.py` 像已存在 | 标注 **⏳ 未实现**（文件不存在、注册表未注册，`sources` 表有种子行，启用会 `KeyError`）；`idp.py`/`onb.py` 同样标注"尚未创建" |
| 11 | §7 部署（:252-259） | "镜像含 apscheduler"、"部署后健康检查 GET /api/health"（含糊） | 重写为：无 apscheduler、进程模型（PID 1 → scheduler + uvicorn）、健康检查 **503 语义**、日志与轮转、资源限制、加固项 |
| 12 | §7 新增 §7.1 | （无） | **安全边界表**：鉴权/网络/传输/运行身份/SSRF/CSRF/上传/静态暴露，逐项"现状 + 影响 + 约定" |
| 13 | §12 当前进度（:355） | "M1 完成，下一步 M2——APScheduler 接入" | "M0–M5 已完成，M6 进行中（na_jp ✅ / BnF ✅ / ndl_jp 待验证）" |
| 14 | §5 心跳 | 未说明节律 | 补"固定节拍：`max(1, 900 - 本轮耗时)`"（批 2 已实现） |
| 15 | §5 目录巡检 | "每周触发一次""7 天过期" | 补 `sources.catalog_cursor` 翻页游标、`exhausted` 严格四条件、修前"没抓满预算就封存"的问题 |
| 16 | §5 失败重试 | "指数退避，重试 ≥5 次" | 改为实际：**上限 5 次**（`MAX_ATTEMPTS`），判断在 `claim_and_start` 的 `UPDATE … AND attempt < ?` 内原子生效；人工重试重置计数；新增"补齐欠卷"（`needs_topup`）与"冷却持久化"（`last_blocked_at`） |

### 5.2 `README.md`

| # | 位置 | 改前 | 改后 |
|---|------|------|------|
| 17 | 迁移脚本指引（:262-290） | `curl -O …/main/migrate-nas.sh` + `sh migrate-nas.sh`（以 root 直接执行可变来源） | 五步：钉固定版本 `$VER` → `shasum -a 256` 校验 → `less` 阅读 → `--dry-run` → `--yes`；并列出脚本内置 4 条护栏 |
| 18 | compose 拉取（2 处） | `…/main/docker-compose.yml` | `…/v0.1.0/docker-compose.yml` |
| 19 | 状态机图（:57） | `failed(重试5次)`（与实现不符的风险） | 保留 `重试 5 次`（已与批 2 的 `MAX_ATTEMPTS=5` 核实一致），并补充"人工重试重置计数 / 欠卷自动补齐 / 限流冷却 30 分钟" |
| 20 | 回归测试（:355） | "（9 组，全部离线）" | "（全部离线，零联网）"，数字不写死；留 `<!-- TODO(批3) -->` 等 pytest 化后补 |
| 21 | NAS 部署（:176-196） | 未提鉴权、未提"必须先建目录"、`latest` 风险未提 | 加安全提醒链接、**必须先建目录**的原因（否则 Docker 建空目录、静默看不到旧书）、`latest` 风险与"钉版本"建议、`docker ps` 的 `(healthy)` 含义 |
| 22 | 匿名卷事故段（:245-260） | "镜像的 `VOLUME /data` 会让 Docker 自动分配匿名卷"（读起来像当前镜像有 VOLUME） | 区分**历史镜像**与**现状**（2026-10 起已移除 VOLUME，失败模式改为"落在可写层、重建即丢，但更诚实"） |
| 23 | 新增「已知限制与安全边界」 | （无） | 新增一节：安全（鉴权/网络/传输/root/SSRF/CSRF/上传/静态暴露 + 最小加固建议）、运维（依赖未锁、tag、健康检查语义、日志）、**测试与质量现状**（含"变异实验 5/8 测不出"的如实披露） |
| 24 | 项目结构（:404-413） | 无 `tests/`、`docs/`；两个遗留脚本未说明 | 补 `tests/`、`docs/`；说明 `digital_archives_to_pdf.py` / `official_pdf_by_cids.py` 为早期实验脚本、已不参与运行、计划删除 |

### 5.3 `docs/READER-DESIGN.md`

| # | 位置 | 改前 | 改后 |
|---|------|------|------|
| 25 | 标题 | "（v1，待确认后开发）" | "（v1）"（R1–R3 已上线） |
| 26 | 文件头 | 无实现状态 | 新增**实现状态块**：R1/R2/R3 ✅；**R4 未实现**（`tesseract\|ocr\|FTS5\|speechSynthesis` 在 `core/ web/ sites/` 零命中），本文档为设计稿 |
| 27 | §7 里程碑表 | 只有"预估"列，看不出做到哪 | 加**状态列**：R1/R2/R3 ✅ 已上线、R4 ⏳ 未实现 |

### 5.4 `.dockerignore`

| # | 改动 |
|---|------|
| 28 | 给两个遗留脚本的排除项加注释（"能力已并入 sites/na_jp.py 与 core/pdfbuild.py，确保永不进镜像"） |

---

## 6. 与 Lead 提示的差异说明

Lead 提示"README/DESIGN 里关于重试5次/心跳15分钟/目录7天巡检的描述先不要写死，等批 2 最终实现"。
执行到文档阶段时 **`docs/review/FIX-BATCH2.md` 已生成**，因此我按其最终实现写实，并逐项回代码核实：

| 说法 | 回代码核实 | 结论 |
|------|-----------|------|
| 重试上限 5 次 | `core/db.py:130 MAX_ATTEMPTS = 5` | ✅ 已写实 |
| 人工重试重置计数 | `core/db.py:534 sets.append("attempt=0")  # 人工重试：从零开始计次` | ✅ 已写实 |
| 心跳 15 分钟 + 固定节拍 | `scheduler.py:38 HEARTBEAT_SEC=900`；`:251 max(1.0, HEARTBEAT_SEC - elapsed)` | ✅ 已写实 |
| 目录 7 天巡检 | `scheduler.py:39 CATALOG_MAX_AGE_S = 7 * 86400`（仍保留，但配合 `catalog_cursor` 游标） | ✅ 已写实并补充游标语义 |
| `exhausted` 严格定义 | `sites/na_jp.py:163-172` 注释与返回字段 `reached_end/next_page/failed` | ✅ 已写实 |

→ 本批**没有残留下需批 2 定稿的文档 TODO**（仅 README 保留 1 处 `TODO(批3)`，见 §7）。

---

## 7. 遗留 TODO 与待验证项

| # | 项 | 原因 / 归属 |
|---|----|------------|
| T1 | `README.md` 的 `<!-- TODO(批3) -->`：回归测试的**用例数/命令**待 pytest 化后补 | 批 3 正在改 `tests/`，数字未定 |
| T2 | **`docker build` / 容器内真实行为未验证** | 本机 Docker daemon 不可用（`failed to connect to the docker API`）。`read_only: true` 是否导致应用写失败、`mem_limit` 取值是否合适、healthcheck 在容器内的真实表现，均需**真机或 CI 冒烟** |
| T3 | `read_only: true` 需实测确认 | 静态看应用只写 `EIGHTNATIONS_DATA`(= `/data`) 与 `/tmp`；但 `uvicorn`/`python` 可能尝试写 `__pycache__`（`/app` 下）。**若真有写入，容器会启动失败** → 建议首次上机时观察 30 分钟；必要时把 `read_only` 去掉（其余加固保留） |
| T4 | `migrate-nas.sh` 的 `${VAR:?}` 加固目前只覆盖 `STAGE_DIR`/`HELPER` | `SRC`/`OLD_IDS`/`cid` 已有 `[ -n ]` 判断；如需更彻底，可再加 `: "${DEST:?...}"` 兜底（当前 DEST 有默认值 + 白名单，风险已消除） |
| T5 | 回归测试当前红（`na_jp.harvest_step` 断言） | **非本批引入**：批 2 给 `harvest_step` 新增了 `reached_end`/`next_page`/`failed` 字段，旧断言 `s == {5键}` 失效。属批 3 的测试更新范围 |
| T6 | `requirements.lock` / `--require-hashes` | 已在 Dockerfile 注释与 README「已知限制」中记录，未实施（Lead 说"不强求上锁"） |
| T7 | 非 root 运行（`gosu` + PUID/PGID） | 已在 Dockerfile 注释记录两步改进路径，未实施（会改默认行为，需与老部署迁移一起评估） |
| T8 | CI（`pull_request` 触发、actions pin SHA、latest 语义） | 不在本批写入范围（`.github/` 归批 3），已在本批报告与 D 报告中记录 |

---

## 8. 变更文件清单

```
 M .dockerignore            注释说明遗留脚本排除理由
 M Dockerfile               HEALTHCHECK 按 JSON 判定 / start-period 60s / TMPDIR / root 与依赖评估结论
 M docker-compose.yml       healthcheck(503) / 资源限制 / read_only+tmpfs+no-new-privileges / 安全注释
 M entrypoint.sh            监督者模型、trap 信号转发、kill_tree 递归收进程、PID 文件传参
 M migrate-nas.sh           DEST 白名单、${VAR:?}、--dry-run/--yes/--help、tar 穿越校验、run() 收口
 M README.md                curl pin+sha256、已知限制与安全边界、部署前置、latest 风险、重试/补齐语义、结构补全
 M docs/DESIGN.md           API 清单重写、APScheduler/htmx/Token 纠正、模块表、签名、§7.1 安全边界、调度语义
 M docs/READER-DESIGN.md    实现状态块、里程碑状态列
 + docs/review/FIX-BATCH4.md 本报告
```

未触及：`core/`、`web/`、`sites/`、`scheduler.py`、`tests/`、`.github/`、`requirements.txt`。
