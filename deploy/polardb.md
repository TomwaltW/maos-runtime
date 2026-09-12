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
向量的加速列与 HNSW 索引）。🔴 **T139 起不能直接 `-f` 那个文件**，要先渲染：

```bash
python3 -c "from maos.store.pg_store import rendered_schema_sql as r; print(r())" \
    | psql "$MAOS_PG_DSN" -v ON_ERROR_STOP=1 -f -
```

文件里的向量列写的是 `vector(@EMBED_DIM@)`，占位符由 `rendered_schema_sql()` 替换成
`maos/kb/retriever.py::EMBED_DIM`。SQL 里再写一个字面量 64 的后果是静默分叉：改了
`EMBED_DIM` 之后 Python 侧算 128 维、PG 侧的列还是 64，每一行的 cast 都失败回 NULL，
**整条向量通道悄悄退回纯 Python 余弦** —— 不报错、不变慢，只是不走索引了。
忘了渲染直接 `-f` 的话 `@EMBED_DIM@` 是语法错，当场炸 —— 有意的，好过静默建错维度。

> 平时不用手跑：`maos/kb/__init__.py::ensure_schema()` 在 PG 后端上会把本文件里除
> `CREATE EXTENSION` 之外的语句一并执行（走的就是 `rendered_schema_sql()`）。

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

## 退款域上 PolarDB —— 三步（T115）

上面那三步搬的是**地基**（`StorePort` 的两条检索通道）。退款那 16 张业务表原本
硬绑在 SQLite 上：`objects._conn()` 直接取 `SqliteStore._conn`，取不到就抛
`TypeError`。这一节讲怎么把**业务纵切**也搬过去。

> **地基通了 ≠ 业务域跑得起来。** 中间隔着三样东西：CHECK 约束落没落、
> 受理幂等是不是真幂等、以及「回执与终态同事务」这条铁律 8 的落点成不成立。
> 前两步各自能全绿而这三样全错，且都不报错。

### 第 1 步 · 换后端开关

```bash
export MAOS_DOMAIN_BACKEND=postgres
export MAOS_PG_DSN='postgresql://<user>:<pass>@<host>:<port>/<db>'
```

缺省是 `sqlite`，行为与 T115 之前**逐字节一致**。配成 `postgres` 却拿不到库
（没装驱动 / 没配 DSN / 连不上），一律抛 `PgBackendUnavailable`，**不回落 sqlite**。

拼错一个字母也抛（只认 `sqlite` / `postgres` 两个字面量）—— 静默跑 sqlite
比报错难查一个量级：你会以为在验 PG，其实一行 PG 代码都没执行。

### 第 2 步 · 建表（**不要手抄 DDL**）

```python
from maos.domain.refund import objects
objects.ensure_schema(store)          # 16 张表 + 索引，幂等，可连跑
```

DDL 只有一份，就是 SQLite 方言的 `maos/domain/refund/schema.sql`；PG 侧由
`maos/domain/_dbport.py::to_pg_ddl()` **现翻**。

**不要在 PG 那边另存一份手抄的 DDL。** 往这个域加表加列的轨不止一条，手抄的那份
一落地就漂，而漂了只在配了 PG 的机器上、只在跑到那张表时才炸
（`relation does not exist`）。翻译器认不出来的构造会当场抛 `UnsupportedDdlError`
并带上原文那一行 —— 那才是「该往哪儿加」的信号。它认的子集与边界见
`maos/tests/test_ddl_translate.py`（44 条，不需要 PG，进缺省全量）。

知识层的 `kb_doc` 同理：

```bash
export MAOS_STORE_BACKEND=postgres
export MAOS_PG_SQLITE_DIALECT=1       # 让 PgStorePort 收 SQLite 方言的 SQL
```
```python
from maos import kb
from maos.store import create_store
kb.ensure_schema(create_store())      # kb_doc + kb_schema_version + PG 侧那几条
```

### 第 3 步 · 验

```bash
python3 scripts/polardb_smoke.py --local        # 六步，第 6 步是退款域 case 往返
MAOS_PG_DSN=... python3 -m pytest maos/tests -q -k pg
```

冒烟脚本**不 import maos**（一台只有 psycopg、没有本仓库的机器上也能跑），
所以第 6 步用的是带 `maos_smoke_` 前缀的靶表，不碰真的 `refund_case`。

### ⚠️ 持久库上连跑演示场景要先清场

