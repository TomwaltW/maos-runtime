# 从 SQLite 迁到 PolarDB PostgreSQL 版

手册 Phase 5 第 3 步要的就是三步：**建库 → 装 pgvector 扩展 → 换连接串**。
下面先给这三步，再分「已实测 / 未实测 / 已知差异」三栏 —— **第二栏和第三栏比第一栏
重要**：这一页的价值在于说清哪些是跑过的、哪些只是推断，而不是把三步写得多漂亮。

> 🔴 **本页不含任何真实连接串、口令或主机名。** 一律写成
> `postgresql://<user>:<pass>@<host>:<port>/<db>` 这种占位形式。连接串只从环境变量
> `MAOS_PG_DSN` 读，禁止落进任何文件（铁律 6）。

---

## 三步迁移

### 第 1 步 · 建库

PolarDB PostgreSQL 版控制台建实例、建数据库、建账号，把实例的内网/外网地址、端口、
库名、账号口令记下来。本机对照物是 `deploy/docker-compose.yml` 里 `pg` profile 下的
pgvector 容器：

```bash
docker compose -f deploy/docker-compose.yml --profile pg up -d pgvector
```

### 第 2 步 · 装 pgvector 扩展

**每个数据库要单独建一次**，装了扩展不等于当前库能用：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

然后把 `maos/store/pg_schema.sql` 灌进去（建 F-2 形状的表、tsvector 的 GIN 索引、
向量的 HNSW 索引）：

```bash
psql "$MAOS_PG_DSN" -v ON_ERROR_STOP=1 -f maos/store/pg_schema.sql
```

漏了 `CREATE EXTENSION` 的症状是 `operator does not exist: vector <=> vector`，
看起来像 SQL 写错了，跟「扩展没装」完全是两个印象。`maos/store/pg_store.py` 会把它
翻成一条点名 `CREATE EXTENSION` 的 `LookupError`。

### 第 3 步 · 换连接串

```bash
export MAOS_PG_DSN='postgresql://<user>:<pass>@<host>:<port>/<db>'
```

**代码一行都不用改。** 可选的两个旋钮：

| 环境变量 | 缺省 | 作用 |
| :-- | :-- | :-- |
| `MAOS_PG_DSN` | 无 | 连接串。没配就抛，不回落 SQLite |
| `MAOS_PG_CONNECT_TIMEOUT` | `5` | 连接超时（秒）。连不上要快速响，别挂住调用方 |
| `MAOS_PG_FTS_CONFIG` | `simple` | 文本检索配置。装了中文分词扩展就指过去 |

驱动是**可选依赖**（核心零运行时依赖，`pyproject.toml` 的 `dependencies = []`）：

```bash
pip install "psycopg[binary]"
```

没装驱动 / 没配 DSN / 连不上，三种情况**都当场抛错，绝不静默回落 SQLite**。
回落的后果是「PG 后端看起来跑通了」而其实一行 PG 代码都没执行 —— 等真接 PG 那天，
所有以为验过的路径都得重验，且没有任何东西提示你该重验。

---

## 已实测

环境：本机 Docker `pgvector/pgvector:pg16`，2026-08-29。

```
PostgreSQL 16.15 (Debian 16.15-1.pgdg12+2) on aarch64-unknown-linux-gnu
extname  | extversion
---------+-----------
 plpgsql | 1.0
 vector  | 0.8.6
```

跑通的东西，逐条列：

| 项 | 实测结果 |
| :-- | :-- |
| 容器 healthcheck（`pg_isready`） | `healthy` |
| `CREATE EXTENSION vector` | `vector 0.8.6` |
| `maos/store/pg_schema.sql` 灌库 | `CREATE EXTENSION / CREATE TABLE / CREATE INDEX x2` 全成功 |
| GIN 索引落地 | `idx_kb_doc_pg_fts_simple gin (to_tsvector('simple'::regconfig, body))` |
| HNSW 索引落地 | `idx_kb_doc_pg_embedding hnsw (embedding vector_cosine_ops)` |
| `execute` / `query` 往返 | 一致，参数只绑不拼（注入串原样存回原样取出，表还在） |
| `fts_search` 走 `to_tsvector` / `ts_rank` | 真返结果，见下 |
| `vector_search` 走 pgvector `<=>` | 排序方向正确，见下 |
| 换后端不换语义 | 同一份数据，SQLite 与 PG 的 `query` 结果逐字节相同 |
| 契约甲 | 驱动缺失 / 连不上 → 抛 `PgBackendUnavailable`，不回落 |
| 测试 | `maos/tests/test_pg_store_live.py` **22 passed**（有库）/ **22 skipped**（无库） |
| 全量 | **824 passed**（有库）/ **802 passed, 22 skipped**（无库） |

