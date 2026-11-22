# 用静态 ffmpeg 构建（无 glibc 依赖，跨发行版可用；避免 apt 拉 ffmpeg 依赖链过重/网络受限）
FROM mwader/static-ffmpeg:7.1 AS ffmpeg

FROM python:3.12-slim

# 从静态镜像复制 ffmpeg/ffprobe
COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg /ffprobe /usr/local/bin/ffprobe

WORKDIR /app

# 只装运行时依赖；技能脚本的额外依赖（fonttools/weasyprint）不在服务路径上
# PIP_INDEX_URL 可覆盖 pip 源；国内部署传 https://mirrors.aliyun.com/pypi/simple/ 加速
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

COPY server/ server/
COPY web/ web/

ENV HOST=0.0.0.0 \
    PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# 单机演示足够；生产请换多进程服务器并前置反代（见 docs/ARCHITECTURE.md）
CMD ["python3", "server/app.py"]