`maos/flows/scenario_6.py` 的 `case_id` / `order_id` 是**写死的**（`case-s6-0001`），
因为「连跑两次输出逐条一致」是它的验收之一。SQLite 那边每次都是 `:memory:` 新库，
所以看不出问题；PolarDB 是持久库，第二次跑会撞上第一次留下的那条 case ——
它已经是 `settled`，而 `submitted -> approved` 从终态迁不过去，于是场景 6 报
`BizStatusTransitionError`。

这不是缺陷，是「演示场景用固定 id」与「持久库」两件事的正常结果。连跑前清一次即可：

```sql
DELETE FROM payment_observation WHERE tenant_id = 'tnt-mfg-001';
DELETE FROM refund_case         WHERE tenant_id = 'tnt-mfg-001';
```

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

#### 🔴 T139：中文分成两档，各有判据（真跑日照这个走）

上面那条「测试没跟着配置走」的账，T139 结掉了。原来只有一条守卫，钉的是「中文必须
抛 `LookupError`」—— 对本机是对的，但 `MAOS_PG_FTS_CONFIG=zhcfg` 一切过去它就变红，
**真跑日当天撞红线 = 当场没法决定「该切还是不该切」**。现在是两条：

| 档 | 判据 | 本机 |
|---|---|---|
| `MAOS_PG_FTS_CONFIG` 指向 PG 内置配置（`simple`） | `test_pg_store_live.py::test_chinese_query_raises_on_builtin_config`：中文查询**必须抛 `LookupError`**，不许静默漏 | 跑，绿 |
| 指向**非内置**配置（链路档） | `test_pg_vector_channel_t139.py::test_second_tier_chinese_recalls_through_the_assembly_path`：中文**必须真召回**，且错误码通道不许因此变瞎 | **跑，绿**（T142 起） |
| 指向真分词器（`zhcfg` / `jiebacfg`） | `test_pg_vector_channel_t139.py::test_chinese_query_recalls_on_real_tokenizer`：中文查询**必须真召回** | **skip**（没装 zhparser） |

#### 🔴 T142：第二档搬上装配路径，并拆成「链路」与「分词」两半

T139 那条第二档判据有两个毛病，T142 一起结掉：

1. **它建在原文靶表上，真跑日会假红。** 原来断言打的是 `test_pg_store_live.py` 里手建
   的 `t10_live_doc`（body 存原文），而 `fts_search()` 内部一律把查询串再过一遍
   `kb.tokenize()` —— 按上表的 zhcfg 建法（索引建在影子表）是**索引侧出词、查询侧出
   字，恒 0 命中**。本机双重门控所以 skip、不显形。现在它搬到了**生产装配路径**上
   （`kb.ensure_schema()` 建表 + `kb.upsert_doc()` 灌语料）。
2. **它在本机永远不执行断言。** 现在中间多了一档「链路档」：本机现造一个
   `COPY = simple` 的**非内置**配置，于是 `fts_search()` 不再走第一档那条
   `LookupError`，第二档整条链路（不抛 → 切词 → `to_tsquery` → 影子表 → 召回）
   **每次跑测试都真走一遍**，断言一个字没弱化。

于是真跑日那条一旦红，病根只剩**唯一一个**：zhparser 在「单字序列」上不出词
（影子表里中文已被 `kb.fts_text()` 按字切开，它拿到的是 `退 款 政 策`）。判据红时会
就地把 `to_tsvector` / `to_tsquery` 的实际产物打出来，省掉现场那一轮排查。

**当场处置**（照这个走，不要临场发挥）：把 `MAOS_PG_FTS_CONFIG` 退回 `simple`，
口径退回第一档 —— 中文照旧抛 `LookupError`、检索器退化走本地实现，召回照常，只是
不走 PG。**不要在现场改判据，也不要改索引形状。**

> ⚠️ 原来这里写的是「红了就知道不该切」。**那句话给的是错误结论**，T142 已删：
> 红了说明的是「这个形状下 zhcfg 没增益」，不是「影子表这条路选错了」——
> 切不切是形状问题，而形状已经定了（见下）。

⚠️ 切之前必须知道的形状约束（T139 换了全文的查询目标，见下面 §6）：全文现在查影子表
`kb_doc_fts`，里面存的是 `kb.fts_text()` 的产物，**中文在那一步已经按字切开**了。所以
zhparser 在这条路上拿到的是 `退 款 政 策` 而不是 `退款政策`，它的词典分词能力**发挥
不出来**，实际效果是「按字 AND」。这比 `simple` 档的「恒不命中」强（中文真的召得回
来），但**不是**真·中文分词检索。

两个连带结论，都要记住：

