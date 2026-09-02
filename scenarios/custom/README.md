# 自定义 case —— 放你自己的数据进去

这个目录只有数据，没有代码。两条路，按你是谁选：

| 你是 | 交什么 | 跑什么 |
| :-- | :-- | :-- |
| 业务方，不写代码 | 一张 CSV（Excel 另存即可），一单一行 | `python3 scripts/run_requests.py <你的.csv>` |
| 工程 / 调试 | 一份完整 case JSON（连靶场带政策） | `python3 scripts/run_case.py <你的.json>` |

## 用法一：交一张退款申请表

**老板每次要给的只有四列**，样例见 `scenarios/custom/refund-requests.csv`：

```text
订单号,诉求类型,申报金额,申请日期,说明
ORD-2026-0001,质量问题,6800,2026-07-10,客户上传锈蚀照片，要求全额退款
ORD-2026-0002,七天无理由,1280,2026-08-15,客户买错型号想退
ORD-2026-0003,质量问题,,2026-08-25,金额留空表示按订单实付
```

- **诉求类型写中文**：质量问题 / 七天无理由 / 发错货（写不认识的词会当场报错，**不猜** ——
  猜错一个词，套用的就是另一条政策）。
- **申报金额留空** = 按订单实付金额。**申请日期留空** = 按今天算。
- 其余一概不用填：租户、渠道、商品、订单版本、下单当时锁定的政策版本，
  全部**按订单号从底账查出来**。让人每次手抄这些，抄错一次裁定就错一次，而且不报错。

底账 `scenarios/custom/ledger.json` 是**配一次**的东西：客户、渠道、商品、订单快照、
公司的售后政策。真实落地时它由 ERP / 订单系统导出，不用天天动。

```bash
python3 scripts/run_requests.py scenarios/custom/refund-requests.csv --csv 结果.csv
```

跑出来是一张中文表（`--csv` 那份带 BOM，Excel 双击直接打开不乱码）：

```text
订单号         诉求        裁定  核准金额  退款状态    依据                         转人工
ORD-2026-0001  质量问题    批准  6800.00   已到账      按基线（命中 3 条售后规则）  1
ORD-2026-0002  七天无理由  驳回  0.00      未发起退款  AS-001@v1                    0
ORD-2026-0003  质量问题    批准  24000.00  已到账      按基线（命中 3 条售后规则）  1

共 3 单：批准 2、驳回 1；已到账 2 单合计 30800.00 元；期间 2 次停下来等人放行。
  · ORD-2026-0002：AS-001@v1 窗口 30 天，第 55 天申请，55 > 30
```

「转人工」那列不是异常：金额超过财务复核阈值的单子，闸过了也要人放行才动钱。

## 用法二：一份完整 case JSON

调试、造对照、验政策改动时用这条 —— 靶场和政策都写在同一个文件里，
不依赖底账，改一处就能看结论怎么变。

```bash
python3 scripts/run_case.py scenarios/custom/refund-case.json
```

流程在 `maos/flows/custom_case.py`，入口在 `scripts/run_case.py`。

## 你能放什么进去

一份 JSON，**顶层键即表名**，列名逐字对齐 `maos/domain/refund/schema.sql`。
多一列少一列都会当场抛 —— 不会静默按缺省值跑绿（跑绿的错结论比报错难查得多）。

| 顶层键 | 是什么 | 必填 |
| :-- | :-- | :-- |
| `tenant` | 租户（`tenant_id` / `name` / `region`） | 是 |
| `channel` | 渠道（`tenant_id` / `channel_id` / `kind` / `name`） | 是 |
| `product_snapshot` | 商品快照，含质保月数 | 是 |
| `order_snapshot` | 订单快照：金额、付款时间、**下单当时锁定的政策版本** | 是 |
| `policy_rule` | 政策规则，可以放多条多版本；`body` 是紧凑 JSON 字符串 | 是 |
| `case` | 本次诉求：诉求类型、申报金额、订单号 —— 建案入参 | 是 |
| `customer_evidence` | 客户提交的证据，会翻成多源信号走聚合 | 否 |
| `requested_at` | 本次诉求的时刻。窗口天数由它与 `paid_at` 现算 | 否，缺省取当下 |
| `gateway` | 演示网关行为：`settle_after`（问几次才有终态）、`fail_with`（注入错误码） | 否 |

以 `_` 开头的顶层键（`_note` / `_provenance`）是给人看的，程序不读。

### `policy_rule.body` 写什么

它是**紧凑 JSON 字符串**，不是条款正文。写成自然语言不会报错，只会静默地按
「全额退、不扣费」算 —— 这是最容易踩且最难发现的一处。

常用键：

