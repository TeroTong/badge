# 技术栈决策记录

> 本文档记录经过 Claude 初提 → GPT-Codex-5.3 评审 → 最终合并后的技术栈选型。
> 所有"暂缓"项均有明确的接入触发条件，不会遗忘。
> v2 - 2026-03-11 更新：对齐开发计划，补充 APScheduler、openpyxl、passlib/JWT、MinIO SDK、图表库、PDF 导出库。

## 最终采纳清单

### 前端 (apps/web)

| 类别 | 选型 | 版本 | 说明 |
|---|---|---|---|
| 框架 | React + TypeScript | 19 / 5.9 | 已有 |
| 构建 | Vite | 7 | 已有 |
| 路由 | React Router | 7 | 已有 |
| 服务端状态 | TanStack Query | 5 | 已有 |
| UI (移动端) | antd-mobile | 5 | 侧边栏 + 工作台 |
| UI (桌面端) | antd | 6 | PC 后台 |
| 图标 | @ant-design/icons | 6 | 配合 antd |
| 图表 | @ant-design/charts | 2 | 统计看板数据可视化（基于 G2，与 antd 设计语言一致） |
| Schema 校验 | zod | 4 | 前后端共享校验逻辑 |
| HTTP 客户端 | ky | 1 | 基于 fetch，轻量 |
| 日期处理 | dayjs | 1 | antd 日期组件依赖 |
| 样式 | CSS Modules + 组件库内置 | — | 全局样式仅保留 reset/主题变量 |

### 后端 (apps/api)

| 类别 | 选型 | 说明 |
|---|---|---|
| 框架 | FastAPI + Pydantic v2 + Pydantic Settings | 已有 |
| ORM | SQLAlchemy 2.0 (同步) | 已有 |
| 迁移 | Alembic | 已有 |
| 异步任务 | dramatiq + Redis | 已有 |
| 定时调度 | APScheduler | 设备心跳超时检查等定时任务 |
| 认证 | python-jose[cryptography] (JWT) + passlib[bcrypt] | PC 后台登录 Token 管理 + 密码哈希 |
| 对象存储 | minio (Python SDK) | S3 兼容，录音文件上传/下载/presigned URL |
| Excel 处理 | openpyxl | Excel 导入（客户数据）+ 导出（质检报告等） |
| PDF 生成 | reportlab | 质检报告 PDF 导出 |
| HTTP 客户端 | httpx | ASR/LLM 外部服务调用 |
| 代码质量 | ruff | 已有 |
| 测试 | pytest + httpx | 已有 |

### 基础设施

| 类别 | 选型 | 说明 |
|---|---|---|
| 数据库 | PostgreSQL 16 | docker-compose 已配置 |
| 缓存/队列 | Redis 7 | docker-compose 已配置 |
| 对象存储 | MinIO (S3 兼容) | docker-compose 已配置 |
| 包管理 | pnpm (前端) + uv (后端) | 已有 |

## 条件采纳（有明确触发条件）

| 库 | 触发条件 | 原因 |
|---|---|---|
| zustand | 出现跨页面、非服务端、非 URL 的共享业务状态 | 先用 TanStack Query + URL + 局部 state，避免和 Query 缓存职责重叠 |

## 明确暂缓（不在 MVP 早期引入）

| 库 | 暂缓原因 | 何时重新评估 |
|---|---|---|
| react-hook-form | antd/antd-mobile 自带 Form 已够用，引入会多一层适配 | 如果发现组件库 Form 无法满足复杂校验场景 |
| async SQLAlchemy | 当前同步模式够用，async 增加复杂度 | 出现明确的并发性能瓶颈时 |
| pyright | 好工具但前期模型快速变化时噪音多 | Week 3-4 数据模型稳定后接入 |

## 明确不用

| 库 | 原因 |
|---|---|
| Tailwind CSS | AI Agent 无视觉反馈，大量 utility class 组合的视觉验证困难 |
| styled-components | 运行时开销 + 调试复杂度 |
| Redux / MobX | 项目规模不需要重量级状态管理 |
| axios | ky 更轻量，TS 体验更好 |

## 表单策略

MVP 阶段直接使用 antd `<Form>` 和 antd-mobile `<Form>` 组件自带的表单管理能力，配合 zod 做手动校验（在 onFinish 中调用 zod parse）。不引入 react-hook-form。

## 验证手段（AI Agent 开发约束）

| 验证层 | 工具 | 频率 |
|---|---|---|
| 前端类型检查 | `tsc --noEmit` | 每次改动后 |
| 前端 lint | `eslint` | 每次改动后 |
| 后端类型检查 | pyright (暂缓) | 数据模型稳定后 |
| 后端 lint/格式化 | ruff | 每次改动后 |
| 后端接口测试 | pytest + httpx | 每个接口完成后 |
| API 契约 | FastAPI 自动生成 OpenAPI | 持续 |

