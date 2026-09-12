# 真跑日 runbook —— 2026-09-18 五 / 09-19 六

> **这是一张照着敲的纸，不是说明文档。** 每一步给「命令 → 期望输出 → 不对时怎么办」三列，
> 命令一律可直接粘。
>
> **为什么只有这两天**：复赛 2026-09-22/23 在杭州，09-20/21 是材料定稿与彩排，
> 09-17 之后不再动生产代码。下面这两件事**只能在真跑日做，且不可重来**：
>
> - **A 段 · PolarDB 真实例**：白名单 + DSN → 冒烟六步 → 单案例四路径 → 截图三张
> - **B 段 · 真 Matrix 房间**：真模型五岗 + 真人 `/approve` 与 `/reject` → 采进束
>
> 材料里有两处占位等的就是它们，09-20/21 定稿时要填：
> `docs/demo-script.md:420`（镜 7 的红线）与 `docs/defense-brief.md:78 / :86`。
> 这三处此刻写的都是「**命令能用，真人在真房间批过这条退款案的记录还没有**」——
> 真跑日采到了才能改口，**采不到就照原样留着，不许改成「真人批过」**（铁律 3）。

## 关于本文里的数字

> 🔴 凡是写了具体条数 / 行号的地方，都是**基线 `c0d8303` 的实测值，整合期复核**。
> p10 Wave F 的另外四轨（T137–T140）都在加测试，数字一定会变。
> 对不上先看是不是这个原因，别当回归报警。

---

## 0 段 · 开跑前五分钟：一条命令探完全部前置

| 命令 | 期望输出 | 不对时怎么办 |
|---|---|---|
| `python3 scripts/real_run_preflight.py` | 11 条探测逐条给结论；**硬项全绿**；结尾 `结论：全绿` 或 `只有软缺`，退出码 `0` 或 `2` | 退出码 `1` = 有硬缺，照着 `[FAIL]` 那几行补，**补完再跑一次**。这条命令只探不改，跑多少次都没有副作用 |
| `python3 scripts/real_run_preflight.py --json` | 一份可 parse 的 JSON，每条探测带 `hard` / `segment` / `ok` / `detail` | —— |

**怎么读它的输出**：每行的 `[硬|A·PolarDB]` 那一格说的是「这条属于哪一段」。
只做 A 段时，`B·真房间` 的红灯先不用管；**`通用` 那几条两段都要绿**。

它**不**回显任何值 —— DSN、token、key 一个字符都不打印（铁律 6，
`maos/tests/test_real_run_preflight.py` 的哨兵测试守着这条）。
**出口 IP 会打印**，那是白名单要用的，且它不泄漏目标 host。

### 这台机器上已知的两条

1. **出口 IP 取得到，但 `dig` 必须带 `-4`**（2026-09-12 整合期 p10-f 查实，
   推翻了当天早些时候「查询被接管」那个结论）：不带 `-4` 时 `dig` 可能走 IPv6 去问
   `resolver1.opendns.com`，那一路到不了真的 OpenDNS resolver，回
   `NOERROR / ANSWER: 0`；`dig -4 +short myip.opendns.com @resolver1.opendns.com`
   稳定返回真实出口 IP（实测连跑三次一致，偶发一次超时，故 preflight 备了第二台
   `@208.67.222.222`）。`real_run_preflight.py` 的第 4 条已经带上 `-4`。
   → **`scripts/polardb_smoke.py::_egress_ip()` 还没带**（它是 8/30 的验收工具，
   本波无人独占它，记在 `docs/BACKLOG.md ## 整合期 p10-f`）：所以连不上时它报的那行
   「本机出口 IP: …」仍然是哑的，别等它 —— 照 preflight 第 4 条打出来的那个 IP 填。
2. **工作区必须干净**才开跑：证据首行是 `# generated at <ISO8601> from <git sha>`，
   脏树会写成 `<sha>-dirty` —— 那个 sha 在 git 历史里根本不存在，
   答辩时说不出「这份证据出自哪个提交」。

---

## A 段 · PolarDB 真实例（09-18 五）

### A.0 先决条件：**用高权限账号**

🔴 **这一条放在最前面，因为踩过**：`deploy/polardb-live.md` §1.3 记着，
2026-08-30 第一次跑出来是 **2/5 不是 5/5** —— 控制台建的**普通账号**跑不完六步：

- `CREATE EXTENSION vector` → `InsufficientPrivilege: permission denied to create extension`
- `public` 里建表 → `InsufficientPrivilege: permission denied for schema public`

