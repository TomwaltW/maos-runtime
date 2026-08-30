# PolarDB PostgreSQL 版 —— 真连实测记录

这份文档只回答一个问题：**MAOS 的 PG 通道要用到的能力，在一台真实的
PolarDB PostgreSQL 版实例上，到底哪些是跑过的、哪些没跑过。**

它存在的理由是：材料里关于 PolarDB 的说法此前只有一句**推理** ——
「PolarDB 兼容 PG 协议 + pgvector 扩展可用，所以能跑」。推理不是实测。
本文件负责把那句话换成一条一条可复核的记录，**包括没跑通的那些**。

## 与 `deploy/polardb.md` 的分工（别把两份文档混起来）

| 文件 | 回答什么 | 天花板 |
| :-- | :-- | :-- |
| `deploy/polardb.md`（T10 轨） | MAOS 的 PG 后端怎么接、怎么配、怎么降级 | 本机 Docker `pgvector/pgvector:pg16` |
| **本文件**（T14 轨） | **真 PolarDB 实例上，地基能力实际跑通了哪几条** | 不碰 MAOS 源码，只验数据库这一侧 |

> 撰写时 `deploy/polardb.md` 尚未落到主干（T10 轨交付中）。凡涉及
> **中文分词口径**、**降级口径**的结论，一律以该文件为准，本文件只出实测数据。

🔴 **一条硬纪律：本文件里「本机 pgvector 跑通」和「PolarDB 跑通」是两件事，
分栏写、不混说。** 前者只能证明脚本本身没问题，证明不了云上那台机器行不行。

---

## 一、已实测

### 1.1 对照组：本机 Docker pgvector —— 五步全绿 ✅

**这一栏不是 PolarDB 的结论**，它的作用是排除「脚本本身有问题」这个可能：
先在一台已知可用的库上跑绿，之后连 PolarDB 再失败，才能归因到 PolarDB。

- 环境：`pgvector/pgvector:pg16` 容器（`deploy/docker-compose.yml` 的 `pg` profile）
- 命令：`python3 scripts/polardb_smoke.py --local`
- 结果：**5/5 步通过，exit=0**

```
====================================================================
PolarDB / pgvector 地基冒烟 —— 五步
====================================================================
驱动：psycopg 3.3.4
目标：本机 pgvector（--local）
--------------------------------------------------------------------
  [ OK ] 1. 连接 + SELECT version() -> PostgreSQL 16.15 (Debian 16.15-1.pgdg12+2) on aarch64-unknown-linux-gnu, compiled by gcc (Debian 12.2.0-14+deb12u1) 12.2.0, 64-bit
  [ OK ] 2. CREATE EXTENSION vector -> pgvector 0.8.6
  [ OK ] 3. 全文 to_tsvector + ts_rank -> 命中 ['d1']，ts_rank=0.099103（越大越相关）
  [note]    （观察）中文 to_tsvector('simple') -> '退款订单已经超时未处理':1
  [ OK ] 4. 向量 vector 列 + <=> 排序 -> top-1=a，余弦距离 a=0.000000, c=0.006116, b=1.000000（越小越相关）
  [ OK ] 5. 清理临时表 -> maos_smoke_fts / maos_smoke_vec 已删
--------------------------------------------------------------------
结论：5/5 步通过
exit=0
```

五步各自验的是什么，以及断言强度：

| # | 验什么 | 断言（不是「没报错就算过」） |
| :-- | :-- | :-- |
| 1 | 连接 + `SELECT version()` | 拿到版本串 |
| 2 | `CREATE EXTENSION IF NOT EXISTS vector` | 且能从 `pg_extension` 读出 `extversion` |
| 3 | `to_tsvector` + `ts_rank` | 命中集合**必须恰好等于** `['d1']`（`fox & brown` 只有 d1 同时含两词），且 `ts_rank > 0` |
| 4 | `vector` 列 + `<=>` + 排序 | 构造已知相似度，**全序必须恰好等于** `['a','c','b']` |
| 5 | 清理 | 两张自建表删净，不留垃圾 |

### 1.2 PolarDB 实例上的五步 —— 五步全绿 ✅

