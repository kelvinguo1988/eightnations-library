# 八国联军图书馆

从各国国家级数字图书馆**自动采集流散中国典籍的 PDF**：限速计划获取、新书发现与人工
审核、局域网 Web 前端浏览与在线阅读。部署目标为 QNAP NAS（Docker），本机亦可全功能
开发运行。

- 设计文档与架构决策：[docs/DESIGN.md](docs/DESIGN.md)
- 八国馆藏调研页（哪些馆藏了什么、去哪找）：[index.html](index.html)
- 仓库：https://github.com/kelvinguo1988/eightnations-library

## 功能特性

- **多馆适配器**：每馆一个 `sites/<id>.py`，统一"目录发现 → 人工审核 → 计划下载"流水线
- **防封禁采集**：预算制增量收割、跨进程文件锁节流、限流自动冷却与续传、页级断点
- **每小时配额**：每源每小时 ≤N 册的真实滑动窗口，可按馆独立配置
- **Web 前端**：书库筛选/详情内嵌阅读器/新书审核（可勾选立即下载）/任务面板/设置
- **元数据完整**：书名（原题+罗马字）、著者、朝代、主题词分类、架藏号、卷页数、权利声明
- **双架构镜像**：ghcr.io 自动构建 linux/amd64 + arm64，NAS 免构建直接拉取

## 各馆接入状态

| 馆 | 适配器 | 发现方式 | 下载方式 | 默认 | 规模 |
|----|--------|---------|---------|------|------|
| 🇺🇸 美国国会图书馆 | `sites/loc.py` | 快照半自动（Cloudflare 盾） | 官方 PDF 直链优先，IIIF 逐页兜底 | 启用 | 永樂大典 41 册 + 中国善本 2,028 条 |
| 🇯🇵 日本国立公文書館 | `sites/na_jp.py` | **自动收割**（站点无盾） | 官方 contentDownload 分块 PDF，IIIF 兜底 | 启用 | 汉籍 fonds 100+ 册（分类: 漢籍/旧藏文库/刊本类别/朝代） |
| 🇫🇷 法国国家图书馆 | `sites/bnf.py` | **自动收割**（目录 SRU 开放） | Gallica IIIF 逐页组 PDF | 停用 | 数字化中文语种文献 1,011 条 |
| 🇯🇵 日本国立国会图书馆 | `sites/ndl_jp.py` | 待实现 | 待实现 | 停用 | 待接口验证 |

> 未接入：英国 BL-IDP（Cloudflare 拦截）、意大利（地理封锁）、俄国（未数字化）——
> 详见 DESIGN.md 实测记录；剑桥 CUDL / 哈佛 / 巴伐利亚 MDZ 探测未通，暂缓。

## 快速开始（本机开发）

```bash
git clone https://github.com/kelvinguo1988/eightnations-library.git
cd eightnations-library
pip install -r requirements.txt        # playwright 需另执行: playwright install chromium

python3 manage.py init-db              # 建库（自动写入各馆种子配置）
nohup python3 scheduler.py >> data/logs/scheduler.log 2>&1 &   # 调度守护（可选）
python3 -m uvicorn web.app:app --host 0.0.0.0 --port 8080      # Web 前端
# 打开 http://127.0.0.1:8080
```

数据默认落在项目内 `data/`（可用环境变量 `EIGHTNATIONS_DATA` 指到别处）。

## 核心概念：数据流水线

```
① 发现（目录收割/快照导入）      ② 审核（人工筛选）        ③ 下载（计划任务）      ④ 阅读
discovered ──approve──▶ queued ──fetch──▶ running ──▶ done
     │                                  │
     └──ignore──▶ ignored               └──失败──▶ failed(重试5次)──▶ dead
```

- **发现**只产生 `discovered` 记录，**不会自动下载**；
- 审核批准后进 `queued`，由调度器按各馆配额消化；
- 下载页级断点续传，进程崩溃自动恢复，PDF 带页数校验与 sha256。

## 详细使用方法

### 1. 美国国会图书馆（LoC）

`www.loc.gov` 元数据在 Cloudflare 盾后，"发现"走低频半自动快照（专藏一年更新几次）；
图像/官方 PDF 在 `tile.loc.gov`（无盾），下载层全自动。

