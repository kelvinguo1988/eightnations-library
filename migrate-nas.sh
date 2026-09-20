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
#
# 安全顺序（务必如此，别再改）：
#   ① 拉镜像 → ② 发现源卷 → ③ 停止旧容器（只 stop，不 rm）
#   → ④ 先把卷数据落到宿主机 DEST 并校验 → ⑤ 才删除旧容器
#   → ⑥ 用固定路径重建 → ⑦ 等 /api/health 就绪 + doctor 自检。
#
#   为什么不能先删容器：旧容器挂载的正是**匿名卷**，`docker rm -f`
#   会级联删除未被引用的匿名卷；删完再 `-v $SRC:/from` 挂上的是同名
#   **空卷**（Docker 会静默新建），于是"迁移"出一座空库。
#   所以：数据没在宿主机落地并通过校验之前，绝不执行 docker rm。
#
#   失败即退出（set -eu），旧容器/源卷都还在原地，清磁盘后直接重跑即可。
# ============================================================
set -eu

DEST="${1:-/share/Container/eightnations/data}"
DEST="${DEST%/}"
IMG="${EIGHTNATIONS_IMAGE:-ghcr.io/kelvinguo1988/eightnations-library:latest}"
NAME="eightnations"
HEALTH_URL="http://127.0.0.1:8080/api/health"

say() { echo "[migrate] $*"; }
die() { echo "[migrate] ✗ $*" >&2; exit 1; }

# 只保留数字，避免 set -e 下 [ x -gt y ] 因脏值报错中断
num() { n=$(printf '%s' "$1" | tr -cd '0-9'); printf '%s' "${n:-0}"; }

command -v docker >/dev/null 2>&1 || die "未找到 docker 命令（请用管理员 SSH）"

# ---------- ① 拉取/确认镜像 ----------
docker image inspect "$IMG" >/dev/null 2>&1 || {
  say "拉取镜像 $IMG …"
  docker pull "$IMG" || die "镜像拉取失败"
}

# ---------- ② 发现含书库数据的卷（books/ 标记） ----------
# 用 here-doc 喂 while read：既避免词分割/通配展开，又不会因子 shell 丢失变量
say "扫描 Docker 卷，寻找书库数据…"
DATA_VOLS=""          # 换行分隔
while IFS= read -r v; do
  [ -n "$v" ] || continue
  if docker run --rm -v "$v":/from:ro "$IMG" sh -c 'test -d /from/books' >/dev/null 2>&1; then
    DATA_VOLS="$DATA_VOLS
$v"
  fi
done <<VOLS
$(docker volume ls -q)
VOLS

