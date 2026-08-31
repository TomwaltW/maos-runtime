-- 应付账款业务对象层 —— 13 张业务表 + 1 张迁移记账表，
-- 由 objects.py::ensure_schema(store) 读本文件建表。
--
-- 三条硬约束（与退款域 schema.sql 同一套，改这个文件前先读）：
--   1. 全部是**新增**表。maos/core/store.py 的现有表结构一字不改，本文件不碰它们；
--      退款域那 15 张表同样不碰 —— 两个域各建各的，表名一个都不重（见下方「表名前缀」）。
--   2. 租户是**主键的一部分**，不是配置项 —— 除 ap_business_ref 外所有表以
--      tenant_id 打头，跨租户读不到彼此的数据靠主键前缀而不是 WHERE 约定。
--   3. 权威事实归外部（铁律 8）。purchase_order / goods_receipt / supplier_invoice
--      存的是「MAOS 执行前从 ERP / WMS / 发票池读到的那一版」，不是那些系统的当前值；
--      read_at 记下读的时刻。付款到没到账的权威在**银行**，落点是
--      ap_payment_observation，只有 ap.observe 写得进去（见 guard.py）。
--
-- ap_case.biz_status 是**业务对象自己的字段**，不是 Task 状态（铁律 9）。
-- 主干三段 + 两个分支：
--   received -> matched -> payment_requested -> settled
--   分支：rejected（三单匹配不过 / 人工驳回）/ compensated（付款走不通后的补偿收口）
-- settled 只能由 ap.observe 写入，且必须同事务附银行回单，见 guard.py。
--
-- ## 表名前缀：为什么 ap_case / ap_payment_observation 带前缀，purchase_order 不带
--
-- 带前缀的是**与退款域同名会撞**的那几张：refund 域有 refund_case、
-- payment_observation、compensation_record、business_ref。同库两个域，
-- `CREATE TABLE IF NOT EXISTS` 撞名的后果不是报错而是**静默跳过** ——
-- 表在、列是对方的、跑起来一切正常，直到某条 INSERT 报 no such column。
-- 所以撞名的一律加 ap_ 前缀，各守各的表。
-- purchase_order / goods_receipt / supplier_invoice 不带前缀：退款域没有同名表，
-- 而这三个词在应付账款语境里就是它们本来的名字，加前缀反而读着别扭。
--
-- 本文件只描述**目标形状**，它自己搬不动老库：整份都是 `IF NOT EXISTS`，
-- 表已存在就整段跳过，于是**改列静默无效**（加表可以，改列不行）。
-- 把老库搬到目标形状的是 objects.py 里的 _MIGRATIONS，记账落在 ap_schema_version。

-- 迁移记账表。**一条已应用的迁移一行**，不是「一行存当前版本」：
-- 前者的 INSERT 天然幂等（版本号是主键，重复插会响），后者是读-改-写，
-- 两个进程同时升级会互相盖掉。当前版本 = MAX(version)，一行都没有就是 0。
--
-- 本表**不能**用来判断「这库是新的还是老的」—— 新库刚建完它同样是空的。
-- 判据在每条迁移步骤自己的探针里，版本号只是「不必再探一遍」的快路径。
-- 它是这 14 张表里唯一一张不带 tenant_id 的：schema 版本是库级事实。
CREATE TABLE IF NOT EXISTS ap_schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (version)
);

-- ------------------------------------------------------------------ 供应商
CREATE TABLE IF NOT EXISTS supplier (
    tenant_id     TEXT NOT NULL,
    supplier_id   TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    -- 付款方式取 UNCL4461（BR-CL-16）。存码值不存名称：名称在
    -- maos/tools/ap_codes.py 里，存两份就会漂。
    payment_means_code TEXT NOT NULL DEFAULT '30',
    payment_terms TEXT NOT NULL DEFAULT '',
    bank_account  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, supplier_id)
);

-- ------------------------------------- 三单之一：采购订单（外部系统快照，带版本）
CREATE TABLE IF NOT EXISTS purchase_order (
    tenant_id     TEXT NOT NULL,
    po_id         TEXT NOT NULL,
    version       INTEGER NOT NULL,
    supplier_id   TEXT NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'CNY',
    ordered_at    TEXT NOT NULL,
    payload_json  TEXT NOT NULL DEFAULT '{}',
    read_at       TEXT NOT NULL,
    PRIMARY KEY (tenant_id, po_id, version)
);

