#!/bin/sh
# ============================================================
# 八国联军图书馆 · NAS 一键迁移脚本
# 把散落在 Docker 匿名卷里的书库数据自动迁移到固定路径，
# 并用固定路径重建容器（自动发现旧卷/旧容器，幂等可重跑）。
#
# 用法（NAS SSH，管理员账号）:
#   sh migrate-nas.sh --dry-run              # 干跑：只打印将要做的事，不停容器、不写盘（推荐先跑这个）
#   sh migrate-nas.sh --yes                  # 真跑，目标路径默认 /share/Container/eightnations/data
#   sh migrate-nas.sh --yes /share/其他路径   # 真跑，自定义目标路径（必须在 /share/ 下）
#   sh migrate-nas.sh --help
#
# 下载后执行（安全做法：先下载、再阅读、校验、干跑，最后才真跑）:
#   # ① 建议用固定版本号，而不是 main（main 随时会变，这是以 root 动宿主机的脚本）
#   curl -O https://raw.githubusercontent.com/kelvinguo1988/eightnations-library/v0.1.0/migrate-nas.sh
#   shasum -a 256 migrate-nas.sh             # 与发布页公布的 sha256 比对
#   # ② 先读一遍（约 300 行），确认无误
#   less migrate-nas.sh
#   # ③ 干跑看它打算做什么
#   sh migrate-nas.sh --dry-run
#   # ④ 真跑
#   sh migrate-nas.sh --yes
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
#
# 破坏性操作的全部护栏（2026-10 加固，起因：DEST 未校验时 `DEST=/` 会让
# `tar -x -C /` 写满 QNAP 系统分区）：
#   * DEST 必须是 /share/ 下的绝对路径、不含 ".." 、不是 /share 本身；
#   * 每个 rm -rf / tar -x 前都用 ${VAR:?} 二次确认变量非空，且再校验一次前缀；
#   * 解包前扫描 tar 成员，拒绝绝对路径与 ".." 穿越；
#   * 默认 --dry-run，必须显式 --yes 才真正动手。
# ============================================================
set -eu

# ---------- 参数解析（先于任何副作用） ----------
DRY_RUN=1              # 默认干跑：改个参数就毁数据的脚本，"默认不执行"是唯一正确的默认值
ASSUME_YES=0
DEST_ARG=""
usage() {
  sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}
for _a in "$@"; do
  case "$_a" in
    --dry-run|-n) DRY_RUN=1 ;;
    --yes|-y)     ASSUME_YES=1; DRY_RUN=0 ;;
    --help|-h)    usage ;;
    --*)          echo "[migrate] ✗ 未知参数: $_a（支持 --dry-run / --yes / --help）" >&2; exit 2 ;;
    *)
      [ -z "$DEST_ARG" ] || { echo "[migrate] ✗ 只能指定一个目标路径（多余参数: $_a）" >&2; exit 2; }
      DEST_ARG="$_a" ;;
  esac
done

say() { echo "[migrate] $*"; }
die() { echo "[migrate] ✗ $*" >&2; exit 1; }

# run：所有**有副作用**的命令都经它执行；干跑时只打印。
# 注意这里用 "$@"，不做 eval、不拼字符串 —— 目标路径里即便有空格/分号也不会被再解释一次。
run() {
  if [ "$DRY_RUN" = 1 ]; then
    echo "  [dry-run] $*"
    return 0
  fi
  "$@"
}

# 只保留数字，避免 set -e 下 [ x -gt y ] 因脏值报错中断
num() { n=$(printf '%s' "$1" | tr -cd '0-9'); printf '%s' "${n:-0}"; }

# ---------- DEST 白名单校验 ----------
# 为什么必须做：本脚本以 root 运行，DEST 会拼进 rm -rf / mkdir -p / tar -x -C。
# `DEST=/`      → STAGE_DIR=/.migrate-staging，解包把 books/、db/ 铺到根分区（QNAP 系统分区常只有几百 MB）
# `DEST=..`     → 相对于当前目录的上层，完全不可预测
# `DEST=/etc`   → 把书库写进系统目录
# 因此只接受 /share/ 下、不含 ".." 的具体目录。
DEST="${DEST_ARG:-/share/Container/eightnations/data}"
# 先判根路径再剥尾部斜杠：否则 `DEST=/` 会被 ${DEST%/} 剥成空串，
# 报错信息变成"收到: ''"（既误导，也让"拒绝根路径"这条原因看不出来）。
[ "$DEST" != "/" ] || die "目标路径不能是根目录 '/'（这会把书库解包到系统分区）"
DEST="${DEST%/}"

