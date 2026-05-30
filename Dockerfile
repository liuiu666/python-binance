# ============================================================
# 币安合约量化交易系统 — Python 运行时镜像
# 所有服务共用此基础镜像, 通过 command 区分
# ============================================================

FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖 (TA-Lib 需要)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    wget \
    && wget -q http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz \
    && tar -xzf ta-lib-0.4.0-src.tar.gz \
    && cd ta-lib \
    && ./configure --prefix=/usr \
    && make \
    && make install \
    && cd .. \
    && rm -rf ta-lib ta-lib-0.4.0-src.tar.gz \
    && apt-get purge -y --auto-remove build-essential wget \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 默认入口 (由 docker-compose.command 覆盖)
CMD ["python", "-m", "collector.main"]