**补跑日期：2026-08-30。** 阿里云 PolarDB PostgreSQL 版，按量付费，走公网地址 +
白名单放行本机出口 IP。连接串只经环境变量传入，本文件不含任何真实值。

```
====================================================================
PolarDB / pgvector 地基冒烟 —— 五步
====================================================================
驱动：psycopg 3.3.4
目标：环境变量 MAOS_PG_DSN
--------------------------------------------------------------------
  [ OK ] 1. 连接 + SELECT version() -> PostgreSQL 16.14 (PolarDB 16.14.20.0 build 1f03f15d) on x86_64-linux-gnu
  [ OK ] 2. CREATE EXTENSION vector -> pgvector 0.8.3.1
  [ OK ] 3. 全文 to_tsvector + ts_rank -> 命中 ['d1']，ts_rank=0.099103（越大越相关）
  [note]    （观察）中文 to_tsvector('simple') -> '退款订单已经超时未处理':1
  [ OK ] 4. 向量 vector 列 + <=> 排序 -> top-1=a，余弦距离 a=0.000000, c=0.006116, b=1.000000（越小越相关）
  [ OK ] 5. 清理临时表 -> maos_smoke_fts / maos_smoke_vec 已删
--------------------------------------------------------------------
结论：5/5 步通过
exit=0
```

🔴 **但第一次跑出来是 2/5，不是 5/5。** 这条必须记 —— 照着做的人会撞同一堵墙，见 §1.3。

### 1.3 前置条件：普通账号跑不完这五步（第一次 2/5 的实录）

控制台建的**普通账号**连上去，第 2、3 步当场挂掉：

```
  [ OK ] 1. 连接 + SELECT version() -> PostgreSQL 16.14 (PolarDB 16.14.20.0 build 1f03f15d) on x86_64-linux-gnu
  [FAIL] 2. CREATE EXTENSION vector -> InsufficientPrivilege: permission denied to create extension "vector"
            HINT:  Must be superuser or user with all of polar_superuser to create this extension.
  [FAIL] 3. 全文 to_tsvector + ts_rank -> InsufficientPrivilege: permission denied for schema public
  [FAIL] 4. 向量 vector 列 + <=> 排序 -> 跳过：第 2 步没拿到 vector 扩展
  [ OK ] 5. 清理临时表 -> maos_smoke_fts / maos_smoke_vec 已删
结论：2/5 步通过   exit=1
```

**这是两个互相独立的坑，别当成一件事**（只修其中一个，另一个照样挡住）：

| 症状 | 根因（实测） | 修法 |
| :-- | :-- | :-- |
| 装不了 `vector` | 普通账号 `rolsuper=f` 且不属于任何角色；PolarDB 要求 `polar_superuser` | 控制台「账号管理 → 创建账号 → **高权限账号**」，用它执行一次 `CREATE EXTENSION` |
| `public` 里建不了表 | PG15 起 `public` 的 ACL 是 `{pg_database_owner=UC/pg_database_owner,=U/pg_database_owner}`，普通用户只有 `USAGE` 没有 `CREATE`；且本库 owner 是 PolarDB 内建的 `aurora`，普通账号对库也没有 `CREATE`，**连自建一个模式绕开都不行** | 高权限账号执行 `GRANT CREATE ON SCHEMA public TO <普通账号>` |

高权限账号跑完这两条 DDL 之后，**后续全程用普通账号**即可 —— 灌 schema、跑测试都不再需要高权限。

账号盘点（实测 `pg_roles`）：`aurora` / `polardb_admin` / `replicator` 三个是 PolarDB
**内建**超级用户，口令不归使用者掌握，**别指望用它们**；控制台新建的高权限账号属于
`pg_polar_superuser` 角色，这才是能用的那个。

---

## 二、未实测

### 2.1 ~~PolarDB 实例上的全部五步 —— 未实测~~ → 已于 2026-08-30 补跑，见 §1.2 ✅

原因存档（这条坑留着，因为它在补跑当天又出现了一次）：首轮之所以没跑成，是因为
交给会话的 `MAOS_PG_DSN` 是派单 §1 里的占位符模板原文
`postgresql://<user>:<pass>@<host>:<port>/<db>`，五个尖括号一字未改。
它暴露的方式很绕：