```bash
# ① 刷新目录快照（弹浏览器；遇人机验证点一次复选框，之后全自动翻页）
python3 tools/loc_snapshot.py --headed --with-items
#    或手工方式: 浏览器打开 https://www.loc.gov/collections/yongle-da-dian/?fo=json
#    另存 .json 文件到任意目录后:
python3 tools/loc_snapshot.py --from-dir ~/Downloads/loc

# ② 导入快照（同目录若有 item_*.json 会自动合并条目详情）
python3 manage.py import-snapshot data/snapshots/loc/<时间戳目录>

# ③ 中国善本多数条目缺"逐页清单/官方PDF直链"，需一次性补条目详情
#    （弹浏览器、过盾一次，之后全自动 ~40 分钟，可断点续跑）
python3 tools/loc_fill_details.py
python3 manage.py import-details data/snapshots/loc/details

# ④ 审核批准（CLI 或 Web /review 页勾选）
python3 manage.py approve --collection yongle-da-dian     # 或 --id N / --kw 关键字
python3 manage.py ignore --collection yongle-da-dian      # 忽略不想要的书
python3 manage.py retry --failed                          # 失败重排（含被限流的）

# ⑤ 调度器自动下载（也可 Web 任务面板点重试/审核页点立即下载）
python3 manage.py fetch-next --source loc --quota 10      # 手动单轮心跳
```

NAS 上刷新 LoC 目录：设置页有"导入快照 JSON"上传框（支持 collection_*.json 与
item_*.json 多选），无需 SSH。

### 2. 日本国立公文書館（na_jp）

站点对脚本友好，**全自动**：启用后调度器会自动收割目录（预算制，见下方节律），
新书出现在审核页。

```bash
# 手动收割（可选；重跑同一命令自动续传）
python3 manage.py import-na-jp --fonds "https://www.digital.archives.go.jp/fonds/3611449?page=1" --pages 50
python3 manage.py approve --source na_jp

# 朝代/著者/架藏号回填（manifest 的 Creator 字段含"編者:黎靖徳（宋）"式标注；
# 新收割自动携带，老数据跑一次即可，零联网）
python3 manage.py backfill-na-jp

# 分类富化：拉取条目详情页补 漢籍/旧藏来源(紅葉山文庫·昌平坂学問所·林羅山等)/
# 版本类别(和刻本·朝鮮刊本·明刊本·写本)。预算制每批 30 册（防触发 ~58 请求限流线），
# 批间隔建议 ≥3 分钟，重复执行自动续传
python3 manage.py enrich-na-jp --budget 30
```

多卷书（如《近思録》）自动从翻页链展开分卷，每卷一个 PDF；重试按卷跳过已完成部分。

### 3. 法国国家图书馆（BnF / Gallica）

目录 SRU 与 Gallica IIIF 均开放，**全自动**；默认停用，设置页勾选启用即可。
默认目录 URL 检索"数字化中文语种文献"（1,011 条），可换成任意 SRU 查询：

```
query=(bib.digitized all "freeAccess") and (bib.anywhere all "chinois")
```

> 写本特藏（伯希和敦煌写卷本体）的目录 SRU 为 403，暂无法自动发现。

### 4. Web 前端（http://<主机>:8080）

| 页面 | 功能 |
|------|------|
| **书库** | 封面卡片网格；按 关键字/馆/专藏/状态 筛选 + 朝代药丸（**多选/反选/未知**）+ 分类药丸（多选，日本分类与英文主题词分组）；书名自动转为繁体显示 |
| **详情** | 完整元数据（著者/朝代/主题/架藏号）、📖 在线阅读（内嵌 PDF）、⬇ 下载、采集记录（sha256） |
| **新书审核** | `discovered` 列表筛选勾选 → **入库下载（仅入队）/ 立即下载（后台线程即刻抓取）/ 忽略** |
| **任务面板** | 每馆进度条、最近任务、失败**重试**按钮、事件日志 |
| **设置** | 每馆 启用开关 / 每小时配额 / 图像档位 / 目录 URL（direct 馆）/ 快照上传（snapshot 馆） |

### 5. 调度与限速（防封禁设计）

调度守护 `scheduler.py` 默认 **15 分钟一轮心跳**，每轮依次执行：

1. direct 策略馆的**目录增量收割**——每心跳至多 40 条（`EIGHTNATIONS_CATALOG_BUDGET`），
   已入库跳过，未完成下个心跳续传；遇站点限流立即中止并**冷却 30 分钟**；
2. **下载心跳**——从 `queued` 取书，每源每小时 ≤ `hourly_quota` 册（真实滑动窗口）；
3. 回收 running 超 60 分钟的册（Web 进程崩溃残留）；每 6 小时裁剪事件日志。

所有对同一域名的请求（无论哪个进程/线程）共享最小间隔：
tile.loc.gov 1s、www.loc.gov 3s、日本站 2.5s、gallica 2s，外加随机抖动；
跨进程通过 flock 锁文件协调（`data/runtime/throttle/`）。

**环境变量**：

| 变量 | 默认 | 说明 |
|------|------|------|
| `EIGHTNATIONS_DATA` | `./data` | 数据根目录（NAS 上为 `/data`） |
| `EIGHTNATIONS_HEARTBEAT` | `900` | 调度心跳间隔（秒），拉长更保守 |
| `EIGHTNATIONS_CATALOG_BUDGET` | `40` | 目录收割每心跳条数上限 |

