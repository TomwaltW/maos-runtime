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
-- 🔴 **T139 起它承担检索了**，注意这与本段以前的说法相反。
--
-- 死的仍然是 `retriever._local_fts_rows()` 那条 `bm25()` + `MATCH`（FTS5 专有语法，
-- 在 PG 上必然失败）。变的是 `PgStorePort.fts_search()`：它查的 tsvector 现在取自
-- **本表的 title / body**，不再取自 `kb_doc` 的原文列。
--
-- 为什么要换（BACKLOG `## task-t132` 第 1 条，「别只改一边」）：`kb.fts_text()` 的
-- `_TOKEN_RE` 把 `ACQ.TRADE_NOT_EXIST` 切成 `acq trade not exist` 四个 token，而
-- `to_tsvector('simple', body)` 对同一串走的是默认 parser 的 file/host 规则，整串
-- 是**一个** token `acq.trade_not_exist`。于是查 `acq` 恒不命中**且不报错** ——
-- 退款域的知识条目里错误码是主键式的线索，这条通道对它是全瞎的。
--
-- 本表的 title / body 存的**正是 `kb.fts_text()` 的产物**（`upsert_doc` 的第三条
-- 语句），所以把索引与查询都挪到本表，两边就都是那一个函数的口径了 —— 切词仍然
-- 只在 Python 里做一次，PG 只负责匹配。这也让两个后端**更**同构：SQLite 的 FTS5
-- 本来就建在本表上，现在 PG 的 tsvector 也是。
--
-- 备选方案是在 PG 的索引表达式里用 regexp 重写一遍 `_TOKEN_RE`（英数按词切、汉字
-- 按字切）。没选它：那是把切词规则抄成第三份，而这一类漂移的症状恰恰是「召回悄悄
-- 变少、日志一片正常」—— `kb.fts_text` 的 docstring 警告的就是这件事。
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
-- 全文通道：tsvector + GIN
-- ---------------------------------------------------------------------------
-- 每个目标两列各一条，与 `retriever.PORT_FTS_FIELDS = ("title", "body")` 一一对应：
-- F-2 的 `fts_search(table, field, q, limit)` 一次只认一列，检索器每列各问一次，
-- 只给 body 建索引的话标题那一次就退化成顺序扫描（不报错，只是慢）。
--
-- 表达式索引里的配置**必须写死成字面量**（这里是 'simple'）：to_tsvector 的单参
-- 形式依赖会话的 default_text_search_config，是 STABLE 不是 IMMUTABLE，建不了索引。
-- 双参形式才是 IMMUTABLE。
--
-- 连带约束：索引只对建索引时那个配置有效。把 MAOS_PG_FTS_CONFIG 换成 zhparser
-- 之类的中文配置之后，查询用的是新配置，**这几条索引就用不上了**，退化成顺序
-- 扫描 —— 不报错，只是慢。换配置就照下面再建对应的索引。

-- 🔴 检索**实际走的是这两条**（建在影子表上，见上面 `kb_doc_fts` 那一节的红字）。
CREATE INDEX IF NOT EXISTS idx_kb_doc_fts_shadow_title
    ON kb_doc_fts USING gin (to_tsvector('simple', title));

CREATE INDEX IF NOT EXISTS idx_kb_doc_fts_shadow_body
    ON kb_doc_fts USING gin (to_tsvector('simple', body));

-- 下面这两条建在 `kb_doc` 原文列上，是 T139 之前那条路留下的。
-- **今天没有调用方**：`fts_search("kb_doc", ...)` 已改查影子表。留着是因为跨轨契约
-- 只许新增索引、不许删（删了在别人的库上是不可逆的），而且 `pg_store` 的影子表映射
-- 有回落分支 —— 影子表不在的旧库上仍然会查到 `kb_doc`，那时这两条还得上。
-- 真要收掉，等影子表这条路在 PolarDB 上也跑过一轮之后单独一轨，别顺手删。
CREATE INDEX IF NOT EXISTS idx_kb_doc_fts_simple_title
    ON kb_doc USING gin (to_tsvector('simple', title));

CREATE INDEX IF NOT EXISTS idx_kb_doc_fts_simple_body
    ON kb_doc USING gin (to_tsvector('simple', body));