1. 开场自检第 5 条只验「环境变量非空」，占位符模板照样报「**已配置**」；
2. 于是第一次连接失败表现为一个指向不明的 `OperationalError` ——
   看起来像白名单没放行，实际上根本没有 host 可连；
3. 分层诊断（DNS → TCP → PG 鉴权）时才在解析端口那一步撞出
   `ValueError: Port could not be cast to integer value as '<port>'`。

**已把等价检测内建进 `scripts/polardb_smoke.py`**：DSN 含尖括号占位符时当场报
「没替换成真实连接串」并 `exit=2`，不再伪装成连接失败。派单自检那一行的
同类修法已记进 `docs/BACKLOG.md ## task-T14`。

**这条检测在补跑当天真拦下了一次**：高权限账号那份 DSN 只换了口令、用户名那格还留着
`<ADMIN_USER>`，检测当场点名「还有占位符没换」，没有再伪装成一次鉴权失败。

### 2.2 原先挂着的四个问号 —— 已全部答掉

| 原问题 | 实测答案（2026-08-30） |
| :-- | :-- |
| `CREATE EXTENSION vector` 能不能装成 | ✅ **能**，但必须用高权限账号，见 §1.3 |
| PG 大版本与 pgvector 版本各是多少 | `PostgreSQL 16.14 (PolarDB 16.14.20.0 build 1f03f15d)` on x86_64-linux-gnu；**pgvector 0.8.3.1** |
| `<=>` 行为是否与本机 pgvector 一致 | ✅ **逐位一致**，见 §3.2 |
| 是否要先在控制台「插件管理」里启用 | ❌ **不需要**。`vector` 本来就在 `pg_available_extensions` 里（该实例共 **189** 个可用扩展），瓶颈只有账号权限这一条 |

> 因此复赛材料里关于 PolarDB 的表述**可以写「已在 PolarDB PostgreSQL 版实例上实测」**，
> 并附 §1.2 的输出。仍然**不能写**的是性能、并发、大数据量、主备切换相关的任何结论 —— 见 §3.4。

### 2.3 复跑方式（一条命令）

脚本设计成**不 import maos**，任何人拿一条 DSN 就能复跑，不需要装 MAOS 本体：

```bash
python3 -m pip install 'psycopg[binary]'        # 刻意不进 pyproject 的 dependencies
export MAOS_PG_DSN='postgresql://真实连接串'    # 只走环境变量，不落任何文件
python3 scripts/polardb_smoke.py
```

§1.2 那段输出就是这么来的。脚本输出**自带脱敏**（host／口令／用户名／库名在打印前
已被抹成 `<redacted>`），所以它的输出可以直接进文档和材料，不必人工再过一遍。

🔴 换一台新实例复跑时，**先照 §1.3 把高权限账号那两条 DDL 做掉**，否则拿到的是 2/5。

---

## 三、已知差异 / 局限

### 3.1 中文分词：PG 自带分词器不切中文（本机实测现象）

第 3 步的观察项实测到：

```
to_tsvector('simple', '退款订单已经超时未处理')  ->  '退款订单已经超时未处理':1
```

**整句被当成了一个 token**（`:1` 是它的位置），没有任何切分。
这不是配置错误，是 PG 默认分词器不认中文 —— 换 `'english'` 配置结果同理。

其后果是：**中文语料上的全文检索基本只能命中「整句完全一致」的查询**，
`ts_rank` 的排序在中文上不具备可用的区分度。

同一条在 **PolarDB 上复测，现象完全相同**：

```
to_tsvector('simple', '退款政策超时未到账')  ->  '退款政策超时未到账':1
```

> 🔴 **口径以 T10 的 `deploy/polardb.md` 为准，本文件只出现象、不下结论。**
> 常见的三条出路（装 `zhparser`／`pg_jieba` 扩展、应用层预分词后写入、
> 全文通道只用于英文与 ID 类字段而中文走向量通道）各自的取舍属于 T10 的面。

**但有一条新事实要交给 T10**：`deploy/polardb.md` 当时推断「托管实例大概率装不了
中文分词扩展」，**实测推翻了这个推断** —— 这台 PolarDB 的可用扩展列表里四个都在：

