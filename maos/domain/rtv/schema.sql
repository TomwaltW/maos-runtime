-- RTV（Return to Vendor，采购退货退款）域业务对象。
--
-- 五张表刻意**不建**：supplier / purchase_order / purchase_order_line /
-- goods_receipt / goods_receipt_line —— 它们在 maos/domain/ap/schema.sql 里已经存在，
-- RTV 只引用不重建。`CREATE TABLE IF NOT EXISTS` 撞名的后果不是报错而是**静默跳过**，
-- 重建一份「看起来一样」的定义，两处一旦漂开，症状会离原因非常远。
--
-- 因此本域**不是自足的**：那五张表的建表责任在持有它们的域（ap），
-- 本文件建不出来也不该建。objects.py::require_upstream_tables() 会在真正要读写
-- 它们之前先探一次，缺表时抛一句指得出去处的错，而不是等到某条 INSERT 报
-- no such table —— 后者的报错离原因太远。
--
-- 三条硬约束（口径同 ap / refund 域的 schema.sql）：
--   1. 全部是**新增**表。maos/core/store.py 的现有表结构一字不改（铁律 1）；
--      ap / claim / investigation / refund 四个域的表同样不碰，表名一个都不重。
--   2. 租户是**主键的一部分**，不是配置项 —— 除 rtv_business_ref 外所有表以
--      tenant_id 打头。
--   3. 权威事实归外部（铁律 8）。本域有**两个**外部权威源，这是它相对 ap 域的增量：
--      「供应商认不认这笔退货」权威在**供应商**，落点 credit_note；
--      「钱到没到账」权威在 **AP / 银行**，落点 rtv_settlement_observation。
--      两张表都只有 rtv.observe 写得进去（见 guard.py）。
--
-- rtv_case.biz_status 是**业务对象自己的字段**，不是 Task 状态（铁律 9）。
-- maos/contracts/states.py 一个新状态、一条新迁移都没加。
--
-- 本文件只描述**目标形状**，它自己搬不动老库：整份都是 `IF NOT EXISTS`，
-- 表已存在就整段跳过，于是**改列静默无效**（加表可以，改列不行）。
-- 把老库搬到目标形状的是 objects.py 里的 _MIGRATIONS，记账落在 rtv_schema_version。

CREATE TABLE IF NOT EXISTS rtv_schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (version)
);

-- ------------------------------------------------------------------ 退货案子
CREATE TABLE IF NOT EXISTS rtv_case (
    tenant_id   TEXT NOT NULL,
    case_id     TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    -- 源头三件套。RTV 必须能指回「退的是哪一张 PO 的哪一次收货」，
    -- 指不回去的退货在对账时无法与贷项通知单勾稽（SOP 第 ① 步）。
    po_id       TEXT NOT NULL,
    po_version  INTEGER NOT NULL,
    gr_id       TEXT NOT NULL,
    -- 处置类型：credit / exchange / replacement。**建案时为空串**，
    -- 由 rtv.dispose 裁定后写入 —— 受理的人不该替裁定的人拍板。
    return_action TEXT NOT NULL DEFAULT '',
    -- 退货方自称的应退金额。真正认的金额以供应商贷项通知单为准（外部权威），
    -- 两者不一致正是本域要拦的事，所以两处都留着，不合并成一处。
    amount_claimed TEXT NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'CNY',
    biz_status  TEXT NOT NULL,
    plan_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id),
    CHECK (return_action IN ('', 'credit', 'exchange', 'replacement')),
    CHECK (biz_status IN ('received', 'disposed', 'shipped',
                          'credited', 'settled', 'rejected', 'compensated'))
);

-- ------------------------------------------------------------------ 退货行
-- 一行对应源收货单的一行。**不改 goods_receipt_line**：
-- PeopleSoft 口径里 `Quantity Received` 是审计量、永不变，退货量单独记一处。
CREATE TABLE IF NOT EXISTS rtv_line (
    tenant_id         TEXT NOT NULL,
    case_id           TEXT NOT NULL,
    line_no           INTEGER NOT NULL,
    gr_line_no        INTEGER NOT NULL,      -- 指回 goods_receipt_line.line_no
    sku               TEXT NOT NULL,
    quantity_returned REAL NOT NULL,
    unit_price        TEXT NOT NULL,
    -- 退货理由编码，必在 maos/tools/rtv_codes.py 的 RETURN_REASONS 里。
    -- 「理由可核对」的落点，口径同 ap 域的 match_result.findings_json。
    reason_code       TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, line_no)
);

