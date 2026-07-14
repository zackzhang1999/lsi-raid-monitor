# ================================================
# LSI RAID Monitor — Docker 镜像
# ================================================

FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖（smartmontools 用于 SMART 采集，sudo 用于调用 storcli）
RUN apt-get update && apt-get install -y --no-install-recommends \
    sudo \
    smartmontools \
    cron \
    && rm -rf /var/lib/apt/lists/*

# 复制项目文件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 数据目录
RUN mkdir -p /app/data
ENV LSI_DATA_DIR=/app/data
ENV STORCLI_PATH=/app/storcli64
ENV FLASK_RUN_HOST=0.0.0.0
ENV FLASK_RUN_PORT=5200

EXPOSE 5200

# 默认启动 Web UI
CMD ["bash", "start_web.sh"]
