"""退款圆桌引擎 —— **平台无关**。

五个岗位依次就一单退款发言：申请受理 → 规则审核 → 证据核验 → 风险反欺诈 → 财务执行。
事实由规则代码（`stages.py`）算，模型（`speaker.py`）只把事实说成人话，
名册与三个钩子在 `team.py`。

本包**不认识 Matrix**，也不认识任何具体房间：往哪儿说话由调用方注入一个
`VoiceSet`（`voice(agent_id).say(text)`）。所以它跑得进 `maos/tests`，
不需要任何服务在跑；换一个 IM、换成 stdout、换成 HTTP 回调都不用改这里一行。
"""

from __future__ import annotations

from maos.roundtable.speaker import SPEECH_LIMIT, SYSTEM_TMPL, Speaker
from maos.roundtable.team import (
    FALLBACK_IDENTITIES,
    TEAM_ORDER,
    TITLES,
    RefundRoundtable,
    StageReport,
    identity_of,
)

__all__ = [
    "FALLBACK_IDENTITIES",
    "SPEECH_LIMIT",
    "SYSTEM_TMPL",
    "TEAM_ORDER",
    "TITLES",
    "RefundRoundtable",
    "Speaker",
    "StageReport",
    "identity_of",
]
