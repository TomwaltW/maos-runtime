-- =====================================================================
-- T117 · 补偿工单的生命周期列（p10 跨轨契约 §B）
-- =====================================================================
--
-- 加在 `compensation_record`（schema.sql:186）上。那张表原先只记「补偿这件事
-- 发生过」，没有承接人、没有关闭时刻、没有「人工线下处理完之后钱到底退没退」
-- 的回填口 —— 于是工单开了就开了，评委问「流程卡住后应由谁补偿」只答得出
-- 一个 operator 字符串。这六列把工单从一行记录补成一条闭环。
--
-- ⚠️ 本片段**不许喂给 `executescript`**。
--    SQLite 的 `ALTER TABLE ADD COLUMN` 没有 `IF NOT EXISTS`，直接跑第二遍就撞
--    duplicate column name。它由 `maos/skills/builtin/refund/compensate.py::
--    ensure_ticket_schema()` 解析后逐条经 `_add_column_if_missing()` 应用（契约 §B.2），
--    那条路径自带探针，在已是目标形状的库上是 no-op。
--    这也是为什么本片段里只有 ALTER、没有 `CREATE TABLE IF NOT EXISTS`：
--    T117 一张新表都不建，只给既有那张加列。
--
-- ⚠️ `compensation_record` **T116 也要加一列** `revision`。两轨各自成段、
--    各自一个片段文件，谁都不重排既有列（契约 §A）。
--
-- 方言：SQLite，只用契约 §B.3 允许的子集 —— `TEXT` + `NOT NULL` +
-- `DEFAULT '<字面量>'`。**没有函数默认值**（`datetime('now')` 之类翻译器不认），
-- 时间戳一律由 Python 侧 `objects._now()` 传进来。
--
-- 为什么六列都 `NOT NULL DEFAULT ''` 而不是可空：老库里已经躺着的补偿记录
-- 加列之后必须有确定值，而「空字符串」在本域已经是「还没发生」的既有写法
-- （见 `compensate.py` 的 `UNOBSERVED`：占位要显式，不能靠 NULL 的三值逻辑
-- 让「还没派单」和「派给了谁但没记下来」长得一模一样）。
--
-- 列义：
--   assignee_role              承接岗（`maos/domain/refund/roles.py` 的角色名）
--   assignee                   承接账号；缺省取该岗目录里的第一个
--   opened_at                  工单开出的时刻（= 补偿执行时刻）
--   resolved_at                工单关闭的时刻；空 = 还没人处理完
--   resolution_kind            人工处理的结论：settled / not_settled
--   resolution_observation_id  回填的那条 `payment_observation` 的自然键
--                              `<request_id>@<observed_at>`；空 = 没有回填过观察。
--                              **不存 biz_status** —— 钱到没到账的权威在
--                              `payment_observation` 那一行，工单只留一个指针
--                              （铁律 8：不许把外部状态在第二处写死为终态）。

ALTER TABLE compensation_record ADD COLUMN assignee_role TEXT NOT NULL DEFAULT '';
ALTER TABLE compensation_record ADD COLUMN assignee TEXT NOT NULL DEFAULT '';
ALTER TABLE compensation_record ADD COLUMN opened_at TEXT NOT NULL DEFAULT '';
ALTER TABLE compensation_record ADD COLUMN resolved_at TEXT NOT NULL DEFAULT '';
ALTER TABLE compensation_record ADD COLUMN resolution_kind TEXT NOT NULL DEFAULT '';
ALTER TABLE compensation_record ADD COLUMN resolution_observation_id TEXT NOT NULL DEFAULT '';
