-- 保险理赔业务对象层 —— 12 张业务表 + 1 张迁移记账表，
-- 由 objects.py::ensure_schema(store) 读本文件建表。
--
-- 三条硬约束（口径照抄退款域 maos/domain/refund/schema.sql，不另立一套）：
--   1. 全部是**新增**表。maos/core/store.py 的现有表结构一字不改，本文件不碰它们；
--      退款域那 15 张表同样不碰 —— 两个域各自建表，不共用、不互相 JOIN。
--   2. 租户 / 赔付方 / 版本是**主键的一部分**，不是配置项 —— 除 claim_business_ref 外
--      所有表以 tenant_id 打头，跨租户读不到彼此的数据靠主键前缀而不是 WHERE 约定。
--   3. 权威事实归外部（铁律 8）。policy_contract / policy_terms / payer 存的是
--      「MAOS 执行前读到的那一版」，不是外部系统的当前值；read_at 记下读的时刻。
--      赔付到没到账更是如此：权威在赔付方，本库只存 claim_payment_observation
--      这种**观察记录**。
--
-- claim_case.biz_status 是**业务对象自己的字段**，不是 Task 状态（铁律 9）。
-- maos/contracts/states.py 在本域一个新状态、一条新迁移都没有加。
-- 主干三段 + 两个分支：
--   submitted -> adjudicated -> payment_requested -> paid
--   分支：rejected（裁定不予赔付 / 审批否决）/ compensated（赔付走不通后的补偿收口）
-- paid 只能由 claim.observe 写入，见 guard.py。
--
-- 本文件只描述**目标形状**，它自己搬不动老库：整份都是 `IF NOT EXISTS`，
-- 表已存在就整段跳过，于是**改列静默无效** —— 加表可以（新表直接生效），改列不行：
-- 往已建好的表加一列，`IF NOT EXISTS` 直接跳过，跑起来一切正常，直到某条 INSERT
-- 报 no such column。把老库搬到目标形状的是 objects.py 里的 `_MIGRATIONS`，
-- 记账落在下面的 claim_schema_version 表。**改列要动两处。**

-- 迁移记账表。**一条已应用的迁移一行**，不是「一行存当前版本」：
-- 前者的 INSERT 天然幂等（版本号是主键，重复插会响），后者是读-改-写，
-- 两个进程同时升级会互相盖掉。当前版本 = MAX(version)，一行都没有就是 0。
--
-- 本表**不能**用来判断「这库是新的还是老的」—— 新库刚建完它同样是空的。
-- 判据在每条迁移步骤自己的探针里，版本号只是「不必再探一遍」的快路径。
-- 顺序上它必须排在数据表前面：迁移读它决定跑什么。
--
-- 它是这 13 张表里唯一一张**不带 tenant_id** 的：schema 版本是库级事实。
CREATE TABLE IF NOT EXISTS claim_schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (version)
);

-- ------------------------------------------------------------------ 赔付方
-- 权威终态的持有者。MAOS 只存它的名字与联络口径，**不存它的账**。
CREATE TABLE IF NOT EXISTS payer (
    tenant_id TEXT NOT NULL,
    payer_id  TEXT NOT NULL,
    kind      TEXT NOT NULL,            -- insurer / reinsurer / tpa
    name      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, payer_id)
);

-- ------------------------------------------ 保单快照（只读，带版本与条款锁定）
-- `terms_version_at_bind` 是本域的招牌字段：**投保当时**锁定的条款版本号。
-- 人家 2023 年投的保，不能拿 2025 年的条款判 —— 权威在这一列上，
-- 不在 policy_terms 表的 max(version) 上。与退款域
-- `order_snapshot.policy_version_at_order` 是同构物。
CREATE TABLE IF NOT EXISTS policy_contract (
    tenant_id            TEXT NOT NULL,
    policy_no            TEXT NOT NULL,
    version              INTEGER NOT NULL,
    product_code         TEXT NOT NULL,
    insured_id           TEXT NOT NULL DEFAULT '',
    sum_insured          REAL NOT NULL,
    deductible           REAL NOT NULL DEFAULT 0,
    coinsurance_rate     REAL NOT NULL DEFAULT 0,
    bound_at             TEXT NOT NULL,   -- 投保时刻，条款版本按它锁定
    terms_version_at_bind INTEGER NOT NULL,
    payer_id             TEXT NOT NULL,
    payload_json         TEXT NOT NULL DEFAULT '{}',
    read_at              TEXT NOT NULL,
    PRIMARY KEY (tenant_id, policy_no, version)
);

-- ------------------------------------------------------ 条款（版本 + 生效范围）
CREATE TABLE IF NOT EXISTS policy_terms (
    tenant_id      TEXT NOT NULL,
    rule_no        TEXT NOT NULL,        -- 条款编号，如 CL-01
    version        INTEGER NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    body           TEXT NOT NULL DEFAULT '',   -- JSON 参数；人写的自然语言条款则留空对象
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    product_scope  TEXT NOT NULL DEFAULT '*',
    loss_scope     TEXT NOT NULL DEFAULT '*',  -- 出险类型范围
    PRIMARY KEY (tenant_id, rule_no, version)
);