SRC=""; SRC_TS=0; SRC_PDF=0
if [ -n "$DATA_VOLS" ]; then
  # 每个候选卷显示概况，并选出 db 最新的一卷为源
  while IFS= read -r v; do
    [ -n "$v" ] || continue
    info=$(docker run --rm -v "$v":/from:ro "$IMG" sh -c \
      'ls /from/books 2>/dev/null | wc -l
       find /from/books -name "*.pdf" 2>/dev/null | wc -l
       date -r /from/db/library.db +%s 2>/dev/null || echo 0' 2>/dev/null || echo "")
    if [ -z "$info" ]; then
      say "  候选卷 ${v}：读取概况失败，跳过"
      continue
    fi
    nbooks=$(num "$(printf '%s\n' "$info" | sed -n 1p)")
    npdf=$(num "$(printf '%s\n' "$info" | sed -n 2p)")
    ts=$(num "$(printf '%s\n' "$info" | sed -n 3p)")
    say "  候选卷 ${v}：${nbooks} 册 / ${npdf} 个 PDF，db 更新时间 $(date -d "@${ts}" 2>/dev/null || echo "$ts")"
    if [ "$ts" -gt "$SRC_TS" ]; then SRC_TS=$ts; SRC=$v; SRC_PDF=$npdf; fi
  done <<VOLS2
$DATA_VOLS
VOLS2
fi

if [ -z "$SRC" ]; then
  if [ -f "$DEST/db/library.db" ]; then
    say "未发现含 books/ 的数据卷，但 $DEST 已有库——跳过拷贝，只做重建容器"
  else
    die "未发现任何含 books/ 的数据卷，且 $DEST 无数据：请先手工确认数据位置再跑"
  fi
else
  say "选定数据源卷：$SRC（db 时间戳 $SRC_TS，$SRC_PDF 个 PDF）"
fi

# ---------- ③ 停止旧容器（只 stop，不 rm） ----------
# 只按容器名精确匹配，不用 --filter ancestor=$IMG：同镜像可能被别的用途
# （试验容器/另一套数据）复用，按镜像删会误杀无关容器。
say "停止旧容器（仅匹配容器名 $NAME）…"
OLD_IDS=""
while IFS= read -r line; do
  [ -n "$line" ] || continue
  cid=$(printf '%s' "$line" | awk '{print $1}')
  cname=$(printf '%s' "$line" | awk '{print $2}')
  [ "$cname" = "$NAME" ] || continue        # name 过滤器是子串匹配，再卡一次精确名
  OLD_IDS="$OLD_IDS
$cid"
  docker stop "$cid" >/dev/null 2>&1 && say "  已停止容器 $cname ($cid)" \
    || say "  容器 $cname ($cid) 无需停止（已退出）"
done <<LIST
$(docker ps -a --filter "name=$NAME" --format '{{.ID}} {{.Names}}' 2>/dev/null || true)
LIST

# 源卷若还被别的容器挂着（例如手工起的试验容器），只停不删：
# 拷贝进行中容器仍在写卷 → 校验用的文件数会漂移，必须先冻结写入。
if [ -n "$SRC" ]; then
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    cid=$(printf '%s' "$line" | awk '{print $1}')
    cname=$(printf '%s' "$line" | awk '{print $2}')
    printf '%s\n' "$OLD_IDS" | grep -qx "$cid" && continue   # 已在待删名单里
    docker stop "$cid" >/dev/null 2>&1 \
      && say "  已停止挂载源卷的容器 $cname ($cid)（不会删除；迁移完可自行 docker start）" \
      || say "  容器 $cname ($cid) 已是停止状态"
  done <<LIST2
$(docker ps -a --filter "volume=$SRC" --format '{{.ID}} {{.Names}}' 2>/dev/null || true)
LIST2
fi

# ---------- ④ 先把卷数据搬到宿主机 DEST（删除容器之前！） ----------
mkdir -p "$DEST"
need_copy=1
if [ -n "$SRC" ] && [ -f "$DEST/db/library.db" ]; then
  dst_ts=$(num "$(date -r "$DEST/db/library.db" +%s 2>/dev/null || echo 0)")
  if [ "$dst_ts" -ge "$SRC_TS" ]; then
    say "固定路径数据不比源卷新（$dst_ts >= $SRC_TS），跳过拷贝"
    need_copy=0
  fi
fi

if [ -n "$SRC" ] && [ "$need_copy" = 1 ]; then
  STAGE_DIR="$DEST/.migrate-staging"
  rm -rf "$STAGE_DIR"                      # 清理上次中断的残留
  TARBALL="$STAGE_DIR/volume-data.tar"

  # 优先"卷 → tar 文件 → 解包"：docker run 的退出码直接可判，
  # 且 tar -t 能识别磁盘写满导致的截断（管道 tail 的退出码不可信）
  if docker run --rm -v "$SRC":/from:ro "$IMG" tar --version >/dev/null 2>&1; then
    mkdir -p "$STAGE_DIR"
    say "打包源卷 $SRC → $TARBALL …（书越多越久，请耐心）"
    if ! docker run --rm -v "$SRC":/from:ro "$IMG" tar -c -C /from . > "$TARBALL"; then
      rm -rf "$STAGE_DIR"
      die "源卷打包失败（磁盘写满？）——旧容器只停未删，数据完好，清理后重跑即可"
    fi
    if ! tar -t -f "$TARBALL" >/dev/null 2>&1; then
      rm -rf "$STAGE_DIR"
      die "打包文件校验失败（疑似截断/写满）——旧容器只停未删，数据完好，清理后重跑即可"
    fi
    say "解包到 $DEST …"
    if ! tar -x -f "$TARBALL" -C "$DEST"; then
      rm -rf "$STAGE_DIR"
      die "解包失败——旧容器只停未删，可重跑"
    fi
    rm -rf "$STAGE_DIR"
  else
    # 兜底（镜像内无 tar 时）：起一个临时只读容器 + docker cp 到宿主机，
    # 依然发生在删除旧容器之前
    say "镜像无 tar，改用 docker cp 兜底 …"
    HELPER="$NAME-migrate-src"
    docker rm -f "$HELPER" >/dev/null 2>&1 || true
    docker run -d --name "$HELPER" -v "$SRC":/from:ro "$IMG" sleep 3600 >/dev/null \
      || die "临时容器启动失败（不影响现有数据）"
    if ! docker cp "$HELPER":/from/. "$DEST"; then
      docker rm -f "$HELPER" >/dev/null 2>&1 || true
      die "docker cp 失败。旧容器与源卷都还在，可重跑"
    fi
    docker rm -f "$HELPER" >/dev/null 2>&1 || true
  fi

  # ---------- 校验：数据完整才允许删旧容器 ----------
  say "校验迁移结果："
  [ -f "$DEST/db/library.db" ] || die "$DEST/db/library.db 不存在——迁移不完整，不删旧容器"
  dbsize=$(num "$(wc -c < "$DEST/db/library.db" 2>/dev/null || echo 0)")
  [ "$dbsize" -gt 0 ] || die "library.db 为 0 字节——迁移不完整，不删旧容器"
  say "  ✓ db/library.db（$dbsize 字节）"
  dst_pdf=$(num "$(find "$DEST/books" -name '*.pdf' 2>/dev/null | wc -l || echo 0)")
  [ "$dst_pdf" = "$SRC_PDF" ] || die "PDF 数量不一致：源卷 $SRC_PDF vs $DEST/books $dst_pdf——迁移不完整，不删旧容器"
  say "  ✓ books/*.pdf 数量一致（$dst_pdf）"
  say "数据已安全落在宿主机，接下来才会删除旧容器"
fi

# ---------- ⑤ 删除旧容器（此时数据已在宿主机，匿名卷被级联删也无所谓） ----------
if [ -n "$OLD_IDS" ]; then
  say "移除旧容器…"
  while IFS= read -r cid; do
    [ -n "$cid" ] || continue
    docker rm -f "$cid" >/dev/null 2>&1 && say "  已移除容器 $cid" \
      || say "  移除容器 $cid 失败（继续，稍后可手工 docker rm -f）"
  done <<IDS
$OLD_IDS
IDS
fi

# ---------- ⑥ 用固定路径重建容器 ----------
say "用固定路径重建容器…"
docker run -d --name "$NAME" --restart unless-stopped \
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
  -p 8080:8080 -e TZ=Asia/Shanghai \
  -v "$DEST":/data "$IMG" || die "容器启动失败（8080 端口被占用？）"

# ---------- ⑦ 自检（等健康检查就绪，不裸 sleep） ----------
say "等待服务就绪…"
i=0
ready=0
while [ "$i" -lt 30 ]; do
  i=$((i + 1))
  if docker exec "$NAME" python3 -c \
    "import urllib.request;urllib.request.urlopen('$HEALTH_URL',timeout=5)" >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 2
done
if [ "$ready" = 1 ]; then
  say "  ✓ /api/health 已就绪（第 $i 次探测）"
else
  say "  ! 60s 内 /api/health 未就绪——docker logs $NAME 看启动日志"
fi

say "数据自检："
docker exec "$NAME" python3 manage.py doctor || true
say "完成 ✅ 打开 http://<NAS_IP>:8080 —— 设置页存储面板应显示 ✓ 已绑定固定路径"
say "确认一切正常后，旧匿名卷可在 Container Station 清理，或 docker volume rm <旧卷ID>"