-- 装了中文分词扩展之后照这几条建（配置名按扩展的实际名字改）。**索引目标是影子表**
-- `kb_doc_fts`，与上面那两条 shadow 索引同一张表 —— 检索查哪张表，索引就建哪张表：
--
--     CREATE EXTENSION zhparser;
--     CREATE TEXT SEARCH CONFIGURATION zhcfg (PARSER = zhparser);
--     ALTER TEXT SEARCH CONFIGURATION zhcfg ADD MAPPING FOR n,v,a,i,e,l WITH simple;
--     CREATE INDEX idx_kb_doc_fts_zh_title ON kb_doc_fts USING gin (to_tsvector('zhcfg', title));
--     CREATE INDEX idx_kb_doc_fts_zh_body  ON kb_doc_fts USING gin (to_tsvector('zhcfg', body));
--
-- 然后 export MAOS_PG_FTS_CONFIG=zhcfg。本层不用改一行代码。
--
-- 切过去会动到哪条判据（T139 把中文分成两档，T142 把第二档搬上装配路径并补了一条
-- 本机就能真跑的链路档）：
--
--   1. `MAOS_PG_FTS_CONFIG` 指向 PG 内置配置（本机今天的 `simple`）
--      → `test_pg_store_live.py::test_chinese_query_raises_on_builtin_config`：
--        中文查询**必须抛 LookupError**，不许静默漏。这一档不依赖装配路径，留在原处。
--   2. 指向非内置配置（真跑日的 `zhcfg` / `jiebacfg`）。判据在
--      `maos/tests/test_pg_vector_channel_t139.py` 的「4. 中文第二档」，两条：
--      · `test_second_tier_chinese_recalls_through_the_assembly_path`
--        —— **本机每次都真跑**。它在本机现造一个 `COPY = simple` 的非内置配置，
--        把第二档链路（不抛 → `kb.tokenize()` → `to_tsquery` → 影子表 → 召回）整条走通，
--        并连带钉住「错误码通道不因此变瞎」。
--      · `test_chinese_query_recalls_on_real_tokenizer` —— 真分词器那一档，本机 skip。
--        它唯一还没验的是「zhparser 对单字序列出不出词」。
--
--      🔴 T142 之前这两条合成一条，且断言建在**原文靶表**上（`t10_live_doc`），
--      而查询侧一律再过一遍 `kb.tokenize()` —— 索引侧出词、查询侧出字，**恒 0 命中**。
--      本机双重门控所以 skip、不显形，真跑日一切过去就是假红。别把它搬回原文靶表。
--
-- 切之前先知道这件事：影子表里存的是 `kb.fts_text()` 的产物，中文在那一步已经
-- **按字切开并用空格分隔**了。所以 zhparser 在这条路上拿到的是 "退 款 政 策"
-- 而不是 "退款政策"，它的词典分词能力**发挥不出来**，实际效果是「按字 AND」。
-- 这比 simple 档的「整串一个 token、恒不命中」强（中文真的召得回来了），但**不是**
-- 真·中文分词检索 —— 所以那句「本仓库缺省支持中文分词检索」在任何一档上都仍然
-- 不许说（跨轨契约 §J，`docs/submission-checklist.md:224` 那行口径没松）。
--
-- 要让 zhparser 真按词切，得让它看到原文，也就是把 zhcfg 的索引建回 `kb_doc` 的
-- title/body，并且查询侧**不过** `kb.fts_text()`。那是第三种形状，会把错误码那条
-- 通道重新打瞎（原文上 `ACQ.TRADE_NOT_EXIST` 又变回一个 token）。两者不可兼得。
--
-- 🔴 **T142 已经把这个取舍定死：选 A，保留影子表口径**（`docs/DECISIONS.md` 的
-- `## task-t142`，`deploy/polardb.md` 那一节同一口径，三处说法一致）。理由一句话：
-- 错误码是退款域知识条目的主键式线索、**是今天生产路径上真在用的一条通道**，而中文
-- 在影子表上**召得回来**（代价只是排序无意义）。拿一条正在用的通道去换另一条通道的
-- 排序质量，不划算。
--
-- 所以**不要**把 zhcfg 的索引建到 `kb_doc` 原文列上 —— 上面那段安装步骤里的索引
-- 目标是 `kb_doc_fts`，不是笔误。真跑日第二档要是 0 命中，处置是把
-- `MAOS_PG_FTS_CONFIG` 退回 `simple`（口径退回第一档，中文照旧走本地退化），
-- **不是**在现场改索引形状。判据的 docstring 里写着同一句话。
--
-- **本机 pgvector/pgvector:pg16 没装 zhparser**，所以本机只有 simple，中文查询由
-- `PgStorePort.fts_search()` 抛 LookupError、检索器退化走本地通道 —— 这是如实的
-- 结果，不许说成「缺省支持中文分词检索」（跨轨契约 §J）。


