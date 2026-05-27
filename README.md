# 智能工牌系统

面向医美咨询场景的录音分析与复盘系统。当前已经打通一条可运行主链路：

`转写 JSON 上传 -> 后台任务分发 -> LLM 分析 -> 结构化结果存储 -> 后台查看 / 导出`

## 当前状态

- 后端：FastAPI + SQLAlchemy async + Alembic + JWT
- 前端：React 19 + Vite + antd + TanStack Query
- 异步任务：Dramatiq + Redis
- 导出：openpyxl Excel
- 实时进度：WebSocket（已鉴权）

目前最稳定的运行方式已经切到 `dramatiq` worker 模式。API 负责接单，worker 负责真正执行分析任务。

## 仓库结构

```text
.
├─ apps/
│  ├─ api/   FastAPI 服务
│  └─ web/   管理后台前端
├─ docs/     项目文档
├─ docker-compose.yml
├─ package.json
└─ README.md
```

## 本地开发

### 1. 启动基础设施

```bash
pnpm dev:infra
```

这会启动 PostgreSQL、Redis 和 MinIO。

### 2. 配置后端环境变量

```bash
cd apps/api
cp .env.example .env
```

至少需要补齐：

- `SECRET_KEY`
- `LLM_API_KEY`

如果需要在同步到诊单时从远程客户档案补全客户性别 / 出生日期，还需要配置：

- `STAFF_DIRECTORY_DSN`

默认 `TASK_DISPATCH_MODE=dramatiq`。如果你只想单进程调试，也可以临时改成：

```env
TASK_DISPATCH_MODE=background
```

`STAFF_DIRECTORY_DSN` 需要指向包含 `cur.customer` / `landing.customer` 的 PostgreSQL，
同步到诊单时会按 `KHBM` / `YYBM` 回填客户性别 `KHXB` 和出生日期 `CSRQ`。

### 3. 启动后端 API

```bash
pnpm dev:api
```

### 4. 启动任务 worker

```bash
pnpm dev:worker
```

### 5. 启动前端

```bash
pnpm install
pnpm dev:web
```

### 6. Windows 快速启动

仓库根目录提供了两个 Windows 启动脚本，可用于本地一键拉起前后端开发服务：

- PowerShell：`./start-dev.ps1`
- 命令行：`start-dev.cmd`

脚本会检查端口占用后再尝试启动：

- 前端：`http://127.0.0.1:5173`
- 后端：`http://127.0.0.1:8000/api/v1/docs`

使用脚本时，请保持新打开的 PowerShell / 命令行窗口持续运行。

## Docker Compose 运行

如果希望直接起一套完整后端栈：

```bash
cp .env.example .env
docker compose up --build api worker
```

说明：

- `api` 和 `worker` 共用 `app-uploads` / `app-results` 卷，确保 worker 能读取 API 上传的原始文件。
- `api` 启动时会自动执行 `alembic upgrade head`。
- `worker` 依赖 `api` 健康检查通过后再启动。

默认暴露端口：

- API: `http://127.0.0.1:8000`
- PostgreSQL: `127.0.0.1:5432`
- Redis: `127.0.0.1:6379`
- MinIO API: `http://127.0.0.1:9000`
- MinIO Console: `http://127.0.0.1:9001`

## 常用命令

```bash
pnpm dev:web
pnpm dev:api
pnpm dev:worker
pnpm dev:infra
pnpm dev:stack
pnpm lint:web
pnpm lint:api
pnpm build:web
pnpm test:api
```

## 健康检查

```bash
curl http://127.0.0.1:8000/api/v1/health
```

返回里会带上：

- 当前任务分发模式
- 是否要求独立 worker
- worker 启动命令
- LLM 是否已配置
- WebSocket 是否要求认证

## 默认账号

- 用户名：`admin`
- 密码：`admin123`
