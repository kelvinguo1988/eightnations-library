#!/bin/sh
# 八国联军图书馆容器入口：调度守护 + Web 前端 同容器
# scheduler 若意外退出则 30s 后自动拉起（容器只随 compose 生命周期终止）
(
  while true; do
    python3 scheduler.py >> /data/logs/scheduler.log 2>&1
    echo "[entrypoint] scheduler 退出，30s 后重启" >> /data/logs/scheduler.log
    sleep 30
  done
) &
exec python3 -m uvicorn web.app:app --host 0.0.0.0 --port 8080