-- ---------------------------------------------------------------------------
-- 向量通道：PG 侧多一列 `embedding_vec`（T139 拍板的形状分叉）
-- ---------------------------------------------------------------------------
-- `kb_doc.embedding` 在 SQLite 侧是 TEXT（存 JSON 数组文本），翻到 PG 仍然是 TEXT
-- —— 这是「同构」的直接后果，也是刻意的：一份 DDL 两个后端，形状必须一样。
-- 代价是 pgvector 的 `<=>` 在这一列上走不通：到 T139 之前 `vector_search` 抛
-- LookupError、检索器退化成纯 Python 余弦（召回照常，只是不走索引，也不走 pgvector）。
--
-- T139 给 PG 侧单开一列。**它是加速列，不是权威列**，这条决定了下面每一行的形状：
--
-- · 权威仍然是 `embedding` 那一列 TEXT。写入口只有 `kb.upsert_doc()` 一个，它写的
--   还是 TEXT，一个字没改；SQLite 侧也一个字没改。
-- · 新列是 **`GENERATED ALWAYS AS (...) STORED` 生成列**，不是普通列 + 触发器，
--   也不是靠回填脚本。理由是「同一次写入派生」这件事只有生成列能由 PG 自己保证：
--   触发器要有人记得建，回填要有人记得跑，而 `upsert_doc` 走的是 `ON CONFLICT
--   DO UPDATE`，覆盖写时**不碰**不在插入列里的列 —— 回填方案会在覆盖写之后留下
--   一条陈旧向量，而且不报错。生成列没有这个缝。
-- · 派生失败一律回 NULL，**绝不拒绝写入**。`kb_doc.embedding` 是 TEXT，历史上什么
--   都塞得进去（解析不了的串、换了嵌入模型之后维度对不上的向量）。让加速列把这些
--   写入挡回去，等于把一条「加速」的路改成了一道闸 —— 那是比没有 HNSW 严重得多的
--   回归。所以 cast 包在 `EXCEPTION WHEN others THEN RETURN NULL` 里，坏数据只是
--   没有加速向量，TEXT 那列照常落库，检索照常由纯 Python 余弦兜住。
-- · 整段包在一条 `DO` 里，且**整段再套一层 EXCEPTION**：`CREATE EXTENSION vector`
--   要高权限账号，云上普通账号建不了（`deploy/polardb-live.md` §1.3）。那种实例上
--   `vector` 类型根本不存在，这几句必然失败 —— 失败只许 RAISE WARNING，不许把
--   `ensure_schema()` 打死。打死的后果是整个知识层在那种实例上起不来，
--   而它本来只是「没有 HNSW」而已。回落路径见 `pg_store.vector_search()`。
-- · 用 `DO` 而不是裸 DDL 还有第二个理由：`ensure_schema()` 把本文件的每条语句都
--   递给 `PgStorePort.execute()`，方言开关开着时它们要过 `_dbport.to_pg_ddl()`，
--   而那个翻译器只认 CREATE TABLE / CREATE INDEX / ALTER TABLE ADD COLUMN 那个
--   子集，见到 `CREATE FUNCTION` 会抛 `UnsupportedDdlError`。`DO` 不以
--   CREATE/ALTER/DROP 开头，`is_ddl()` 判 False，整条原样发出去（实测逐字不变）。
--
-- ⚠️ `@EMBED_DIM@` 是占位符，**不是 SQL**。维度的唯一事实源是
--    `maos/kb/retriever.py::EMBED_DIM`，由 `pg_store.rendered_schema_sql()` 在读取
--    本文件时替换。这里写死一个 64 的后果是：改了 EMBED_DIM 之后 Python 侧算 128 维、
--    PG 侧的列还是 vector(64)，每一行的 cast 都失败回 NULL，于是**整条向量通道静默
--    退回纯 Python 余弦** —— 不报错、不变慢、召回只是悄悄不走索引了。
--    手跑本文件时不能直接 `psql -f`，要先渲染：
--
--        python3 -c "from maos.store.pg_store import rendered_schema_sql as r; print(r())" \
--            | psql -U <user> -d <db> -f -
--
--    忘了渲染的话 `@EMBED_DIM@` 是语法错，当场炸 —— 这是有意的，好过静默建错维度。
DO '
BEGIN
    CREATE OR REPLACE FUNCTION kb_embedding_vec(text) RETURNS vector
        LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS ''
        BEGIN
            RETURN $1::vector(@EMBED_DIM@);
        EXCEPTION WHEN others THEN
            RETURN NULL;
        END'';
    ALTER TABLE kb_doc ADD COLUMN IF NOT EXISTS embedding_vec vector(@EMBED_DIM@)
        GENERATED ALWAYS AS (kb_embedding_vec(embedding)) STORED;
    CREATE INDEX IF NOT EXISTS idx_kb_doc_embedding_hnsw
        ON kb_doc USING hnsw (embedding_vec vector_cosine_ops);
EXCEPTION WHEN others THEN
    RAISE WARNING USING MESSAGE =
        ''kb_doc 的向量加速列 embedding_vec 建不起来，向量通道回落到 TEXT 列''
        '' → LookupError → 检索器走纯 Python 余弦（召回照常，只是不走 HNSW）。原因：''
        || SQLERRM;
END' LANGUAGE plpgsql;