1. **那句口径没松。** `docs/submission-checklist.md:224` 的「本仓库缺省支持中文分词
   检索」在**任何一档上都仍然不许说**（跨轨契约 §J）。本机走 `simple`，中文通道是抛
   错退化的；真跑日走 `zhcfg`，也只是按字 AND。
2. **真跑日第二档要是 0 命中**，最可能的原因是 zhparser 把单字 token 过滤掉了
   （它有 `zhparser.punctuation_ignore` 一类的开关）。那时的选择是「保持 `simple`、
   中文照旧走本地退化」，**不要在现场改判据**。

#### 🔴 形状取舍已定：**A 影子表口径**（T142）

两条路，不可兼得，T139 摆出来但没拍板，**T142 拍了**：

| | A · 影子表口径（**选它**） | B · zhcfg 建回原文列 |
|---|---|---|
| zhcfg 索引目标 | `kb_doc_fts`（存 `kb.fts_text()` 的产物） | `kb_doc` 的 title/body 原文 |
| 查询侧 | 过 `kb.fts_text()`（与索引侧同一个函数） | **不过** `fts_text()` |
| 中文 | 召得回来，但实际是**按字 AND**（排序无意义） | zhparser 真按词切 |
| 错误码（`ACQ.TRADE_NOT_EXIST`） | **通**（切成四个 token） | **瞎**（原文上黏成一个 token） |
| 改动面 | 零（今天就是这个形状） | 索引 + 查询侧 + 一条现有判据当场红 |

**选 A 的理由一句话**：错误码是退款域知识条目的主键式线索，**是今天生产路径上真在用
的一条通道**（判据 `test_error_code_is_searchable_after_the_shadow_table_switch`）；
而中文在 A 上**召得回来**，只是排序无意义。拿一条正在用的通道去换另一条通道的排序
质量，不划算 —— 何况混合召回里中文还有向量那一路兜着。

所以**不要**把 zhcfg 的索引建到 `kb_doc` 原文列上（上面安装步骤里索引目标写的是
`kb_doc_fts`，不是笔误）。这个取舍在三处逐字一致：本节、`maos/store/pg_schema.sql`
中文那一节、`docs/DECISIONS.md` 的 `## task-t142`。

> **那句口径仍然没松**：A 口径下中文是按字 AND，不是真·分词检索。
> 「本仓库缺省支持中文分词检索」在**任何一档上都不许说**
> （`docs/submission-checklist.md:224`，T139 定，T142 没翻案）。

### 2. 占位符方言：`?` vs `%s`

SQLite 用 `?`，PG 用 `%s`。**缺省仍然不做自动翻译** —— `?` 同时是 PG 的 jsonb 算子，
字符串字面量里的 `?` 更不能动，粗暴的 `str.replace` 迟早改错一条而且没有症状。
传了参数却还写着 `?` 的话，本层抛一条说人话的 `ValueError`，而不是让 psycopg
报一句语法错。

**T115 起多了一个开关**：`MAOS_PG_SQLITE_DIALECT=1` 让 `PgStorePort` 收 SQLite 方言
的 SQL（`?` 占位符、`INSERT OR REPLACE`、SQLite DDL），发出去之前现翻。缺省**关**，
关着的时候行为一个字节不变。

翻译器不是 `str.replace`：它**按引号状态扫一遍**，`WHERE note LIKE '%?%'` 里那个
`?` 逐字保住（`maos/tests/test_ddl_translate.py` 钉着）。`INSERT OR REPLACE` 的
冲突目标从 PG 的系统目录**现查主键**，不在代码里另抄一份。

打开它是为了知识层：`kb_doc` 的建表、写入与阶段一预过滤都是同一份 SQLite 方言的
SQL。不给这个开关，「知识层跑在 PolarDB 上」就只能靠在 `maos/kb/**` 里散着写方言
分支 —— 那是把一处收口换成十处分叉。

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

### 6. 表达式索引绑死了 FTS 配置；全文的查询目标是影子表（T139 改）

`pg_schema.sql` 里的 GIN 索引建在 `to_tsvector('simple', title)` 与
`to_tsvector('simple', body)` 上（T115 起两列各一条 —— F-2 的 `fts_search` 一次
只认一列，检索器每列各问一次，只给 body 建索引的话标题那一次退化成顺序扫描）。
换了 `MAOS_PG_FTS_CONFIG` 之后查询用的是新配置，**这几条索引就用不上了**，
同样退化成顺序扫描 —— 不报错，只是慢。换配置就照 `pg_schema.sql` 的注释再建。

🔴 **T139 起全文查的是影子表 `kb_doc_fts`，不是 `kb_doc` 的原文列。**

