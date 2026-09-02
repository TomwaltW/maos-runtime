# MAOS 使用说明

一页读完的操作面 —— 只回答「怎么跑」。想知道「为什么这么设计」，
读 [`README.md`](../README.md) 与 [`architecture.md`](architecture.md)。

## 1. 先决条件

- **Python ≥ 3.10**，本机一律用 `python3`。
- **核心零依赖、零出网、不需要任何 API key** —— 缺省走 Scripted 模型客户端，
  按关键字返回预置应答，任何机器上状态迁移序列逐条一致。
- 分支必须是 `goai-restructure`。先自证一句，不对就停下：

  ```bash
  git rev-parse --abbrev-ref HEAD     # 必须回 goai-restructure
  ```

  `main` 上是已封存的 TypeScript 骨架，它**看起来像对的**（自带一份同名 README），
  clone 会安静地成功，直到本文每条命令都失败 —— 那个分支上没有 `maos/`、没有 `run.py`。
- Docker 可选。装了就在容器里跑沙箱；没装自动降级为裸 subprocess，场景照跑，
  代价是隔离性那一维的证据是空的。要走容器路径先建镜像：

  ```bash
  docker build -t maos-sandbox -f deploy/sandbox.Dockerfile .
  ```

## 2. 四条命令

全部在**仓库根目录**执行，顺序不能换：

```bash
python3 -m pytest maos/tests -q     # ① 全量测试
python3 run.py                      # ② 场景 1-7 端到端，exit=0
python3 scripts/make_evidence.py    # ③ 跑 7 场景 + RAG 对照，落成 evidence/ 八束
python3 scripts/verify.py           # ④ 八项逐条重放核验 -> RESULT: 8/8 PASS，exit=0
```

- ①② 只证明「代码能跑」。评委看的 `8/8 PASS` 要 ③④ **两条都跑**，一条都不能省。
- `*.db` 不入 git，所以**新克隆直接跑 ④ 会报「缺数据库」并退出 2** —— 这是设计行为，
  不是故障，先跑 ③。
- 本机实测（2026-09-01，无 PG、无 key）：① `1572 passed, 39 skipped`，约 46 秒；
  ④ `RESULT: 8/8 PASS`，exit=0。**条数随开发增长，别拿这行当门禁**，
  权威期望值在 `scripts/demo_preflight.sh`。

录 demo / 提交前，用一条命令跑完全部前置并逐条断言期望值：

```bash
bash scripts/demo_preflight.sh      # 任一步不符期望即非 0 退出，退出码 = 出错的步号
```

## 3. 常用开关

| 命令 | 作用 |
| :-- | :-- |
| `python3 run.py` | 顺跑场景 1-7（等价于 `python3 -m maos.main`） |
| `python3 run.py --scenario 7` | 只跑第 7 场；`--scenario` 取值 1..7 |
| `python3 run.py --matrix` | 事件链镜像进 Matrix 房间，连不上自动降级，场景照跑 |
| `python3 run.py --contrast` | 三组对照 case（租户 / 渠道 / 政策版本），**不进**缺省八束 |
| `python3 -m maos.kb.experiment` | R5：RAG 有无对照实验，唯一变量是 `MAOS_KB_ENABLED` |
| `python3 scripts/gen_docs.py --check` | 三份代码生成的文档与代码不一致即非零退出 |

`--contrast` 不接受其它参数（`--matrix` 除外），多传会 exit=2。

## 4. 八束场景

