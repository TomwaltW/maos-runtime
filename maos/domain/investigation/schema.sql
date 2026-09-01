-- 银行差错处理域的业务对象层 —— 6 张业务表 + 1 张迁移记账表，
-- 由 objects.py::ensure_schema(store) 读本文件建表。
--
-- 三条硬约束（口径同退款域 maos/domain/refund/schema.sql，改这个文件前先读）：
--   1. 全部是**新增**表。maos/core/store.py 的现有表结构一字不改，本文件不碰它们。
--      也不碰退款域那 15 张表 —— 两个域的表名各自带域前缀，谁也别复用谁的。
--   2. 租户是主键的一部分，不是配置项。所有表以 tenant_id 打头（迁移记账表除外，
--      schema 版本是库级事实）。
--   3. 权威事实归外部（铁律 8）。本域的权威在**清算方**，不在 MAOS 库里。
--      resolution_observation 存的是「MAOS 问到的那一次答复」，不是资金的真实下落。
--
-- investigation_case.biz_status 是**业务对象自己的字段**，不是 Task 状态（铁律 9）。
-- 主干四段 + 两个分支：
--   filed -> classified -> cancellation_sent -> returned
--   分支：rejected（清算方否定决议后收口）/ compensated（问不出结果后补偿收口）
-- returned 只能由 investigation.observe 写入，且必须附一份 **pacs.004** 的观察，
-- 见 guard.py。
--
-- 本文件只描述**目标形状**，它自己搬不动老库：整份都是 `IF NOT EXISTS`，
-- 表已存在就整段跳过，于是**改列静默无效**（同退款域踩过的坑）。
-- 把老库搬到目标形状的是 objects.py 里的 `_MIGRATIONS`。

-- 迁移记账表。一条已应用的迁移一行（不是「一行存当前版本」）——
-- 理由同退款域：前者的 INSERT 天然幂等，后者是读-改-写，两个进程同时升级会互相盖掉。
-- 本表不能用来判断「这库是新的还是老的」：新库刚建完它同样是空的。
CREATE TABLE IF NOT EXISTS investigation_schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (version)
);

-- ---------------------------------------------------------------- 原始支付快照
-- 被质疑的那一笔原始支付（pacs.008）在 MAOS 读到它时的样子。
-- **不是**清算系统的当前值 —— read_at 记下读的时刻，与退款域的 order_snapshot 同口径。
CREATE TABLE IF NOT EXISTS original_payment_snapshot (
    tenant_id        TEXT NOT NULL,
    original_msg_id  TEXT NOT NULL,   -- 原报文 GrpHdr/MsgId
    version          INTEGER NOT NULL,
    end_to_end_id    TEXT NOT NULL,   -- 端到端参考号，跨行追踪同一笔的锚点
    interbank_amount REAL NOT NULL,
    currency         TEXT NOT NULL,
    value_date       TEXT NOT NULL,   -- 起息日
    debtor_agent     TEXT NOT NULL,   -- 付款行 BIC
    creditor_agent   TEXT NOT NULL,   -- 收款行 BIC
    settlement_method TEXT NOT NULL DEFAULT '',
    payload_json     TEXT NOT NULL DEFAULT '{}',
    read_at          TEXT NOT NULL,
    PRIMARY KEY (tenant_id, original_msg_id, version)
);

-- ------------------------------------------------------------------ 案件主体
-- 一个差错案件。case_id 对应 camt.056 的 Case/Id，是与清算方对话的案号。
CREATE TABLE IF NOT EXISTS investigation_case (
    tenant_id         TEXT NOT NULL,
    case_id           TEXT NOT NULL,
    creator_agent     TEXT NOT NULL,   -- case creator（发起方 BIC）
    assignee_agent    TEXT NOT NULL,   -- case assignee（被指派方 BIC）
    original_msg_id   TEXT NOT NULL,
    original_version  INTEGER NOT NULL,
    end_to_end_id     TEXT NOT NULL,
    amount            REAL NOT NULL,
    currency          TEXT NOT NULL,
    -- 定性结论：ExternalCancellationReason1Code 里的一条。filed 阶段为空，
    -- classify 之后才有 —— 「还没定性」与「定性成 XXX」必须分得开。
    cancellation_reason_code TEXT NOT NULL DEFAULT '',
    biz_status        TEXT NOT NULL,
    plan_id           TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id),
    CHECK (biz_status IN ('filed', 'classified', 'cancellation_sent',
                          'returned', 'rejected', 'compensated'))
);

