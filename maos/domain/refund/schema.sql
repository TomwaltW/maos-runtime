-- 退款业务对象层 —— 14 张业务表 + 1 张迁移记账表，
-- 由 objects.py::ensure_schema(store) 读本文件建表。
--
-- 三条硬约束（派单 R-1 步骤 1，改这个文件前先读）：
--   1. 全部是**新增**表。maos/core/store.py 的现有表结构一字不改，本文件不碰它们。
--   2. 租户 / 渠道 / 版本是**主键的一部分**，不是配置项 —— 除 business_ref 外
--      所有表以 tenant_id 打头，跨租户读不到彼此的数据靠主键前缀而不是 WHERE 约定。
--   3. 权威事实归外部（铁律 8）。order_snapshot / product_snapshot / policy_rule
--      存的是「MAOS 执行前读到的那一版」，不是外部系统的当前值；read_at 记下读的时刻。
--
-- refund_case.biz_status 是**业务对象自己的字段**，不是 Task 状态（铁律 9）。
-- 主干三段 + 两个分支：
--   submitted -> approved -> gateway_accepted -> processing -> settled
--   分支：rejected（审批否决）/ compensated（失败后补偿收口）
-- settled 只能由 payment.observe 写入，见 guard.py。
--
-- 本文件只描述**目标形状**，它自己搬不动老库：整份都是 `IF NOT EXISTS`，
-- 表已存在就整段跳过，于是**改列静默无效** —— 加表可以（新表直接生效），改列不行：
-- 往已建好的表加一列，`IF NOT EXISTS` 直接跳过，跑起来一切正常，直到某条 INSERT
-- 报 no such column（BACKLOG `## task-R1` 第 5 条、`## task-T17` 第 2 条）。
--
-- 把老库搬到目标形状的是 `objects.py` 里的 `_MIGRATIONS`，记账落在下面的
-- `refund_schema_version` 表。**改列要动两处**：这里写进目标形状，那边补一条
-- 迁移步骤。只改这里的后果不是报错，是老库永远升不上来 —— 演示期的库都是
-- `:memory:` 或每次新建，所以不咬人；PolarDB 是持久库，上线第一天就咬，
-- 而且咬得没有声音。

-- 迁移记账表。**一条已应用的迁移一行**，不是「一行存当前版本」：
-- 前者的 INSERT 天然幂等（版本号是主键，重复插会响），后者是读-改-写，
-- 两个进程同时升级会互相盖掉。当前版本 = MAX(version)，一行都没有就是 0。
--
-- 本表**不能**用来判断「这库是新的还是老的」—— 新库刚建完它同样是空的。
-- 判据在每条迁移步骤自己的探针里（该干的事干没干），版本号只是
-- 「不必再探一遍」的快路径。顺序上它必须排在数据表前面：迁移读它决定跑什么。
--
-- 它是这 15 张表里唯一一张**不带 tenant_id** 的：schema 版本是库级事实，
-- 不是租户级事实。同一个库上跑的所有租户共享一份表结构。
CREATE TABLE IF NOT EXISTS refund_schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (version)
);

-- ---------------------------------------------------------------- 租户与渠道
CREATE TABLE IF NOT EXISTS tenant (
    tenant_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    region      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id)
);

CREATE TABLE IF NOT EXISTS channel (
    tenant_id   TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, channel_id)
);

-- ------------------------------------------------ 外部系统快照（只读，带版本）
CREATE TABLE IF NOT EXISTS order_snapshot (
    tenant_id               TEXT NOT NULL,
    order_id                TEXT NOT NULL,
    version                 INTEGER NOT NULL,
    sku                     TEXT NOT NULL,
    amount_paid             REAL NOT NULL,
    paid_at                 TEXT NOT NULL,
    channel_id              TEXT NOT NULL,
    policy_version_at_order INTEGER NOT NULL,
    payload_json            TEXT NOT NULL DEFAULT '{}',
    read_at                 TEXT NOT NULL,
    PRIMARY KEY (tenant_id, order_id, version)
);

CREATE TABLE IF NOT EXISTS product_snapshot (
    tenant_id       TEXT NOT NULL,
    sku             TEXT NOT NULL,
    version         INTEGER NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT '',
    warranty_months INTEGER NOT NULL DEFAULT 0,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, sku, version)
);

