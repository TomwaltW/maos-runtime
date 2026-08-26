# Phase 5（D6 · 8.31）可观测 + 证据束 + 部署

## 目标

一条命令起全套，五场景证据落盘。🆙 隔离证据入束。

## 步骤

1. maos/obs/trace.py（必做）：export_trace(plan_id) -> trace.json——从 event_log 把该 plan 的事件按 trace_id 织成 span 树（StateTransition/SkillInvoked/ToolInvoked 各成 span，parent 按 task 归属），字段命名对齐 OTel 语义（trace_id/span_id/parent_span_id/name/start/end/attributes）。

2. maos/obs/otel.py（可选，时间不够直接跳过）：检测到 opentelemetry-sdk 已装则在 _transit 与 SkillInvoker 挂真 span，OTLP 导出；没装则静默不启用。方案文档里写"JSON 导出已对齐 OTel 语义，接 Studio 仅换 exporter"。

3. scripts/make_evidence.py：一键跑五场景，每个场景产出 evidence/scenario-N/：run.log（完整 stdout）、trace.json、result.json（终态+关键指标：耗时/rework 次数/事件数；🆙 场景 5 加 "deterministic": true；🆙 各场景汇总 tokens_total，来源为 SkillInvoked 里的 usage）、kb-dump.json；场景 3 目录留 SCREENSHOT-HERE.md 提示人类把 Element 截图放进来。🆙 场景 5 无需 key。🆙 增产 evidence/isolation/escape-attempt.log：真实执行一次隔离负例的完整输出。**脚本失败即报错退出，绝不写占位假数据。**

🆕 **证据完整性三条硬要求**：

   - 每个产出文件首行写入 `# generated at <ISO8601> from <git rev-parse HEAD>`，由脚本自动生成，不许手填；
   - 所有 stdout 落盘前过一层脱敏：凡形如 sk-*、Bearer *、MAOS_LLM_API_KEY=*、MATRIX_TOKEN=* 的片段一律替换成 ***REDACTED***；
   - 脚本必须用 subprocess 真实执行并 tee，禁止由模型"复述"命令输出。

4. deploy/docker-compose.yml：maos 服务（挂载 scenarios+evidence，读 .env）+ 注释块指引 HiClaw/Synapse 怎么并排起；deploy/.env.example 全量配置样例。🆙 compose 说明块里注明 maos-sandbox 镜像需先 build（Phase 2 第 2 步）。

5. .gitignore 确认：evidence/ 提交（它就是交付物），沙箱临时目录/数据库文件不提交，🆕 .env 不提交，🆕 .contracts.lock **必须提交**。

## 验收

```bash
MAOS_LLM_API_KEY=... python scripts/make_evidence.py   # evidence/ 五目录 + isolation/ 齐，trace.json 可被 jq 解析
docker compose -f deploy/docker-compose.yml up          # 容器内场景 1 跑通
python -m pytest maos/tests -q
```

🆕 **证据抽查**（人类自己做，2 分钟）：

```bash
head -1 evidence/scenario-*/run.log evidence/isolation/*.log   # 每份都有时间戳+commit sha
ls -l -T evidence/                                             # mtime 与 commit 时间对得上
grep -rIniE 'sk-[a-zA-Z0-9]|bearer |api[_-]?key' evidence/ | head   # 必须为空
```

判断真伪的三个特征：耗时数字是否过于整齐（编造的常是整数）；有没有真实的 warning/deprecation 噪音（真输出总是脏的，太干净就可疑）；用例数与 fixture 对不对得上。

## 提交

`feat(p5): otel-aligned trace export, evidence generator (+isolation & token metering), docker-compose`
