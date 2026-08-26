# Phase 6（D7 · 9.1）文档日

## 目标

评审打开仓库 15 分钟能跑通、能对号入座。

## 步骤

1. docs/agent-identity.md：六角色清单，**从代码里的 Identity 生成**（agent_id / role / duty / allowed_skills / allowed_tools / write_scope / max_risk / model_tier / 协同关系），按参赛手册附录 A 的字段顺序排。

2. docs/skill-catalog.md：7 个 Skill × 九要素，**从 SkillContract 实例自动生成**（写个 scripts/gen_docs.py，保证文档和代码永不打架），末尾加"版本/发布/回滚/质量评估"一节（registry 多版本 + SkillInvoked 数据）。

3. docs/agentteams-mapping.md：总体方案 §3 五项映射表，每行补上代码文件+行号链接，以及最终采用 B/C 哪一档的说明。🆙 文档中明确一行：**AutoGen 降级为方案文档中的可选 Worker 内核，复赛代码不集成**。复赛的模型调用路径是 OpenAICompatClient + SkillInvoker，不经任何 agent 框架。方案 PPT 与 52 页文档中涉及 AutoGen 的表述同步改口为"可插拔内核之一（未在复赛演示中启用）"。

4. docs/toolport-contract.md：ToolPort 九要素 + 三个已实现工具的契约表 + "迁移到 MCP = 换 entrypoint 传输层，schema 与审计不变"迁移成本一节。🆙 补一节沙箱边界：网络禁用 / 只读文件系统 / 资源限额 / 环境变量白名单，附隔离负例与 evidence/isolation/ 索引。**不使用"OS 级隔离"表述**（没有 Landlock/seccomp 就不说，免被追问）。

5. docs/architecture.md：总体方案 §2 的 mermaid 图 + 数据流九步。🆙 补一句（评审话术的代码依据）：治理层的每一条路径（replan / 补偿 / 审批 / 返工上限）均可在无模型条件下确定性复现——**治理的可测试性独立于模型**。

6. 重写根 README.md，顺序：场景故事（§5.1 那段）→ 架构图 → 5 分钟快速开始（含无 key 的 Scripted 模式，评审没有 key 也能跑！）→ 五场景说明 → evidence 索引 → 安全边界（沿用旧 README 那节并更新，🆙 含沙箱边界与隔离证据）→ 与提案/比赛要求的映射索引。

7. 新克隆冒烟：git clone 到全新目录，严格按新 README 从零跑场景 1（Scripted 模式），掐表 ≤15 分钟，过不了就改 README 直到过。

8. 🆕 通读 docs/DECISIONS.md，把其中影响架构叙事的决策回写进 docs/architecture.md 或 agentteams-mapping.md——评审如果问"为什么这么设计"，答案要在仓库里找得到。

## 验收

新克隆冒烟通过；python scripts/gen_docs.py --check 确认文档与代码一致；ls docs/ 七份齐（含 BACKLOG.md、DECISIONS.md）。
🆙 另有一条"授权变量名不得出现在 docs/ 与 README.md"的泄漏检查，由人类按本地完整手册附 C 执行（该检查的字面命令不入库，否则自破）。

## 提交

`docs(p6): identity/skill/mapping/toolport/architecture docs + README rewrite (sandbox boundary, determinism claim)`
