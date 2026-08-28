-- 知识层建表 —— 全部是**新增表**，一张现有表都不碰（铁律 2）。
--
-- 建表机制的坑（BACKLOG ## task-R1 第 5 条）：executescript 全是
-- CREATE TABLE IF NOT EXISTS，没有迁移路径 —— 表已存在就整段跳过，
-- **改列静默无效**，跑起来一切正常，直到某条 INSERT 报 no such column。
-- 所以列定义一次定对；真要改列就改本文件并重建库（演示期都是 :memory:，无历史数据）。

-- kb_doc：知识文档。
-- doc_id / kind / source_case_id 三个列名被 scripts/verify.py 第 5、7 项写死消费（F-4），不许改。
CREATE TABLE IF NOT EXISTS kb_doc (
    tenant_id        TEXT NOT NULL,
    doc_id           TEXT NOT NULL,
    biz_type         TEXT,
    channel_id       TEXT,
    region           TEXT,
    sku              TEXT,
    policy_version   INTEGER,
    workflow_version INTEGER,
    rule_no          TEXT,
    gateway_code     TEXT,
    kind             TEXT NOT NULL,
    title            TEXT NOT NULL DEFAULT '',
    body             TEXT NOT NULL DEFAULT '',
    embedding        TEXT,
    outcome          TEXT,
    source_case_id   TEXT,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (tenant_id, doc_id),
    -- 取值域写进 CHECK 而不是只写在注释里：写错 kind 的条目查得出来但归不了类，
    -- 而错误发生在写入侧、暴露在几周后的检索侧，是最难回溯的一类脏数据。
    CHECK (kind IN ('policy', 'history_case', 'failure_hint', 'error_code_playbook')),
    CHECK (outcome IS NULL OR outcome IN ('success', 'failed'))
);

-- 阶段一预过滤的复合索引。顺序与 retriever.PREFILTER_FIELDS 一致：
-- tenant_id 永远是最左前缀，跨租户的知识连候选集都进不了。
CREATE INDEX IF NOT EXISTS idx_kb_doc_prefilter
    ON kb_doc(tenant_id, biz_type, channel_id, region, sku);
CREATE INDEX IF NOT EXISTS idx_kb_doc_kind ON kb_doc(tenant_id, kind);

-- 全文通道的影子表。分词器用默认的 unicode61，中文由写入侧先切开：
-- unicode61 不切中文（整段当一个 token，中文语料上召回恒为空），
-- 而 trigram 又查不了两个字的词（"锈蚀" 够不到 3 字符）—— 两条都会让
-- 「BM25 通道」变成一句空话。所以 title/body 进影子表前过一遍 kb.fts_text()：
-- 英数按词、中文按字，空格分隔。查询侧走同一个函数，两边口径必然一致。
-- 内容由 upsert_doc() 显式同步（先删后插），不挂触发器：
-- 写入口只有一个，逻辑集中在一处比散在触发器里好测也好查。
CREATE VIRTUAL TABLE IF NOT EXISTS kb_doc_fts USING fts5(
    doc_id UNINDEXED,
    tenant_id UNINDEXED,
    title,
    body
);