控制台「账号管理 → 创建账号 → **高权限账号**」，用它执行一次
`CREATE EXTENSION vector` 与 `GRANT CREATE ON SCHEMA public TO <普通账号>`，
之后全程用普通账号即可。**实例是重建的话这两条要重做一遍。**

### A.1 五步

| # | 命令 | 期望输出 | 不对时怎么办 |
|---|---|---|---|
| 1 | `python3 scripts/real_run_preflight.py` | `A·PolarDB` 那几条硬项全绿 | 见 0 段 |
| 2 | `export MAOS_PG_DSN='postgresql://<高权限账号>:<口令>@<host>:<port>/<db>'` | 无输出 | **只 export，不写进任何文件**（铁律 6）。写完复跑一次 preflight，第 3 条应从「未配置」变「已配置，形态合法，指向非本机实例」 |
| 3 | `python3 scripts/polardb_smoke.py` | 六步全绿、`结论：6/6 步通过`、`exit 0`；版本串形如 `PostgreSQL 16.14 (PolarDB …)`、pgvector `0.8.x` | 见下方「A 段退路」第 1、2 条。**第 1 步失败只报驱动异常类名**（防 host 泄漏），分层诊断会自动跑，看它报 DNS / TCP 哪一层 |
| 4 | `python3 scripts/make_case_bundle.py --all-paths --domain-backend postgres` | 四条路径（`happy` / `reject` / `drift` / `gateway_fail`）各一个 `[OK]`，顶层 `evidence/case-real-01-pg/INDEX.json` 汇总 **4 束** | 建表失败多半还是权限（回 A.0）。四条路径在本机 PG 上**已全通**（T133，基线事实），所以这里失败大概率是实例侧不是代码侧 |
| 5 | `python3 -m pytest maos/tests -q`（**带着 `MAOS_PG_DSN`**） | **`4046 passed, 13 skipped`**（约 160 s）<br>🔴 `integrate/p10-f` 合并树上**在主仓**实测（2026-09-12 整合期，本机 docker PG），worktree 那一档见下方注 | 无 DSN 那一档是 `3958 passed, 101 skipped`；差值 88 条就是 PG 门控。**别用 `-k pg` 去数门控条数**，实测只选得中 77 条。有库仍 skip 的 13 条里有一条是 PG 门控测试（`test_pg_store_live.py:206` 中文分词要 zhparser），所以按 skip 理由数是 89 条、按两档差值是 88 条，**真源认差值** |

> 🔴 **主仓比 worktree 多 3 条 passed、少 3 条 skipped，两边都对。**
> `maos/tests/test_rtv_sop_doc.py` 有 3 条依赖 `review/rtv-contracts.md`，那个文件走
> `.git/info/exclude` —— 主仓有、worktree 没有，于是 3 条从 passed 挪到 skipped。
> **真跑日是在主仓跑的**，所以那天看到的会是 `4046 / 13`（有库）与 `3958 / 101`（无库）；
> 同一棵树在 worktree 里是 `4043 / 16` 与 `3955 / 104`。
> 收集数两边都一样，`docs/expected-metrics.json` 的守卫比的是 `collected == passed + skipped`
> 这个和，两档都过。**看到 4046 不是回归。**

### A.2 截图三张（每张要框住什么）

| 张 | 框住什么 | 为什么是这张 |
|---|---|---|
| 1 · 实例详情页 | 实例 ID、**引擎版本**（PolarDB PostgreSQL 16.x）、地域、**白名单里那条出口 IP** | 证明这是一台真实例，不是本机容器 |
| 2 · 终端 | `polardb_smoke.py` 的**完整六步输出**，含版本串那行与 `结论：6/6 步通过` | 版本串里有 `PolarDB` 字样，是「真连上了」的直接证据 |
| 3 · 表行数 | 在控制台或 `psql` 里 `SELECT count(*)` 几张业务表（`refund_case` / `payment_observation` / `compensation_record`），带库名与时间 | 证明 A.1 第 4 步那四束**真的写进了这台实例**，不是写在本机 |

🔴 截图里**不许出现**完整 DSN、口令、access_token。终端那张截图前先 `clear`，
别把上面 `export` 那行留在屏幕里。

### A 段退路（比命令更值钱的一节）

