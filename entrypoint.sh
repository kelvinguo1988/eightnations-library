#!/bin/sh
# ============================================================
# 八国联军图书馆容器入口：调度守护 + Web 前端 同容器
#
# 进程模型（2026-10 重写，起因：docker stop 时 scheduler 变孤儿进程，
# 可能与重启后的新容器**双写同一个 /data**）：
#
#   本脚本 (PID 1)
#     ├─ scheduler 监督循环（后台子 shell，它自己再拉起 python3 scheduler.py）
#     └─ uvicorn（后台进程，由下面的前台 wait 等待）
#
# 设计要点：
#   1) 主脚本常驻做监督者：无论 scheduler 还是 uvicorn 先退出，
#      另一个都会被收掉，容器状态永远是"两个都在跑"或"整体退出"，
#      不留半活状态（半活 = 旧进程还在写 /data，新容器又起来了一个）；
#   2) 收到 SIGTERM/SIGINT（docker stop / Ctrl-C）时，先 TERM 整棵进程树，
#      给 scheduler 落盘的时间，超时才 KILL；
#   3) scheduler 意外退出则 30s 后拉起；连续 5 次在 60s 内崩溃则放弃，
#      整体退出交给 Docker restart 策略（无限重试只会把 /data 刷满日志）；
#   4) 退出码：uvicorn 自己退出 → 用它的退出码；scheduler 被判"不可恢复"
#      → 退出码 1。两者都会让 compose 的 restart: unless-stopped 介入。
#
# 为什么不用 `exec uvicorn`（旧写法）：
#   exec 让 uvicorn 顶替 PID 1 后本脚本就消失了，没人能给后台的 scheduler
#   转发信号 —— 实测 docker stop 后 scheduler 的 PPID 变成 1（孤儿），
#   继续跑着写 /data。改成"主脚本 wait uvicorn"后，主脚本才是收信号的人。
# ============================================================
set -eu

DATA_ROOT="${EIGHTNATIONS_DATA:-/data}"
LOG_DIR="$DATA_ROOT/logs"

mkdir -p "$LOG_DIR" "$DATA_ROOT/db" || {
  echo "[entrypoint] ✗ $DATA_ROOT 不可写——检查容器存储映射是否指向有效的宿主机目录" >&2
  exit 1
}

LOG_MAX=$((20 * 1024 * 1024))   # 单日志超 20MB 轮转为 .1（只留一代，NAS 够用）
FASTFAIL_WINDOW=60              # 存活不足 60s 记一次"快速失败"
FASTFAIL_LIMIT=5                # 连续 5 次快速失败即放弃重试
RETRY_DELAY=30                  # 正常退出后的重启间隔

# 子 shell 里的变量传不回主脚本，故用文件把"当前 python3 scheduler.py 的 PID"传出去
SCHED_PY_FILE="${TMPDIR:-/tmp}/e8n-sched-py.$$"
: > "$SCHED_PY_FILE" 2>/dev/null || true
SCHED_PID=""       # scheduler 监督循环（子 shell）
SCHED_PY=""        # 它当前拉起的 python3 scheduler.py（真正的执行者）
WEB_PID=""
SHUTTING_DOWN=0

# ---------- 进程组 ----------
# 打开 job control（set -m）后，每个 `cmd &` 成为一个独立进程组，组长 PID =
# 该命令 PID，于是 `kill -- -$pid` 可连同 python3 孙进程一起处理。
# 但 dash / busybox sh 对 set -m 的支持不一致（有的会报错、有的行为不同），
# 所以下面每个 kill 都写成"先试负 PGID、失败退回单 PID"。
set -m 2>/dev/null || true

say()  { echo "[entrypoint] $*"; }
saye() { echo "[entrypoint] $*" >&2; }

# 杀一个进程及其所有后代
# 背景（实测）：`set -m` 只让 `cmd &` 这**一层**成为独立进程组，而
# `python3 -m uvicorn` 和 `python3 scheduler.py` 是更深的孙进程，
# 杀掉包装进程的进程组不会带走它们（实测残留）。所以这里做两件事：
#   ① kill 直接子进程（+ 负 PGID 尝试整组）；
#   ② 递归清理它的后代（pkill -P），把 python3 一起收掉。
# pkill 在 QNAP 的 busybox 与 Debian slim 的 procps 里都有；没有时退回单 PID。
# 顺序很重要（实测踩坑）：**先给自己发一次、再递归子进程**。
# 若先递归子进程、最后再用负 PGID 打整组，SIGTERM 会到达两次 ——
# dash/busybox 在 trap 执行前就因第二个 TERM 直接终止，trap 里的
# "落盘 / 释放租约"逻辑根本跑不到（实测：probe 文件为空）。
# 所以：
#   ① 先 kill 自己（让 trap 有机会跑）——不用负 PGID，避免连带孙进程时
#      因"组内第二个 TERM"提前退出；
#   ② 再递归后代，确保子 shell 与 python3 都被通知；
#   ③ 最后若自己还活着，用单 PID TERM 收尾（不再打整组）。
kill_tree() {
  _sig="$1"; _pid="$2"
  [ -n "$_pid" ] || return 0
  kill -"$_sig" "$_pid" 2>/dev/null || true          # ① 自己
  for _c in $(ps -eo pid,ppid 2>/dev/null | awk -v p="$_pid" '$2==p {print $1}'); do
    kill_tree "$_sig" "$_c"                          # ② 后代（递归）
  done
  return 0
}