CREATE TABLE IF NOT EXISTS purchase_order_line (
    tenant_id   TEXT NOT NULL,
    po_id       TEXT NOT NULL,
    version     INTEGER NOT NULL,
    line_no     INTEGER NOT NULL,
    sku         TEXT NOT NULL,
    -- 数量与单价用 REAL，金额用 TEXT。理由见 supplier_invoice_line。
    quantity    REAL NOT NULL,
    unit_price  TEXT NOT NULL,
    tax_category_code TEXT NOT NULL DEFAULT 'S',   -- UNCL5305（BR-CL-17）
    tax_rate    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, po_id, version, line_no)
);

-- ------------------------------------- 三单之二：收货单（WMS 快照，一次收货一张）
CREATE TABLE IF NOT EXISTS goods_receipt (
    tenant_id    TEXT NOT NULL,
    gr_id        TEXT NOT NULL,
    po_id        TEXT NOT NULL,
    po_version   INTEGER NOT NULL,
    received_at  TEXT NOT NULL,
    warehouse    TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    read_at      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, gr_id)
);

CREATE TABLE IF NOT EXISTS goods_receipt_line (
    tenant_id         TEXT NOT NULL,
    gr_id             TEXT NOT NULL,
    line_no           INTEGER NOT NULL,
    sku               TEXT NOT NULL,
    quantity_received REAL NOT NULL,
    -- 验收不合格的数量。三单匹配判的是**合格数**，不是到货数 ——
    -- 到了但验收没过的货不该付钱。
    quantity_rejected REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, gr_id, line_no)
);

-- ------------------------------------- 三单之三：供应商发票（发票池快照）
CREATE TABLE IF NOT EXISTS supplier_invoice (
    tenant_id    TEXT NOT NULL,
    invoice_id   TEXT NOT NULL,
    supplier_id  TEXT NOT NULL,
    po_id        TEXT NOT NULL,
    -- UNCL1001-inv（BR-CL-01）。380 = Commercial invoice。
    invoice_type_code TEXT NOT NULL DEFAULT '380',
    currency     TEXT NOT NULL DEFAULT 'CNY',
    issued_at    TEXT NOT NULL,
    due_at       TEXT NOT NULL DEFAULT '',
    -- 以下四个金额是发票**自称**的合计，勾稽由三单匹配按 BR-CO-10/13/15/16 现算，
    -- 不信发票自己报的数。全部 TEXT：金额永远不进浮点（同 tools/ap.py）。
    line_net_total  TEXT NOT NULL DEFAULT '0',   -- BT-106
    total_excl_vat  TEXT NOT NULL DEFAULT '0',   -- BT-109
    total_vat       TEXT NOT NULL DEFAULT '0',   -- BT-110
    total_incl_vat  TEXT NOT NULL DEFAULT '0',   -- BT-112
    prepaid_amount  TEXT NOT NULL DEFAULT '0',   -- BT-113
    amount_due      TEXT NOT NULL DEFAULT '0',   -- BT-115
    payload_json TEXT NOT NULL DEFAULT '{}',
    read_at      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, invoice_id)
);

CREATE TABLE IF NOT EXISTS supplier_invoice_line (
    tenant_id   TEXT NOT NULL,
    invoice_id  TEXT NOT NULL,
    line_no     INTEGER NOT NULL,
    sku         TEXT NOT NULL,
    -- 数量是可以有小数的物理量（3.5 吨），用 REAL 没问题；
    -- 单价与金额是钱，一律 TEXT + Decimal 运算 —— 0.1+0.2 那种误差在勾稽
    -- （BR-CO-13/15/17）上会直接变成一条假的拒付理由。
    quantity    REAL NOT NULL,               -- BT-129
    unit_price  TEXT NOT NULL,               -- BT-146
    line_net    TEXT NOT NULL,               -- BT-131
    tax_category_code TEXT NOT NULL DEFAULT 'S',  -- BT-151 / UNCL5305
    tax_rate    REAL NOT NULL DEFAULT 0,     -- BT-119
    PRIMARY KEY (tenant_id, invoice_id, line_no)
);

-- -------------------------------------------------------------- 应付案例主体
-- biz_status 的唯一写入路径是 guard.create_case / guard.update_biz_status。
-- CHECK 约束是第二道防线：即便有人绕开 guard 直连连接写进来，也只能写这七个值之一。
CREATE TABLE IF NOT EXISTS ap_case (
    tenant_id   TEXT NOT NULL,
    case_id     TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    po_id       TEXT NOT NULL,
    po_version  INTEGER NOT NULL,
    invoice_id  TEXT NOT NULL,
    gr_id       TEXT NOT NULL,
    -- 发票自称的应付金额。真正打出去的金额由三单匹配算出来并落在 match_result 上，
    -- 两者不一致正是本域要拦的事，所以两处都留着，不合并成一处。
    amount_claimed TEXT NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'CNY',
    biz_status  TEXT NOT NULL,
    plan_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id),
    CHECK (biz_status IN ('received', 'matched', 'payment_requested',
                          'settled', 'rejected', 'compensated'))
);

