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

> ✅ **本机走 compose 的话这一步不用手动做**（2026-08-31 起）：`deploy/docker-compose.yml`
> 的 `pgvector` 服务挂了 `deploy/pg-initdb/`，首次初始化数据目录时自动执行这条 DDL。
> 实测起完容器直接查 `SELECT extversion FROM pg_extension WHERE extname='vector'`
> → **`0.8.6`**，全程没跑任何手动 DDL。
>
> 🔴 **只在 `pgdata` 卷为空时执行**（官方 postgres 镜像的行为）。卷里已有数据就
> **安静跳过、不报错** —— 「改了 initdb 脚本却没生效」是这套机制最常见的坑。
> 要让它重跑：`docker compose --profile pg down -v`（`-v` 才会删卷）再 `up`。

🔴 **托管实例上这一步要用高权限账号**（本机 Docker 不会遇到，因为容器里的账号就是
superuser）。PolarDB 实测：控制台建的普通账号执行这条会得到

```
permission denied to create extension "vector"
HINT:  Must be superuser or user with all of polar_superuser to create this extension.
```

而且它**在 `public` 里也建不了表**（PG15 起 `public` 只给普通用户 `USAGE`）。所以托管实例
上要先用高权限账号跑两条，跑完之后全程用普通账号即可：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
GRANT CREATE ON SCHEMA public TO <普通账号>;
```

细节与实录见 `deploy/polardb-live.md` §1.3。

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

### 追加：真 PolarDB 实例上的复测（2026-08-30）

上面那一栏跑在本机 Docker 上。同一份代码在**阿里云 PolarDB PostgreSQL 版**实例上又跑了一遍：

| 项 | 实测结果 |
| :-- | :-- |
| 实例 | `PostgreSQL 16.14 (PolarDB 16.14.20.0 build 1f03f15d)` on x86_64-linux-gnu |
| `CREATE EXTENSION vector` | `vector 0.8.3.1`（需高权限账号，见上面第 2 步） |
| `pg_schema.sql` 灌库 | `CREATE EXTENSION / CREATE TABLE / CREATE INDEX x2` 全成功 |
| GIN / HNSW 索引落地 | `gin (to_tsvector('simple'::regconfig, body))`、`hnsw (embedding vector_cosine_ops)` |
| `test_pg_store_live.py` + `test_kb_pg_channel.py` | **33 passed** |
| 全量 | **1098 passed, 10 skipped**（有库）/ **1069 passed, 39 skipped**（无库）。差的 **29 条全是 DSN 门控**（22 条 `test_pg_store_live.py` + 7 条 `test_pg_rank_parity.py`）。2026-08-31 实测 |
| 地基冒烟五步 | 5/5，且每个数值与本机 pgvector **逐字节相同** |

🔴 **跑全量前先起 Docker**，并确保镜像在：
`docker build -t maos-sandbox -f deploy/sandbox.Dockerfile .`。
没有它，`test_verify_warn` 的两条会红 —— 沙箱容器隔离（`--network none` / `--read-only` /
`--user 1000:1000`）会**静默降级**，test_report 照样产出、只在 `verify.py` 的 warn 里留一行。
那不是回归，但**降级跑出来的证据在隔离性这一维是空的**，别拿它当交付证据。

有库档那 10 条 skip 是 RocketMQ / Nacos 的门控，**不是 PG 的**。判据是
「`SKIPPED` 行里不许出现 `test_pg_`」，不是「skipped 必须为 0」——
写死数字会随下一组门控测试作废，要守的不变量始终是「PG 那 29 条一条都不许被饿死」。

逐条对照与差异分析在 `deploy/polardb-live.md`，那份文档只管数据库这一侧。

### 追加二轮：中文分词与向量索引（2026-08-30）

| 项 | 实测结果 |
| :-- | :-- |
| `CREATE EXTENSION zhparser` | ✅ **装得上**，`zhparser 2.2`。建 `zhcfg` 配置后中文真的切开（`退款/政策/超时/到/账`） |
| 中文召回（24 条真语料，6 条查询） | `zhcfg` 全文 **8/10**；向量 top-5 **10/10**；`simple` 配置下**一条都查不了**（全部抛 `LookupError`） |
| `MAOS_PG_FTS_CONFIG=zhcfg` 是否要改代码 | ✅ **不用**。库代码一行没改，CJK 查询立刻走 PG。唯一变红的是那条断言「CJK 必须抛错」的测试 —— 它写死了「配置一定是内置的」这个前提，账记在 `docs/BACKLOG.md ## polardb-live-r2` |
| HNSW 查询性能（20 万行） | 顺序扫描 p50 **72.5 ms** → HNSW `ef_search=40` p50 **0.62 ms**，**117 倍**，召回 99.3% |
| HNSW 延迟随规模 | 数据 4 倍（5 万→20 万），HNSW p50 几乎不动（0.58→0.62 ms）；顺序扫描线性涨（16.1→72.5 ms） |
| HNSW 构建耗时 | 5 万行 8.7 s → 20 万行 **77.9 s**（**超线性**：数据 4 倍，耗时 9 倍） |
| HNSW 索引体积 | 20 万行 **109 MB**，堆表 123 MB —— 接近 1:1 |

