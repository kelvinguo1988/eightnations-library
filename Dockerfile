FROM python:3.12-slim

WORKDIR /app
# PYTHONDONTWRITEBYTECODE=1：容器根文件系统只读（compose read_only: true）。
# 若允许写 .pyc，import 时会尝试在 /app/**/__pycache__ 落盘 → PermissionError，
# 进程直接起不来。设为 1 后完全不写字节码（镜像启动只需解析一次，无性能损失）。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EIGHTNATIONS_DATA=/data \
    TZ=Asia/Shanghai \
    TMPDIR=/tmp

# ---- 运行时依赖 ----
# 单一真相源 = requirements.txt（不在这里重复硬编码包名）。
# 先只 COPY 依赖清单，代码变动不会让 pip 层缓存失效。
#
# ⚠️ 已知限制：requirements.txt 只有宽松的版本区间、**没有 lock 也没有 hash**，
#    所以同一 commit 在不同时间构建出的镜像可能装到不同小版本（不可复现），
#    也无法抵御 PyPI 侧的供应链投毒。
#    → 缓解约定：升级/重建镜像前先跑 `python3 tests/test_regressions.py`（须全绿）
#      + 起容器冒烟 /api/health。真正修法是引入 requirements.lock +
#      `pip install --require-hashes`（列为 TODO，不在本轮改动）。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ core/
COPY sites/ sites/
COPY web/ web/
COPY tools/ tools/
COPY manage.py scheduler.py entrypoint.sh ./
RUN chmod +x entrypoint.sh && mkdir -p /data/db /data/logs

# ---- 刻意不写 VOLUME /data ----
# 镜像层声明卷会让"忘记挂 -v"的部署把数据静默写进**匿名卷**，而匿名卷在
# `docker rm -f` / 容器清理时会被级联删除（正是 2026-09 NAS 迁移事故的根因）。
# 不声明 VOLUME 时，忘记挂载的数据落在容器可写层：容器一重建就丢，但至少
# 不会在多份匿名卷之间"看起来有数据"地漂移，失败模式更诚实、更好排查。
# 数据目录必须显式挂载（见 docker-compose.yml 的 volumes 段）。

# ---- 刻意不加 USER（以 root 运行）：评估结论 ----
# 保持 root 的理由：现网 NAS 的挂载目录（/share/Container/…）为 root 属主，
# 改成非 root 会让**已有部署**无法写 /data（破坏性变更、静默失败）。
# 这是权衡后的决定，不是遗漏。风险面是真实的：容器内有「上传快照」（写文件）、
# 「下载远端 PDF」（解析第三方内容）、且 Web 无鉴权，root 会放大任何一处缺陷。
# 低风险改进路径（未实施，列为 TODO）：
#   ① 可选降权：镜像装 gosu，entrypoint 检测到 PUID/PGID 环境变量时
#      `chown -R` 后 gosu 切用户；不设置则行为与现在完全一致（零破坏）；
#   ② 长期：一次性 init 步骤把 /data 转交 uid 1000，再 `USER 1000`。
# 目前由 docker-compose.yml 的 read_only / no-new-privileges / 资源限制缓解。

EXPOSE 8080

# ---- 健康检查 ----
# 语义（与 web/app.py 的 /api/health 对齐，2026-10 起该接口不再恒 200）：
#   DB 可真正查询 → 200 + {"ok":true,"db":true} → 退出 0（healthy）
#   DB 损坏/被锁/磁盘满 → 503 + ok:false       → 退出 1（unhealthy）
# 这里必须解析 JSON 的 ok/db 字段，而不是"urlopen 不抛异常就算健康"：
# 旧写法（只看 200）会让容器在数据库坏掉时依然显示 (healthy)。
# start-period 给首次建库与老库 ALTER 迁移留时间。
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD python3 -c "import json,sys,urllib.request as u;d=json.load(u.urlopen('http://127.0.0.1:8080/api/health',timeout=8));sys.exit(0 if (d.get('ok') and d.get('db')) else 1)"

# ---- 进程模型 ----
# entrypoint.sh 是 PID 1：负责收 SIGTERM、优雅停 scheduler、并在任一进程
# 意外退出时让整个容器退出（交给 restart: unless-stopped）。
# 不要把这里改成 `exec uvicorn`（那样没人为后台 scheduler 转发信号，
# docker stop 后会残留孤儿进程继续写 /data）。
CMD ["sh", "entrypoint.sh"]
