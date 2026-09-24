-- 客服前台（cs）会话对象 —— 3 张业务表 + 1 张迁移记账表，
-- 由 objects.py::ensure_schema(store) 读本文件建表。形状照跨轨契约
-- review/p12-cs-contracts.md §1.3 逐列抄写：列名、类型、缺省、主键一个字不改。
--
-- 硬约束（改这个文件前先读）：
--   1. 全部是**新增**表，全部 cs_ 前缀。maos/core/store.py 的现有表一字不改（铁律 1）。
--   2. 业务表以 tenant_id 打头并进主键：跨租户读不到彼此靠主键前缀，不靠 WHERE 约定。
--      租户映射不到就是空串 ''（不猜默认租户），空串照样是合法的主键前缀。
--   3. stage / delivery 是**会话对象与卡片自己的字段**，不是 Task 状态（铁律 9）；
--      stage 的合法迁移在 maos/domain/cs/types.py 的 STAGE_FLOW。
--   4. 只用 _dbport.to_pg_ddl 翻得动的子集：TEXT / INTEGER、字面量 DEFAULT、
--      CHECK (...)、PRIMARY KEY (...)、CREATE INDEX IF NOT EXISTS。
--      时间戳一律由 Python 侧传入，**不用** datetime('now') —— 两个后端各填各的时刻。
--   5. CHECK 的取值与 types.py 的 STAGES / ROUTES / DELIVERIES / HANDOFF_REASONS 逐个对齐，
--      由 maos/tests/test_cs_schema_t167.py 按枚举全集逐值钉住（多一个少一个都红）。
--
-- 本文件只描述**目标形状**：整份 IF NOT EXISTS，对已存在的表改不动一列。
-- 改列要动两处 —— 这里写目标形状，objects.py 的 _MIGRATIONS 末尾追加一步迁移。

-- 迁移记账表：一条已应用的迁移一行，当前版本 = MAX(version)，一行都没有就是 0。
-- 库级事实，不带 tenant_id（同一个库上的所有租户共享一份表结构）。
CREATE TABLE IF NOT EXISTS cs_schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- 一段会话：(租户, 渠道, 客服账号, 客户) 唯一确定，id 由 types.conversation_id_for 算。
-- open_kfid 回信必需；external_userid = InboundMessage.chat_id，是回信目标。
-- 这两列是客户身份，**只住在这里**，不进 event_log。
CREATE TABLE IF NOT EXISTS cs_conversation (
    tenant_id       TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    channel         TEXT NOT NULL,
    open_kfid       TEXT NOT NULL DEFAULT '',
    external_userid TEXT NOT NULL,
    stage           TEXT NOT NULL CHECK (stage IN ('active', 'handed_off', 'closed')),
    fallback_streak INTEGER NOT NULL DEFAULT 0,
    turn_count      INTEGER NOT NULL DEFAULT 0,
    opened_at       TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, conversation_id)
);

-- 一轮对话。客户原文与回复原文是会话对象本身（转人工卡片要带），只住在这里；
-- event_log 只落 types.text_digest 摘要。
-- handoff_reason 空串 = 本轮没转人工。
CREATE TABLE IF NOT EXISTS cs_turn (
    tenant_id       TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    turn_id         TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    msg_dedup_key   TEXT NOT NULL DEFAULT '',
    inbound_text    TEXT NOT NULL,
    reply_text      TEXT NOT NULL DEFAULT '',
    route           TEXT NOT NULL CHECK (route IN ('answer', 'fallback', 'handoff', 'silent',
                        'clarify')),
    intent          TEXT NOT NULL DEFAULT '',
    handoff_reason  TEXT NOT NULL DEFAULT '' CHECK (handoff_reason IN ('', 'requested',
                        'complaint', 'anger', 'compensation', 'privacy', 'needs_order_lookup',
                        'unverified_claim', 'repeated_fallback', 'tenant_unmapped',
                        'identity_unverified', 'order_unmapped', 'lookup_failed',
                        'refund_request')),
    draft_json      TEXT NOT NULL DEFAULT '{}',
    check_json      TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, turn_id)
);
CREATE INDEX IF NOT EXISTS idx_cs_turn_conv_seq ON cs_turn (tenant_id, conversation_id, seq);

-- 转人工卡片。handoff_id 与 turn_id 同值：一轮至多一张卡。
-- delivery 是卡片自己的投递状态（不是 Task 状态），delivered_to 是投到的 渠道:chat_id。
CREATE TABLE IF NOT EXISTS cs_handoff (
    tenant_id       TEXT NOT NULL,
    handoff_id      TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    turn_id         TEXT NOT NULL,
    reason          TEXT NOT NULL CHECK (reason IN ('requested', 'complaint', 'anger',
                        'compensation', 'privacy', 'needs_order_lookup', 'unverified_claim',
                        'repeated_fallback', 'tenant_unmapped', 'identity_unverified',
                        'order_unmapped', 'lookup_failed', 'refund_request')),
    card_json       TEXT NOT NULL,
    delivery        TEXT NOT NULL CHECK (delivery IN ('pending', 'delivered', 'failed',
                        'unconfigured')),
    delivered_to    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, handoff_id)
);
