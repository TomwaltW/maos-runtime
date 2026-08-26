# Phase 4（D5 · 8.30）闭环首尾 + 补偿回滚 + Replan

🆕 本 Phase 建议先走 Plan mode：补偿回滚与 Replan 触发条件涉及状态机边界，方案确认后再执行。
🆕 本 Phase 会新增 knowledge 表——这是**新增表**，冻结守卫不会拦；但如果它顺手动了既有表，test_contracts_frozen.py 会立刻红。

## 目标

补上聚合（头）、沉淀（尾）、回滚、冲突重规划，八要素闭环全绿。🆙 补偿零模型化，replan 确定性化。

## 步骤

1. 输入侧：scenarios/inputs/ 造多源信号（1 个 issue.json、2 条 feedback.txt、1 段 error.log，故意有重复）；Skill issue.aggregate v1.0.0：聚合去重 → {goal, evidence_refs, duplicates_merged}；run.py --scenario 1 的入口从手写 goal 改为先过 aggregate。

2. 知识层：maos/core/store.py **新增** knowledge 表（id / plan_id / kind(rule|case) / title / body / tags / created_at）——只加表，不动现有表；Skill kb.sink v1.0.0（Plan 终态时把 findings+verdicts 复盘成 1-3 条规则写入）与 kb.retrieve v1.0.0（按 tags/关键词查，空结果不阻塞）；在 runtime 层加 PlanFinalizer：轮询到 Plan 进入 DONE/FAILED 后调 kb.sink——**不把模型调用塞进 Control Plane**。Requirement Agent 执行前先 kb.retrieve，检索结果进 prompt 上下文。

3. 🆙 补偿回滚（反向应用正补丁，零模型）：

   1. compensation artifact 不再是模型生成的逆补丁，改为引用结构：{"mode": "reverse", "patch_ref": "<正向 patch_set 的 artifact id>"}。Coding Agent 对 effect_risk=H 的任务，产出 patch_set 时**自动附带**此引用，零模型调用；
   2. 第五道闸 _gate_compensation 从"查存在"改为"验可执行"：effect_risk=H 时，必须存在 compensation，且在沙箱工作目录对被引用补丁跑 git apply -R --check 干跑成功；干跑失败即 blocker，findings 里写明失败的 hunk；
   3. control_plane.human_decision(approved=False)：若补丁已落沙箱 → sandbox.git_apply(patch, reverse=True) 执行回滚 → append_event_log(event_type="CompensationExecuted", detail={...}) → 再走既有 BLOCKED→FAILED(human_reject) 迁移。**states.py 不加新状态、不加新迁移**；
   4. sandbox.git_apply 的 reverse 参数在 Phase 2 已就位（param_schema 增量，非契约变更，冻结守卫不拦）；
   5. architecture_contract 的可逆性声明（Phase 2 第 6 步已改）在此闭环：不可逆产物禁止 effect_risk=H 自动执行，contract 校验一行断言。

4. Replan：在 on_review_verdict 的 rework 分支加触发 judgment：同一任务第 2 次 rework、或单轮 findings 中 blocker ≥ 2 → Plan RUNNING→PENDING("replan"，已有迁移) → 冻结未派发任务 → Manager 带全部 findings 重规划剩余工作 → start_plan 重启。🆙 maos/main.py 加场景 5：**全程强制 ScriptedModelClient**（--scenario 5 忽略 MAOS_LLM_API_KEY，启动时打印一行"治理路径演示，无模型确定性复现"）。脚本第一版规划**固定产出**会撞双 blocker 的方案，确定性触发 replan；第二版规划固定通过。真模型只进场景 1/2/3。**原则：replan、补偿、审批是控制面行为，其正确性不得依赖模型的智力表现。**

5. 新增测试：aggregate 去重正确性；kb sink/retrieve 闭环；reject 触发补偿且沙箱文件真实还原；🆙 补偿干跑失败→blocker 负例；replan 触发条件三种边界。

## 验收

```bash
python -m pytest maos/tests -q                              # 冻结守卫必须仍然绿
python -m pytest maos/tests -q -k compensation              # 🆙 含干跑失败负例
MAOS_LLM_API_KEY=... python run.py --scenario 3 --matrix    # /reject → 沙箱 git log 出现 revert
python run.py --scenario 5                                  # 🆙 不带 key。replan 路径 → DONE，任何机器任何时刻结果一致
sqlite3 <db> "select title from knowledge"                  # 有沉淀条目
MAOS_LLM_API_KEY=... python run.py --scenario 1             # 第二次跑，日志可见 kb.retrieve 命中
```

## 提交

`feat(p4): aggregate/sink skills, knowledge table, reverse-apply compensation w/ dry-run gate, deterministic replan scenario`
