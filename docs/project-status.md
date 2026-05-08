# 朗姿智能工牌系统项目状态

> 更新日期：2026-04-14
> 当前阶段：企微工作台与后台主链路稳定运行，进入文档清理与历史模块收口阶段

## 1. 当前已经完成的能力

### 核心分析链路

- 上传转写 JSON，创建分析任务
- 通过 LLM 提取客户诉求、顾虑、画像、接诊评价
- 结果落库并写入结果文件
- 管理后台查看录音列表、录音详情、分析结果详情
- 单任务 / 批量导出 Excel

### 管理后台

- 标签分类与标签 CRUD
- 热词组与热词 CRUD
- 总结模板 CRUD
- 质检维度与检查点 CRUD
- 人员 CRUD
- Dashboard 统计页
- 企微工作台 H5（免密登录、移动端接诊/录音查看与关联）

### 认证与实时能力

- JWT 登录与 `/auth/me`
- 业务路由 Bearer Token 鉴权
- WebSocket 任务进度推送
- WebSocket 已补上认证校验

## 2. 最近一轮已补齐的工程化改动

### 配置与路径统一

- LLM 配置接入 `Settings`
- 上传目录与结果目录统一走配置
- 分析 runner / 旧结果接口统一使用同一套路径规则

### 任务可靠性

- 新增任务分发层，支持 `dramatiq` / `background` / `eager`
- 默认模式已切到 `dramatiq`
- 任务执行增加“认领”动作，避免同任务被重复执行
- 失败任务重试改为原子状态切换，减少并发竞态

### 文件与数据安全

- 上传文件名冲突时自动避让
- 删除任务时清理受管 raw/result 文件
- 批量导入会校验 result schema
- WebSocket 连接要求 token

### 遗留脚本修复

- 重建了旧的分析 CLI、分段 CLI、本地 viewer
- 修掉了原先损坏的编码/语法问题

### 基础设施

- 新增 `apps/api/Dockerfile`
- `docker-compose.yml` 现在包含 `api` 和 `worker`
- `api` / `worker` 共用上传与结果卷
- 默认 compose 启动链路已经支持真正的 worker 模式

## 3. 当前验证状态

已通过：

- `uv run pytest`
- `uv run ruff check`
- `uv run python -m compileall src/smart_badge_api`
- `pnpm --dir apps/web lint`
- `pnpm --dir apps/web build`

当前后端测试覆盖了：

- 登录与当前用户
- 上传任务并完成分析
- 删除任务清理文件
- 批量导入跳过非法结果
- 单任务 / 批量 Excel 导出
- WebSocket 认证与广播
- 分析任务重复执行保护
- 健康检查运行时信息
- Staff / Customer / Visit 全 CRUD
- 录音上传 → ASR 转写 → 片段自动拆分 E2E
- 片段关联 / 取消关联到诊单、重新拆分
- 录音格式校验与手动触发转写
- Tag / Hotword / Template / Quality 配置 CRUD
- 四级权限边界（super_admin / system_admin / hospital_admin / staff）
- Dashboard 统计数据一致性
- Refresh Token 签发、刷新、失效

测试数：219 项（全部通过）

## 4. 当前默认运行方式

推荐开发方式：

1. 启动基础设施：`pnpm dev:infra`
2. 启动 API：`pnpm dev:api`
3. 启动 Worker：`pnpm dev:worker`
4. 启动前端：`pnpm dev:web`

如果只想临时单进程调试，可以在 `apps/api/.env` 中把：

```env
TASK_DISPATCH_MODE=background
```

## 5. 还没做完的核心业务模块

按优先级看，现阶段还缺：

- ~~客户管理~~ ✅ 已完成
- ~~到诊单管理~~ ✅ 已完成
- ~~录音文件上传管理~~ ✅ 已完成
- ~~ASR 转写任务~~ ✅ 已完成（faster-whisper 本地转写，BatchedInferencePipeline + small 模型 + int8 量化，9 个真实录音全部转写成功）
- ~~录音片段拆分与到诊单关联~~ ✅ 已完成（自动拆分 + 片段关联到诊单 + 重新拆分）
- ~~企微侧边栏 H5~~ ❌ 已归档（当前正式主入口为企微工作台）
- ~~企微工作台 H5~~ ✅ 已完成（免密登录 + 移动工作台 + 接诊/录音主链路）
- ~~更细粒度 RBAC~~ ✅ 已完成（四级角色 super_admin/system_admin/hospital_admin/staff + 后端数据范围 + 前端菜单过滤）

## 6. 仍值得继续优化的工程项

