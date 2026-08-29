FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    EIGHTNATIONS_DATA=/data \
    TZ=Asia/Shanghai

# 仅运行时依赖（快照工具 playwright 在宿主机跑，不进镜像，保持轻量）
RUN pip install --no-cache-dir \
    "requests>=2.31" "pypdf>=3.0" \
    "fastapi>=0.110" "uvicorn>=0.29" \
    "jinja2>=3.1" "python-multipart>=0.0.9"

COPY core/ core/
COPY sites/ sites/
COPY web/ web/
COPY tools/ tools/
COPY manage.py scheduler.py entrypoint.sh ./
RUN chmod +x entrypoint.sh && mkdir -p /data/db /data/logs

VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=8)"

CMD ["sh", "entrypoint.sh"]