# 等进程退出，最多 $2 秒；超时返回 1（只等不杀）
wait_for() {
  _pid="$1"; _limit="$2"; _i=0
  while [ "$_i" -lt "$_limit" ]; do
    kill -0 "$_pid" 2>/dev/null || return 0
    sleep 1
    _i=$((_i + 1))
  done
  return 1
}

# 取"当前正在跑的 python3 scheduler.py"的 PID（可能为空：循环处于重启等待期）
current_sched_py() {
  [ -f "${SCHED_PY_FILE:-}" ] || return 0
  _p=$(head -n 1 "$SCHED_PY_FILE" 2>/dev/null | tr -cd '0-9')
  [ -n "$_p" ] || return 0
  kill -0 "$_p" 2>/dev/null || return 0   # 已退出，不返回
  printf '%s' "$_p"
}

# ---------- 优雅停机 ----------
# 顺序有意为之：先 scheduler（可能正在写 /data、持着下载租约），再 web。
# 预算 8s + 5s：docker stop 默认只给 10s 就 SIGKILL，留出余量。
shutdown_all() {
  [ "$SHUTTING_DOWN" = 0 ] || return 0
  SHUTTING_DOWN=1
  say "收到停止信号，正在优雅退出（先 scheduler 后 web）…"
  # python3 scheduler.py 才是真正持有 socket / 写 /data 的进程，必须让它先收到 TERM
  # （只 TERM 包装子 shell 的话，python3 收不到，会被后面的 KILL 硬杀 → 半截 /data 写入）
  kill_tree TERM "${SCHED_PID:-}"
  kill_tree TERM "$(current_sched_py)"
  wait_for "$(current_sched_py || echo 0)" 8 >/dev/null 2>&1 || kill_tree KILL "$(current_sched_py)"
  kill_tree KILL "${SCHED_PID:-}"
  kill_tree TERM "${WEB_PID:-}"
  wait_for "${WEB_PID:-0}" 5 >/dev/null 2>&1 || kill_tree KILL "${WEB_PID:-}"
  say "已退出"
}
# trap 里用单引号：若用双引号，$SCHED_PID 会在注册瞬间被展开（那时还是空串）
trap 'shutdown_all; exit 143' TERM
trap 'shutdown_all; exit 130' INT
trap 'say "收到 HUP，忽略（容器内无终端重载语义）"' HUP

# ---------- 日志轮转 ----------
# web 侧日志走 stdout（docker logs / json-file 驱动限流，见 docker-compose.yml
# 的 max-size/max-file）。这里保留 web.log 的轮转只为兼容历史版本遗留的文件，
# 避免它在 /data 里无声长满。scheduler 日志仍落盘（NAS 上排查调度问题方便），
# 在每次重启 scheduler 前轮转。
rotate_logs() {
  for _f in "$LOG_DIR/scheduler.log" "$LOG_DIR/web.log"; do
    [ -f "$_f" ] || continue
    _sz=$(wc -c < "$_f" 2>/dev/null | tr -cd '0-9')
    if [ "${_sz:-0}" -gt "$LOG_MAX" ]; then
      mv -f "$_f" "$_f.1" && : > "$_f" && say "$_f 超 20MB，已轮转为 .1"
    fi
  done
  return 0
}

