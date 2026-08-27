# Task-A 判断记录

派单没写死、由本轨自行取舍的地方，一条一行。格式：`日期 | 契约点 | 情境 | 选择 | 理由`。
（本文件是 Task-A 专用，不动 docs/DECISIONS.md —— 那份归主干，多轨并行时同改必冲突。）

## 判断

2026-08-27 | A-12 | 真模型客户端叫什么、放哪 | 新增 `GatewayModelClient`（OpenAI 兼容协议，走 `{base_url}/chat/completions`），`HigressModelClient` 原样不动 | 后者的 docstring 写明归 Track B；改它等于替别人的轨做决定。

2026-08-27 | A-12 | 真模型分支用什么发 HTTP | 标准库 `urllib.request` | 装依赖属于「必须问人」的四类之一（CLAUDE.md），而这一层用不上 httpx/requests 的任何特性。

2026-08-27 | A-12 | 降级判据 | `MAOS_LLM_BASE_URL` / `MAOS_LLM_API_KEY` / `MAOS_LLM_MODEL` 三个全部非空才走真模型，缺任一降级 Scripted | 派单只写了「无 key 降级」；但缺 base_url 或 model 时真模型必然发不出请求，与无 key 等价，早降级比晚报错好。日志只打**缺失的变量名**，不打值（铁律 6）。

2026-08-27 | A-12 | tier 怎么参与 | tier 不选模型，只作为 `X-MAOS-Tier` 请求头传给网关 | client.py 模块 docstring 原文：「tier 到具体模型的映射是治理决策，属于网关的职责」。

2026-08-27 | 铁律 6 | key 泄露面 | key 存 `_api_key`、`__repr__` 不含它、出网异常一律 `_scrub()` + `raise ... from None` | 掐断异常链是关键：底层 traceback 会带 Request 头，链一留 Authorization 就可能进日志/evidence。

2026-08-27 | 附录B | `code.repo-patch` 的 failure_policy | `escalate`（max_retries=0），不 retry | 重试归 worker 的 attempt 层（max_attempts）；skill 再叠一层会让 attempt 计数失真，且安全违规重试等于「多试几次绕过」。

2026-08-27 | 附录B | `req.normalize` 无 model 时的行为 | 规则兜底（确定性输出），不抛 | 上层忘了传 model 不该把链路拖挂；但**有** model 而输出不合出参契约时坚决抛，交给 `failure_policy="retry"` —— 静默降级会把「模型坏了」伪装成「需求就长这样」。

2026-08-27 | 附录B | `req.normalize` 兜底要不要造 constraints | 不造。只搬运 `context["constraints"]`，空就空 | 约束编错会误导后续全部任务；acceptance_suggestions 是「建议」，用通用模板可以接受。

2026-08-27 | 附录B | `code.repo-patch` 出参要不要裁字段 | 原样返回模型给的 dict，只 `setdefault` 补齐 summary / self_check | 出参形状必须与现行 `GOOD_PATCH` 一致；裁字段会让 artifact 内容与改动前不等。

2026-08-27 | 附录B | `self_check` 取值要不要校验 | 不校验，只透传 | 判 build/lint 是 ReviewerGate 的活。skill 抢着判，Gate 就永远见不到失败样本，场景 2 的返工链会断。

2026-08-27 | A-9 | `attempt` 怎么进 skill | 走 `extras`，不进 payload | payload 字段以附录 B 为准（title/inputs/acceptance/rework_findings），扩字段就不是「逐字段照抄」了；`extras` 本就是 invoker 的旁路（docstring 列了 model/plan_id/task_id/trace_id）。

2026-08-27 | A-9 | `title` 从哪来 | `ctx.inputs.get("title") or ctx.task_id` | TaskContext 与 TaskAssignment 事件里都没有 title 字段（`control_plane.py:112`），而 contracts/events.py 冻结不许加。

2026-08-27 | A-9 | 安全事件怎么跨模块识别 | 约定字符串前缀：invoker 把异常压成 `"<类名>: <消息>"`，`coding.py` 按 `SECURITY_ERROR_PREFIX = "ProtectedPathViolation"` 认 | invoker 只透出字符串，拿不到异常类型。与它自己的 `skill_not_found:<name>` 同属字符串协议，把守闸是既有的 `test_protected_path_blocked`。

2026-08-27 | C-1 | 谁触发 builtin 动态发现 | `CodingAgent.run()` 里延迟 `import maos.skills.builtin` | 放模块顶部会成环：`agents -> skills.builtin -> (任一 import 了 agents 的 skill) -> agents`。B/D 两轨也要往 builtin 投文件，不能给他们埋这个雷。**但这条只解决运行期**，见下方未决项 2。

2026-08-27 | A-5 | `kb.retrieve` 怎么接 | `CodingAgent.run()` 里真调一次，成功才把结果塞进 `inputs["knowledge"]` | 现在恒未注册 → invoker 软兜底 failed，不抛不阻塞；Task-D 合并当天这里零改动自动升级。

2026-08-27 | A-5 | 派单 §5 写「detail 七字段」，主线 commit 0510b44 给 SkillInvoked.detail 加了第八个字段 invocation_id | 跟主线走八字段，改本轨断言，不回滚 invocation_id | 那是主线为「权威事实守卫做 actor 溯源」加的增量（events.py 一行没碰），属口径差不是契约破坏；本轨 test_skills.py 的字段集与函数名同步改成八字段。

2026-08-27 | 把守闸 | 人类授权本轨改 `maos/tests/test_registry_autodiscovery.py` 两处（方案 A） | 见下 | 授权只覆盖这一个文件、这两处。

## 曾经 BLOCKED、现已解决

1. **`test_unregistered_skill_returns_soft_failure` 借 `code.repo-patch` 当「未注册」样本** ——
   本轨按附录 B 注册该 skill 后必然假红。已由主线会话改成 `_PROBE_IDENTITY` +
   `probe.never-implemented`（永不实现的哨兵），断言原文一字未改。
   本轨在其之上补一条 `assert registry.get("probe.never-implemented") is None`：
   哨兵哪天被谁实现了，这条测试会退化成一次**真调用**而断言照样绿 —— 静默失效，
   软兜底路径从此无人把守。把「哨兵未注册」本身钉成断言，失效当场变红。

2. **`test_select_model_client_signature_is_frozen` 第 309 行是环境依赖的哑雷** ——
   真模型分支落地后，不带 force_scripted 的那次调用取决于 env，配齐 key 的机器
   （演示机就是）会拿到 GatewayModelClient，`isinstance(..., ScriptedModelClient)` 变红，
   而红的原因与该测试要守的签名冻结无关。已在函数开头 `monkeypatch.delenv` 三个
   `MAOS_LLM_*`（raising=False）。摘的是环境不是语义：force_scripted=True 那条原样还在。
   实测：配齐三个 env 跑全量同样 106 passed。

3. **派单 §6 的 `python3 -c "from maos.skills.registry import get; ..."` 输出 `None None`** ——
   根因是该命令没触发 builtin 动态发现，不是 skill 未注册。等价可通过写法：
   `python3 -c "import maos.skills.builtin; from maos.skills.registry import get; print(...)"`。
   **没有**改 `maos/skills/__init__.py` 去 import builtin：那会让任何 `import maos.agents`
   都触发全量 builtin 发现，B/D 投的 skill 只要 import 了 `maos.agents.*` 就成环，
   这个雷不该由本轨埋给他们。发现仍由 `CodingAgent.run()` 延迟触发。
