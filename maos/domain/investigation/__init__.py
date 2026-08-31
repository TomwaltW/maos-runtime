"""银行差错处理域（payment investigation / exception handling）。

对外只有四个入口：
  - `schema.sql`            业务对象层的 6 张新增表（由 objects.ensure_schema 建）
  - `guard.py`              returned guard —— investigation_case 的唯一写入路径
  - `objects.py`            原始支付快照与本域的 SQL 读写口径
  - `iso20022_codes.json`   从 ISO 20022 官方码表逐条抄来的四个 External Code Set

本域的业务状态是 `investigation_case.biz_status`，**不是 Task 状态**：
`maos/contracts/states.py` 在本域一个新状态、一条新迁移都没有加（铁律 9）。

## 码表数据文件为什么放在这里，而不是挨着 `maos/tools/investigation_codes.py`

两条理由，第二条是硬的：

1. 它是**本域的外部词汇表**，与 `schema.sql` 同性质 —— 都是「这个域和外部世界怎么
   对话」的声明，放在域包里与业务对象同处一地是对的。
2. 派单 §4 给 `maos/tools/` 的白名单是**两个具名文件**（`investigation.py` 与
   `investigation_codes.py`），不是通配；而 `maos/domain/investigation/**` 是通配。
   往 tools/ 放第三个文件会越界。（已记 docs/DECISIONS.md）

`maos/tools/investigation_codes.py` 读它、包成冻结 dataclass 再对外供judgement，
判据逻辑一行码值都不硬编。
"""
