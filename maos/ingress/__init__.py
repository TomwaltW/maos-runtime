"""渠道接入层 —— 系统的**最前面**：外部 IM 里的一句话进来，一次真实处置出去。

已有的入口都要求「会用命令行」：`run.py` 跑场景、`scripts/run_case.py` 喂 JSON、
`scripts/run_requests.py` 读 CSV。这一层回答的是另一个问题：**业务方在自己每天
待的那个群里，能不能直接把活派进来、并在那里把审批点掉**。

## 三件事，一件都不能少

  · **入站**（群消息 -> 起单）：`/refund ORD-2026-0001 质量问题 6800` -> 合成 case
    -> `custom_case.run_payload()` 跑一次真实处置。合成走 `scripts/run_requests.py`
    的 `build_case()`，**不另抄一份**：两套口径迟早分叉，症状是「同一个订单，
    群里问和 CSV 跑，两个结论」，且两边都不报错。
  · **出站**（状态迁移 -> 群里）：复用 `hiclaw/matrix_bus.py` 的 `summarize` /
    `redact`，镜像内容与 Matrix 房间逐字一致。
  · **审批回收**（群里一行命令 -> `HumanApprovalQueue.decide()`）：直接复用
    `RoomApprovalBridge`，连「先认命令词、再查名单、最后校参数」那三步顺序
    一起复用。审批是不可逆动作，这套判定序在 Matrix 上已经跑绿，不重写。

## 与 hiclaw/ 的分工

`hiclaw/` 是 Matrix 一家的对接；本层是**平台无关**的那一圈：把「签名怎么校、
回调怎么解包、消息怎么发」收敛进各自的 adapter，其余全部共用。Matrix 没有被
搬进来 —— 它的 `MirrorChannel` 已经是同一个形状，两边在 `router.py` 汇合。

## 这一层不做什么

  · 不造新事件类型、不加新状态（铁律 1/9）。它产出的是一次 `run_payload()`，
    事件与状态迁移仍由控制面自己发。
  · 不持有权威事实（铁律 8）。IM 里收到的是「某人说他要退款」这个**观察**，
    不是「这笔单该退」。回帖措辞跟着这条走：只说受理与裁定，不说「已退款」——
    钱到没到账只有 `payment.observe` 说了算。
"""

from maos.ingress.contracts import (          # noqa: F401
    CHANNELS,
    ChannelAdapter,
    ChannelDepMissing,
    ChannelNotConfigured,
    InboundMessage,
    OutboundMessage,
    VerifyError,
    WebhookRequest,
)