# ---------- scheduler 监督循环（后台） ----------
scheduler_loop() {
  fails=0
  while :; do
    rotate_logs
    started=$(date +%s 2>/dev/null || echo 0)
    code=0
    # 后台起 + 立即记 PID：这样停机时能精确 TERM 到 python3 本体。
    # 用 wait 取退出码（不阻塞信号 —— TERM 到达子 shell 时 wait 会立即返回 128+n）。
    python3 scheduler.py >> "$LOG_DIR/scheduler.log" 2>&1 &
    SCHED_PY=$!
    # 把 PID 写给主脚本（子 shell 的变量传不出去，文件可以）
    printf '%s\n' "$SCHED_PY" > "$SCHED_PY_FILE" 2>/dev/null || true
    wait "$SCHED_PY" || code=$?
    # 若本子 shell 已进入停机（SHUTTING_DOWN 由主脚本置位后 kill 传进来），
    # 让 python3 有机会跑完自己的退出钩子，再收尾。
    kill_tree TERM "$SCHED_PY" 2>/dev/null || true
    SCHED_PY=""
    : > "$SCHED_PY_FILE" 2>/dev/null || true
    up=$(( $(date +%s 2>/dev/null || echo 0) - started ))
    if [ "$up" -lt "$FASTFAIL_WINDOW" ]; then
      fails=$((fails + 1))
    else
      fails=0     # 活过了窗口期，说明上次只是偶发，清零
    fi
    echo "[entrypoint] scheduler 退出（code=$code，本次存活 ${up}s，连续快速失败 $fails/$FASTFAIL_LIMIT）" \
      >> "$LOG_DIR/scheduler.log"
    if [ "$fails" -ge "$FASTFAIL_LIMIT" ]; then
      msg="[entrypoint] ✗ scheduler 连续 $fails 次在 ${FASTFAIL_WINDOW}s 内崩溃，停止重试；容器整体退出，由 Docker restart 策略重启（请查 $LOG_DIR/scheduler.log）"
      echo "$msg" >> "$LOG_DIR/scheduler.log"
      saye "$msg"
      exit 1        # 交给监督者：收掉 web → 整体退出
    fi
    # 分片睡眠：sleep 30 会一直阻塞到 30s，分片后 SIGTERM 能在 1s 内生效
    _i=0
    while [ "$_i" -lt "$RETRY_DELAY" ]; do
      sleep 1
      _i=$((_i + 1))
    done
  done
}

say "启动：scheduler（后台监督）+ uvicorn:8080（前台）"
say "数据目录 $DATA_ROOT（日志 $LOG_DIR）"

# 注意（实测踩过的坑）：`scheduler_loop &` 会让它在**子 shell** 里跑，
# 它在内部设置的 SCHED_PY（当前 python3 的 PID）永远传不回主脚本 ——
# 实测停机时 SCHED_PY 恒为空，真正的 python3 只能被 KILL 硬杀。
# dash 也不支持"set -m 后后台函数在当前 shell 执行"（实测变量仍不可见）。
# 因此改用最可移植的办法：让循环把 PID 写进 SCHED_PY_FILE，主脚本读取。
scheduler_loop &
SCHED_PID=$!
: "${SCHED_PID:?无法启动 scheduler 监督循环}"

# web 侧日志走 stdout，交给容器日志驱动（json-file max-size=10m max-file=3）
python3 -m uvicorn web.app:app --host 0.0.0.0 --port 8080 &
WEB_PID=$!
: "${WEB_PID:?无法启动 uvicorn}"

say "scheduler 监督循环 PID=$SCHED_PID，uvicorn PID=$WEB_PID"

# ---------- 监督者主循环 ----------
# 为什么不用"一个桩 wait 另一个子进程"的写法（我试过，实测不可靠）：
#   dash 下，桩子 shell 里的 `wait "$pid"` 拿不到父 shell 的作业表条目，
#   uvicorn 退出码会变成 127，且不报错 —— 静默丢退出码。
#   （对照实验：main 里 `wait "$WEB_PID"` 拿得到 7；桩里 `wait` 得 127。）
# 所以这里用最朴素也最可移植的做法：**主脚本自己轮询**两个子进程的存活，
# 需要退出码时再在 main 里直接 `wait` 它本身。
#
# 主脚本必须自己收 TERM：`wait "$PID"` 会被信号打断并立即返回，
# 所以 1 秒轮询期间任何 SIGTERM 都能在 1s 内被 trap 处理。

WEB_RC=0
while [ "$SHUTTING_DOWN" = 0 ]; do
  if ! kill -0 "$WEB_PID" 2>/dev/null; then
    # uvicorn 没了：在 main 里直接 wait 取真实退出码（此时它已是僵尸，wait 立即返回）
    wait "$WEB_PID" 2>/dev/null || WEB_RC=$?
    [ "$SHUTTING_DOWN" = 0 ] || exit 143
    saye "[entrypoint] ✗ uvicorn 意外退出（code=$WEB_RC），容器整体退出以触发 restart 策略"
    kill_tree TERM "$(current_sched_py)"
    kill_tree TERM "$SCHED_PID"
    wait_for "$SCHED_PID" 5 >/dev/null 2>&1 || kill_tree KILL "$SCHED_PID"
    exit "$WEB_RC"
  fi

  if ! kill -0 "$SCHED_PID" 2>/dev/null; then
    # scheduler 监督循环自己退出了（连续快速崩溃 → 它 exit 1）
    [ "$SHUTTING_DOWN" = 0 ] || exit 143
    saye "[entrypoint] ✗ scheduler 监督循环已退出，容器整体退出以触发 restart 策略"
    kill_tree TERM "$(current_sched_py)"
    kill_tree TERM "$WEB_PID"
    wait_for "$WEB_PID" 5 >/dev/null 2>&1 || kill_tree KILL "$WEB_PID"
    exit 1
  fi

  # 1 秒一次轮询；sleep 被信号打断是正常的（trap 里会 exit）
  sleep 1 2>/dev/null || true
done

exit 143