-- ------------------------------------------------------------ 三单匹配结果
-- 一次匹配一行（按 attempt 区分），**保留历史** —— 返工重匹配时旧结论要留着，
-- 否则「第一次为什么没过」这个问题在库里查不到。
CREATE TABLE IF NOT EXISTS match_result (
    tenant_id     TEXT NOT NULL,
    case_id       TEXT NOT NULL,
    attempt       INTEGER NOT NULL,
    matched       INTEGER NOT NULL,          -- 0/1
    -- 匹配通过时算出来的应付金额（BR-CO-16 口径）。不通过时为空字符串。
    payable_amount TEXT NOT NULL DEFAULT '',
    -- 拒付理由，JSON 数组。每条必带 rule_id，且该编号必在
    -- maos/tools/ap_codes.py 的 RULES 里 —— 「理由可核对」的落点。
    findings_json TEXT NOT NULL DEFAULT '[]',
    tolerance_json TEXT NOT NULL DEFAULT '{}',
    matched_by    TEXT NOT NULL,
    matched_at    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, attempt)
);

-- ---------------------------------------------------------------- 人工审批
-- 由**人**的决定写入（房间里的 /approve 或 CLI），ap.execute 只读不写 ——
-- 让付款方自己写下「我被批准了」，等于没有审批。
CREATE TABLE IF NOT EXISTS payment_approval (
    tenant_id  TEXT NOT NULL,
    case_id    TEXT NOT NULL,
    approver   TEXT NOT NULL,
    decision   TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, approver, decided_at),
    CHECK (decision IN ('approved', 'rejected'))
);

-- ---------------------------------------------------------------- 付款指令
-- UNIQUE (tenant_id, idempotency_key)：一张发票只允许有一笔付款指令。
-- 这是「不会付出第二笔」在库这一层的落点，与 MockBank 的幂等键是同一件事的两道防线。
CREATE TABLE IF NOT EXISTS payment_instruction (
    tenant_id       TEXT NOT NULL,
    case_id         TEXT NOT NULL,
    instruction_id  TEXT NOT NULL,
    amount          TEXT NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'CNY',
    payment_means_code TEXT NOT NULL DEFAULT '30',   -- UNCL4461
    bank            TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    revoked         INTEGER NOT NULL DEFAULT 0,
    submitted_at    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, instruction_id),
    UNIQUE (tenant_id, idempotency_key)
);

-- 权威回单落点：settled 必须与这里的一行同事务写入，见 guard.py。
-- bank_reference（银行流水号）是 NOT NULL 且守卫要求非空：没有流水号的
-- 「已付」就是把外部状态写死为终态。
CREATE TABLE IF NOT EXISTS ap_payment_observation (
    tenant_id           TEXT NOT NULL,
    case_id             TEXT NOT NULL,
    instruction_id      TEXT NOT NULL,
    observed_state      TEXT NOT NULL,
    bank_reference      TEXT NOT NULL DEFAULT '',
    value_date          TEXT NOT NULL DEFAULT '',
    raw_advice_json     TEXT NOT NULL DEFAULT '{}',
    observed_at         TEXT NOT NULL,
    actor_invocation_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, instruction_id, observed_at)
);

-- ------------------------------------------------------------------ 补偿
CREATE TABLE IF NOT EXISTS ap_compensation_record (
    tenant_id   TEXT NOT NULL,
    case_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    executed_at TEXT NOT NULL,
    operator    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, kind, executed_at)
);

-- -------------------------------- DAG/Task/Artifact -> 业务对象（只存引用）
-- 与退款域的 business_ref 同形，但**是另一张表**：两个域的 object_type 取值域
-- 不同，共用一张表会让 resolve 的分派表变成两个域都要改的公共面。
CREATE TABLE IF NOT EXISTS ap_business_ref (
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

CREATE INDEX IF NOT EXISTS idx_ap_case_plan      ON ap_case(plan_id);
CREATE INDEX IF NOT EXISTS idx_ap_obs_case       ON ap_payment_observation(tenant_id, case_id);
CREATE INDEX IF NOT EXISTS idx_ap_biz_ref_obj    ON ap_business_ref(tenant_id, object_type, object_id);
CREATE INDEX IF NOT EXISTS idx_inv_line_invoice  ON supplier_invoice_line(tenant_id, invoice_id);
CREATE INDEX IF NOT EXISTS idx_po_line_po        ON purchase_order_line(tenant_id, po_id, version);
CREATE INDEX IF NOT EXISTS idx_gr_line_gr        ON goods_receipt_line(tenant_id, gr_id);
