#!/bin/sh
# ============================================================
# 八国联军图书馆 · NAS 一键迁移脚本
# 把散落在 Docker 匿名卷里的书库数据自动迁移到固定路径，
# 并用固定路径重建容器（自动发现旧卷/旧容器，幂等可重跑）。
#
# 用法（NAS SSH，管理员账号）:
#   sh migrate-nas.sh                       # 目标路径默认 /share/Container/eightnations/data
#   sh migrate-nas.sh /share/其他路径        # 自定义目标路径
#
# 下载后执行:
#   curl -O https://raw.githubusercontent.com/kelvinguo1988/eightnations-library/main/migrate-nas.sh
#   sh migrate-nas.sh
# ============================================================
set -u

DEST="${1:-/share/Container/eightnations/data}"
IMG="${EIGHTNATIONS_IMAGE:-ghcr.io/kelvinguo1988/eightnations-library:latest}"
NAME="eightnations"

say() { echo "[migrate] $*"; }
die() { echo "[migrate] ✗ $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "未找到 docker 命令（请用管理员 SSH）"

# ---------- ① 拉取/确认镜像 ----------
docker image inspect "$IMG" >/dev/null 2>&1 || {
  say "拉取镜像 $IMG …"
  docker pull "$IMG" || die "镜像拉取失败"
}

# ---------- ② 发现含书库数据的卷（books/ 标记） ----------
say "扫描 Docker 卷，寻找书库数据…"
DATA_VOLS=""
for v in $(docker volume ls -q); do
  if docker run --rm -v "$v":/from "$IMG" sh -c 'test -d /from/books' 2>/dev/null; then
    DATA_VOLS="$DATA_VOLS $v"
  fi
done
[ -n "$DATA_VOLS" ] || die "未发现任何含 books/ 的数据卷——如果数据已在固定路径，直接重建容器即可"

# 每个候选卷显示概况，并选出 db 最新的一卷为源
SRC=""; SRC_TS=0
for v in $DATA_VOLS; do
  info=$(docker run --rm -v "$v":/from "$IMG" sh -c \
    'ls /from/books 2>/dev/null | wc -l; date -r /from/db/library.db +%s 2>/dev/null || echo 0')
  nbooks=$(echo "$info" | sed -n 1p)
  ts=$(echo "$info" | sed -n 2p)
  say "  候选卷 ${v}：${nbooks} 册，db 更新时间 $(date -d "@${ts}" 2>/dev/null || echo "$ts")"
  if [ "$ts" -gt "$SRC_TS" ]; then SRC_TS=$ts; SRC=$v; fi
done
[ -n "$SRC" ] || die "候选卷均无 db/library.db"
say "选定数据源卷：$SRC"

# ---------- ③ 停止并移除旧容器（同名或同镜像） ----------
say "停止/移除旧容器…"
for c in $(docker ps -aq --filter "name=$NAME"); do
  docker rm -f "$c" >/dev/null 2>&1 && say "  已移除容器 $c"
done
for c in $(docker ps -aq --filter "ancestor=$IMG"); do
  docker rm -f "$c" >/dev/null 2>&1 && say "  已移除容器 $c"
done

# ---------- ④ 拷贝数据到固定路径（目标已有更新数据则跳过拷贝） ----------
mkdir -p "$DEST"
need_copy=1
if [ -f "$DEST/db/library.db" ]; then
  dst_ts=$(date -r "$DEST/db/library.db" +%s 2>/dev/null || echo 0)
  if [ "$dst_ts" -ge "$SRC_TS" ]; then
    say "固定路径数据比源卷新（$dst_ts >= $SRC_TS），跳过拷贝"
    need_copy=0
  fi
fi
if [ "$need_copy" = 1 ]; then
  say "拷贝数据 $SRC → $DEST …（书越多越久，请耐心）"
  docker run --rm -v "$SRC":/from -v "$DEST":/to "$IMG" \
    sh -c 'cp -a /from/. /to/ && echo "  拷贝完成: $(ls /to | tr "\n" " ")"'
fi

# ---------- ⑤ 用固定路径重建容器 ----------
say "用固定路径重建容器…"
docker run -d --name "$NAME" --restart unless-stopped \
  -p 8080:8080 -e TZ=Asia/Shanghai \
  -v "$DEST":/data "$IMG" || die "容器启动失败（8080 端口被占用？）"

# ---------- ⑥ 自检 ----------
sleep 3
say "数据自检："
docker exec "$NAME" python3 manage.py doctor || true
say "完成 ✅ 打开 http://<NAS_IP>:8080 —— 设置页存储面板应显示 ✓ 已绑定固定路径"
say "确认一切正常后，旧匿名卷可在 Container Station 清理，或 docker volume rm <旧卷ID>"