成因（BACKLOG `## task-t132` 第 1 条，「按错误码检索恒不命中且不报错」）：
`kb.fts_text()` 的 `_TOKEN_RE` 把 `ACQ.TRADE_NOT_EXIST` 切成 `acq trade not exist`
四个 token，而 `to_tsvector('simple', body)` 在原文上走默认 parser 的 file/host
规则，`acq.trade` 被黏成一个 token（实测切成 `'acq.trade' 'not' 'exist'` 三个）。
两边各切各的，查 `acq` 恒 0 命中，**而且不报错** —— 退款域的知识条目里错误码是
主键式的线索，这条通道对它是全瞎的。

影子表的 title / body 存的**正是 `kb.fts_text()` 的产物**（`kb.upsert_doc()` 的第三条
语句），所以把索引与查询都挪过去，两边就都是那一个函数的口径了。查询侧同时从
`plainto_tsquery`（让 PG 自己再切一遍）换成 `to_tsquery`（Python 切好的 token 用 `&`
连）。切词于是在全仓库只剩 `kb.tokenize()` 一处。

顺带一个好处：两个后端**更**同构了 —— SQLite 的 FTS5 本来就建在这张影子表上。

| | T139 之前 | 之后 |
|---|---|---|
| 查询目标 | `kb_doc` 的 title / body（原文） | `kb_doc_fts` 的 title / body（切过） |
| 查询构造 | `plainto_tsquery(cfg, q)` | `to_tsquery(cfg, 'a & b & c')` |
| 索引 | `idx_kb_doc_fts_simple_{title,body}` | `idx_kb_doc_fts_shadow_{title,body}` |
| 查 `acq` | **0 命中**（不报错） | 命中 |
| 查 `lesson` | 命中 | 命中，条数不变 |

旧的两条 `idx_kb_doc_fts_simple_*` **留着没删**（跨轨契约只许新增索引），今天没有
调用方；影子表不在的旧库上 `_fts_target()` 会回落查 `kb_doc`，那时它们还得上。

**注入面**：`&` `|` `!` `:` `*` `(` `)` 都是 `to_tsquery` 的元字符，直接拼用户串会让
查询变语法错（被检索器当成「后端坏了」把整条通道判死），或者更糟 —— `!lesson` 变成
「不含 lesson」，召回集整个翻过来而不报错。所以端口侧**不信任输入**，再过一遍
`kb.tokenize()`：对已切好的串幂等，对没切过的串把元字符全丢掉。判据见
`maos/tests/test_pg_vector_channel_t139.py::test_tsquery_metacharacters_are_stripped_not_executed`。

### 7. ~~`kb_doc` 的主键与 F-2 的 `id` 约定对不上~~（T115 已解决，留档）

原状：F-2 约定「源表主键，列名固定为 `id`」，而 `maos/kb/schema.sql` 的 `kb_doc`
主键是 `(tenant_id, doc_id)`。所以 `pg_schema.sql` 当时给的是**符合 F-2 的参考形状**
`kb_doc_pg`，不是 `kb_doc` 的翻译版。

后来 T13 给 `kb_doc` 补了 `id` 生成列（`tenant_id || ':' || doc_id`），T115 又把
`kb_doc` 真的建到了 PG 上（`VIRTUAL` 生成列翻成 PG 的 `STORED`）。于是
`kb_doc_pg` 成了一张没有租户列、没人写也没人读的重复表，**已退役**
（`maos/store/pg_schema.sql` 里有退役说明）。

> `deploy/polardb-live.md` 里那些提到 `kb_doc_pg` 的实测读数**一字未动**（铁律 3）：
> 那是 2026-08-30 在云上那张表上量到的，表不再随代码交付，读数仍然是当时的读数。

### 8. ~~PG 上 `kb_doc.embedding` 是 TEXT，向量通道走不通~~（T139 已解决，留档）

原状：`kb_doc.embedding` 在 SQLite 侧是 TEXT（存 JSON 数组文本），翻到 PG 仍然是
TEXT。代价是 pgvector 的 `<=>` 在这一列上用不了，`vector_search` 抛 `LookupError`，
检索器退化成纯 Python 余弦 —— 召回照常，只是不走索引、也不走 pgvector。

#### 🔴 T139 拍的板：PG 侧多一列，这是**两个后端唯一的形状分叉**

| | SQLite | PG |
|---|---|---|
| 权威列 | `embedding TEXT` | `embedding TEXT`（**一模一样**） |
| 加速列 | 无 | `embedding_vec vector(N)`，`GENERATED ALWAYS AS (kb_embedding_vec(embedding)) STORED` |
| 索引 | 无 | `idx_kb_doc_embedding_hnsw`，`hnsw (embedding_vec vector_cosine_ops)` |
| 写入口 | `kb.upsert_doc()`，只写权威列 | **同一个函数，一个字没改** |