🔴 后两行的数据是**合成的**（真语料句子重组 + 真实 `embed()`），真语料只有 24 条，
撑不起规模测试。完整表格、方法与一条「低 `ef_search` 召回随构建波动」的教训
见 `deploy/polardb-live.md` §1.4 / §1.5 / §3.6。

---

## 未实测

~~PolarDB PostgreSQL 版的实例本身没有连过。~~ **2026-08-30 已连过并跑通**，
所以这一栏收窄成两半：先是当初列的清单里**已经验掉的那几条**（连带一条推断被推翻），
然后是**仍然没验的**。

### 当初列的清单里，已经验掉的

| 当初的疑问 | 实测结论 |
| :-- | :-- |
| 兼容性只是推断 | ✅ 变成验证：协议兼容、psycopg 直连、`to_tsvector` / `ts_rank` / `<=>` 全部按预期工作 |
| `vector` 能不能建、建到哪版 | ✅ 能，**0.8.3.1**；不需要在控制台启用什么，但**必须高权限账号** |
| 读写分离地址会不会把 DDL 路由到只读节点 | ✅ 不会。走 `rwlb` 地址时 `pg_is_in_recovery() = false`，`CREATE EXTENSION` 与建索引都实际生效 |
| 公网地址 / 白名单 | ✅ 公网地址 + IP 白名单实测可连（白名单不放行时的症状是 **TCP 静默超时**，不是拒绝，容易误判成网络故障） |
| `sslmode=require` | ✅ 可用。**该实例支持 SSL，2026-08-31 已在控制台开启**（`SHOW ssl` = `on`，`ssl_in_use = True`）。开启前的实测确为 `off`、`require` 连不上，那段明文期的实录与**口令未轮换**这个残留风险见 `polardb-live.md` §3.5 |

🔴 **一条推断被实测推翻，要点名改掉**：原文写「`zhparser` / `pg_jieba` 这类中文分词扩展
**大概率装不了**（托管实例通常只允许白名单内的扩展）」。实测该实例共 **189** 个可用扩展，
`zhparser 2.2`、`pg_jieba 1.1.2`、`pg_bigm 1.2`、`pgroonga 4.0.5` **四个都在可用列表里**。
所以下面「中文全文」那条局限，在 PolarDB 上**有解的可能**——
但「在可用列表里」离「装上了」再离「中文召回是好的」还有两步，**这两步都没做**。

### 仍然没验的

- **连接池 / PgBouncer / 并发**：全程单连接，连接治理一条没测。
- **内网地址**：只连过公网地址。生产要走 VPC 内网（更快更省更安全），那条链路没验。
- 备份、主备切换期间连接断开后的重连行为（本层缓存连接，`connect()` 只在连接
  `closed` 时重建，主备切换的半开连接没测过）。
- HNSW 的**调参**：`m` / `ef_construction` 全程用 pgvector 缺省值（16 / 64），没调过。
- **并发下**的向量检索性能：§1.5 那些数字全是单连接单查询。

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

这条路径此前挂着一个前提问号「托管 PolarDB 能不能装这类扩展」，**现在答完了**：
`zhparser 2.2` 已在该实例上**实际安装并实测**，建 `zhcfg` 配置后中文真的切开，
`MAOS_PG_FTS_CONFIG=zhcfg` 之后本层**一行代码没改**，CJK 查询立刻走 PG，
24 条真语料上全文召回 8/10（漏的两条是 `plainto_tsquery` 的 AND 语义，不是分词的锅）。
详见 `deploy/polardb-live.md` §1.4。

⚠️ 一处连带：`maos/tests/test_pg_store_live.py::test_chinese_query_raises_instead_of_silently_missing`
断言「CJK 查询必抛 `LookupError`」，它写死了「配置一定是 PG 内置的」这个前提，
所以在 `MAOS_PG_FTS_CONFIG=zhcfg` 下会红（39 passed, 1 failed）。**库代码没问题，
是这条测试没有跟着配置走**，账记在 `docs/BACKLOG.md ## polardb-live-r2`。

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