| 扩展 | 可用版本 | 已安装 |
| :-- | :-- | :-- |
| `zhparser` | 2.2 | 否 |
| `pg_jieba` | 1.1.2 | 否 |
| `pg_bigm` | 1.2 | 否 |
| `pgroonga` | 4.0.5 | 否 |

🔴 **「在可用列表里」≠「装得上」≠「装上以后中文召回是好的」**，这是三件事，本轮只验到第一件：
没有 `CREATE EXTENSION zhparser`，没有建文本检索配置，没有测过任何一条中文召回。
要下「中文全文在 PolarDB 上可用」的结论，得把后两件也跑了。

### 3.2 PolarDB 与本机 pgvector 的行为差异 —— 检索行为零差异，差异在别处

**五步里的每一个数值逐字节相同**（左：本机 Docker `pgvector/pgvector:pg16`；
右：PolarDB 16.14）：

| 项 | 本机 pgvector | PolarDB | 差异 |
| :-- | :-- | :-- | :-- |
| 全文命中集 | `['d1']` | `['d1']` | 无 |
| `ts_rank` | `0.099103` | `0.099103` | 无 |
| `<=>` 全序 | `a, c, b` | `a, c, b` | 无 |
| 余弦距离 | `0.000000 / 0.006116 / 1.000000` | `0.000000 / 0.006116 / 1.000000` | 无 |
| 中文 `to_tsvector('simple')` | 整句一个 token | 整句一个 token | 无（见 §3.1） |

差异全在检索行为之外的这三条：

| 项 | 本机 pgvector | PolarDB | 影响 |
| :-- | :-- | :-- | :-- |
| pgvector 版本 | 0.8.6 | **0.8.3.1**（旧两个小版本） | 本层只用 `vector` 类型与 `<=>`，两版实测无差 |
| PG 版本串 | `16.15 (Debian ...)` 社区版 | `16.14 (PolarDB 16.14.20.0 build 1f03f15d)` | 版本串本身就是「这是不是真 PolarDB」的判据 |
| 权限模型 | 容器里的账号是 superuser，五步随便跑 | 普通账号跑不完，需高权限账号先做两条 DDL | 见 §1.3 —— **这正是本机对照组测不出来的那一类问题** |

### 3.3 分数方向：两个通道相反，接 StorePort 时必须转换

本机实测到的方向（PolarDB 上预计相同，但**未实测**）：

| 通道 | 算子 | 原始值方向 | 实测样例 |
| :-- | :-- | :-- | :-- |
| 全文 | `ts_rank(...)` | **越大越相关** | 命中 d1 → `0.099103` |
| 向量 | `<=>`（余弦距离） | **越小越相关** | `a=0.000000, c=0.006116, b=1.000000` |

契约 F-2 附则要求两个后端的 `fts_search` / `vector_search` 都返回
**「越大越相关」** 的分数。所以向量通道在实现侧必须做一次方向翻转
（例如 `1 - (embedding <=> query)`），**否则排序会整个倒过来，而两边的测试
各自都可能是绿的** —— 这属于 T10 实现面的事，此处只记录方向差异这个事实。

### 3.5 🔴 该实例不支持 SSL，公网链路是明文

实测（客户端侧 libpq 的判定 + 服务端参数，两边互相印证）：

```
sslmode=prefer   ✅ 连上，客户端侧 SSL 生效 = False
sslmode=require  ❌ OperationalError: server does not support SSL, but SSL was required
SHOW ssl                  -> off
SHOW password_encryption  -> md5
```

`sslmode=prefer` 是 libpq 的**缺省值**，语义是「能加密就加密，**不能就明文连**」——
所以 DSN 里不写 `sslmode` 时不会有任何提示，连接照常成功，你不会知道它没加密。

口径要分清，别把话说过头：

- 口令**不是**明文过线 —— `password_encryption = md5`，走的是挑战应答，线上传的不是原文口令；
- 但 **查询语句与查询结果全程明文**，且 md5 认证本身早已不足以对抗离线爆破；
- 这是一条**公网**链路，中间人可读可改。