validate_dest() {
  case "$DEST" in
    /*) ;;                                  # 必须是绝对路径
    *) die "目标路径必须是绝对路径（收到: '$DEST'）" ;;
  esac
  case "$DEST" in
    *..*) die "目标路径不得包含 '..'（收到: '$DEST'）" ;;
  esac
  case "$DEST" in
    /share/*) ;;                            # 只允许 /share/ 下（QNAP 数据共享目录）
    *) die "目标路径必须位于 /share/ 之下（收到: '$DEST'）。
  这是硬性限制：脚本以 root 运行并执行 rm -rf / tar -x，禁止指向 / 、/etc 、/tmp 等目录。
  如确需别的路径，请手工迁移或修改本脚本的校验规则。" ;;
  esac
  # 排除 /share、/share/Container 这类过于宽泛的目标（会在共享根上创建/覆盖文件）
  case "$DEST" in
    /share|/share/Container|/share/Container/eightnations) \
      die "目标路径过于宽泛（'$DEST'），请指向具体的 data 目录" ;;
  esac
  # 该路径若已存在，必须是目录（防止 DEST 指向某个文件导致后续 mkdir/解包行为诡异）
  if [ -e "$DEST" ] && [ ! -d "$DEST" ]; then
    die "目标路径已存在但不是目录（'$DEST'），请手工确认后删除或改用其它路径"
  fi
}
validate_dest

IMG="${EIGHTNATIONS_IMAGE:-ghcr.io/kelvinguo1988/eightnations-library:latest}"
NAME="eightnations"
HEALTH_URL="http://127.0.0.1:8080/api/health"

# 变量自己的"最后一道锁"：以下三个变量一旦为空，rm -rf "$X" 会退化成 rm -rf ""（无害但无意义）
# 或被 shell 展开成别的路径；用 ${VAR:?} 保证它们非空，且再断言以 DEST 开头。
STAGE_DIR="${DEST}/.migrate-staging"
: "${STAGE_DIR:?内部错误：STAGE_DIR 为空}"
case "$STAGE_DIR" in
  "$DEST"/*) ;;
  *) die "内部错误：STAGE_DIR（'$STAGE_DIR'）不在 DEST 之下，拒绝继续（保护性中止）" ;;
esac

# 安全清理暂存目录：只允许删除 DEST 下的 .migrate-staging
cleanup_stage() {
  [ -n "${STAGE_DIR:-}" ] || return 0
  case "$STAGE_DIR" in
    "$DEST"/.migrate-staging) ;;
    *) say "  ! 拒绝清理异常路径 '$STAGE_DIR'（保护性跳过）"; return 0 ;;
  esac
  run rm -rf "$STAGE_DIR"
}

say "="
say "目标路径 DEST = $DEST"
if [ "$DRY_RUN" = 1 ]; then
  say "模式 = 干跑（--dry-run）：只打印将要执行的动作，不停容器、不写盘。"
  say "       确认无误后加 --yes 真跑：" 
  say "         sh $0 --yes${DEST_ARG:+ $DEST_ARG}"
else
  say "模式 = 真跑（--yes）"
fi
say "="

command -v docker >/dev/null 2>&1 || die "未找到 docker 命令（请用管理员 SSH）"
if [ "$DRY_RUN" = 1 ]; then
  say "提示：干跑仍需要 docker 命令存在（用于只读探测卷/容器现状），但不会执行任何写操作。"
fi

# ---------- ① 拉取/确认镜像 ----------
if docker image inspect "$IMG" >/dev/null 2>&1; then
  say "镜像已存在：$IMG"
else
  say "拉取镜像 $IMG …"
  run docker pull "$IMG" || die "镜像拉取失败"
fi

# 干跑时若镜像尚未拉取，后续只读探测（docker run）会失败；
# 这里显式提示，但不阻断（让用户先看到路径校验与计划）。
IMG_READY=1
docker image inspect "$IMG" >/dev/null 2>&1 || IMG_READY=0

# ---------- ② 发现含书库数据的卷（books/ 标记） ----------
# 用 here-doc 喂 while read：既避免词分割/通配展开，又不会因子 shell 丢失变量
say "扫描 Docker 卷，寻找书库数据…"
DATA_VOLS=""          # 换行分隔
if [ "$IMG_READY" = 1 ]; then
  while IFS= read -r v; do
    [ -n "$v" ] || continue
    if docker run --rm -v "$v":/from:ro "$IMG" sh -c 'test -d /from/books' >/dev/null 2>&1; then
      DATA_VOLS="$DATA_VOLS
$v"
    fi
  done <<VOLS
$(docker volume ls -q)
VOLS
else
  say "  ! 镜像未就绪（干跑模式不拉取），跳过卷扫描"
fi

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
  if [ "$IMG_READY" = 0 ]; then
    die "镜像尚未拉取（干跑模式不拉取镜像），无法判断数据位置。请先 docker pull $IMG，或去掉 --dry-run 直接 --yes 运行"
  fi
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
  [ -n "$cid" ] || continue                 # 空 ID 绝不进删除名单
  OLD_IDS="$OLD_IDS
$cid"
  run docker stop "$cid" >/dev/null 2>&1 && say "  已停止容器 $cname ($cid)" \
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
    [ -n "$cid" ] || continue
    printf '%s\n' "$OLD_IDS" | grep -qx "$cid" && continue   # 已在待删名单里
    run docker stop "$cid" >/dev/null 2>&1 \
      && say "  已停止挂载源卷的容器 $cname ($cid)（不会删除；迁移完可自行 docker start）" \
      || say "  容器 $cname ($cid) 已是停止状态"
  done <<LIST2
$(docker ps -a --filter "volume=$SRC" --format '{{.ID}} {{.Names}}' 2>/dev/null || true)
LIST2
fi

# ---------- ④ 先把卷数据搬到宿主机 DEST（删除容器之前！） ----------
run mkdir -p "$DEST"
need_copy=1
if [ -n "$SRC" ] && [ -f "$DEST/db/library.db" ]; then
  dst_ts=$(num "$(date -r "$DEST/db/library.db" +%s 2>/dev/null || echo 0)")
  if [ "$dst_ts" -ge "$SRC_TS" ]; then
    say "固定路径数据不比源卷新（$dst_ts >= $SRC_TS），跳过拷贝"
    need_copy=0
  fi
fi

if [ -n "$SRC" ] && [ "$need_copy" = 1 ]; then
  TARBALL="$STAGE_DIR/volume-data.tar"

  # 优先"卷 → tar 文件 → 解包"：docker run 的退出码直接可判，
  # 且 tar -t 能识别磁盘写满导致的截断（管道 tail 的退出码不可信）
  if docker run --rm -v "$SRC":/from:ro "$IMG" tar --version >/dev/null 2>&1; then
    cleanup_stage                           # 清理上次中断的残留
    run mkdir -p "$STAGE_DIR"
    say "打包源卷 $SRC → $TARBALL …（书越多越久，请耐心）"
    if [ "$DRY_RUN" = 1 ]; then
      echo "  [dry-run] docker run --rm -v $SRC:/from:ro $IMG tar -c -C /from . > $TARBALL"
    elif ! docker run --rm -v "$SRC":/from:ro "$IMG" tar -c -C /from . > "$TARBALL"; then
      cleanup_stage
      die "源卷打包失败（磁盘写满？）——旧容器只停未删，数据完好，清理后重跑即可"
    fi
    if [ "$DRY_RUN" = 0 ]; then
      if ! tar -t -f "$TARBALL" >/dev/null 2>&1; then
        cleanup_stage
        die "打包文件校验失败（疑似截断/写满）——旧容器只停未删，数据完好，清理后重跑即可"
      fi

      # ---- 解包前的路径穿越校验 ----
      # 源卷里的目录名来自远端元数据（专藏/条目 uid）。正常情况已被 core/pipeline._dest_dir
      # 校验过，但迁移脚本读的是**历史数据**，可能早于该校验上线。
      # tar 成员若含绝对路径或 ".."，会逃出 DEST（以 root 身份写到任意位置）。
      if tar -t -f "$TARBALL" | grep -qE '^(/|\.\./|.*(^|/)\.\.(/|$))'; then
        cleanup_stage
        die "打包文件含绝对路径或 '..' 成员，拒绝解包（数据安全性优先）。
  旧容器只停未删，数据完好。请把该卷的内容手工检查一遍（可能是历史脏数据）。"
      fi
    fi

    say "解包到 $DEST …"
    if ! run tar -x -f "$TARBALL" -C "$DEST" --no-same-owner; then
      cleanup_stage
      die "解包失败——旧容器只停未删，可重跑"
    fi
    cleanup_stage
  else
    # 兜底（镜像内无 tar 时）：起一个临时只读容器 + docker cp 到宿主机，
    # 依然发生在删除旧容器之前
    say "镜像无 tar，改用 docker cp 兜底 …"
    HELPER="$NAME-migrate-src"
    : "${HELPER:?内部错误：HELPER 为空}"
    run docker rm -f "$HELPER" >/dev/null 2>&1 || true
    run docker run -d --name "$HELPER" -v "$SRC":/from:ro "$IMG" sleep 3600 >/dev/null \
      || die "临时容器启动失败（不影响现有数据）"
    if ! run docker cp "$HELPER":/from/. "$DEST"; then
      run docker rm -f "$HELPER" >/dev/null 2>&1 || true
      die "docker cp 失败。旧容器与源卷都还在，可重跑"
    fi
    run docker rm -f "$HELPER" >/dev/null 2>&1 || true
  fi

  # ---------- 校验：数据完整才允许删旧容器 ----------
  say "校验迁移结果（只有通过才删除旧容器）："
  if [ "$DRY_RUN" = 1 ]; then
    say "  [dry-run] 真跑时会校验：$DEST/db/library.db 存在且非 0 字节、"
    say "            books/*.pdf 数量 = 源卷 $SRC_PDF 个"
    say "  [dry-run] 干跑不做校验（没有写入任何文件），也**不会**删除旧容器。"
  else
    [ -f "$DEST/db/library.db" ] || die "$DEST/db/library.db 不存在——迁移不完整，不删旧容器"
    dbsize=$(num "$(wc -c < "$DEST/db/library.db" 2>/dev/null || echo 0)")
    [ "$dbsize" -gt 0 ] || die "library.db 为 0 字节——迁移不完整，不删旧容器"
    say "  ✓ db/library.db（$dbsize 字节）"
    dst_pdf=$(num "$(find "$DEST/books" -name '*.pdf' 2>/dev/null | wc -l || echo 0)")
    [ "$dst_pdf" = "$SRC_PDF" ] || die "PDF 数量不一致：源卷 $SRC_PDF vs $DEST/books $dst_pdf——迁移不完整，不删旧容器"
    say "  ✓ books/*.pdf 数量一致（$dst_pdf）"
    say "数据已安全落在宿主机，接下来才会删除旧容器"
  fi
fi

# ---------- ⑤ 删除旧容器（此时数据已在宿主机，匿名卷被级联删也无所谓） ----------
if [ -n "$OLD_IDS" ]; then
  say "移除旧容器…"
  while IFS= read -r cid; do
    [ -n "$cid" ] || continue
    run docker rm -f "$cid" >/dev/null 2>&1 && say "  已移除容器 $cid" \
      || say "  移除容器 $cid 失败（继续，稍后可手工 docker rm -f）"
  done <<IDS
$OLD_IDS
IDS
fi

# ---------- ⑥ 用固定路径重建容器 ----------
say "用固定路径重建容器…"
run docker run -d --name "$NAME" --restart unless-stopped \
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
  -p 8080:8080 -e TZ=Asia/Shanghai \
  -v "$DEST":/data "$IMG" || die "容器启动失败（8080 端口被占用？）"

if [ "$DRY_RUN" = 1 ]; then
  say "="
  say "干跑结束：以上为将要执行的动作，**没有改动任何文件、没有停止或删除任何容器**。"
  say "确认无误后执行："
  say "  sh $0 --yes${DEST_ARG:+ $DEST_ARG}"
  exit 0
fi

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
