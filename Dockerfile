FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    EIGHTNATIONS_DATA=/data \
    TZ=Asia/Shanghai

# 运行时依赖：单一真相源 = requirements.txt（不在这里重复硬编码包名）
# 先只 COPY 依赖清单，代码变动不会再让 pip 层缓存失效
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ core/
COPY sites/ sites/
COPY web/ web/
COPY tools/ tools/
COPY manage.py scheduler.py entrypoint.sh ./
RUN chmod +x entrypoint.sh && mkdir -p /data/db /data/logs

# 刻意不写 VOLUME /data：镜像层声明卷会让"忘记挂 -v"的部署把数据
# 静默写进匿名卷（正是 NAS 迁移事故的根因）。数据目录必须显式挂载。
# 刻意不加 USER：现网 NAS 挂载目录为 root 属主，改非 root 会让老部署
# 无法写 /data（破坏性变更），需要时连同 chown 迁移方案一起评估。
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=8)"

CMD ["sh", "entrypoint.sh"]
