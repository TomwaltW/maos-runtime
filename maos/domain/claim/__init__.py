"""保险理赔域。

对外只有三个入口：
  - `schema.sql`  业务对象层的 12 张新增表 + 1 张迁移记账表（由 objects.ensure_schema 建）
  - `guard.py`    paid guard —— claim_case 的唯一写入路径，权威终态边界
  - `objects.py`  保单快照 / 条款版本锁定 / 业务引用的读写口径

理赔流程的业务状态是 `claim_case.biz_status`，**不是 Task 状态**：
`maos/contracts/states.py` 在本域一个新状态、一条新迁移都没有加（铁律 9）。

    submitted -> adjudicated -> payment_requested -> paid
    分支：rejected（不予赔付）/ compensated（赔付走不通后的补偿收口）

`paid` 全系统只有 `claim.observe` 写得进去（铁律 8）—— 赔款到没到账，权威在赔付方。
"""