| # | 一句话 | 证明什么 |
| :-- | :-- | :-- |
| 1 | 正常闭环 | 四角色 DAG 跑到 `DONE`，验收证据是真跑的测试报告，不是 Agent 自述 |
| 2 | 返工闭环 | 第一轮自查全写 pass 也过不了闸，拦下它的是真挂掉的用例 |
| 3 | 高风险审批 | 闸全过也停 `BLOCKED`，等人工放行 |
| 4 | 幂等验证 | 重复投递同一个结果不产生第二次状态迁移 |
| 5 | 治理路径闭环 | 多源聚合 → 撞双 blocker → 确定性 replan → `DONE` → 知识沉淀 |
| 6 | 退款 · 顺利路径 | 换域只换 Skill / ToolPort / 业务对象，内核零改动 |
| 7 | 退款 · 失败路径 | 问不出终态就什么都不写，补偿 + 转人工，**从未进入 `settled`** |
| R5 | RAG 有无对照 | 关掉检索 → 计划漏排财务核算 → 被拦；打开 → 命中历史案例补上 |

## 5. 跑你自己的数据

两条路，按交东西的人是谁选。**不写代码的人交一张 CSV**，一单一行、只填四列
（订单号 / 诉求类型 / 申报金额 / 申请日期），其余按订单号从底账查：

```bash
python3 scripts/run_requests.py scenarios/custom/refund-requests.csv --csv 结果.csv
```

跑出来是一张中文结果表（批不批、退多少、钱到没到账、依据哪条政策、几次转人工）。
底账 `scenarios/custom/ledger.json` 配一次即可，真实落地由 ERP 导出。
字段与写法见 [`scenarios/custom/README.md`](../scenarios/custom/README.md)。

调试、造对照、验政策改动走另一条：一份**完整 case JSON**，靶场与政策都在同一个文件里，
改一份 JSON 就够，代码不用动：

```bash
cp scenarios/custom/refund-case.json my-case.json   # 改里面的订单、政策、金额、诉求
python3 scripts/run_case.py my-case.json            # 跑出裁定、核准金额、钱到没到账
```

输入是一份 JSON，**顶层键即表名**：租户 / 渠道 / 商品快照 / 订单快照 / 政策规则，
外加一个 `case` 块（本次诉求）。字段清单、`policy_rule.body` 该怎么写、以及
「改哪个字段换哪种结论」的对照表，都在
[`scenarios/custom/README.md`](../scenarios/custom/README.md)。

样例（质量问题、6800 元、付款后第 8 天申请）跑出来长这样：

```text
  裁定      : approve —— 命中 2 条 AS- 规则，其中没有适用于 quality_defect 的时限规则
  人工介入  : 1 次转人工，全部放行 · 核算退款金额并写财务分录
  核准金额  : 6800.00（按政策 v1，依据 ["AS-001@v1", "AS-002@v1"]）
  支付      : 1 条观察，终态 settled（poll_count=2），网关码 10000
  业务状态  : settled（settled 只可能由 payment.observe 写入）
```

| 开关 | 作用 |
| :-- | :-- |
| `--reject` | 主管驳回而不是放行 |
| `--fail-with <码>` | 给网关注入错误码，看退款失败怎么收场 |
| `--json out.json` | 结果另存一份机读的 |
| `--quiet` | 不打状态迁移轨迹，只留结果摘要 |
| `--matrix` | 每次状态迁移镜像进 Matrix 房间（见下） |

进真房间跑要先装环境变量，并且**必须换解释器** —— 系统 `python3` 没装 `matrix-nio`，
`--matrix` 会静默降级，而降级的终端输出与真房间一模一样：

```bash
. ~/.maos-matrix/room.env
~/.maos-matrix/venv/bin/python scripts/run_case.py my-case.json --matrix
```

没接通时入口自己 `exit 4` 并告诉你换哪个解释器（`--allow-degraded` 可放行）。

改政策的 `refund_ratio` 金额就跟着变、超窗就裁定 reject、把 `gateway.settle_after`
设大就**一条观察都不写** —— 这三件事各有一条测试钉着（`maos/tests/test_custom_case.py`）。

这条入口**不比对任何期望值**（你的数据没有标准答案），也不做域内补偿。
要看带判据的对照实验，跑 `python3 run.py --contrast`。

## 6. 结果落在哪


