-- PG 侧的建表与索引 DDL —— StorePort 两条检索通道在 PostgreSQL 上要的东西。
--
-- 怎么用（本机 Docker，实测见 deploy/polardb.md）：
--
--     docker compose -f deploy/docker-compose.yml --profile pg up -d pgvector
--     docker compose -f deploy/docker-compose.yml exec -T pgvector \
--         psql -U <user> -d <db> -f - < maos/store/pg_schema.sql
--
-- 本文件**只建新表**（铁律 2：现有表结构禁改，只许新增）。它也不是 SQLite 侧
-- maos/kb/schema.sql 的翻译版 —— 那张 kb_doc 的主键是 (tenant_id, doc_id)，
-- 而 F-2 约定「源表主键，列名固定为 id」，两边对不上。所以这里给的是**符合 F-2
-- 的参考形状**：知识层要接 PG 得先把这个口径对齐，那是 T13 轨的线，不在本轨。

-- pgvector 扩展。镜像自带扩展文件，但每个库要显式建一次 —— 不建的话 `<=>`
-- 报的是 "operator does not exist"，看起来像语法错，跟「扩展没装」是两个印象。
CREATE EXTENSION IF NOT EXISTS vector;


-- ---------------------------------------------------------------------------
-- F-2 形状的文档表
-- ---------------------------------------------------------------------------
-- id 必须叫 id：fts_search / vector_search 都固定 SELECT id，换个名字两条通道
-- 一起抛 UndefinedColumn（本层会把它翻成 LookupError，检索器据此退化为本地实现）。
--
-- embedding 的维度必须与写入侧一致。本仓库的本地嵌入是 maos/kb/retriever.py 的
-- EMBED_DIM = 64；换嵌入模型就要改这里并**重算全部向量** —— pgvector 在查询层
-- 一次性报 "different vector dimensions"，报不出是哪一行，比 SQLite 侧难查。
CREATE TABLE IF NOT EXISTS kb_doc_pg (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    embedding  vector(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- 全文通道：tsvector + GIN
-- ---------------------------------------------------------------------------
-- 表达式索引里的配置**必须写死成字面量**（这里是 'simple'）：to_tsvector 的单参
-- 形式依赖会话的 default_text_search_config，是 STABLE 不是 IMMUTABLE，建不了索引。
-- 双参形式才是 IMMUTABLE。
--
-- 连带约束：索引只对建索引时那个配置有效。把 MAOS_PG_FTS_CONFIG 换成 zhparser
-- 之类的中文配置之后，查询用的是新配置，**这条索引就用不上了**，退化成顺序扫描 ——
-- 不报错，只是慢。换配置就照下面再建一条对应的索引。
CREATE INDEX IF NOT EXISTS idx_kb_doc_pg_fts_simple
    ON kb_doc_pg USING gin (to_tsvector('simple', body));

-- 装了中文分词扩展之后照这条建（配置名按扩展的实际名字改）：
--
--     CREATE EXTENSION zhparser;
--     CREATE TEXT SEARCH CONFIGURATION zhcfg (PARSER = zhparser);
--     ALTER TEXT SEARCH CONFIGURATION zhcfg ADD MAPPING FOR n,v,a,i,e,l WITH simple;
--     CREATE INDEX idx_kb_doc_pg_fts_zh ON kb_doc_pg USING gin (to_tsvector('zhcfg', body));
--
-- 然后 export MAOS_PG_FTS_CONFIG=zhcfg。本层不用改一行代码。


-- ---------------------------------------------------------------------------
-- 向量通道：HNSW + 余弦
-- ---------------------------------------------------------------------------
-- 算子类必须与查询用的算子对上：查询走 `<=>`（余弦距离）就得用 vector_cosine_ops。
-- 配错的症状是索引建得上、查询也返得回结果，只是**索引根本没被用**（顺序扫描），
-- 库小的时候完全看不出来。
--
-- 维度 <= 2000 才建得了 HNSW；本仓库 64 维，够用。数据量再大时调 m / ef_construction。
CREATE INDEX IF NOT EXISTS idx_kb_doc_pg_embedding
    ON kb_doc_pg USING hnsw (embedding vector_cosine_ops);
