# 工牌系统测试环境

后续改工牌系统，默认先改测试环境：

- 测试代码目录：`/home/ymailancy/badge-test`
- 测试前端：`http://gongpai.bravou.tech:5174`
- 测试 API：`http://127.0.0.1:8001`
- 生产代码目录：`/opt/badge`
- 生产前端：`http://gongpai.bravou.tech:5173`
- 生产 API：`http://127.0.0.1:8000`

测试环境和生产环境共用同一个后台数据库，但测试环境默认关闭钉钉录音同步、历史归档同步、失败补处理、员工目录定时同步、ASR 热词自动巡检、腾讯云 ASR 自动调用、SAP 自动回传和 SAP 真实发送。

测试环境启动：

```bash
/home/ymailancy/badge-test/scripts/ensure-smart-badge-api-test.sh
/home/ymailancy/badge-test/scripts/deploy-smart-badge-web-test-proxy.sh
```

同步测试环境录音列表/分析结果页展示数据：

```bash
/home/ymailancy/badge-test/scripts/sync-smart-badge-test-data.sh
```

测试环境说明文档：

```text
/home/ymailancy/badge-test/docs/staging-environment.md
```

注意：不要在 `/opt/badge/apps/web` 里随手运行 `pnpm build`，这会直接覆盖生产前端 dist。前端测试构建应在 `/home/ymailancy/badge-test/apps/web` 内完成。