1. **白名单出口 IP 漂移** —— 症状是 **TCP 静默超时，不是拒绝**
   （`docs/BACKLOG.md` 2026-08-31 那条：同日在另一个 worktree 连得上，换个网络就全程超时）。
   `polardb_smoke.py` 的分层诊断会报「DNS 通 + TCP 静默超时 = 大概率白名单没放行」。
   → **当天改白名单**（控制台看当前来访 IP，别等脚本报，见 0 段第 1 条）。
2. **改不了白名单 / 实例连不上** → 退本机 PG：
   ```bash
   export MAOS_PG_DSN='postgresql://maos:maos-local-dev@127.0.0.1:5432/maos'
   ```
   口径**退回「PolarDB 8/30 实测 + 本机 PG 同构证据」**，许可的说法逐字在
   `docs/submission-checklist.md` 的 **StorePort / PolarDB 那一行**（实测在 `:224`）。
   🔴 照那一行的措辞写，**别照 `docs/phases/phase-10.md` §6.1 写的 `:215` 去找**，那是漂掉的行号。
   退路走了就**不许说**「跑在 PolarDB 上」。
3. **实例是重建的** → `zhparser 2.2` 与 `zhcfg` 要**重装重建**（8/30 装过一次）。
   那五句 DDL 在 `maos/store/pg_schema.sql` 末尾（注释形态的模板）。
   不装的话中文那一档按 T139 的判据 skip —— **skip 是如实的结果，不是失败**，
   不许说成「缺省支持中文分词检索」。
4. **只有 psycopg2 没有 v3** → preflight 第 2 条会点名。这个坑最会骗人：
   冒烟脚本回落 v2 后照样报 **5/5 全绿**，而 `pg_store._driver()` 只认 v3、
   每次调用都抛 `PgBackendUnavailable`。`python3 -m pip install 'psycopg[binary]'`。

---

## B 段 · 真 Matrix 房间 + 真模型（09-19 六）

### B.0 先决条件：三件事

1. 🔴 **系统 `python3` 没装 matrix-nio**，必须用 `~/.maos-matrix/venv/bin/python`
   才走得到活路径。拿系统解释器起房间会**静默降级 log-only** ——
   终端照刷「房间消息」，而房间里一条都没有。这个坑在
   `docs/submission-checklist.md` 的 Matrix 那一行记着。
2. **`com.maos.room-ingress` 是 launchd KeepAlive 常驻**。preflight 第 8 条只读地探它。
   没在跑的话让它 `launchctl kickstart` 一下，**不要 `kill` / `unload` / 改 plist**。
3. **Synapse 有默认限流**，实测打穿过（一轮 approve 4 条 429）。
   房间里别连着刷命令，一条一条来。
4. **采集脚本要的三个变量在 `~/.maos-matrix/room.env` 里，不在 `~/.maos.env` 里**
   （整合期 p10-f 补）：`capture_room_transcript.py` 的 `_required_env()` 要
   `MATRIX_HOMESERVER` / `MATRIX_TOKEN` / `MATRIX_ROOM_ID`，而 `~/.maos.env` 只有
   `MAOS_LLM_*` 与 `SSL_CERT_FILE`。`start_room.sh` 里那句 `set -a; . room.env`
   **只作用于它自己那个进程**，变量进不了你的 shell —— 所以 B.1 第 2 步两个都要
   `source`，漏掉第二个的话第 3 步直接 `capture failed: 缺 Matrix 环境变量`。

### B.1 六步

