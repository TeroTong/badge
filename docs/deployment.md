# 朗姿智能工牌系统 — 部署文档

> 更新日期：2026-04-04

## 1. 环境要求

| 组件 | 最低版本 |
|------|----------|
| Python | 3.13+ |
| Node.js | 20+ |
| pnpm | 9+ |
| Docker & Docker Compose | 27+ / v2+ |
| uv (Python 项目管理) | 0.9+ |

## 2. 项目结构

```
├── apps/
│   ├── api/          # FastAPI 后端
│   └── web/          # React 前端 (Vite)
├── docker-compose.yml
├── pnpm-workspace.yaml
└── docs/
```

## 3. 本地开发

### 3.1 安装依赖

```bash
# 前端
pnpm install

# 后端（进入 api 目录）
cd apps/api
uv sync
```

### 3.2 环境变量

复制 `apps/api/.env.example`（如果有），或手动创建 `apps/api/.env`：

```env
DATABASE_URL=sqlite+aiosqlite:///./smart_badge.db
UPLOAD_DIR=uploads
RESULTS_DIR=results
SECRET_KEY=你的随机密钥           # 生产环境务必更换
TASK_DISPATCH_MODE=background     # background | eager | dramatiq
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=你的LLM密钥
LLM_MODEL=gpt-5.2-chat-latest
DINGTALK_IOT_APP_ID=设备管理平台IOT应用ID
DINGTALK_IOT_APP_SECRET=设备管理平台IOT应用密钥
DINGTALK_IOT_HOSPITAL_CODES=6501
DINGTALK_IOT_AUDIO_DEFAULT_LOOKBACK_DAYS=7
```

### 3.3 初始化数据库

```bash
cd apps/api
uv run alembic upgrade head
```

首次启动时会自动创建默认超级管理员账号：`admin` / `admin123`。

### 3.4 启动各服务

| 终端 | 命令 | 说明 |
|------|------|------|
| 1 | `cd apps/api && uv run uvicorn smart_badge_api.main:app --port 8000 --reload` | API 服务 |
| 2 | `cd apps/web && pnpm dev` | 前端（Vite，默认 5173） |
| 3（可选） | `cd apps/api && uv run dramatiq smart_badge_api.task_queue` | Worker（dramatiq 模式下） |

前端代理规则已在 `vite.config.ts` 中配置：`/api` → `http://127.0.0.1:8000`。

### 3.5 运行测试

```bash
# 后端
cd apps/api
uv run pytest tests/ -v

# 后端 lint
uv run ruff check src/

# 前端构建检查
cd apps/web
pnpm build
```

## 4. Docker Compose 部署

### 4.1 服务组成

| 服务 | 镜像/构建 | 端口 | 说明 |
|------|-----------|------|------|
| postgres | postgres:16 | 5432 | 主数据库 |
| redis | redis:7.4 | 6379 | 任务队列 |
| minio | minio/minio | 9000/9001 | 对象存储（可选） |
| api | 本地构建 | 8000 | FastAPI 服务 + 自动 migrate |
| worker | 本地构建 | — | Dramatiq 后台 Worker |

### 4.2 必配环境变量

在项目根目录创建 `.env`：

```env
SECRET_KEY=一个安全的随机密钥
LLM_API_KEY=你的LLM密钥
DINGTALK_IOT_APP_ID=设备管理平台IOT应用ID
DINGTALK_IOT_APP_SECRET=设备管理平台IOT应用密钥
DINGTALK_IOT_HOSPITAL_CODES=6501
DINGTALK_IOT_AUDIO_DEFAULT_LOOKBACK_DAYS=7
# 可选：
# FRONTEND_URL=http://你的前端域名
# LLM_BASE_URL=https://api.deepseek.com/v1
# LLM_MODEL=gpt-5.2-chat-latest
```

### 4.3 启动

```bash
# 构建并启动全部服务
docker compose up -d --build

# 查看日志
docker compose logs -f api

# 停止
docker compose down
```

API 启动时会自动执行 `alembic upgrade head`，无需手动迁移。

