#!/bin/sh
# 八国联军图书馆容器入口：调度守护 + Web 前端 同容器
# scheduler 意外退出则 30s 后拉起；但若"连续快速崩溃"则不再无限重试
# （无限重试只会把 /data 刷满日志），直接让容器退出交给 Docker restart 策略。
mkdir -p /data/logs /data/db || {
  echo "[entrypoint] ✗ /data 不可写——检查容器存储映射是否指向有效的宿主机目录" >&2
  exit 1
}

LOG_DIR=/data/logs
LOG_MAX=$((20 * 1024 * 1024))   # 单日志超 20MB 轮转为 .1（只留一代，NAS 够用）
FASTFAIL_WINDOW=60              # 存活不足 60s 记一次"快速失败"
FASTFAIL_LIMIT=5                # 连续 5 次快速失败即放弃重试

# 轮转 scheduler.log / web.log：超过 LOG_MAX 则 mv 成 .1 再重建空文件。
# 只留一代，最坏占用约 2×LOG_MAX（=40MB），不会数月填满 /data。
rotate_logs() {
  f="$LOG_DIR/scheduler.log"
  if [ -f "$f" ]; then
    sz=$(wc -c < "$f" 2>/dev/null | tr -cd '0-9')
    [ "${sz:-0}" -gt "$LOG_MAX" ] && { mv -f "$f" "$f.1"; : > "$f"; echo "[entrypoint] scheduler.log 超 20MB，已轮转为 scheduler.log.1"; }
  fi
  f="$LOG_DIR/web.log"
  if [ -f "$f" ]; then
    sz=$(wc -c < "$f" 2>/dev/null | tr -cd '0-9')
    [ "${sz:-0}" -gt "$LOG_MAX" ] && { mv -f "$f" "$f.1"; : > "$f"; echo "[entrypoint] web.log 超 20MB，已轮转为 web.log.1"; }
  fi
  return 0
}

(
  fails=0
  while true; do
    rotate_logs
    started=$(date +%s 2>/dev/null || echo 0)
    python3 scheduler.py >> "$LOG_DIR/scheduler.log" 2>&1
    code=$?
    up=$(( $(date +%s 2>/dev/null || echo 0) - started ))
    if [ "$up" -lt "$FASTFAIL_WINDOW" ]; then
      fails=$((fails + 1))
    else
      fails=0     # 活过了窗口期，说明上次只是偶发，清零
    fi
    echo "[entrypoint] scheduler 退出（code=$code，本次存活 ${up}s，连续快速失败 $fails/$FASTFAIL_LIMIT）" \
      >> "$LOG_DIR/scheduler.log"
    if [ "$fails" -ge "$FASTFAIL_LIMIT" ]; then
      msg="[entrypoint] ✗ scheduler 连续 $fails 次在 ${FASTFAIL_WINDOW}s 内崩溃，停止重试；容器即将退出，由 Docker restart 策略整体重启（请查 $LOG_DIR/scheduler.log）"
      echo "$msg" >> "$LOG_DIR/scheduler.log"
      echo "$msg" >&2
      # 终止前台 uvicorn（$$ 在子 shell 里仍是主脚本 PID），容器随之退出重来
      kill -TERM "$$" 2>/dev/null || true
      exit 1
    fi
    sleep 30
  done
) &

# Web 前端日志走容器 stdout（docker logs / json-file 驱动限流，见 docker-compose.yml）；
# 若历史版本/外部进程写过 /data/logs/web.log，上面的 rotate_logs 也会一并截断，
# 不会让它在 /data 里无声长满。
exec python3 -m uvicorn web.app:app --host 0.0.0.0 --port 8080