| # | 命令 | 期望输出 | 不对时怎么办 |
|---|---|---|---|
| 1 | `bash ~/.maos-matrix/start_room.sh` | 启动行报「圆桌发声：5 岗 …」；补件页监听 `127.0.0.1:8787`（开关 `MAOS_UPLOAD_URL` 就在这个脚本里，不配 = 不起补件页、不挂按钮） | 终端有消息而房间里没有 = 撞上 B.0 第 1 条 |
| 2 | `source ~/.maos.env && source ~/.maos-matrix/room.env` | 无输出 | 这一步同时带来 `MAOS_LLM_*` 三件套与 `SSL_CERT_FILE`。**本文只写变量名不写值。** 不设 `SSL_CERT_FILE` 的症状是任何 https 都 `CERTIFICATE_VERIFY_FAILED`（本机 Python 缺根证书）。跑一次 `python3 scripts/real_run_preflight.py`，第 5、6 条应该都绿 |
| 3 | `python3 scripts/capture_room_transcript.py mark --boundary-out work/p10-boundary.json` | `boundary recorded: $…`，exit `0` | **必须在房间里跑那一轮之前做**。边界文件放 `work/`（不进版本库） |
| 4 | 房间里走一单（见 B.2） | —— | —— |
| 5 | `python3 scripts/capture_room_transcript.py append --boundary-file work/p10-boundary.json --transcript evidence/room/transcript.md --title '## 2026-09-19 真跑日 · 退款圆桌五岗 + 真人 /approve 与 /reject'` | `appended <N> room messages to evidence/room/transcript.md`，exit `0` | 🔴 **`--title` 一定要给**，日期按实际那天填。不给会落成中性标题；而这个脚本 2026-09-01 之前硬编码的是 `## P8 退款核心链（--case refund-s7b）`，那是一句**错的出处**（T141 已改掉）。<br>exit `2` + 「已采集」= 同一个边界跑了两次，那是幂等拦截，**不是失败**，transcript 没被改。<br>exit `2` + 「边界之后尚无房间消息」= 那一轮没发出去，回 B.0 第 1 条 |
| 6 | `python3 scripts/make_case_bundle.py --path happy --live-model` | 束出到 `evidence/case-real-01/happy-live/`，里面带 `model-usage.json` | `verify.py` **不认**这一束（真模型束重放比不了），这是设计如此。真模型不稳就走 B 段退路第 3 条 |

**把逐字记录收进束**（可选，采到了就做）：
`make_case_bundle.py` 有 `--room-transcript <文件>`，会把一份真房间逐字记录原样收进束、
首行出处由脚本写。

### B.2 房间里那一单要走完的动作

按顺序，**一条一条来**（B.0 第 3 条）：

1. `/refund <订单号> <诉求类型>` —— 触发圆桌，**五岗依次发言**（真模型）
2. 真人 **`/approve <案号>`** 一次 → 回帖卡上要能看到 **「对客户口径：…」** 那一行
3. 真人 **`/reject <另一个案号> <理由>`** 一次
4. 四条结果面命令各一次：
   `/assign <工单号> payment_ops` → `/resolve <工单号> <渠道流水号> <线下凭证摘要>`
   → `/confirm <案号>` → `/complain <案号> <内容>`
5. 需要时 `/compensate <案号>` —— 自动开单没成时补开一张（已有单不重开）

🔴 **第 2 步那一行「对客户口径」是 `docs/defense-brief.md:86` 的唯一出处**：
它只在真 Matrix 房间这条路上打得出来（圆桌财务岗的执行段只由 `maos/ingress/router.py` 调到），
`make_case_bundle.py` 走的是预检段，所以**五束 `roundtable.json` 里一个字都没有**。
这一次采不到它，材料里那句话就只能继续写「证据等真跑日采集」。

### B.3 采下来长什么样（离线演练已钉住，`maos/tests/test_room_transcript_capture.py`）

transcript 里每条消息是这个形状，**多行卡片整张落盘**：

```````text
#### 9. `m.notice` — @maos-bot:maos.local — 2026-09-18T08:01:35+00:00

`````
已放行 RC-ORD-2026-0007（操作人 @boss:maos.local）
ORD-2026-0007（质量问题） · 案子 RC-ORD-2026-0007
裁定：批准 —— …
核准金额：9600.00（政策 v1，依据 ["AS-001@v1", …]）
业务状态：已提交网关·未确认
对客户口径：已提出退款
到账观察：0 条 —— 已提交网关但**未确认到账**，最后一次观察是 gateway_accepted
`````

> 首行与第二行是**两处**模板拼出来的（`router.py` 的 `已放行 <案号>（操作人 …）`
> 加 `_render()` 的 `<摘要> · 案子 <案号>`）。整合期 p10-f 之前这里写成一行
> `已放行 · 案子 RC-…`，那个形状房间里不会产出 —— 照它去对采集结果会以为采漏了。
> 闸上驳回是另一张：`已驳回 <案号> 的「<闸标题>」这一步（操作人 …）`，没有「到账观察」那行。
```````

- **五岗**：一岗一条 `m.notice`。五个岗位账号没配齐时走代言形态，正文带名牌
  `【申请受理岗 · refund-intake】…`，靠这个前缀分辨；配齐了就靠 `sender` 分辨。
- **真人命令**：`m.text`，`sender` 是真人的 mxid —— 这是「真人敲的」与「机器人发的」
  在逐字副本里唯一的区分，别用正文去猜。
- **不进 transcript 的**：`m.reaction`（有人给卡片点赞会产生它，没有 `content.body`）、
  边界之前的一切。

### B 段退路

