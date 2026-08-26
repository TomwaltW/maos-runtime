# Phase 1（D2 · 8.27）Skill 层 + 真模型客户端

## 目标

Skill 从权限字符串变成实体层；模型从脚本假模型换成真 LLM（测试仍用假模型）。

## 步骤

1. maos/skills/contract.py：

   - @dataclass SkillContract：name / version（semver 字符串）/ purpose / input_schema（dict）/ output_schema（dict）/ preconditions（list[str]）/ depends_tools（list[str]）/ failure_policy（枚举：retry|fallback|escalate，含 max_retries）/ security_boundary（str）/ reuse_note（str）/ owner_roles（list[str]）
   - @dataclass SkillResult：status（ok|failed）/ output / error / duration_ms

2. maos/skills/registry.py：@register_skill 装饰器；get(name, version=None) 默认取最高版本；**保留历史版本**（dict[name][version]），这是"发布/回滚"叙事的代码依据。

3. maos/skills/invoker.py：SkillInvoker(identity, store)，调用流程：

   1. 校验 skill.name ∈ identity.allowed_skills，不在 → 抛 PermissionDenied（复用 agents/base 里现有异常）；
   2. 逐条检查 preconditions；
   3. 执行，按 failure_policy 处理失败；
   4. 无论成败，store.append_event_log({... event_type: "SkillInvoked", detail: {skill, version, status, duration_ms, input_digest, output_hash}})——**用现有 append_event_log，不新增 Topic，不碰 events.py**。

4. 首发 2 个 Skill 落地到 maos/skills/builtin/（其余 5 个在 Phase 2/4）：

   - req.normalize v1.0.0：包住 Requirement 的模型调用；
   - code.repo-patch v1.0.0：包住 Coding 现有逻辑（路径白名单校验保留在 Skill 的 security_boundary 执行处）。

5. 改 maos/agents/base.py：BaseAgent 持有 self.skills = SkillInvoker(self.identity, store)；Coding 的 run() 改为经 invoker 调 code.repo-patch。Manager 暂不动（它 allowed_tools 本来就是空的）。

6. maos/model/client.py 新增 OpenAICompatClient（🆙 参数全面修订）：

   - 只用标准库 urllib.request（保持核心零依赖），读 MAOS_LLM_BASE_URL / MAOS_LLM_API_KEY / MAOS_LLM_MODEL；
   - 🆙 超时：MAOS_LLM_TIMEOUT 环境变量，**默认 120s**（30s 对长生成必超）；
   - 🆙 重试：仅对 429 / 500 / 502 / 503 / 529 重试，指数退避 2s → 6s → 18s，各加 ±30% 抖动，至多 3 次；响应带 Retry-After 则优先遵从。**其余 4xx 立即失败不重试**（限流时盲目重试是火上浇油）；
   - 🆙 计量：响应的 usage 字段（prompt_tokens / completion_tokens / total_tokens）写入该次 SkillInvoked 事件的 detail；provider 不返回 usage 时容错为 null。这是七层架构表里"token 计量"承诺唯一的真实数据来源；
   - 🆕 **异常与日志里禁止回显 api_key 或带 key 的完整 URL**：报错信息只允许出现 base_url 的 host 部分和 HTTP 状态码；
   - maos/main.py 按环境变量选择：有 key 用真模型，没有回落 ScriptedModelClient 并打印醒目提示；
   - **maos/tests/ 一律强制 ScriptedModelClient**，测试不许发网络请求。

7. 新增测试 maos/tests/test_skills.py：注册/取版本/越权拒绝（identity 无此 skill → PermissionDenied）/失败策略 retry 生效/SkillInvoked 事件落库，≥5 条。

## 验收

```bash
python -m pytest maos/tests -q                      # 旧 9 条 + 冻结 2 条 + 新 ≥5 条全绿
python run.py                                       # 无 key：Scripted 跑通
MAOS_LLM_API_KEY=... python run.py                  # 有 key：场景 1 真模型跑通
sqlite3 <db> "select count(*) from event_log where event_type='SkillInvoked'"   # >0
```

## 提交

`feat(p1): skill contract/registry/invoker + real LLM client (120s timeout, tiered backoff, usage metering)`
