-- PG 侧**翻译器翻不出来**的那部分 DDL —— 扩展、影子表、全文索引。
--
-- 怎么用（本机 Docker，实测见 deploy/polardb.md）：
--
--     docker compose -f deploy/docker-compose.yml --profile pg up -d pgvector
--     docker compose -f deploy/docker-compose.yml exec -T pgvector \
--         psql -U <user> -d <db> -f - < maos/store/pg_schema.sql
--
-- 平时不用手跑：`maos/kb/__init__.py::ensure_schema()` 在 PG 后端上会把本文件
-- 里除 `CREATE EXTENSION` 之外的语句一并执行（见 `pg_store.kb_extra_statements`）。
--
-- 本文件**只建新表**（铁律 2：现有表结构禁改，只许新增）。
--
-- ---------------------------------------------------------------------------
-- T115 起：业务表与 kb_doc 不在本文件里，**由翻译器现翻**
-- ---------------------------------------------------------------------------
-- 退款域 16 张表与知识层的 `kb_doc` / `kb_schema_version`，DDL 只有一份，
-- 就是 SQLite 方言的 `maos/domain/refund/schema.sql` 与 `maos/kb/schema.sql`；
-- PG 侧由 `maos/domain/_dbport.py::to_pg_ddl()` 现翻。
--
-- **不要在这个文件里手抄一份 PG 版**。同一波次里有好几条轨在往退款域加表加列
-- （跨轨契约 §B），手抄的那份一落地就漂，而漂了只在配了 PG 的机器上、只在跑到
-- 那张表时才炸（`relation does not exist`）。翻译器认不出来的构造会当场抛
-- `UnsupportedDdlError` 并带上原文那一行 —— 那才是该往哪儿加的信号。
--
-- ---------------------------------------------------------------------------
-- `kb_doc_pg` 已退役（T115）
-- ---------------------------------------------------------------------------
-- 它当初存在的理由（`docs/DECISIONS.md` 2026-08-29 那条）是：F-2 约定源表主键
-- **列名固定为 `id`**，而 `kb_doc` 的主键是 `(tenant_id, doc_id)`，两边对不上，
-- 照 `kb_doc` 翻一份到 PG 的话两条检索通道都用不了。
--
-- 那个错配后来被 T13 修掉了：`kb_doc` 自己长出了 `id` 生成列
-- （`tenant_id || ':' || doc_id`）。到 T115 把 `kb_doc` 真的建到 PG 上之后，
-- `kb_doc_pg` 就成了一张没有租户列、没人写、也没人读的重复表 —— 留着只会让
-- 后来的人分不清哪张才是知识层的表。所以退役，不是「对齐」（对齐出来的是
-- `kb_doc` 的同形复制品，同一个歧义换个名字而已）。
--
-- ⚠️ `deploy/polardb-live.md` 里那些提到 `kb_doc_pg` 的实测读数**一字不动**
--    （铁律 3：历史读数改了就是篡改证据）。那是 2026-08-30 在云上那张表上量到的，
--    今天这张表不再随代码交付，但当时量到的东西仍然是当时量到的。

-- pgvector 扩展。镜像自带扩展文件，但每个库要显式建一次 —— 不建的话 `<=>`
-- 报的是 "operator does not exist"，看起来像语法错，跟「扩展没装」是两个印象。
--
-- 🔴 `ensure_schema()` **不跑这一句**（`kb_extra_statements()` 会把它滤掉）：
--    建扩展要高权限账号，云上普通账号建不了（`deploy/polardb-live.md` §1.3），
--    挂在建表路径上会让整个知识层在那种实例上起不来。本机 compose 由
--    `deploy/pg-initdb/01-extensions.sql` 在初始化时建好。
CREATE EXTENSION IF NOT EXISTS vector;


