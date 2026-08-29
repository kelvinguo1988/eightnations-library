# 八国联军图书馆

从各国国家级数字图书馆自动采集流散中国典籍的 PDF，限速计划获取、新书发现与人工筛选，
并在局域网 Web 前端浏览与在线阅读。部署目标：QNAP NAS（Docker）。

- 设计与开发计划：[docs/DESIGN.md](docs/DESIGN.md)
- 八国馆藏调研页：[index.html](index.html)

## 当前进度

| 里程碑 | 状态 |
|--------|------|
| M1 骨架 + LoC 首册跑通 | ✅ 2026-08-29 |
| M2 LoC 全量流水线 | ✅ 永樂大典 41 册全部归档（调度守护 + 滑动配额 + 断点续传完整性校验） |
| M3 Web v1 | ✅ 五页全部上线并目检（书库/详情含内嵌阅读器/审核/任务面板/设置） |
| M4 日本迁移 | ✅ na_jp 适配器（实时收割 100 册 + 官方 contentDownload 下载实测 29 页）；ndl_jp 待下轮（需验证接口） |
| M5 Docker/QNAP | ✅ Dockerfile + compose + entrypoint 就绪（本机 Docker 未启动，待 NAS 构建验证） |
| M6 扩馆（BnF/ÖNB/IDP/…） | 未开始 |
| 待办 | 中国善本 1977 册缺条目详情：跑 `tools/loc_fill_details.py`（人工过盾一次，约 40 分钟）后 `manage.py import-details` + 重试 |

## 目录结构

```
core/        公共内核：db(SQLite/WAL) · http(限速/重试/Range续传完整性) · limiter(每小时配额)
             · pdfbuild(JPEG无损组PDF, 纯标准库) · pipeline(队列→下载→状态机) · models
sites/       每馆一个适配器：base.py(契约) · loc.py(美国国会图书馆) · na_jp.py(国立公文書館)
web/         FastAPI + Jinja2 前端：书库/详情(内嵌阅读器)/新书审核/任务面板/设置
tools/       loc_snapshot.py(目录快照半自动) · loc_fill_details.py(补条目详情, 可续跑)
fixtures/    真实快照样例（永樂大典 43 条 + 首条详情），用于开发/回归
data/        本地数据（git 忽略）：db/library.db · books/<馆>/<专藏>/<条目ID>/ · snapshots/
manage.py    管理CLI
scheduler.py 常驻调度守护：每 5 分钟心跳，按 sources 表配额逐源限量下载
Dockerfile / docker-compose.yml / entrypoint.sh   NAS 容器部署（web+调度同容器）
```

每本书落盘：`data/books/loc/yongle-da-dian/2018421651/{book.pdf, cover.jpg, meta.json}`

## 快速上手

```bash
pip install -r requirements.txt          # playwright 需另跑: playwright install chromium

python3 manage.py init-db
# 刷新目录（低频，LoC 专藏一年更新几次；两种方式二选一）
python3 tools/loc_snapshot.py --headed --with-items          # 弹浏览器，遇盾人工点一次复选框
python3 tools/loc_snapshot.py --from-dir ~/Downloads/loc     # 或手工另存 ?fo=json 页面后归档
python3 manage.py import-snapshot data/snapshots/loc/<时间戳目录>

# 日本国立公文書館（站点对脚本友好，直接实时收割）
python3 manage.py import-na-jp --fonds "https://www.digital.archives.go.jp/fonds/3611449?page=1" --pages 3

python3 manage.py books --status discovered              # 看新书（或 web /review 页勾选审批）
python3 manage.py approve --collection yongle-da-dian    # 批准入库（或 ignore 忽略）
python3 manage.py fetch-next --source loc --quota 10     # 手动单轮心跳
nohup python3 scheduler.py >> data/logs/scheduler.log 2>&1 &   # 或常驻守护（每5min心跳×每小时10册）
python3 manage.py stats

# Web 前端
python3 -m uvicorn web.app:app --host 0.0.0.0 --port 8080     # http://127.0.0.1:8080
```

## QNAP NAS 部署（M5，镜像已发布 ghcr.io）

镜像：`ghcr.io/kelvinguo1988/eightnations-library:latest`（linux/amd64 + linux/arm64 双架构，
由 GitHub Actions 在每次 main 推送 / v* 标签时自动构建发布）。

**Container Station 步骤**：
1. File Station 先建共享文件夹 `/share/Container/eightnations/data`（存数据库与书）
2. Container Station → 应用程序 → 创建应用程序 → 粘贴仓库里的 `docker-compose.yml` → 创建
3. 打开 `http://<NAS_IP>:8080`

或 SSH 直接：
```bash
mkdir -p /share/Container/eightnations/data
cd /share/Container/eightnations
curl -O https://raw.githubusercontent.com/kelvinguo1988/eightnations-library/main/docker-compose.yml
docker compose up -d
```

首次启动自动建库；之后把 Mac 上的 `data/` 拷过去即可继承已归档书目，或从零开始采集。
版本发布：`git tag v0.1 && git push --tags` 会额外产出 `:0.1` 镜像（latest 始终跟随 main）。

## 已知待办

- 中国善本 1977 册集合级快照缺"逐页清单/官方PDF直链"（LoC 只给页数整数）：
  `python3 tools/loc_fill_details.py`（弹浏览器人工过盾一次后自动补齐，可断点续跑）
  → `python3 manage.py import-details data/snapshots/loc/details` → 任务面板"重试"。
  在此之前这些书下载会明确报"缺条目详情"并停在 failed，不会误下。
- Web 静态挂载暂不支持 Range 请求（浏览器内嵌阅读不受影响，拖动进度条为渐进加载）。
- ndl_jp（日本国立国会图书馆）适配器待接口验证后接入。

## 关键事实（决定架构，2026-08-29 实测）

1. `www.loc.gov` 元数据在 Cloudflare 盾后 → "发现新书"走低频半自动快照（tools/loc_snapshot.py）。
2. `tile.loc.gov` 图像与官方 PDF **无盾** → 下载层可数月无人值守直连。
3. **LoC 条目自带官方整本 PDF 直链**（`resources[].pdf`，约 200KB/页）→ 首选直接下载；
   无 PDF 的条目回退 IIIF 逐页下载 + 本地无损组 PDF（`/full/1600,/` 分级档实测可用）。

## 限速与礼貌约定

每域串行 + 最小间隔(tile 1s / www 3s) + 抖动；每小时配额默认 10 册；
诚实 UA（eightnations-archiver）；仅采集 rights 为公有领域/无已知版权限制的条目，
meta.json 记录原馆链接与校验和。Image credit: Library of Congress, Asian Division.
