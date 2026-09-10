-- T116 的 schema 片段 —— 六类业务对象的版本 / 修订列。
--
-- 跨轨契约 §B.1：**不改 `schema.sql` 主文件、不改 `objects.ensure_schema`**。
-- 每轨把自己的 DDL 放进自己的片段文件，由自己的模块在首次使用时应用。
-- 本文件由 `maos/domain/refund/case_pack.py::ensure_t116_schema()` 读入并逐条执行。
--
-- ## 为什么这里是 ALTER 而不是 CREATE
--
-- 契约 §B.1 的样板是 `CREATE TABLE IF NOT EXISTS`，那是**加表**的形态。T116 加的是
-- **列**，而这六张表已经在 `schema.sql` 里了 —— 往那份文件里加列是静默无效的
-- （整份都是 `IF NOT EXISTS`，表已存在就整段跳过，见 `schema.sql` 抬头第 17 行起）。
-- 所以片段里写 `ALTER TABLE … ADD COLUMN`，由 Python 侧逐条过
-- `_add_column_if_missing()` 的探针（契约 §B.2：SQLite 的 ADD COLUMN 没有
-- `IF NOT EXISTS`，幂等性只能由探针给）。
--
-- **目标形状仍然只有一份**：加载器不在代码里另抄一张 (表, 列, 声明) 清单，
-- 它解析本文件。抄一份的后果是哪天有人改了这里、代码里那份没跟着改，
-- 而两边都不报错 —— 正是 `schema.sql` 抬头警告的那类静默分叉。
--
-- ## 为什么一半叫 version、一半叫 revision
--
-- **version** 给「同一个对象的第 N 版事实」：一份证据被重新提交（换了更清晰的
-- 照片、补了原始文件），一笔退款请求被换渠道重发 —— 两者的 (tenant, case, id)
-- 主键不变，变的是内容，所以第 N 版与第 1 版是同一个对象的两个版本。
--
-- **revision** 给「同一件事的第 N 次做」：审批被推翻重批、财务二次核算、
-- 通知重发、补偿再执行一次。它们的每一次都是独立发生的动作（表上也确实各占一行，
-- 主键里带着 `decided_at` / `executed_at` / `content_digest`），修订号记的是
-- 「这是第几次」，不是「同一行的第几版」。
--
-- 分界不是措辞洁癖，它决定 resolve 的收窄方式：`version` 那几张进
-- `objects._VERSIONED_REF_TABLES`（`resolve_business_ref` 会 `AND version=?`），
-- `revision` 那几张不进（那句 SQL 里的列名是写死的 `version`，而
-- `resolve_business_ref` 不是本轨可动的面）。详见 `objects.py` 那两个常量处的注释
-- 与 `docs/DECISIONS.md` 的 `## task-t116`。
--
-- ## 缺省值为什么是 1 而不是 0
--
-- 老库里已有的行是**第一版**，不是第零版。缺省 0 会让「没升级过的库」与
-- 「升级了但还没改过的对象」在数据上分不开，而带版本引用的判据正是
-- `business_ref.object_version == 对象当前版本` —— 两边差一，引用当场悬空。

ALTER TABLE customer_evidence   ADD COLUMN version  INTEGER NOT NULL DEFAULT 1;
ALTER TABLE approval_record     ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE finance_entry       ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE refund_request      ADD COLUMN version  INTEGER NOT NULL DEFAULT 1;
ALTER TABLE notification        ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE compensation_record ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