-- ------------------------------------------------------ 政策（版本 + 生效范围）
CREATE TABLE IF NOT EXISTS policy_rule (
    tenant_id      TEXT NOT NULL,
    rule_no        TEXT NOT NULL,
    version        INTEGER NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    body           TEXT NOT NULL DEFAULT '',
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    channel_scope  TEXT NOT NULL DEFAULT '*',
    sku_scope      TEXT NOT NULL DEFAULT '*',
    PRIMARY KEY (tenant_id, rule_no, version)
);

-- ------------------------------------------------------------------ 案例主体
CREATE TABLE IF NOT EXISTS refund_case (
    tenant_id      TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    channel_id     TEXT NOT NULL,
    order_id       TEXT NOT NULL,
    order_version  INTEGER NOT NULL,
    sku            TEXT NOT NULL,
    reason_code    TEXT NOT NULL,
    amount_claimed REAL NOT NULL,
    biz_status     TEXT NOT NULL,
    plan_id        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id),
    CHECK (biz_status IN ('submitted', 'approved', 'gateway_accepted',
                          'processing', 'settled', 'rejected', 'compensated'))
);

CREATE TABLE IF NOT EXISTS customer_evidence (
    tenant_id    TEXT NOT NULL,
    case_id      TEXT NOT NULL,
    evidence_id  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    uri          TEXT NOT NULL,
    digest       TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS approval_record (
    tenant_id  TEXT NOT NULL,
    case_id    TEXT NOT NULL,
    approver   TEXT NOT NULL,
    decision   TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, approver, decided_at),
    CHECK (decision IN ('approved', 'rejected'))
);

CREATE TABLE IF NOT EXISTS finance_entry (
    tenant_id       TEXT NOT NULL,
    case_id         TEXT NOT NULL,
    amount_approved REAL NOT NULL,
    breakdown_json  TEXT NOT NULL DEFAULT '{}',
    rule_refs       TEXT NOT NULL DEFAULT '[]',
    checked_by      TEXT NOT NULL,
    checked_at      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id)
);

CREATE TABLE IF NOT EXISTS refund_request (
    tenant_id       TEXT NOT NULL,
    case_id         TEXT NOT NULL,
    request_id      TEXT NOT NULL,
    amount          REAL NOT NULL,
    gateway         TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    submitted_at    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, request_id),
    UNIQUE (tenant_id, idempotency_key)
);

-- 权威回执落点：settled 必须与这里的一行同事务写入，见 guard.py
CREATE TABLE IF NOT EXISTS payment_observation (
    tenant_id           TEXT NOT NULL,
    case_id             TEXT NOT NULL,
    request_id          TEXT NOT NULL,
    gateway_code        TEXT NOT NULL,
    raw_receipt_json    TEXT NOT NULL DEFAULT '{}',
    observed_state      TEXT NOT NULL,
    observed_at         TEXT NOT NULL,
    actor_invocation_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, request_id, observed_at)
);

CREATE TABLE IF NOT EXISTS notification (
    tenant_id      TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    channel        TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    sent_at        TEXT NOT NULL,
    ack_at         TEXT,
    PRIMARY KEY (tenant_id, case_id, channel, content_digest)
);

CREATE TABLE IF NOT EXISTS compensation_record (
    tenant_id   TEXT NOT NULL,
    case_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    executed_at TEXT NOT NULL,
    operator    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, kind, executed_at)
);

-- -------------------------------- DAG/Task/Artifact -> 业务对象（只存引用）
CREATE TABLE IF NOT EXISTS business_ref (
    plan_id        TEXT NOT NULL,
    task_id        TEXT NOT NULL,
    tenant_id      TEXT NOT NULL,
    object_type    TEXT NOT NULL,
    object_id      TEXT NOT NULL,
    object_version INTEGER NOT NULL DEFAULT 0,
    purpose        TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    PRIMARY KEY (plan_id, task_id, tenant_id, object_type, object_id, object_version, purpose)
);

CREATE INDEX IF NOT EXISTS idx_refund_case_plan   ON refund_case(plan_id);
CREATE INDEX IF NOT EXISTS idx_business_ref_obj   ON business_ref(tenant_id, object_type, object_id);
CREATE INDEX IF NOT EXISTS idx_pay_obs_case       ON payment_observation(tenant_id, case_id);
CREATE INDEX IF NOT EXISTS idx_policy_rule_no     ON policy_rule(tenant_id, rule_no);