-- ------------------------------------------------------- camt.056 撤销请求
-- 发出去的撤销请求。一个案子可以有多笔（重发/改派），靠 idempotency_key 防重。
CREATE TABLE IF NOT EXISTS cancellation_request (
    tenant_id       TEXT NOT NULL,
    case_id         TEXT NOT NULL,
    request_id      TEXT NOT NULL,   -- 清算方给的受理号
    message_type    TEXT NOT NULL,   -- 恒为 camt.056.001.xx
    assignment_id   TEXT NOT NULL,   -- Assgnmt/Id，报文级指派号
    reason_code     TEXT NOT NULL,   -- ExternalCancellationReason1Code
    amount          REAL NOT NULL,
    currency        TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    clearing_house  TEXT NOT NULL,
    sent_at         TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, request_id),
    UNIQUE (tenant_id, idempotency_key)
);

-- --------------------------------------------- camt.029 / pacs.004 决议观察
-- **本域的权威回执落点**。returned 必须与这里的一行同事务写入，见 guard.py。
--
-- 一张表装两种报文，因为它们回答的是**同一个问题的两个部分**，
-- 而把它们拆成两张表会让「先收到 camt.029/CNCL、再收到 pacs.004」这条真实时序
-- 变成跨表 join 才看得出来的东西：
--
--   camt.029（ResolutionOfInvestigation）  答「你那个撤销请求，我怎么处理的」
--   pacs.004（PaymentReturn）              答「钱退回来了」—— 只有它证明得了资金
--
-- 所以 confirmation_code / rejection_code 只在 camt.029 行上有值，
-- return_reason_code / returned_amount 只在 pacs.004 行上有值。
-- observed_state 是归一之后的口径，取值见 guard.OBSERVED_STATES。
--
-- **每一次问询都落一行**，不是只落最后那一次。中间那几次里就有本域的题眼：
-- 一条 camt.029/CNCL（「撤销成功」）出现在顺利路径与失败路径的**同一个位置**，
-- 只落最后一次的话，「系统看见了肯定答复却没有写 returned」这件事没有证据。
--
-- `poll_seq` 进主键而不只靠 `observed_at`：三次问询可能落在同一微秒里，
-- 那时 INSERT OR REPLACE 会把前一条**静默覆盖**掉 —— 症状是观察莫名少几行，
-- 而这正是本域拿来当证据的那几行。
CREATE TABLE IF NOT EXISTS resolution_observation (
    tenant_id           TEXT NOT NULL,
    case_id             TEXT NOT NULL,
    request_id          TEXT NOT NULL,
    poll_seq            INTEGER NOT NULL DEFAULT 0,  -- 第几次问询问到的
    message_type        TEXT NOT NULL,   -- camt.029.001.xx | pacs.004.001.xx
    confirmation_code   TEXT NOT NULL DEFAULT '',  -- ExternalInvestigationExecutionConfirmation1Code
    rejection_code      TEXT NOT NULL DEFAULT '',  -- ExternalPaymentCancellationRejection1Code
    return_reason_code  TEXT NOT NULL DEFAULT '',  -- ExternalReturnReason1Code
    returned_amount     REAL,                      -- 只有 pacs.004 才有；NULL = 这条不是退款报文
    raw_message_json    TEXT NOT NULL DEFAULT '{}',
    observed_state      TEXT NOT NULL,
    observed_at         TEXT NOT NULL,
    actor_invocation_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, request_id, poll_seq, observed_at)
);

-- ---------------------------------------------------- 人工调账审批（硬监管要求）
-- 差错处理里的人工调账必须有人批，这不是产品选项，是监管要求。
-- 由**人**的决定写入（本轮走 Matrix 房间），任何 skill 只读不写 ——
-- 让调账方自己写下「我被批准了」，等于没有审批（口径同退款域 approval_record）。
CREATE TABLE IF NOT EXISTS adjustment_approval (
    tenant_id  TEXT NOT NULL,
    case_id    TEXT NOT NULL,
    approver   TEXT NOT NULL,
    decision   TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, approver, decided_at),
    CHECK (decision IN ('approved', 'rejected'))
);

-- ------------------------------------------------------------------ 补偿留档
CREATE TABLE IF NOT EXISTS investigation_compensation (
    tenant_id   TEXT NOT NULL,
    case_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    executed_at TEXT NOT NULL,
    operator    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, kind, executed_at)
);

CREATE INDEX IF NOT EXISTS idx_inv_case_plan    ON investigation_case(plan_id);
CREATE INDEX IF NOT EXISTS idx_inv_obs_case     ON resolution_observation(tenant_id, case_id);
CREATE INDEX IF NOT EXISTS idx_inv_req_case     ON cancellation_request(tenant_id, case_id);
CREATE INDEX IF NOT EXISTS idx_inv_snap_e2e     ON original_payment_snapshot(tenant_id, end_to_end_id);