1. **真房间跑不起来** → 降级为 Scripted 束 + 截图，并**如实**改材料口径。
   🔴 **不许说「真人在房间里批过」** —— 那两处占位照原样留着就是正确的做法。
2. **真模型不稳**（key / 证书 / 空补丁集）→ `verify.py` 只认 Scripted 束，
   `--live-model` 束单独产、单独标。Wave C 之后**不给 `--live-model` 一定是 Scripted**，
   这是机器缺省，不用额外设什么。
3. **`/approve` 回帖卡上没有「对客户口径」那一行** → 说明这一单还没到可对外说的三态
   （卡上会念「对客户口径：尚未到可对外说的三态」）。换一单走到付款之后再试，
   **不要为了让那行出现去改任何代码**。
4. **采集脚本报 `翻页 20 次仍找不到边界`** → 边界文件是上一轮的。重新 `mark` 一次，
   但那样会丢掉这一轮 —— 所以 B.1 第 3 步的顺序不能乱。

---

## C 段 · 收束（两段都跑完之后）

| # | 命令 | 期望输出 | 不对时怎么办 |
|---|---|---|---|
| 1 | 干净树重产全部证据，顺序照跨轨契约 §B（**不设** `MAOS_EVIDENCE_PINNED_SHA`）：<br>`make_evidence.py` → `--domains` → `make_case_bundle.py --all-paths` → `--path happy --live-model` → `python3 -m maos.kb.experiment --r8` → `--domain-backend postgres`（四条路径）→ `scripts/render_trace.py` | 各步 exit `0` | 顺序反了会让后面的束引用不到前面的产物 |
| 2 | `python3 scripts/verify.py` | **`RESULT: 10/10 PASS`**，exit `0`<br>🔴 基线 `c0d8303` 实测值，整合期复核 | `evidence/room/` 那几条 `warn` 是**预期内的**（截图与逐字记录靠人工采集，没有生成器）。真跑日采完之后它们的出处 sha 才会更新 |
| 3 | `python3 scripts/check_docs.py` | **阻断 0**（基线共 44 条 / 提示 44）<br>🔴 基线 `c0d8303` 实测值，整合期复核 | 阻断不为 0 才要停下来 |
| 4 | `python3 scripts/gen_docs.py --check` | 3 份生成物与代码逐字节一致 | 不一致就跑一次 `gen_docs.py` 重生成（重生成**不算**手改） |
| 5 | `bash scripts/make_release.sh` | 产出 `dist/maos-runtime-<sha7>.zip`，并**当场解压验一遍**（pytest 全绿 + `make_evidence` + `verify` 到 10/10） | 它自带密钥哨兵反查，不过就非 0 退出 —— 那是好事，说明拦住了 |
| 6 | 首行 sha 检查：<br>`head -1 evidence/*/INDEX.json evidence/*/*.json 2>/dev/null \| grep -c dirty` | **`0`** | 非 0 = 有证据是脏树跑的。按那个 sha checkout 也复现不出来（那份代码不在 git 历史里）。**干净树重跑**，别手改首行 |

### C 段退路

- **A 段走了退路（本机 PG）** → C 段第 1 步里 `--domain-backend postgres` 那四束
  产的是本机 PG 的证据。束本身没问题，**口径要跟着退**（见 A 段退路第 2 条）。
- **B 段没采到真房间** → `evidence/room/` 保持原样（那五张截图跑的是一个
  `role=coding` 的软件域任务），`verify.py` 的 `warn` 照旧。
  材料里那两处占位**照原样留着**。

---

## 附 · 真跑日之前人类要先做的事

| 什么 | deadline | 为什么只能人做 |
|---|---|---|
| PolarDB 实例开好，**建一个高权限账号**，白名单加当前出口 IP | 09-17 四 | 要登阿里云控制台，本仓库所有会话一律不碰阿里云域名 |
| 实例若是重建的：重装 `zhparser 2.2` + 重建 `zhcfg` | 09-17 四 | 同上，且要高权限账号 |
| 确认 `~/.maos.env` 里的真模型 key 还有额度 | 09-18 五早 | 会花钱，本仓库所有会话都不跑真模型 |
| 脱敏需求确认（材料里哪些字段要打码） | 09-14 一 | 业务判断 |
| 09-17 之后**不再动生产代码** | 09-17 四 | 窗口约定，见跨轨契约 |

**开跑当天的第一条命令永远是**：

```bash
python3 scripts/real_run_preflight.py
```