-- ------------------------------------------------------------------ 案件主体
CREATE TABLE IF NOT EXISTS claim_case (
    tenant_id      TEXT NOT NULL,
    claim_id       TEXT NOT NULL,
    payer_id       TEXT NOT NULL,
    policy_no      TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    loss_type      TEXT NOT NULL,
    incident_at    TEXT NOT NULL,        -- 出险时刻
    reported_at    TEXT NOT NULL,        -- **报案时点** —— 条款版本按保单锁定，不按今天
    amount_claimed REAL NOT NULL,
    biz_status     TEXT NOT NULL,
    plan_id        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, claim_id),
    CHECK (biz_status IN ('submitted', 'adjudicated', 'payment_requested',
                          'paid', 'rejected', 'compensated'))
);

-- 赔付明细行。一个案子可以有多项（医疗费 / 误工 / 施救），逐项裁定逐项核算。
CREATE TABLE IF NOT EXISTS claim_line (
    tenant_id      TEXT NOT NULL,
    claim_id       TEXT NOT NULL,
    line_no        INTEGER NOT NULL,
    item_code      TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    amount_claimed REAL NOT NULL,
    amount_allowed REAL NOT NULL DEFAULT 0,
    carc_code      TEXT NOT NULL DEFAULT '',   -- 被削减/拒付时挂的 X12 CARC
    group_code     TEXT NOT NULL DEFAULT '',   -- 该调整由谁承担（CO/PR/OA/PI）
    PRIMARY KEY (tenant_id, claim_id, line_no)
);

CREATE TABLE IF NOT EXISTS claim_evidence (
    tenant_id    TEXT NOT NULL,
    claim_id     TEXT NOT NULL,
    evidence_id  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    uri          TEXT NOT NULL,
    digest       TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, claim_id, evidence_id)
);

-- 裁定产物。**rule_no + terms_version 是本表存在的全部理由**：
-- 「按哪一条、哪一版判的」必须能被 verify.py 那类重放校验直接读到，
-- 埋在 breakdown_json 里就得靠解析字符串才查得到，而那不是可核对的字段。
CREATE TABLE IF NOT EXISTS adjudication (
    tenant_id      TEXT NOT NULL,
    claim_id       TEXT NOT NULL,
    rule_no        TEXT NOT NULL,
    terms_version  INTEGER NOT NULL,
    decision       TEXT NOT NULL,
    allowed_amount REAL NOT NULL DEFAULT 0,
    rule_refs      TEXT NOT NULL DEFAULT '[]',  -- ["CL-01@v1", ...]
    breakdown_json TEXT NOT NULL DEFAULT '{}',
    adjudicated_by TEXT NOT NULL,
    adjudicated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, claim_id, rule_no, terms_version),
    CHECK (decision IN ('approve', 'reject'))
);

CREATE TABLE IF NOT EXISTS claim_approval (
    tenant_id  TEXT NOT NULL,
    claim_id   TEXT NOT NULL,
    approver   TEXT NOT NULL,
    decision   TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, claim_id, approver, decided_at),
    CHECK (decision IN ('approved', 'rejected'))
);

CREATE TABLE IF NOT EXISTS claim_payment_request (
    tenant_id       TEXT NOT NULL,
    claim_id        TEXT NOT NULL,
    request_id      TEXT NOT NULL,
    amount          REAL NOT NULL,
    payer_id        TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    submitted_at    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, claim_id, request_id),
    -- 一个案子只允许有一笔在途赔付指令。重跑、重试、重复投递都撞同一个键。
    UNIQUE (tenant_id, idempotency_key)
);

-- 权威回执落点：paid 必须与这里的一行同事务写入，见 guard.py。
-- carc_code / group_code / remark_codes 留的是**赔付方拒付或调整时说的话**，
-- 到账时它们为空 —— 空不代表没查，代表赔付方没有可说的调整。
CREATE TABLE IF NOT EXISTS claim_payment_observation (
    tenant_id           TEXT NOT NULL,
    claim_id            TEXT NOT NULL,
    request_id          TEXT NOT NULL,
    carc_code           TEXT NOT NULL DEFAULT '',
    group_code          TEXT NOT NULL DEFAULT '',
    remark_codes        TEXT NOT NULL DEFAULT '[]',
    raw_receipt_json    TEXT NOT NULL DEFAULT '{}',
    observed_state      TEXT NOT NULL,
    observed_at         TEXT NOT NULL,
    actor_invocation_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, claim_id, request_id, observed_at)
);

CREATE TABLE IF NOT EXISTS claim_compensation (
    tenant_id   TEXT NOT NULL,
    claim_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    executed_at TEXT NOT NULL,
    operator    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, claim_id, kind, executed_at)
);

-- -------------------------------- DAG/Task/Artifact -> 业务对象（只存引用）
-- **另起一张表而不是借用退款域的 business_ref**：那张表长在
-- maos/domain/refund/schema.sql 里，是退款域的资产。借用它就是给本域接一条跨域依赖 ——
-- 退款域改一次表结构、或某天被单独裁掉，理赔域跟着塌，而「换域只换 Skill /
-- ToolPort / 业务对象」这句话当场不成立。形状刻意保持一致，好让两个域的
-- verify 脚本用同一套读法。
CREATE TABLE IF NOT EXISTS claim_business_ref (
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

CREATE INDEX IF NOT EXISTS idx_claim_case_plan    ON claim_case(plan_id);
CREATE INDEX IF NOT EXISTS idx_claim_ref_obj      ON claim_business_ref(tenant_id, object_type, object_id);
CREATE INDEX IF NOT EXISTS idx_claim_obs_case     ON claim_payment_observation(tenant_id, claim_id);
CREATE INDEX IF NOT EXISTS idx_policy_terms_no    ON policy_terms(tenant_id, rule_no);