```text
evidence/
  INDEX.json               本次生成的清单：git sha、每场 span/event 计数
  scenario-1 .. scenario-7/, scenario-R5/
    run.log                本场完整 stdout
    result.json            Plan/Task 终态、每个任务的 role/attempt/risk、business_outcome
    trace.json             OTel 对齐的 span 树（核验第 4 项重放它）
    business-objects.json  业务对象与 business_ref（第 2 项校验引用不悬空）
    kb-hits.json           本场命中了哪些知识（第 5 项校验命中是真的）
```

每个证据文件首行是 `# generated at <ISO8601> from <git sha>`，由生成脚本写入。
所以**跑完 ③ 之后 `git status` 会有几十行 `evidence/` 改动，属预期** ——
出处头每跑一次都变。别用 `git checkout` 单独还原，两条出路见 `README.md` 第 3 节。

## 7. 接真模型（可选）

只影响 Agent 的语义产出，不影响状态机。密钥**只读环境变量，禁止写进任何文件**：

```bash
export MAOS_LLM_BASE_URL=...   # OpenAI 兼容接口
export MAOS_LLM_API_KEY=...
export MAOS_LLM_MODEL=...
```

场景 5 与全部测试强制 scripted，配了 key 的机器上也不打真网络 ——
replan / 补偿 / 审批是控制面行为，其正确性不得依赖模型的智力表现。

其余旋钮：

| 环境变量 | 作用 | 缺省 |
| :-- | :-- | :-- |
| `MAOS_SANDBOX_FORCE_SUBPROCESS=1` | 沙箱恒走裸 subprocess（无 Docker 环境用） | 未设 = 优先容器 |
| `MAOS_SANDBOX_TIMEOUT` | 单次沙箱执行超时秒数 | 300 |
| `MAOS_MAX_REPLAN` | replan 次数上限，超限转人工 | 2 |
| `MAOS_KB_ENABLED` | RAG 有无对照实验的唯一变量 | 开 |
| `MAOS_PG_DSN` | 配了就走 PostgreSQL 后端，并解锁 29 条门控测试 | 未设 = SQLite |
| `MATRIX_*` / `MAOS_APPROVERS` | Matrix 房间镜像与审批人名单 | 未设 = 降级 log-only |

## 8. 卡住了先看这四条

| 症状 | 原因与处理 |
| :-- | :-- |
| `verify.py` 报「缺数据库」并退出 2 | 没跑 ③。`*.db` 不入 git，核验器要的是库不是快照 |
| 每条命令都说找不到文件 | 分支不对，回到第 1 节自证 `goai-restructure` |
| 测试条数与 `demo_preflight.sh` 的期望不符 | 不等于回归。先判断是新增了测试（期望值该刷）还是真红了 —— 看 `failed` 是不是 0 |
| `verify.py` 多出两行 warn | 没有 `maos-sandbox` 镜像，沙箱降级到 subprocess。见第 1 节的 `docker build` |

`verify.py` 输出里的 `warn:` / `info:` 行是**点名但不判负**的提示，不改判定，
也没有被藏起来。判定只看最后一行 `RESULT: n/8 PASS` 与退出码。

## 9. 再往下读

| 想知道 | 去哪 |
| :-- | :-- |
| 整体设计、分层图、生命周期时序 | [`architecture.md`](architecture.md) |
| 为什么问不出终态就什么都不写 | [`authoritative-facts.md`](authoritative-facts.md) |
| 换域到底改了几行 | [`domain-portability.md`](domain-portability.md) |
| Demo 怎么录，每镜什么命令 | [`demo-script.md`](demo-script.md) |
| 裸 clone 冒烟实录（含没跑通的几遍） | [`clone-smoke-report.md`](clone-smoke-report.md) |
| 真 Matrix 房间怎么起 | [`matrix-room-runbook.md`](matrix-room-runbook.md) |
| 容器里从零跑到 8/8 | [`deploy/README.md`](../deploy/README.md) |
| 提交前自查 | [`submission-checklist.md`](submission-checklist.md) |