全文通道实测（靶表 4 行，语料与 `test_store_port.py` 的 SQLite 侧同一份）：

```
fts_search('timeout')        -> [('d1', 0.06079271), ('d2', 0.06079271)]
fts_search('refund timeout') -> [('d1', 0.09910322)]     # 词间是 AND
```

向量通道实测 —— **排序方向正查反查各钉一次**：

```
vector_search([1,0,0]) -> [('d1', 1.0), ('d2', 0.9938837488013375), ('d3', 0.0)]
vector_search([0,1,0]) -> [('d3', 1.0), ('d2', 0.11043153221558755), ('d1', 0.0)]
```

pgvector 的 `<=>` 是余弦**距离**（越小越近），而 F-2 要求分数「越大越相关」，
适配器取 `1 - 距离`。取反了的症状是排序整个倒过来而**仍然有结果**，肉眼看不出来，
所以两个方向都测。

---

## 未实测

🔴 **PolarDB PostgreSQL 版的实例本身没有连过。** 本页「已实测」栏的全部输出都来自
本机 Docker 的 `pgvector/pgvector:pg16`。

兼容性依据是**推断，不是验证**，依据有两条：

1. PolarDB PostgreSQL 版兼容 PostgreSQL 协议与 SQL 语法，标准 libpq 驱动（psycopg）
   可以直连 —— 本层没有用任何 PG 私有特性，用到的只有 `to_tsvector` / `ts_rank` /
   `plainto_tsquery` 与 pgvector 的 `<=>`。
2. pgvector 在其可用扩展列表中。

下面这些**一条都没在 PolarDB 上验过**，迁过去第一天就该逐条过：

- 云上链路：`sslmode=require` / VPC 白名单 / 公网地址与内网地址的差别。
- 连接治理：连接池、PgBouncer、以及 PolarDB 的读写分离地址会不会把
  `CREATE EXTENSION` 这类 DDL 路由到只读节点。
- 托管实例的扩展白名单：`vector` 能不能建、能建到哪个版本；
  `zhparser` / `pg_jieba` 这类中文分词扩展**大概率装不了**（托管实例通常只允许
  白名单内的扩展）—— 这直接决定下面「中文全文」那条局限在 PolarDB 上能不能解。
- 数据量上来之后的 HNSW 参数（`m` / `ef_construction`）与索引构建耗时。
  本机靶表只有 4 行，索引建得上，但**没有任何性能结论**。
- 备份、主备切换期间连接断开后的重连行为（本层缓存连接，`connect()` 只在连接
  `closed` 时重建，主备切换的半开连接没测过）。

---

## 已知差异 / 局限

### 1. 🔴 中文全文检索：PG 不带中文分词

**实测事实**（不报错，所以只能靠测试记着）：

```
SELECT to_tsvector('simple', '退款政策超时未到账');
 -> '退款政策超时未到账':1
```

整串汉字是**一个 token**。查「退款政策」一条都命不中，**而且不报错**。这跟 SQLite
侧缺省 unicode61 的毛病是同一个（`maos/kb/schema.sql` 第 41 行起记着同一条坑），
区别是 SQLite 可以换 `tokenize='trigram'` 绕过去，PG 换不了 —— 内置的 29 个文本
检索配置一个都没有中文分词器。

**本轨的选择**：不假装它和 SQLite FTS5 一样。查询串含 CJK 字符、而当前配置是 PG
内置配置时，`fts_search` **抛 `LookupError`**，不返回空集：

```
LookupError: 查询串含中日韩字符，而当前文本检索配置是 PG 内置的 'simple' ——
内置配置一个都没有中文分词器，to_tsvector 会把整串汉字当成一个 token，
子串查询恒不命中**且不报错**。……修法：给库装 zhparser 或 pg_jieba，
再把 MAOS_PG_FTS_CONFIG 指向那个配置……
```