修法（控制台侧，本文件不代做）：开启 SSL 加密，然后把 DSN 改成 `...?sslmode=require`
（`require` 只加密不验证证书，要验证书就 `verify-ca` 并配 `sslrootcert`）。
开启 SSL 会有一次短暂闪断。**开启之前用过的口令应视为已在公网上暴露过，建议一并轮换。**

### 3.4 本轮验证没有覆盖的面

- 未验证**连接池 / 并发**行为，只用了单连接
- 未验证**向量索引**（`ivfflat` / `hnsw`）—— 五步只验了顺序扫描下的 `<=>` 正确性，没建索引、没测召回与性能
- 未验证**大数据量**下的表现，测试数据是 3 行文档 + 3 条向量
- ~~未验证 SSL / 加密连接要求~~ → 已验，见 §3.5（结论是**该实例不支持 SSL**）
- 本机对照组一共跑了三次、结果逐字一致：前两次跑在**已由 T10 轨起用的共享容器**上
  （未 `up` 也未 `down`，自建表以 `maos_smoke_` 前缀隔离并跑完即删），
  第三次跑在 T10 释放后**本轨自己新建的容器**上。三次输出相同，说明该结果与
  容器实例无关、可复现。理由与经过见 `docs/DECISIONS.md ## task-T14`

---

## 四、给人类的截图清单

评委要看的是**实例真实存在**的证据。

### 补跑已完成，这三张可以截了

1. 控制台实例详情页：**规格 + PG 大版本**（🔴 打码 host / 实例 ID）
2. **§1.2 那段五步全绿的终端输出** —— 脚本输出自带脱敏，可直接截。
   它里面的 `PolarDB 16.14.20.0 build 1f03f15d` 就是「这是真 PolarDB」的判据
3. 账号管理页：高权限账号存在 —— 这张的价值是配合 §1.3 说明那条前置条件

「插件管理页 `vector` 已启用」这张**截不出来也不必截**：实测该实例不需要在控制台启用
任何东西，`vector` 本来就在可用扩展列表里（§2.2）。

两条禁令仍然成立：

- ❌ 不要截本机 Docker pgvector 的输出去充当 PolarDB 的证据 —— §1.1 那段写着
  `PostgreSQL 16.15 (Debian ...)`，是 Debian 容器里的社区版 PG，懂行的评委一眼能看出来。
  要截就截 §1.2 那段。
- ❌ 不要拿控制台「实例存在」的页面去暗示「跑通过」—— 实例存在与能力验证是两件事，
  能力那半边的证据是 §1.2 的终端输出，不是控制台截图。

---

## 五、安全声明（铁律 7）

- DSN 与口令**只从环境变量读**，脚本不接受命令行传入连接串（命令行会进 shell 历史）。
- 本文件、`scripts/polardb_smoke.py`、两份账本中**不含任何真实 host、口令、实例 ID**，
  一律使用 `postgresql://<user>:<pass>@<host>:<port>/<db>` 这类占位形式。
- 脚本的脱敏做了双保险，并经**临时自验脚本实测**（假 host／口令／用户名／库名四样全被抹、
  泄漏条数 0、正文表名不误伤）：
  - host／口令／整条 DSN → 无条件子串替换为 `<redacted>`；
  - 用户名／库名 → 词边界替换（避免把 `maos_smoke_fts` 误伤成 `<redacted>_smoke_fts`）；
  - 兜底正则抹掉任何形如 `scheme://...@...` 的串；
  - **第 1 步（连接）失败时只报驱动异常类名、不报 message** —— 多数驱动会把 host
    拼进连接失败的 message 里。
- 🔴 **该实例当前未启用 SSL，链路是明文**（§3.5）。在开启 SSL 之前，这条公网链路上
  跑过的一切都应视为可被第三方看到；开启后建议轮换口令。
- 高权限账号只用于 §1.3 那两条 DDL，**用完即可删除或改密** —— 日常连库一律用普通账号，
  它在 `GRANT` 之后已经够用。
- 🔴 **实例用完请立刻删除并收回白名单**：按量付费会一直计费，
  临时放开的 `0.0.0.0/0` 更不能留着。
