"""AP Match Agent —— 三单匹配那一端：把发票、订单、收货单之间的分歧摆出来。

## 匹配不过时这个 Agent 返回 ok，不是 failed

这是本域最容易写错的一处。「三单对不上」是一个**结论**，不是一次执行失败：
skill 跑完了、结论产出了、依据（带规则编号的拒付理由）也齐了。判成 failed 会让
控制面去返工 —— 而数据一个字都没变，返工一万次结论一样。

所以：`status=ok`，但把拒付理由挂进 `open_questions`。挂 open_questions 的后果是
任务落 `BLOCKED`（control_plane 对此有明确语义），也就是**停下来等人看** ——
那正是这一档该有的出口。

这一条与本轨要买的第二件东西正好互为表里：「Agent 回 ok」只说明这一步跑完了，
不说明业务成功了。业务成没成看的是 `ap_case.biz_status` 与银行回单，不是
`AgentOutput.status`。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.model.client import Tier

from ._base import KIND_MATCH_RESULT, artifact, extras_of, failed

SKILL_MATCH = "ap.match"

#: 挂进 open_questions 的拒付理由最多几条。**不是为了好看** —— open_questions 会
#: 原样进返工提示词与房间卡片，几十条会把真正要人看的那一条淹掉。
#: 超出的条数在摘要里如实说，不假装只有这么多。
MAX_SHOWN_FINDINGS = 5


@register
class ApMatchAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="ap-match",
        role="ap_match",
        duty="三单匹配：逐行比数量与单价、按 EN16931/Peppol 规则验勾稽，产出可核对的"
             "拒付理由与应付金额",
        allowed_skills=frozenset({"ap.match"}),
        allowed_tools=frozenset(),          # 匹配只读库，不碰外部系统
        write_scope=frozenset({"artifact"}),
        max_risk="M",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        res = self.skills.invoke(SKILL_MATCH, {
            "tenant_id": ctx.inputs.get("tenant_id"),
            "case_id": ctx.inputs.get("case_id"),
            "attempt": ctx.attempt,
            "tolerance": ctx.inputs.get("tolerance"),
        }, extras=extras_of(self, ctx))
        if res.status != "ok" or not isinstance(res.output, dict):
            return AgentOutput(status="failed", error=failed(res, SKILL_MATCH))

        out = res.output
        findings = out["findings"]
        matched = out["matched"]

        summary = (
            f"三单匹配{'通过' if matched else '未通过'}："
            f"{out['line_count']} 行，跑了 {len(out['checked'])} 条判据"
            + (f"，应付 {out['payable_amount']}"
               if matched else f"，拒付理由 {len(findings)} 条")
            + f"；容差 数量={out['tolerance']['quantity']} "
              f"单价={out['tolerance']['unit_price']} 税额={out['tolerance']['tax']}"
              f"；biz_status={out['biz_status']}"
        )

        return AgentOutput(
            status="ok",                      # ← 见模块 docstring：结论 ≠ 失败
            open_questions=self._open_questions(findings),
            artifacts=[artifact(KIND_MATCH_RESULT, {
                "matched": matched,
                "payable_amount": out["payable_amount"],
                "findings": findings,
                "checked": out["checked"],
                "tolerance": out["tolerance"],
                "biz_status": out["biz_status"],
                "invocation_id": out["invocation_id"],
            }, summary=summary)],
            metrics={"matched": matched, "findings": len(findings),
                     "checked": len(out["checked"]), "is_rework": ctx.is_rework},
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _open_questions(findings: list[dict]) -> list[str]:
        """把拒付理由翻成给人看的话。**每条都带规则编号与出处**。

        编号与原文照抄 skill 产出的那份，不在这里重新映射一遍 —— 重映射就有了
        第二套措辞，而两套措辞迟早对同一条规则说出不同的话，且没有症状。
        """
        blocking = [f for f in findings if f.get("severity") == "block"]
        if not blocking:
            return []
        shown = blocking[:MAX_SHOWN_FINDINGS]
        out = [f"[{f['rule_id']}] {f['message']}（规范原文：{f['text']}；出处 {f['source']}）"
               for f in shown]
        if len(blocking) > len(shown):
            out.append(f"另有 {len(blocking) - len(shown)} 条拒付理由未列出，"
                       f"完整清单在 match_result 产物与 match_result 表里")
        return out
