"""业务域层 —— 每个域一个子包，只放业务对象与该域的守卫。

内核（contracts/ runtime/ core/）对本目录零依赖：换域只换这里，
`maos/contracts/**` 与 `maos/runtime/**` 一行不改（铁律 9）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .ap import guard as ap_guard
from .claim import guard as claim_guard
from .investigation import guard as investigation_guard
from .refund import guard as refund_guard


@dataclass(frozen=True)
class DomainSpec:
    """证据导出与核验共用的业务表映射；权威口径直接引用各域 guard。"""

    name: str
    case_table: str
    case_id_column: str
    observation_table: str
    observation_fields: tuple[str, ...]
    authoritative_writer: str
    authoritative_states: frozenset[str]
    receipt_states: Mapping[str, frozenset[str]]
    terminal_states: frozenset[str]
    required_fields: tuple[str, ...]
    authoritative_evidence: Mapping[str, Any] | None = None

    def observation_error(self, status: str, observation: Mapping[str, Any]) -> str:
        """返回违反域守卫的回执判据；空串表示这张回执确实支持该权威终态。"""
        allowed = self.receipt_states.get(status)
        if not allowed or observation.get("observed_state") not in allowed:
            return f"observed_state={observation.get('observed_state')!r} 不支持 {status}"
        missing = [key for key in self.required_fields if not observation.get(key)]
        if missing:
            return f"权威回执缺字段 {missing}"
        if self.authoritative_evidence is not None:
            evidence = self.authoritative_evidence.get(status)
            if evidence is None:
                return f"{status} 没有配置权威证据判据"
            family = investigation_guard.message_family(str(observation.get("message_type") or ""))
            if family != evidence.message_family:
                return f"{status} 只认 {evidence.message_family}，观察来自 {family!r}"
            if evidence.requires_amount and observation.get("returned_amount") in (None, ""):
                return "权威观察缺 returned_amount"
            if evidence.requires_code and not str(observation.get(evidence.requires_code) or "").strip():
                return f"权威观察缺 {evidence.requires_code}"
        return ""


def _spec(name: str, guard: Any, case_table: str, case_id_column: str,
          observation_table: str, observation_fields: tuple[str, ...]) -> DomainSpec:
    evidence = getattr(guard, "AUTHORITATIVE_EVIDENCE", None)
    receipt_states = ({state: rule.observed_states for state, rule in evidence.items()}
                      if evidence is not None else guard.AUTHORITATIVE_RECEIPT_STATE)
    return DomainSpec(
        name=name, case_table=case_table, case_id_column=case_id_column,
        observation_table=observation_table, observation_fields=observation_fields,
        authoritative_writer=guard.AUTHORITATIVE_WRITER,
        authoritative_states=guard.AUTHORITATIVE_STATES, receipt_states=receipt_states,
        terminal_states=frozenset(state for state, outgoing in guard.BIZ_STATUS_FLOW.items() if not outgoing),
        required_fields=guard._OBSERVATION_REQUIRED, authoritative_evidence=evidence,
    )


DOMAIN_REGISTRY: dict[str, DomainSpec] = {
    "refund": _spec("refund", refund_guard, "refund_case", "case_id", "payment_observation",
                    ("request_id", "gateway_code", "observed_state", "actor_invocation_id")),
    "claim": _spec("claim", claim_guard, "claim_case", "claim_id", "claim_payment_observation",
                   ("request_id", "carc_code", "group_code", "observed_state", "actor_invocation_id")),
    "investigation": _spec("investigation", investigation_guard, "investigation_case", "case_id",
                           "resolution_observation", ("request_id", "poll_seq", "message_type",
                           "confirmation_code", "rejection_code", "return_reason_code", "returned_amount",
                           "observed_state", "actor_invocation_id")),
    "ap": _spec("ap", ap_guard, "ap_case", "case_id", "ap_payment_observation",
                ("instruction_id", "observed_state", "bank_reference", "value_date", "actor_invocation_id")),
}
