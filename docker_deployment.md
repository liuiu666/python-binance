# 📡 BXM40 量化交易系统 Docker 启动与部署说明文档

本项目使用 Docker / Docker Compose 实现一键化基础设施搭建和应用容器化部署。下面是关于系统架构、环境配置、本地调试、生产部署以及容器状态验证的完整说明。

---

## 1. 系统架构与服务说明

BXM40 系统采用 **微服务架构**，分为“数据库/基础设施层”与“应用逻辑层”：

| 服务名称 | 对应容器名 | 职责描述 | 外部暴露端口 |
| :--- | :--- | :--- | :--- |
| **Redis** | `bxm40-redis` | 用于实时行情数据推送、Websocket 数据转发、补平进度缓存、策略订阅热更新信号。 | `6379` |
| **ClickHouse** | `bxm40-clickhouse` | 高性能列式存储，负责存放全量分笔明细成交 (agg_trades) 和各周期 K 线数据 (klines)。 | `8123` (HTTP)<br>`9000` (TCP) |
| **PostgreSQL** | `bxm40-postgres` | 关系型数据库，存放币种监控配置、系统配置、模拟交易账户与挂单历史。 | `5432` |
| **Collector** | `bxm40-collector` | 行情数据采集主程序：多线程订阅币安 WS，秒级写入 Redis 流并批量持久化至 ClickHouse。 | `8080` (健康检查) |
| **API** | `bxm40-api` | 后端 FastAPI 网关：提供交易对管理、历史数据回填、断流分析接口、纸单模拟接口与 WebSocket 代理。 | `8000` (API网关) |
| **Strategy** | `bxm40-strategy` | 策略计算引擎：订阅 Redis 行情数据流，计算技术指标并触发开/平仓信号。 | 仅容器内通信 |
| **Executor** | `bxm40-executor` | 交易执行器：监听策略信号，对接到模拟账户 (PostgreSQL) 或实盘交易所。 | 仅容器内通信 |
| **AI** | `bxm40-ai` | AI 指标/模型定时调度和训练程序。 | 仅容器内通信 |

---

## 2. 环境配置文件 `.env`

在启动任何 Docker 容器前，请确保在项目根目录下存在 `.env` 文件。你可以复制 `.env.example` 模板：

```bash
# 拷贝模板
cp .env.example .env
```

打开并配置 `.env`，关键字段说明：
```ini
# 币安 API 凭证 (非敏感接口只读时非必填，实盘必须提供)
BINANCE_API_KEY=your_binance_api_key
BINANCE_API_SECRET=your_binance_api_secret

# 管理员 API Key (前端某些敏感控制指令校验用)
ADMIN_API_KEY=change_me

# 各项外部服务地址 (如果在本机运行 Python 代码，则指向 localhost; 如果全 Docker 运行，则会自动被 Compose 覆盖为容器名)
REDIS_URL=redis://localhost:6379/0
CLICKHOUSE_HOST=localhost
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=
PG_DSN=postgresql://bxm40:bxm40_secret@localhost:5432/bxm40
```

---

## 3. 部署方案一：仅部署基础设施（推荐本地开发调试）

如果你希望在本地直接运行和断点调试 Python 代码（例如 `python -m api.main` 或 `python -m collector.main`），你可以选择**仅在 Docker 中运行 Redis、ClickHouse 和 PostgreSQL**。

### 🚀 启动基础设施
在项目根目录下，运行以下命令：
```powershell
docker-compose -f docker-compose.infra.yml up -d
```

### 🛑 停止基础设施
```powershell
docker-compose -f docker-compose.infra.yml down
```

### 🗑️ 清空所有数据库数据（谨慎！）
```powershell
docker-compose -f docker-compose.infra.yml down -v
```

---

## 4. 部署方案二：全服务 Docker 化部署（推荐生产/独立环境）

如果你的代码已开发完毕，需要部署到云服务器或让系统全后台自动运行，你可以使用根目录下的 `docker-compose.yml` 实现全服务一键托管。

### 🚀 构建并启动全部服务
```powershell
docker-compose up -d --build
```
> **提示**：该命令会自动编译根目录下的 `Dockerfile`（会自动拉取依赖并编译安装量化必备的 `TA-Lib` 底层 C 库），由于需要编译 `TA-Lib`，首次构建可能需要 3-5 分钟，请耐心等待。

### 🛑 停止运行
```powershell
docker-compose down
```

---

## 5. 部署方案三：仅将数据采集（Collector）部署到 Docker

如果你想在 Docker 中后台稳定采集行情，而 API 等辅助服务仍在本地调试，可以单独启动 Collector。

### 1. 本地独立构建镜像
```powershell
docker build -t bxm40-collector .
```

### 2. 运行采集容器
```powershell
docker run -d \
  --name bxm40-collector \
  --env-file .env \
  --network host \
  --restart unless-stopped \
  bxm40-collector
```
> **提示**：这里使用 `--network host` 能够让容器内的采集器直接连接到宿主机上的本地数据库 and Redis 服务。

---

## 6. 验证与排错指南

### 📊 检查容器运行状态
```powershell
docker ps
```
正常情况下，所有容器的 `STATUS` 应显示为 `Up` 或者是 `healthy`。

### 🩺 校验健康状态接口
- **数据采集器健康状态**（每30秒自检，15秒无行情自动判定 degraded）：
  ```powershell
  curl http://localhost:8080/health
  ```
- **后端 API 健康状态**：
  打开浏览器访问 `http://localhost:8000/docs` 可进入 Swagger UI 接口文档页面。

### 📜 查看运行日志
- **查看所有服务日志**：
  ```powershell
  docker-compose logs -f --tail 100
  ```
- **仅查看数据采集器日志**：
  ```powershell
  docker logs -f bxm40-collector
  ```
- **仅查看后端 API 日志**：
  ```powershell
  docker logs -f bxm40-api
  ```
