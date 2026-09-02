"""应付账款域四个 Agent 的共用件 —— 本域 artifact kind 口径。

四个 Agent 都是**薄壳**：Identity + 经 SkillInvoker 调 1–2 个 skill + 把 output 包成
artifact。业务判定一行都不在 Agent 里 —— 这不是风格洁癖，是本域要证明的那句话的
直接后果：

    同一个编排内核，换个领域只换 Skill / ToolPort / 业务对象。

判定一旦漏进 Agent，「换域只换 Skill」就不成立了：下一个业务域得把这些 Agent 也
重写一遍。所以这里只有搬运和包装。

`extras_of` / `artifact` / `failed` 三个函数的实现已下沉到
`maos/agents/_domain_base.py`（三个域共用一份），本文件转出，对外 import 路径不变。

**本域真正的验收判据**（`artifact()` 把 `self_check` 恒填 pass 的前提）：本域产物
没有 build/lint 这回事，真正的判据是三单匹配的规则编号、审批记录与银行回单 ——
那些由本域自己的断言把守，不由 Gate 的 self_check 判。
"""

from __future__ import annotations

from .._domain_base import artifact, extras_of, failed  # noqa: F401 —— 对外 import 路径不变

# 本域新增的 artifact kind。**刻意不进 maos/artifacts.py 的 ALL_KINDS** ——
# 那份清单是跨轨冻结口径，单轨往里加会和别人撞（口径同 agents/refund/_base.py）。
# Gate 对非代码类产物不查 kind 白名单，只按 self_check / summary 判，所以安全。
#
# 更要紧的是**不能**复用 patch_set / test_report：Gate 用产物类型判「这是不是代码类
# 任务」，沾上这两个 kind，应付账款任务就会被要求交一份跑出来的测试报告，
# 而本域根本没有那种东西 —— 闸会恒 blocker，且报错信息指向测试而不是应付账款。
#
# 同理**不下沉到 `_domain_base.py`**：这份清单是本域自己的口径，并进骨架就等于
# 让三个域共用一份 kind 表，谁加一个别的域都跟着变。
KIND_INVOICE_INTAKE = "ap_invoice_intake"
KIND_MATCH_RESULT = "ap_match_result"
KIND_PAYMENT_PLAN = "ap_payment_plan"
KIND_PAYMENT_INSTRUCTION = "ap_payment_instruction"
KIND_BANK_ADVICE = "ap_bank_advice"

ALL_AP_KINDS = (
    KIND_INVOICE_INTAKE, KIND_MATCH_RESULT, KIND_PAYMENT_PLAN,
    KIND_PAYMENT_INSTRUCTION, KIND_BANK_ADVICE,
)
