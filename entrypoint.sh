#!/bin/sh
# 八国联军图书馆容器入口：调度守护 + Web 前端 同容器
python3 scheduler.py >> /data/logs/scheduler.log 2>&1 &
exec python3 -m uvicorn web.app:app --host 0.0.0.0 --port 8080
