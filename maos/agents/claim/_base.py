"""理赔域四个 Agent 的共用件 —— 本域 artifact kind 与回执键名口径。

四个 Agent 都是**薄壳**：Identity + 经 SkillInvoker 调 1–2 个 skill + 把 output 包成
artifact。业务判定一行都不在 Agent 里 —— 这不是风格洁癖，是本轨要证明的那句话的
直接后果：

    同一个编排内核，换个领域只换 Skill / ToolPort / 业务对象。

判定一旦漏进 Agent，「换域只换 Skill」就不成立了：下一个业务域得把这些 Agent 也重写
一遍。所以这里只有搬运和包装。

`extras_of` / `artifact` / `failed` 三个函数的实现已下沉到
`maos/agents/_domain_base.py`（三个域共用一份），本文件转出，对外 import 路径不变。

**本域真正的验收判据**（`artifact()` 把 `self_check` 恒填 pass 的前提）：理赔域产物
没有 build/lint 这回事，本域真正的判据是条款版本、赔款核算与到账回执 ——
那些由本域的守卫与断言把守，不由 Gate 的 self_check 判。
"""

from __future__ import annotations

from .._domain_base import artifact, extras_of, failed  # noqa: F401 —— 对外 import 路径不变

# 本域新增的 artifact kind。**刻意不进 maos/artifacts.py 的 ALL_KINDS** ——
# 那份清单是跨轨冻结口径（且本轨只读不写），单轨往里加会和别人撞。
# Gate 对非代码类产物不查 kind 白名单，只按 self_check / summary 判，所以安全。
#
# 更要紧的是**不能**复用 patch_set / test_report：Gate 用产物类型判「这是不是代码类
# 任务」，沾上这两个 kind，理赔任务就会被要求交一份跑出来的测试报告，
# 而理赔域根本没有那种东西 —— 闸会恒 blocker，且报错信息指向测试而不是理赔。
#
# 同理**不下沉到 `_domain_base.py`**：这份清单是本域自己的口径，并进骨架就等于
# 让三个域共用一份 kind 表，谁加一个别的域都跟着变。
KIND_CLAIM_DRAFT = "claim_case_draft"
KIND_ADJUDICATION = "claim_adjudication"
KIND_SETTLEMENT = "claim_settlement"
KIND_PAYMENT_INSTRUCTION = "claim_payment_instruction"
KIND_PAYER_RECEIPT = "claim_payer_receipt"

ALL_CLAIM_KINDS = (
    KIND_CLAIM_DRAFT, KIND_ADJUDICATION, KIND_SETTLEMENT,
    KIND_PAYMENT_INSTRUCTION, KIND_PAYER_RECEIPT,
)

#: 🔴 赔付方回执在 artifact content 里挂的键名。**必须是 payer_receipt，不能是
#: receipt。**
#:
#: `maos/runtime/gate.py` 的第七道闸 `_gate_gateway` 按数据形状触发：任一 artifact 的
#: `content["receipt"]` 是带 `code` 的 dict 就进闸，然后拿那个 code 去查
#: `maos/tools/gateway_codes.py`（**支付宝**那张表）。X12 的 CARC 与支付宝的码值空间
#: 完全不相交，`96` 送进去只会得到一条「未知错误码」的 blocker —— 理赔任务会因为
#: 一条完全正确的拒付回执被判不合格。
#:
#: 这不是绕开闸，是**两张码表本来就不该混查**：拿支付宝的码表去解释 X12 的码，
#: 判出来的任何结论都是错的。第七道闸的码表硬绑在 gateway_codes 上这件事已记
#: `docs/BACKLOG.md ## task-T37`，本轨不当场改内核（铁律 4）。
#: 这条边界有回归测试守着：`maos/tests/test_claim_gate_isolation.py`。
RECEIPT_FIELD = "payer_receipt"
