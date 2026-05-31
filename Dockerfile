# ============================================================
# 币安合约量化交易系统 — Collector 专用镜像
# 仅运行数据采集服务, 纯 Python 依赖, 无需编译工具链
# ============================================================
FROM python:3.11-slim

WORKDIR /app

# 配置 pip 国内镜像源加速 (清华大学)
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    && pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn

# 先复制依赖文件, 利用 Docker 缓存层加速重建
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 只复制 Collector 和公共模块代码 (不含 api/strategy/executor/frontend)
COPY common/ ./common/
COPY collector/ ./collector/

# 创建日志目录
RUN mkdir -p /app/logs

# 默认入口: 数据采集服务
CMD ["python", "-m", "collector.main"]