### 4.4 健康检查

```bash
curl http://localhost:8000/api/v1/health
```

返回示例：

```json
{
  "status": "ok",
  "task_dispatch_mode": "dramatiq",
  "requires_worker": true,
  "worker_entrypoint": "uv run dramatiq smart_badge_api.task_queue",
  "llm_configured": true,
  "websocket_auth_required": true
}
```

## 5. 前端独立构建与部署

```bash
cd apps/web
pnpm build
```

产物在 `apps/web/dist/`，可通过 Nginx 或任意静态服务器部署。

### 当前服务器推荐方案：5173 反向代理容器

当前这台服务器没有直接写宿主机 `nginx` 站点配置的前提，因此仓库内提供了一套用户态部署脚本：使用 Docker 启动一个 `nginx` 容器，占用 `5173` 端口，对外提供前端静态文件，并将 `/api`、`/ws` 转发到本机 `8000` 后端。

部署命令：

```bash
/opt/badge/scripts/deploy-smart-badge-web-proxy.sh
```

保活检查：

```bash
/opt/badge/scripts/ensure-smart-badge-web-proxy.sh
```

默认访问地址：

- `http://192.168.5.162:5173`
- `http://gongpai.bravou.tech:5173`

配置文件位置：

- 代理配置：`/opt/badge/deploy/nginx/smart-badge-web-proxy.conf`
- 启动脚本：`/opt/badge/scripts/start-smart-badge-web-proxy.sh`
- 部署脚本：`/opt/badge/scripts/deploy-smart-badge-web-proxy.sh`

### Nginx 参考配置

```nginx
server {
    listen 80;
    server_name badge.example.com;

    root /var/www/smart-badge/dist;
    index index.html;

    # SPA 路由回退
    location / {
        try_files $uri $uri/ /index.html;
    }

    # API 反代
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # WebSocket 反代
    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

## 6. 数据库迁移

```bash
cd apps/api

# 生成迁移脚本
uv run alembic revision --autogenerate -m "描述变更"

# 执行迁移
uv run alembic upgrade head

# 查看当前版本
uv run alembic current
```

## 7. 认证与安全

- 默认超级管理员账号：`admin` / `admin123`（**首次部署后请立即修改密码**）
- JWT：Access Token 30 分钟过期，Refresh Token 7 天过期
- 当前权限层级：超级管理员 / 系统管理员 / 医院管理员 / 普通员工
- `SECRET_KEY` 生产环境必须设置为随机安全值
- WebSocket 连接要求 token 认证

## 8. 任务分发模式

| 模式 | 适用场景 | 是否需要 Worker |
|------|----------|----------------|
| `eager` | 单元测试、快速调试 | 否（同步执行） |
| `background` | 本地开发 | 否（asyncio.create_task） |
| `dramatiq` | 生产环境 | 是（需启动 Worker 进程） |

通过 `TASK_DISPATCH_MODE` 环境变量切换。

## 9. 目录与文件存储

| 目录 | 用途 | 配置项 |
|------|------|--------|
| `uploads/` | 录音文件、转写 JSON | `UPLOAD_DIR` |
| `results/` | 分析结果 JSON | `RESULTS_DIR` |

- 数据库中的 `file_path` 存储为相对路径（兼容旧版绝对路径）
- Docker 部署时通过 volume 挂载持久化

## 10. 常见问题

**Q: 启动报 `CHANGE-ME-IN-PRODUCTION`？**
A: 设置 `SECRET_KEY` 环境变量为安全的随机字符串。

**Q: 前端 build 报 antd chunk > 1300kB？**
A: 这是正常的，`chunkSizeWarningLimit` 已设置为 1300。antd 无法进一步 tree-shake。

**Q: ASR 转写结果是假数据？**
A: 当前默认已接入 `faster-whisper` 真实转写链路；如需切换 provider，可通过 `ASR_PROVIDER` 配置控制。

**Q: Worker 不处理任务？**
A: 确认 `TASK_DISPATCH_MODE=dramatiq`，Redis 可达，Worker 进程已启动。
