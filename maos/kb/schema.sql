-- 知识层建表 —— 全部是**新增表**，一张现有表都不碰（铁律 2）。
--
-- 本文件只描述**目标形状**，它自己搬不动老库：整份都是 `IF NOT EXISTS`，
-- 表已存在就整段跳过，于是**改列静默无效** —— 跑起来一切正常，直到某条
-- SELECT 报 no such column（BACKLOG `## task-R1` 第 5 条、`## task-T13` 第 3 条）。
--
-- 把老库搬到目标形状的是 `maos/kb/__init__.py` 里的 `_MIGRATIONS`，
-- 记账落在下面的 `kb_schema_version` 表。**改列要动两处**：这里写进目标形状，
-- 那边补一条迁移步骤。只改这里的后果不是报错，是老库永远升不上来 ——
-- 演示期的库都是 `:memory:` 或每次新建，所以不咬人；PolarDB 是持久库，
-- 上线第一天就咬，而且咬得没有声音（T17 轨买的就是这个）。

-- 迁移记账表。**一条已应用的迁移一行**，不是「一行存当前版本」：
-- 前者的 INSERT 天然幂等（版本号是主键，重复插会响），后者是读-改-写，
-- 两个进程同时升级会互相盖掉。当前版本 = MAX(version)，一行都没有就是 0。
--
-- 本表**不能**用来判断「这库是新的还是老的」—— 新库刚建完它同样是空的。
-- 判据在每条迁移步骤自己的探针里（该干的事干没干），版本号只是
-- 「不必再探一遍」的快路径。顺序上它必须排在数据表前面：迁移读它决定跑什么。
CREATE TABLE IF NOT EXISTS kb_schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (version)
);

-- kb_doc：知识文档。
-- doc_id / kind / source_case_id 三个列名被 scripts/verify.py 第 5、7 项写死消费（F-4），不许改。
--
-- `id` 列是 F-2 对齐用的（T13 轨）：契约附则约定源表主键**列名固定为 id**，
-- 而本表的主键是 (tenant_id, doc_id) —— 两条口径都没写错，是没对齐，
-- 于是 StorePort 的 vector_search 在本表上恒抛 no such column: id，
-- 检索器那条端口分支一次都走不到（BACKLOG `## task-X3` 第 1、2 条）。
-- 做成 **GENERATED ... VIRTUAL** 而不是普通列，是为了避开「只加列不填值」那种
-- 无症状故障：生成列不可写、也无从忘填，永远等于主键本身。
-- 取值口径与 maos/kb/__init__.py 的 `doc_row_id()` 是同一份，改一处必须改两处。
--
-- VIRTUAL 这个词还兼着第二个用处：SQLite 的 `ALTER TABLE ADD COLUMN` **能**加
-- VIRTUAL 生成列（STORED 的不行，报 cannot add a STORED column）。所以老库补这一列
-- 是一条 ALTER 的事，不必「建新表→拷数据→换名」，迁移后的形状与本文件逐字相同。
-- 下面那条表达式若要改，`__init__.py` 的 `DOC_ROW_ID_EXPR` 必须同步改（有测试钉着）。
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
    id               TEXT GENERATED ALWAYS AS (tenant_id || ':' || doc_id) VIRTUAL,
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
--
-- `id` 与源表那列同一口径（`doc_row_id()`），理由见上面 kb_doc 的注释。
-- 这里只能是普通列：FTS5 虚表不支持生成列，所以它由 upsert_doc() 显式填 ——
-- 影子表的写入口同样只有那一个，忘填会在建表期就被本文件的读者发现。
-- UNINDEXED：它是回查用的键，不参与 BM25，进索引只会污染打分。
--
-- 老库补这一列**没有** ALTER 可走：虚表一律 `virtual tables may not be altered`。
-- 迁移只能删表→按本条 DDL 重建→从 kb_doc 重灌（`_migrate_v1_row_id`）。
-- 重建取的就是本条语句本身，不在 Python 里另抄一份 —— 抄一份的后果是
-- 老库重建出来的影子表和新库不是同一张表，而两边都不报错。
CREATE VIRTUAL TABLE IF NOT EXISTS kb_doc_fts USING fts5(
    id UNINDEXED,
    doc_id UNINDEXED,
    tenant_id UNINDEXED,
    title,
    body
);