-- ---------------------------------------------------------------- 处置裁定
-- 一次裁定一行（按 attempt 区分），**保留历史** —— 返工重裁时旧结论要留着。
CREATE TABLE IF NOT EXISTS rtv_disposition (
    tenant_id      TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    attempt        INTEGER NOT NULL,
    return_action  TEXT NOT NULL,
    -- 裁定依据，JSON 数组。每条必带 rule_id，且该编号必在
    -- maos/tools/rtv_codes.py 的 RULES 里。
    rationale_json TEXT NOT NULL DEFAULT '[]',
    decided_by     TEXT NOT NULL,
    decided_at     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, attempt),
    CHECK (return_action IN ('credit', 'exchange', 'replacement'))
);

-- ------------------------------------------------------------------ 发运登记
CREATE TABLE IF NOT EXISTS rtv_shipment (
    tenant_id      TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    shipment_id    TEXT NOT NULL,
    carrier        TEXT NOT NULL,
    tracking_no    TEXT NOT NULL,
    -- 承运商回执里的状态。承运商是**外部**，这一列是观察结果不是我方决定。
    carrier_status TEXT NOT NULL,
    shipped_at     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, shipment_id)
);

-- ------------------------------------------- 供应商贷项通知单（外部权威事实）
-- 🔴 这张表**只由 rtv.observe 写**。贷项通知单是供应商开的，不是我方能决定的。
-- 我方写一行「供应商给我开了贷项通知单」，等于自己给自己开发票。
CREATE TABLE IF NOT EXISTS credit_note (
    tenant_id       TEXT NOT NULL,
    case_id         TEXT NOT NULL,
    credit_note_id  TEXT NOT NULL,           -- 供应商侧的单号，我方不生成
    -- UNCL1001 单据类型码。贷项通知单恒为 '381'，见 rtv_codes.py。
    document_type   TEXT NOT NULL DEFAULT '381',
    amount_credited TEXT NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'CNY',
    issued_at       TEXT NOT NULL,           -- 供应商开出的时间，不是我方观察到的时间
    observed_at     TEXT NOT NULL,           -- 我方观察到的时间，两者刻意分开
    observed_by     TEXT NOT NULL,
    invocation_id   TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, credit_note_id)
);

-- ---------------------------------------------------------------- 三方对账
CREATE TABLE IF NOT EXISTS rtv_reconciliation (
    tenant_id       TEXT NOT NULL,
    case_id         TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    reconciled      INTEGER NOT NULL,        -- 0/1
    -- 对上时算出来的应收金额。对不上时为空串。
    creditable_amount TEXT NOT NULL DEFAULT '',
    -- 对不上的理由，JSON 数组，每条必带 rule_id（同 rtv_disposition）。
    findings_json   TEXT NOT NULL DEFAULT '[]',
    tolerance_json  TEXT NOT NULL DEFAULT '{}',
    reconciled_by   TEXT NOT NULL,
    reconciled_at   TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, attempt)
);

-- --------------------------------------------- 终态观察（外部权威事实）
-- 🔴 只由 rtv.observe 写。口径同 ap 域的 ap_payment_observation。
CREATE TABLE IF NOT EXISTS rtv_settlement_observation (
    tenant_id      TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    -- AP 侧的调整凭单 / 借项通知单号，外部系统生成。
    adjustment_id  TEXT NOT NULL,
    observed_state TEXT NOT NULL,
    ap_reference   TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    observed_by    TEXT NOT NULL,
    invocation_id  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, seq)
);

-- ------------------------------------------------------------------ 补偿留痕
CREATE TABLE IF NOT EXISTS rtv_compensation_record (
    tenant_id     TEXT NOT NULL,
    case_id       TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    detail_json   TEXT NOT NULL DEFAULT '{}',
    compensated_by TEXT NOT NULL,
    compensated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, seq)
);

-- ------------------------------------------------------------ 业务对象引用
CREATE TABLE IF NOT EXISTS rtv_business_ref (
    plan_id        TEXT NOT NULL,
    task_id        TEXT,
    object_table   TEXT NOT NULL,
    object_id      TEXT NOT NULL,
    object_version INTEGER,
    purpose        TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);