- ~~Dashboard 统计目前仍是按任务全量扫描，后续需要缓存或汇总表~~ ✅ 已优化（合并 COUNT 查询 + 30 秒内存缓存，16 条 SQL → 8 条）
- ~~Dashboard 缺少转写与片段统计~~ ✅ 已补充（total_transcripts / completed / failed / total_segments / segments_with_visit），前端已展示
- ~~列表 API 缺少分页~~ ✅ 已补充通用分页（后端 7 条路由 + 前端 antd Table 服务端分页）
- ~~has_transcript 基于 transcript_text 判断不准~~ ✅ 已修复为 Transcript relationship
- ~~前端主包仍偏大，`vite build` 会给出 chunk warning~~ ✅ 已完成代码拆分（React.lazy + manualChunks，主包 1478→26 KB）
- ~~目前还没有 refresh token~~ ✅ 已完成（30 分钟 access + 7 天 refresh，前端 401 自动刷新）
- ~~旧版 `/admin/analysis` 页面仍是"文件系统视角"，后续建议逐步统一到结果视角~~ ✅ 已统一（移除旧入口，统一收敛到 `/admin/llm-results` 与 `/admin/recordings`）
- ~~还没有端到端测试和部署文档闭环~~ ✅ 已完成（39 项 E2E 测试 + docs/deployment.md）
- ~~录音文件存储 `file_path` 为绝对路径，环境迁移后需调整~~ ✅ 已修复为相对路径存储（resolve_file_path / make_relative_path，向后兼容旧绝对路径）

## 7. 真实 ASR 集成（faster-whisper）

### 架构

- ASR Provider 模式：支持 `mock` 和 `whisper` 两种 provider（通过 `.env` 的 `ASR_PROVIDER` 配置切换）
- Whisper Provider：`apps/api/src/smart_badge_api/asr/whisper_provider.py`
  - 使用 `faster-whisper` 1.2.1 + `BatchedInferencePipeline` 进行分块并行推理（比逐段顺序推理快 2-5 倍）
  - 模型惰性加载（进程级单例），首次使用时自动从 HuggingFace 下载
  - 支持配置模型大小（`small`/`medium`/`large-v3`）、设备（`cpu`/`cuda`/`auto`）、量化类型（`int8`/`float16`/`int8_float16`/`auto`）
  - 当前使用 `large-v3` 模型 + `cuda` (RTX 3060 6GB) + `int8_float16` 量化
  - Windows 下自动注册 `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` 的 DLL 路径（`os.add_dll_directory`）
- 服务层：`apps/api/src/smart_badge_api/asr/service.py`
  - `asyncio.Semaphore(1)` 限制并发，避免多个 Whisper 任务争抢 GPU
  - `loop.run_in_executor` 将 CPU 密集型推理放到线程池
- 批量导入端点：`POST /recordings/batch-import`
  - 接受 `directory` 参数扫描音频文件
  - `asyncio.create_task` 异步触发转写（HTTP 立即返回）

### 配置项（`apps/api/.env`）

```env
ASR_PROVIDER=whisper          # mock | whisper
WHISPER_MODEL_SIZE=large-v3     # tiny | base | small | medium | large-v3
WHISPER_DEVICE=cuda            # cpu | cuda | auto
WHISPER_COMPUTE_TYPE=int8_float16  # int8 | float16 | int8_float16 | auto
```

### GPU 加速

- GPU: NVIDIA GeForce RTX 3060 Laptop (6GB VRAM), CUDA 12.3, Driver 546.92
- CUDA 运行时依赖：`nvidia-cublas-cu12` + `nvidia-cudnn-cu12`（通过 `uv pip install` 安装到 venv）
- `whisper_provider.py` 在 Windows 上自动调用 `os.add_dll_directory()` 注册 DLL 搜索路径

### 性能对比

| 配置 | 总时长 (9 文件 389min 音频) | 实时倍率 |
|------|----------------------------|----------|
| `large-v3` + CPU (`int8`) | ~150 min | ~2.6x |
| `large-v3` + GPU (`int8_float16`) | ~35 min | ~11.1x |

GPU 单文件处理速度 7-19x 实时（不含首次模型加载），总体提速约 **4.3 倍**。

### 验证结果

9 个真实医美咨询录音（2.5MB-16.5MB，共 ~389 分钟音频）全部转写成功：

| 文件 | 时长 | 文本长度 | 语句数 | 片段数 |
|------|------|----------|--------|--------|
| audioId_106 | 94 min | 11,925 字 | 88 | 22 |
| audioId_115 | 24 min | 5,913 字 | 55 | 14 |
| audioId_116 | 50 min | 6,604 字 | 64 | 16 |
| audioId_117 | 20 min | 4,837 字 | 41 | 11 |
| audioId_118 | 39 min | 5,759 字 | 51 | 13 |
| audioId_119 | 92 min | 12,330 字 | 108 | 27 |
| audioId_120 | 14 min | 3,353 字 | 22 | 6 |
| audioId_121 | 45 min | 11,204 字 | 93 | 24 |
| audioId_122 | 11 min | ~3,000 字 | ~25 | 3 |

## 8. 下一步建议

核心 MVP 业务模块已全部完成，真实 ASR 已集成并验证，推荐继续推进的方向：

1. ~~接入真实 ASR~~ ✅ 已完成（faster-whisper 本地转写，9 个录音全部成功）
2. AI 质检评分对接对话片段（当前质检维度已配置，需将评分流程接入 Segment）
3. ~~端到端测试与部署文档闭环~~ ✅ 已完成
4. ~~前端性能优化（代码拆分、懒加载降低主包体积）~~ ✅ 已完成
5. 对话说话人分离（当前所有发言标记为 unknown，后续可接入 speaker diarization）
6. 录音时长自动提取（当前 duration_seconds 在上传时为空，可用 ffprobe 或 mutagen 提取）