**边界（整合期照这个核）**：

1. **只在 PG 侧新增列与索引。** SQLite 侧一个字没改，`maos/kb/schema.sql`、
   `maos/domain/refund/schema.sql`、`maos/core/store.py` 都没动。DDL 只落在
   `maos/store/pg_schema.sql`。
2. **权威仍在 TEXT 那一列**，加速列是派生的。cast 失败一律回 NULL，**绝不拒绝写入**
   —— `embedding` 是 TEXT，历史上什么都塞得进去（解析不了的串、换了模型之后维度对
   不上的旧向量）。让加速列挡回这些写入，等于把一条「加速」的路改成一道闸，那是比
   没有 HNSW 严重得多的回归。坏数据只是没有加速向量，检索由纯 Python 余弦兜住。
3. **维度从 `retriever.EMBED_DIM` 取**，SQL 里是占位符 `@EMBED_DIM@`（见第 2 步）。
4. **回落路径留着**：加速列不存在（云上普通账号建不了 `vector` 扩展，见
   `polardb-live.md` §1.3）或一行都没派生出来时，`vector_search` 抛 `LookupError`，
   检索器退化成纯 Python 余弦 —— 也就是 T139 之前的行为，逐字相同。整条检索不许
   因为「没有 HNSW」而挂掉。

**为什么是生成列，不是普通列 + 回填**：`kb.upsert_doc()` 翻成
`INSERT ... ON CONFLICT DO UPDATE SET <插入列>`，而加速列不在插入列里。回填方案会在
**覆盖写**之后留下一条陈旧向量 —— 查询照常返回、分数照常有，只是对应的早已不是这条
知识的内容了，没有任何症状。生成列由 PG 自己在同一次写入里派生，没有这个缝。

**「真走了索引」的证明**（不是「跑得过」—— 退化路径也跑得过，只是一次 pgvector 都没
碰到）：`maos/tests/test_pg_vector_channel_t139.py::test_vector_search_plan_uses_hnsw_index`
把两条 SQL 摆在一起 `EXPLAIN`，本机实测：

```
索引形状  ORDER BY embedding_vec <=> q LIMIT n（内层）
  -> Index Scan using idx_kb_doc_embedding_hnsw on kb_doc
退化形状  ORDER BY 1 - (embedding_vec <=> q) DESC
  -> Seq Scan on kb_doc
```

两者结果集一致（不然就是「为了走索引把召回改了」）。**两层查询不是为了好看**：
planner 认的是算子本身，认不出 `ORDER BY 1 - dist DESC` 与「按距离升序」等价，所以
内层按距离取 top-limit 走索引，外层再翻成 F-2 要的「相似度降序、同分 id 升序」。

⚠️ 一处必须知道的连带：加速列**不许从 `PgStorePort.query()` 漏出去**（见
`pg_store._ACCEL_COLUMNS`）。`kb_doc` 的读路径大多是 `SELECT *`，PG 侧凭空多一列的
后果是同一条 SQL 在两个后端返回的行不再相等 —— 「换后端不换语义」那条硬判据当场破，
案例证据束的「业务状态与 SQLite 束逐字一致」也跟着破。差异是本层制造的，本层收掉；
本层内部要看加速列走 `_raw_query()`，不过滤。

### 9. `workflow_version` 的类型：SQLite 收、PG 拒

`scenarios/refund/history/history_cases.json` 那 24 条历史案例的 `workflow_version`
是 `"1.0.0"` 这样的语义化版本串，而 `kb_doc.workflow_version` 声明的是 `INTEGER`。

SQLite 的**类型亲和性**把它照单收下（存成文本），PG 直接拒：
`invalid input syntax for type integer: "1.0.0"`。所以
`fixtures.seed_history_kb()` 今天在 PG 上灌不进去。

这不是「PG 太严」——它是 SQLite 替我们藏了几个月的一处数据缺陷：
`retriever.prefilter` 拿 `workflow_version = ?` 去比的时候，比的是字符串还是整数
取决于**当初存进去的是什么**，而两边都不报错。

修它要改语料或改列类型，两者都在 T115 白名单外，已记 `docs/BACKLOG.md` 的
`## task-t115` 第 1 条，并由 `maos/tests/test_kb_pg_prefilter.py` 里一条
`xfail(strict=True)` 钉着 —— 改对了那条会「意外通过」而报错，逼人回来摘掉豁免。