| 键 | 作用 |
| :-- | :-- |
| `refund_ratio` | 退款比例，`"1"` 即全额 |
| `deduct_fee` | 扣除的手续费 |
| `no_reason_days` | 无理由退货窗口天数；超窗即裁定 reject |
| `applies_when.reason_code` | 这条规则管哪几种诉求类型 |
| `extra_tasks` | 展开成额外任务（如渠道商核销），`owner_role` 决定谁干 |
| `approver_role` | 谁来审批，缺省 `supervisor` |

## 改一个字段，换一个结论

样例是「质量问题、6800 元、第 8 天申请」，跑出来 approve、退 6800.00、`settled`。
四个改法各换一种结论，全都不用碰代码：

| 想看到 | 怎么改 |
| :-- | :-- |
| 金额变 5390.00 | 把命中规则的 `refund_ratio` 改 `"0.8"`、`deduct_fee` 改 `"50"` |
| 裁定 reject | `case.reason_code` 改 `no_reason_return`，`requested_at` 推到付款后 30 天以上 |
| 政策版本用错会怎样 | 把 `order_snapshot.policy_version_at_order` 改成 `2` —— 命中的规则跟着换版本 |
| 退款失败 | `--fail-with ACQ.SELLER_BALANCE_NOT_ENOUGH`（码必须在 `maos/tools/gateway_codes.py` 里） |
| 网关问不出终态 | `gateway.settle_after` 设成 `99` —— 案子停在 `gateway_accepted`，**一条观察都不写** |

最后一条是这个系统最要紧的一句话：**钱的下落归网关，不归它**。
问不出终态时它什么都不写，不猜、不推断。

## 命令

```bash
python3 scripts/run_case.py <你的.json>                         # 跑，缺省由 CLI 代人放行
python3 scripts/run_case.py <你的.json> --reject                # 主管驳回
python3 scripts/run_case.py <你的.json> --fail-with ACQ.TRADE_NOT_EXIST
python3 scripts/run_case.py <你的.json> --json out.json          # 结果另存
python3 scripts/run_case.py <你的.json> --quiet                  # 不打状态迁移轨迹
```

## 进真房间跑

加 `--matrix`，这一跑的每一次状态迁移都会镜像进 Matrix 房间（Element 里能看到）：

```bash
. ~/.maos-matrix/room.env                                  # 八个键，在仓库外，永不入库
~/.maos-matrix/venv/bin/python scripts/run_case.py scenarios/custom/refund-case.json --matrix
```

🔴 **必须用那个 venv 解释器。** 系统 `python3` 没装 `matrix-nio`，`--matrix` 会当场降级，
而**降级的终端输出与真房间一模一样** —— 截那个窗口当证据和真的分辨不出来。
所以入口自己拦：没接通就 `exit 4` 并告诉你换哪个解释器，除非显式 `--allow-degraded`。

2026-09-01 实测：两跑都进了房间，`task-rc-demo-001-{intake,policy,finance,payment,notify}`
的 TaskAssignment / TaskResult / ReviewVerdict / StateTransition 逐条可见。

房间里**由人放行**的那条路（在 Element 里打 `/approve`）走的是另一个入口：
`~/.maos-matrix/venv/bin/python -m hiclaw.room_demo --case approve`。
本入口的审批是 CLI 代跑的，不等房间命令。

## 结果怎么读

```text
  裁定      : approve —— 命中 2 条 AS- 规则，其中没有适用于 quality_defect 的时限规则
  人工介入  : 1 次转人工，全部放行（沈思锴（supervisor)）
              · 核算退款金额并写财务分录 —— gate_needs_human（human_approval）
  核准金额  : 6800.00（按政策 v1，依据 ["AS-001@v1", "AS-002@v1"]）
  支付      : 1 条观察，终态 settled（poll_count=2），网关码 10000
  业务状态  : settled（settled 只可能由 payment.observe 写入）
```

- **人工介入**不是异常。高风险任务（真把钱退出去）过了闸也停在 `BLOCKED` 等人；
  裁定 reject 的计划还会被第六道闸判「报了超阈值金额却没排核算」而转人工 ——
  CLI 代跑了人的那一半，并把每一次转人工的原因原样打出来。
- **`poll_count`** 是「终态是问出来的、不是本地推断的」仅有的可核字段。
- **裁定 reject 时不排核算任务**，核准金额是 `0.00` —— 一份 0 元分录会让下游
  误以为「核算过了，只是金额为零」。

## 边界

- 本流程**不做域内补偿**（网关明确失败后的转人工工单、撤销退款请求）。
  那条路径是场景 7 的面：`python3 run.py --scenario 7`。
- 数据是你自己的，**没有判据**：它不比对任何期望值。要看「同一份数据两种判法
  结论相反」的对照实验，跑 `python3 run.py --contrast`（判据在
  `scenarios/refund/cases/` 各自的 `_expected` 块里）。
- 无 key、零出网：全程 ScriptedModelClient + MockGateway，与 `run.py` 同一条路。

守着这条入口的测试在 `maos/tests/test_custom_case.py`。