-- ---------------------------------------------------------------------------
-- 全文影子表 `kb_doc_fts` —— PG 上是一张**普通镜像表**，不是索引
-- ---------------------------------------------------------------------------
-- SQLite 那边它是 FTS5 虚表；FTS5 是 SQLite 专属，翻译器碰到它整段跳过
-- （`_dbport._fts5_skip()`，翻译器唯一允许的跳过）。
--
-- 那为什么 PG 上还要有这张表？因为**写入口只有一个**：`kb.upsert_doc()` 一次写
-- 三条语句（写 kb_doc、删影子行、插影子行），而那个函数在跨轨契约里不归本轨改。
-- 表不在，PG 上每一次写知识都会炸在第二条语句上。
--
-- 它在 PG 上**不承担检索**：`retriever._local_fts_rows()` 那条 `bm25()` + `MATCH`
-- 是 FTS5 专有语法，在 PG 上必然失败，检索器自己会退化并只告警一次（那段注释里
-- 原话就是「这条路在 PG 后端上是死的」）。PG 的全文走的是下面那两条 GIN 索引 +
-- `PgStorePort.fts_search()` 的 tsvector。
--
-- 没有主键是**照着 SQLite 侧的形状来的**（FTS5 虚表也没有），`upsert_doc` 靠
-- 「先删后插」保证不攒重复行，不靠主键。
CREATE TABLE IF NOT EXISTS kb_doc_fts (
    id        TEXT,
    doc_id    TEXT,
    tenant_id TEXT,
    title     TEXT,
    body      TEXT
);

CREATE INDEX IF NOT EXISTS idx_kb_doc_fts_key ON kb_doc_fts(tenant_id, doc_id);


-- ---------------------------------------------------------------------------
-- 全文通道：tsvector + GIN，建在 `kb_doc` 自己身上
-- ---------------------------------------------------------------------------
-- 两列各一条，与 `retriever.PORT_FTS_FIELDS = ("title", "body")` 一一对应：
-- F-2 的 `fts_search(table, field, q, limit)` 一次只认一列，检索器每列各问一次，
-- 只给 body 建索引的话标题那一次就退化成顺序扫描（不报错，只是慢）。
--
-- 表达式索引里的配置**必须写死成字面量**（这里是 'simple'）：to_tsvector 的单参
-- 形式依赖会话的 default_text_search_config，是 STABLE 不是 IMMUTABLE，建不了索引。
-- 双参形式才是 IMMUTABLE。
--
-- 连带约束：索引只对建索引时那个配置有效。把 MAOS_PG_FTS_CONFIG 换成 zhparser
-- 之类的中文配置之后，查询用的是新配置，**这两条索引就用不上了**，退化成顺序
-- 扫描 —— 不报错，只是慢。换配置就照下面再建两条对应的索引。
CREATE INDEX IF NOT EXISTS idx_kb_doc_fts_simple_title
    ON kb_doc USING gin (to_tsvector('simple', title));

CREATE INDEX IF NOT EXISTS idx_kb_doc_fts_simple_body
    ON kb_doc USING gin (to_tsvector('simple', body));

-- 装了中文分词扩展之后照这两条建（配置名按扩展的实际名字改）：
--
--     CREATE EXTENSION zhparser;
--     CREATE TEXT SEARCH CONFIGURATION zhcfg (PARSER = zhparser);
--     ALTER TEXT SEARCH CONFIGURATION zhcfg ADD MAPPING FOR n,v,a,i,e,l WITH simple;
--     CREATE INDEX idx_kb_doc_fts_zh_title ON kb_doc USING gin (to_tsvector('zhcfg', title));
--     CREATE INDEX idx_kb_doc_fts_zh_body  ON kb_doc USING gin (to_tsvector('zhcfg', body));
--
-- 然后 export MAOS_PG_FTS_CONFIG=zhcfg。本层不用改一行代码。
-- **本机 pgvector/pgvector:pg16 没装 zhparser**，所以本机只有 simple，中文查询由
-- `PgStorePort.fts_search()` 抛 LookupError、检索器退化走本地通道 —— 这是如实的
-- 结果，不许说成「缺省支持中文分词检索」（跨轨契约 §J）。


-- ---------------------------------------------------------------------------
-- 向量通道：本期在 PG 上**没有**
-- ---------------------------------------------------------------------------
-- `kb_doc.embedding` 在 SQLite 侧是 TEXT（存 JSON 数组文本），翻到 PG 仍然是 TEXT
-- —— 这是「同构」的直接后果，也是刻意的：一份 DDL 两个后端，形状必须一样。
-- 代价是 pgvector 的 `<=>` 在这一列上走不通，`vector_search` 抛 LookupError、
-- 检索器退化成纯 Python 余弦（召回照常，只是不走索引）。
--
-- 要在 PG 上真用 HNSW，得给 PG 侧单开一列 `vector(64)` 并在写入侧双写 ——
-- 那就是两个后端形状分叉，不是本轨该拍的板。已记进 docs/BACKLOG.md `## task-t115`。