依据是 F-2 原话「**『后端没准备好』不许伪装成『没命中』**」。`maos/kb/retriever.py`
的 `_port_search` 捕获异常后把该通道判定为不可用、退化为本模块的本地实现，
所以**中文召回照常是好的，只是不经过 PG**。

⚠️ **连带后果，必须知道**：`_port_search` 是「探一次记一次」，一次 CJK 查询抛错就把
该 store 的全文通道**永久**标记为不可用，此后连英文查询也走本地实现。在本仓库这种
中文语料上，等于 PG 全文通道基本不会被用上。这是如实的结果，不是缺陷伪装 ——
把它写在这里，好过让人以为 PG 的 BM25 在替我们干活。

**升级路径是一个环境变量**，不用改代码：装好 `zhparser` / `pg_jieba`，
`export MAOS_PG_FTS_CONFIG=zhcfg`，本层立刻把 CJK 查询也交给 PG。
配套索引的建法见 `maos/store/pg_schema.sql` 的注释。
但见上一栏：**托管 PolarDB 能不能装这类扩展没验过**。

### 2. 占位符方言：`?` vs `%s`

SQLite 用 `?`，PG 用 `%s`。**本层不做自动翻译** —— `?` 同时是 PG 的 jsonb 算子，
字符串字面量里的 `?` 更不能动，机器改写迟早改错一条而且没有症状。换后端时调用方
自己改 SQL；传了参数却还写着 `?` 的话，本层抛一条说人话的 `ValueError`，
而不是让 psycopg 报一句语法错。

### 3. 分数不可跨后端比较：`ts_rank` 与 `bm25` 不是同一把尺子

`ts_rank` 缺省**不做文档长度归一**，`bm25` 做。同一条查询、同一份语料，实测：

| 后端 | `fts_search('timeout')` |
| :-- | :-- |
| PG（`ts_rank`） | `d1` 与 `d2` **同分 0.06079271**，并列后按 id 升序 |
| SQLite（`-bm25`） | `d2` 严格高于 `d1`（短文档得分更高） |

两边都满足 F-2 的「越大越相关、降序、同分按 id 升序」，但**具体名次可能不同**。
要长度归一就给 `ts_rank` 传 normalization 参数，那会改变现有排序，属于检索调优，
不在本轨范围（记进 `docs/BACKLOG.md` 的 `## task-T10`）。

**向量通道没有这个问题**：两边都是余弦相似度，实测分数逐位对得上（`1e-6` 内）。

### 4. 向量维度不匹配：PG 报不出是哪一行

SQLite 侧逐行比对，能点名 `id=d3` 那行；PG 侧由 pgvector 在查询层一次性报
`different vector dimensions 3 and 2`，**没有行号**。两边都抛 `ValueError`、
都不跳过那行，但排查成本不同 —— 换嵌入模型后要重算全部向量，别指望报错告诉你漏了谁。

### 5. 标识符大小写：PG 折成小写，SQLite 不折

本层沿用 SQLite 适配器的做法，校验形状后**不加引号**直接拼（加引号 `"KB_Doc"`
反而要求精确匹配，更容易踩）。所以表名/列名一律用小写，别用驼峰。

### 6. 表达式索引绑死了 FTS 配置

`pg_schema.sql` 里的 GIN 索引建在 `to_tsvector('simple', body)` 上。换了
`MAOS_PG_FTS_CONFIG` 之后查询用的是新配置，**这条索引就用不上了**，退化成顺序扫描
—— 不报错，只是慢。换配置就照 `pg_schema.sql` 的注释再建一条对应的索引。

### 7. `kb_doc` 的主键与 F-2 的 `id` 约定对不上

F-2 约定「源表主键，列名固定为 `id`」，而 `maos/kb/schema.sql` 的 `kb_doc` 主键是
`(tenant_id, doc_id)`。所以 `pg_schema.sql` 给的是**符合 F-2 的参考形状**
（`kb_doc_pg`），不是 `kb_doc` 的翻译版。知识层要接 PG 得先对齐这个口径 ——
那是另一轨的线，不在本轨范围。同一个错配在 SQLite 侧同样存在
（`retriever.py` 注释里写着两条通道都抛 `LookupError`），不是 PG 带来的新问题。
