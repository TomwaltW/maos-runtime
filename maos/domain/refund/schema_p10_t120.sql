-- T120 结果验证层 —— 三张**新增**表，一张现有表都不碰（铁律 1）。
-- 由 outcome.py::ensure_outcome_schema(store) 读本文件建表，
-- **不并进 schema.sql 主文件、不改 objects.ensure_schema**（跨轨契约 §B 第 1 条）。
--
-- 为什么单开这一层：`refund_case.biz_status` 说的是「MAOS 这边把这单推到哪一步了」，
-- 而评委问的是另一件事 ——「这单业务到底成没成」。两者不是同一个判断：
-- 所有 Agent 都回复完成、Plan 走到 DONE、biz_status 一路推到底，业务照样可以没成
-- （钱没到账、客户不认、有人在投诉）。所以结果判据必须有自己的落点，
-- 不能挂在 biz_status 上当它的第八个取值（那还会顺手违反铁律 9）。
--
-- 三张表分别是四判据的**结论**（case_outcome）、其中一条判据的**原始事实**
-- （complaint），以及失败实例的**聚合**（failure_hint_index）。
--
-- 方言限制照契约 §B 第 3 条：只用 TEXT / INTEGER / REAL / NOT NULL /
-- DEFAULT '<字面量>' / PRIMARY KEY / CHECK / CREATE INDEX IF NOT EXISTS。
-- 没有 AUTOINCREMENT、没有函数默认值 —— 时间戳一律 Python 侧
-- `datetime.now(timezone.utc).isoformat()` 传进来（沿用 `objects._now()`）。
-- 每张表第一列都是 tenant_id 且主键以它打头（契约 §B 第 4 条）。

-- ---------------------------------------------------------------- 四判据结论
-- 一个 case 一行，**每次重算就地覆盖**（`ON CONFLICT ... DO UPDATE`，见 outcome.py）。
-- 存的是「按当前库里的观察能推出什么」，不是外部系统的当前值（铁律 8）——
-- 所以有 computed_at，且 arrival_basis 必须指回那一行观察，可回查。
--
-- 取值域写进 CHECK 而不是只写在注释里：写错枚举的行查得出来但归不了类，
-- 而错误发生在写入侧、暴露在几周后的核验侧（同 kb/schema.sql 的理由）。
--
-- **arrival 只由 payment_observation 的行决定**（铁律 8）。这里没有任何一列
-- 记录 biz_status，正是为了让「从 biz_status 反推到账」这条路在表结构上就走不通：
-- 想反推的人得先往这张表加一列，那一刻就会被 review 看见。
--
-- evidence_complete / business_success 用 INTEGER 存 0/1：SQLite 没有 BOOLEAN，
-- 而契约 §B 的允许子集里也没有它。读出来一律 `bool(row[...])`。
CREATE TABLE IF NOT EXISTS case_outcome (
    tenant_id             TEXT NOT NULL,
    case_id               TEXT NOT NULL,
    arrival               TEXT NOT NULL DEFAULT 'unknown',
    arrival_basis         TEXT NOT NULL DEFAULT '',
    customer_confirmation TEXT NOT NULL DEFAULT 'none',
    manual_correction     TEXT NOT NULL DEFAULT 'none',
    complaint             TEXT NOT NULL DEFAULT 'none',
    evidence_complete     INTEGER NOT NULL DEFAULT 0,
    business_success      INTEGER NOT NULL DEFAULT 0,
    computed_at           TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id),
    CHECK (arrival IN ('settled', 'unsettled', 'unknown')),
    CHECK (customer_confirmation IN ('confirmed', 'disputed', 'none')),
    CHECK (manual_correction IN ('none', 'overridden', 'compensated')),
    CHECK (complaint IN ('none', 'open', 'closed')),
    CHECK (evidence_complete IN (0, 1)),
    CHECK (business_success IN (0, 1))
);

-- ------------------------------------------------------------------ 投诉原始事实
-- 「投诉」是四判据里唯一一条**全仓零命中**的（phase-10.md §1 缺口 8）：
-- 在此之前没有任何一张表存得下「客户投诉了这一单」，于是那条判据只能是空话。
--
-- 存 content_digest 而不是原文：投诉正文是客户个人信息的高发区，证据束要脱敏，
-- 而判据只需要「有没有、开没开着」。原文归 IM / 工单系统，那才是它的权威所在。
--
-- closed_at 可空 = 还开着。判 open 用 `closed_at IS NULL` 而不是另加一个 status 列：
-- 两个字段说同一件事就会有对不上的那一天，而症状是「已关闭的投诉仍然一票否决」。
CREATE TABLE IF NOT EXISTS complaint (
    tenant_id      TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    channel        TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    opened_at      TEXT NOT NULL,
    closed_at      TEXT,
    resolution     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, case_id, channel, content_digest)
);

-- -------------------------------------------------------------- 失败实例聚合
-- 评委原话的落点：「失败实例则用于提示**哪类渠道、支付返回或政策组合**需要额外步骤」。
-- 键就是那句话里的三个词：渠道 × 网关返回码 × 政策规则号（契约 §G）。
--
-- 它与 kb_doc(failure_hint) 是两件事，不是一份数据的两处副本：
-- 前者是**聚合**（这类组合一共栽过几次、该补哪几步），后者是**单条实例**
-- （那一单当时发生了什么）。Planner 要的是前者 —— 检索单条实例只会拿到
-- 「上一单是怎么栽的」，而不是「这类组合普遍缺哪一步」。T119 在 Wave B 消费本表。
--
-- extra_steps 存 JSON 数组文本：SQLite 没有数组类型，而契约 §B 不许用函数/扩展。
-- count 是这个组合累计命中的次数，`bump_failure_hint()` 每次 +1。
CREATE TABLE IF NOT EXISTS failure_hint_index (
    tenant_id    TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    gateway_code TEXT NOT NULL,
    rule_no      TEXT NOT NULL,
    extra_steps  TEXT NOT NULL DEFAULT '[]',
    count        INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (tenant_id, channel_id, gateway_code, rule_no)
);

CREATE INDEX IF NOT EXISTS idx_case_outcome_success ON case_outcome(tenant_id, business_success);
CREATE INDEX IF NOT EXISTS idx_complaint_open       ON complaint(tenant_id, case_id, closed_at);
CREATE INDEX IF NOT EXISTS idx_failure_hint_channel ON failure_hint_index(tenant_id, channel_id);
