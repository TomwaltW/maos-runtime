"""制造企业售后退款域。

对外只有三个入口：
  - `schema.sql`  业务对象层的 14 张新增表（由 objects.ensure_schema 建）
  - `guard.py`    settled guard —— refund_case 的唯一写入路径
  - `objects.py`  快照/政策/引用的读写口径

退款流程的业务状态是 `refund_case.biz_status`，**不是 Task 状态**：
`maos/contracts/states.py` 在本域一个新状态、一条新迁移都没有加（铁律 9）。
"""