### 6. 图像档位

| 档位 | 含义 | 适用 |
|------|------|------|
| `auto` | 官方 PDF 直链优先，无直链时 1600px 组图 | 默认，推荐 |
| `pdf` | 仅官方 PDF（无则报错待补详情） | 强制官方 |
| `orig` | IIIF 原图逐页 | 珍本白名单 |
| `mid` / `thumb` | 1600px / 1024px 组图 | 省空间 |

设置页按馆配置；LoC 中国善本全量 `auto` 约 80 GB。

## QNAP NAS 部署（Container Station）

镜像：`ghcr.io/kelvinguo1988/eightnations-library:latest`（amd64+arm64，Actions 随 main 自动构建）。

1. File Station 建共享文件夹 `/share/Container/eightnations/data`
2. Container Station → 应用程序 → 创建 → 粘贴仓库里的 `docker-compose.yml` → 创建
3. 打开 `http://<NAS_IP>:8080`；设置页启用想要的书馆

或 SSH：

```bash
mkdir -p /share/Container/eightnations/data
cd /share/Container/eightnations
curl -O https://raw.githubusercontent.com/kelvinguo1988/eightnations-library/main/docker-compose.yml
docker compose up -d
```

**从本机迁移已有进度**（书 + 数据库 + 审核状态）：

```bash
rsync -av "/Users/<你>/Documents/coding/八国联军图书馆/data/" \
  admin@<NAS_IP>:/share/Container/eightnations/data/
```

**版本发布**：`git tag v0.1 && git push --tags` 额外产出 `:0.1` 镜像，`latest` 跟随 main。

## 数据与备份

```
data/
├── db/library.db              # 全部状态（书/任务/事件）——备份这一份即可
├── books/<馆>/<专藏>/<条目>/  # book.pdf(+多卷) · cover.jpg · meta.json(含sha256)
├── snapshots/                 # 目录快照与条目详情（可重导，非必需备份）
└── logs/                      # scheduler/web 日志
```

备份策略：拷 `db/library.db` + `meta.json`（图像可随时重下，不纳入备份）。

**重部署安全性**：全部持久数据（书目/审核状态/任务/分类/PDF）都在挂载卷
`/share/Container/eightnations/data`，`docker compose pull && up -d` 重建容器不触碰
数据卷——已实测（重建前后书目/任务/PDF 完全一致，随机抽检 PDF 可读）。
重建后自检：

```bash
docker exec eightnations python3 manage.py doctor
# 检查：库结构 / done 书目 vs 磁盘 PDF 对账 / 孤儿目录 / 站点配置 / 节流目录
```

## 故障排查

| 现象 | 原因与处理 |
|------|-----------|
| 书一直 `discovered` 没下载 | 正常——需在审核页批准后才进入队列 |
| 日本馆收割变慢/中断 | 站点对连续约 58 次请求限流；系统会自动冷却 30 分钟后续传，无需干预 |
| LoC 中国善本下载报"缺条目详情" | 跑一次 `tools/loc_fill_details.py` + `manage.py import-details`（见上文①） |
| `running` 卡住 | 调度器 60 分钟后自动回收重排；也可任务面板手动重试 |
| Web 手动触发与调度器同时抓同一本 | 已有原子认领与跨进程节流，无需处理 |
| ghcr 拉取慢 | 境内网络常见，重试或配置镜像加速 |

## 法律与采集约定

- 仅采集 **rights 为公有领域/无已知版权限制**的条目；`meta.json` 记录原馆链接与校验和
- 每域串行 + 最小间隔 + 随机抖动，诚实 UA（`eightnations-archiver`），每小时配额硬上限
- 图片致谢格式按各馆要求（如 Image credit: Library of Congress, Asian Division）

## 项目结构

```
core/     db(SQLite/WAL) · http(节流/重试/Range续传) · limiter(滑动窗口配额)
          pdfbuild(流式JPEG组PDF) · pipeline(状态机) · importer(快照导入) · models
sites/    base.py(契约) · loc.py · na_jp.py · bnf.py（每馆一个适配器）
web/      FastAPI+Jinja2 五页前端 + static/templates
tools/    loc_snapshot.py(目录快照半自动) · loc_fill_details.py(补条目详情)
fixtures/ 真实快照样例（回归测试用）
manage.py 管理CLI    scheduler.py 调度守护    Dockerfile/compose/entrypoint.sh
```

## 开发状态

M1-M5 已完成（骨架/LoC 全量/Web/日本/上 NAS），M6 扩馆进行中（BnF ✅）。
三轮代码审查累计修复 17 项（并发竞态、崩溃恢复、PDF 规范合规、跨进程节流等），
关键路径均有离线回归测试。贡献前跑 `python3 -m py_compile core/*.py sites/*.py`。
