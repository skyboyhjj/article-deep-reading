FROM python:3.12-slim

# ffmpeg 用于抽音轨/切片；不装的话只能直传纯音频
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 只装运行时依赖；技能脚本的额外依赖（fonttools/weasyprint）不在服务路径上
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ server/
COPY web/ web/

ENV HOST=0.0.0.0 \
    PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# 单机演示足够；生产请换多进程服务器并前置反代（见 docs/ARCHITECTURE.md）
CMD ["python3", "server/app.py"]
