# RTV 采购退货 SOP —— 五步主干、三种处置、两个权威源

RTV（Return to Vendor，采购退货退款）是本仓库上的**第五个业务域**，也是
[`domain-portability.md`](domain-portability.md) 那句主张（换域只换 Skill /
ToolPort / 业务对象）迄今最硬的一次演示：它比前四个域多**一个外部权威源**。

本文只做两件事：

1. 把这套 SOP 写清楚 —— 五步、三种处置、七个状态、谁说了算；
2. 让写下的每一句都**可核对** —— 要么给出可复跑的命令与它的输出，要么指回
   冻结契约 `review/rtv-contracts.md` 的具体小节号。

## 关于本文的引用口径（读之前先知道这一条）

本文成稿时（2026-09-02），RTV 域的代码由 T61–T64 四轨并行落地，**在本文作者的基线
`4c956a8` 上尚不存在**。因此本文的引用分三类，读者据此判断可信度：

| 引用类型 | 写法 | 现在能核吗 |
| :-- | :-- | :-- |
| 本仓已存在的文件 | 带反引号，如 `maos/domain/ap/schema.sql:48` | ✅ 能，命令就在正文里 |
| 冻结契约的条款 | 引小节号，如 C-R2 / C-R3 | ✅ 能，见 `review/rtv-contracts.md` |
| RTV 域的未来落点 | **不带反引号**的纯文本路径，如 maos/domain/rtv/schema.sql | ❌ 待 T61–T64 合入后核验 |

第三类刻意不加反引号：文档守卫 `scripts/check_docs.py` 的判据 E 会对反引号里的
`路径:行号` 做存在性与行数校验，把还不存在的路径写进反引号，守卫当场判红 ——
而那正是它该做的事。**未来路径不是断言，是约定**，两者在本文里形状不同。

---

## 1. 这份 SOP 是什么、出处在哪

RTV 的五步主干与三种处置类型**不是自创的**。四个出处，逐条说明引了它的哪一条、
落在本文哪一节：

| # | 出处 | 引了它的什么 | 落在本文 |
| :-- | :-- | :-- | :-- |
| 1 | Oracle PeopleSoft FSCM《Understanding the RTV Business Process》 | 五步主干（定位源 PO/收货 → 建 RTV → 发运 → 对账 → AP 调整凭单）、三种 return action、header/line 两级状态流、收货单四个量分开记的口径 | §2 / §3 / §4 |
| 2 | Microsoft Dynamics 365 Field Service《Process a return (RMA and RTV)》 | RMA 与 RTV 的分工、processing action 三选一、credit memo 的触发点 | §3 / §5 |
| 3 | Oracle《Managing Vendor Returns》 | 供应商退货的单据链与状态推进口径，作为出处 1 的交叉印证 | §2 / §4 |
| 4 | SAP Business One《Goods Returns and Credit Memos》 | 采购退货与贷项通知单（credit memo）的凭证链：退货单 → 贷项通知单 → AP 调整 | §5 / §6 |

URL 逐条：

1. https://docs.oracle.com/cd/G35227_01/fscm92pbr54/eng/fscm/spog/UnderstandingtheRTVBusinessProcess-9f3e8e.html
2. https://learn.microsoft.com/en-us/dynamics365/field-service/process-return
3. https://docs.oracle.com/cd/E27605_01/fscm91pbr2/eng/psbooks/spog/htm/spog47.htm
4. https://sap-ds.com/training/sap-business-one/logistics/purchasing/goods-returns-and-credit-memo

两条口径说明：

- **单据类型码不另找出处**。贷项通知单在 UNCL1001 里恒为 `381 = Credit note`，
  本域沿用仓库已有的 `maos/tools/ap_codes.py`（Peppol BIS Billing 3.0）。
  两份出处 = 两个口径，必漂。
- **本文不对这四个 URL 发网络请求**，测试里也只做字符串断言。URL 会不会 404 是
  外部世界的事，不是本仓库能守住的判据；假装能守住才是吹牛。

---

## 2. 五步主干

每一步都写清四件事：谁做、输入什么、产出什么、**谁说了算**。最后一列是本域的重点：
五步里只有前两步是 MAOS 说了算，后三步全在外部。

| 步 | 谁做（角色，C-R7） | 输入 | 产出 | 谁说了算 |
| :-- | :-- | :-- | :-- | :-- |
| ① 定位源 PO / 收货单 | `rtv_intake` | 退货诉求：供应商、SKU、数量、理由码 | 退货案子 + 退货行 + 业务对象引用 | **MAOS**（观察） |
| ② 建 RTV、选处置 | `rtv_disposition` | 退货理由码、合同条款 | 处置裁定（三选一）+ 裁定依据 | **MAOS**（推断） |
| ③ 发运退货 | `rtv_logistics` | 裁定结果、发货地址 | 运单号 + 承运商回执 | **承运商** |
| ④ RTV 对账 | `rtv_reconcile` | 退货行 × 贷项通知单 × 到账 | 对账结论 + 应收金额 / 差异清单 | **供应商**（开不开票） |
| ⑤ AP 建调整凭单 | `rtv_settlement` | 贷项通知单号 | 终态观察回执 | **AP / 银行** |

逐步展开：

**① 定位源 PO / 收货单。** RTV 必须能指回「退的是哪一张 PO 的哪一次收货」。
指不回去的退货在第 ④ 步无法与贷项通知单勾稽 —— 供应商开票时引的是他那侧的单号，
两边只有靠 PO/GR 才能对上。所以案子表上 `po_id` / `po_version` / `gr_id`
三件套是必填（C-R1）。

这一步**不改收货单**：PeopleSoft 口径里 `quantity_received` 是审计量、永不变，
退货量单独记在退货行上。把收货量减回去，账面好看，但「这批货当初到底收了多少」
这个事实就没了。

**② 建 RTV、选处置。** 建案时处置类型为空串，由裁定这一步写入 —— 受理的人不该
替裁定的人拍板（C-R1 的 `return_action` 默认空串就是这条的机器强制）。裁定必须
留下依据，每条带 `rule_id`，口径同 `ap` 域的 `match_result.findings_json`。

**③ 发运退货。** 状态推进到 `shipped` **只能由承运商回执得到**，不是「我方点了
发货按钮」。承运商是外部系统，回执里的 `carrier_status` 是观察结果，不是我方决定。

**④ RTV 对账。** 三方对账：退货行（我方声称退了什么）× 贷项通知单（供应商认了
多少钱）× 到账（钱有没有回来）。这是 `ap` 域三单匹配的镜像 —— 方向反过来。
对不上时把差异如实落进 `findings_json`，**不许**自己把金额改成对得上的那个。

**⑤ AP 建调整凭单。** 调整凭单由 AP 侧按自己的规则建，RTV 域**只查不写**
（C-R5 把 `ap.adjust_query` 定死为只读）。两处都能写，「这笔调整是谁建的」
就失去唯一答案。

---

## 3. 三种处置：credit / exchange / replacement

| 处置 | 判定条件（典型） | 供应商那侧发生什么 | 钱的走向 |
| :-- | :-- | :-- | :-- |
| `credit` | 多收、错发、超期未用；或买方不再需要该物料 | 开贷项通知单 | 退钱：AP 冲减应付 |
| `exchange` | 规格/型号发错，买方仍要这批物料的正确版本 | 换一批发回来 | 不退钱：金额对冲 |
| `replacement` | 破损、质量缺陷，同一物料重新发一批 | 补发同物料 | 不退钱：金额对冲 |

三者的差别只有一句话是关键：**`credit` 的终点是钱，另外两个的终点是货。**
本仓库当前把三种处置都记进裁定表（C-R1 的 `rtv_disposition`，`CHECK` 约束限定
三选一），但状态机主干只演到 `credit` 那条线走完 —— 见 §7 第 2 条，那是本域
如实认下的缺口，不是没写完的功能。

裁定要保留历史：`rtv_disposition` 按 `attempt` 一次裁定一行，返工重裁时旧结论
留着。「当初为什么判成 credit、后来为什么改判」在表上要查得出来。

---

## 4. 状态流：这是业务对象自己的字段，不是 Task 状态

C-R2 冻结的状态机（落点 maos/domain/rtv/guard.py 的 `BIZ_STATUS_FLOW`）：

```python
BIZ_STATUS_FLOW: dict[str, tuple[str, ...]] = {
    "received":    ("disposed", "rejected"),
    "disposed":    ("shipped", "rejected", "compensated"),
    "shipped":     ("credited", "compensated"),
    "credited":    ("settled", "compensated"),
    "settled":     (),
    "rejected":    (),
    "compensated": (),
}

INITIAL_STATUS = "received"
```

**七个状态、九条转移边。** 自己数（契约副本在 `review/rtv-contracts.md`）：

```bash
sed -n '/^BIZ_STATUS_FLOW/,/^}/p' review/rtv-contracts.md | grep -c '^    "'
# 7                    ← 七个状态
sed -n '/^BIZ_STATUS_FLOW/,/^}/p' review/rtv-contracts.md \
  | grep -o '(\(.*\))' | grep -o '"[a-z]*"' | wc -l
# 9                    ← 九条边（键不计，只数每个 tuple 里的目标状态）
```

对应 SOP 五步：

| SOP 步 | 状态跳转 | 谁说了算 |
| :-- | :-- | :-- |
| ① 定位源 PO/收货单 | → `received` | MAOS（观察） |
| ② 建 RTV、选处置 | `received` → `disposed` | MAOS（推断） |
| ③ 发运退货 | `disposed` → `shipped` | **承运商** |
| ④ RTV 对账 | `shipped` → `credited` | **供应商**（开票） |
| ⑤ AP 建调整凭单 | `credited` → `settled` | **AP / 银行** |

主干之外的四条边都是失败路径，一条都不许省：

- `received` → `rejected`：受理即驳回（退货诉求不成立）。
- `disposed` → `rejected`：裁定不通过。
- `disposed` / `shipped` / `credited` 三处都能进 `compensated`：货已经动了
  （或裁定已经出了），但供应商不认、也问不出来 —— 补偿做完之后的收口。

分辨得出「这笔为什么没退成」是这四条边存在的全部理由。都收敛成一个 `failed`，
「供应商拒赔」和「我方裁定不通过」在账上就长一个样。

### 🔴 它不是 Task 状态（铁律 9）

`biz_status` 是 `rtv_case` 表上的一个字段，与 `maos/contracts/states.py` 里的
Task 状态机**没有任何关系**。上这个域**不许**往 Task 状态机加任何新状态或新迁移：

```bash
grep -c "received\|disposed\|credited" maos/contracts/states.py
# 0
```

这条不是自觉，是铁律 1 的契约指纹锁（`maos/tests/test_contracts_frozen.py`）
钉住的：`contracts/states.py` 的 sha256 一个字节都不许变。业务状态是业务对象
自己的字段 —— 这正是「换域只换业务对象」这句话在状态机这一层的确切含义。

---

## 5. 权威边界：两个外部权威源、两个权威终态

**这是本域最值钱的一节。** 评委那个问题的形状是：

> 你怎么知道供应商认了这笔退货？

答案不许是「因为 `supplier.rma_submit` 没抛异常」。照 `ap` 域
`maos/skills/builtin/ap/observe.py` 文件头那段问答的形状，本域的答案是：

    问：你怎么知道供应商认了这笔退货？
    答：不是因为退货发出去了、也不是因为供应商回了「收到」，是因为
        supplier.credit_query 问到第 N 次时供应商回了 issued 并给出了贷项通知单号，
        那份回单连同状态更新在同一个事务里落了库（credit_note 表），
        invocation_id 指回是哪一次调用观察到的。

    问：那你怎么知道钱回来了？
    答：那是**另一个**外部系统说的。ap.adjust_query 回 settled 并给出调整凭单号，
        落进 rtv_settlement_observation 表。供应商认账和钱到账是两件事，
        中间可以隔很久，也可以永远不发生。

### 5.1 两个权威源，两个权威终态

C-R3 冻结：

| 权威终态 | 谁说了算 | 回执必须长什么样 | 落在哪张表 |
| :-- | :-- | :-- | :-- |
| `credited` | 供应商（开票） | `observed_state == "issued"` 且有 `credit_note_id` | credit_note |
| `settled` | AP / 银行（到账） | `observed_state == "settled"` 且有 `adjustment_id` | rtv_settlement_observation |

两张表**都只由 `rtv.observe` 写**（C-R3 的 `AUTHORITATIVE_WRITER`）。我方写一行
「供应商给我开了贷项通知单」，等于自己给自己开发票。

这是本域相对前四个域的增量。`ap` 域只有一个权威终态：

```bash
grep -n "^AUTHORITATIVE_STATES" maos/domain/ap/guard.py
# 67:AUTHORITATIVE_STATES = frozenset({"settled"})
```

RTV 域是两个（`credited` 与 `settled`），因此 C-R3 要求
`AUTHORITATIVE_RECEIPT_STATE` 与 `AUTHORITATIVE_STATES` **同增同减** ——
加一个权威终态就必须同时给出它的判据，**漏配不放行**（见到没有判据的权威终态
直接拒）。这条比「多一个状态」重要得多：漏配时若默认放行，等于新终态没有判据
也能写进去。

### 5.2 为什么 `acknowledged` 不等于 `credited`

供应商侧的状态取值域是 `submitted` / `acknowledged` / `issued` / `disputed` /
`unknown`。其中 `acknowledged` 与 `issued` 由契约 C-R3 定死，另外三个的出处是
T62 的模拟器 —— 本文成稿时那份代码还不存在，所以 `test_rtv_sop_doc.py` 把这三个
名字**如实登记成「暂时无源」**（带理由的豁免），整合期合入后应当删掉豁免、
改由真实模块接住。最危险的一对是：

| 回执值 | 中文 | 能推进到 `credited` 吗 |
| :-- | :-- | :-- |
| `acknowledged` | 供应商**收到退货了** | ❌ 不能 |
| `issued` | 供应商**开出了贷项通知单** | ✅ 能 |

两者在回执里字段齐全、形状一模一样，**差着一次会计确认**。「东西收到了」和
「这笔钱我认」是两件事：供应商完全可以收下货之后判定不属于质量问题，回
`disputed`。把 `acknowledged` 收进 `credited` 的判据集合，本域要证明的那件事
当场作废 —— 那时候 `credited` 就成了「我方观察到对方收货」的同义词，是我方推断，
不是外部权威。

口径同 `ap` 域拒收 `accepted` 那条：银行收下付款指令 ≠ 钱付出去了。

### 5.3 三种非终态，一个都不许乐观处理

| 回执值 | 含义 | 正确动作 |
| :-- | :-- | :-- |
| `submitted` | 单子提上去了 | 继续问，不推进 |
| `acknowledged` | 供应商收到退货 | 继续问，不推进 |
| `unknown` | 供应商侧自己也说不清 | 继续问，**尤其不许重发 RMA** |

`unknown` 最危险：那笔退货**可能已经被受理了**，只是回执没拿到。在这里替它下结论
（无论判成功还是判失败）就是把外部状态写死为终态（铁律 8）。重发 RMA 更糟 ——
可能变成两张退货单。

轮询到顶仍非终态时**一行状态都不推、一条观察都不写**。「我问累了」和「供应商说
不认」是两回事。这与「明确失败要留痕」不矛盾：供应商明确回 `disputed` 时观察要
落库，因为那是**供应商说的**；到顶没问出来时不落，因为那时候供应商什么都没说。

### 5.4 这条边界怎么被机器钉住

与 `ap` / `refund` 两域同一套四道闸（C-R3 与 T61 的 guard 落点）：

1. 建案不接受调用方指定 `biz_status` —— 想直接建成 `settled` 的路从一开始就不存在。
2. `biz_status` 只有唯一写入路径，非法跳转抛 `BizStatusTransitionError`。
3. 写权威终态时 actor 必须是 `rtv.observe`，否则抛 `AuthoritativeFactViolation`
   并落一条同名事件 —— 事件名与 `ap` / `refund` 域共用（C-R3 的 `VIOLATION_EVENT`）。
4. 权威终态还要求回执齐全且判据对得上（见 §5.1 那张表）。

这套边界在仓库里的通用论证见 [`authoritative-facts.md`](authoritative-facts.md)。

---

## 6. 与 `ap` 域的对照：镜像关系与五张表复用

RTV 是 `ap` 域的镜像 —— 钱的方向反过来：

| | `ap` 域（已有） | `rtv` 域（本轮） |
| :-- | :-- | :-- |
| 钱的方向 | 我付供应商 | 供应商退我 |
| 核心比对 | 三单匹配（PO × GR × Invoice） | 三方对账（退货行 × 贷项通知单 × 到账） |
| 外部权威源 | **一个**：银行回单 | **两个**：供应商开票 + AP/银行到账 |
| 权威终态 | `settled` | `credited` **与** `settled` |
| 权威写入方 | `ap.observe` | `rtv.observe` |

### 6.1 复用了哪五张表

`supplier` / `purchase_order` / `purchase_order_line` / `goods_receipt` /
`goods_receipt_line` 五张表**全部复用 `ap` 域的既有定义，一张都不重建**。
它们在 `ap` 域 schema 里的位置：

```bash
grep -nE "^CREATE TABLE IF NOT EXISTS (supplier|purchase_order|purchase_order_line|goods_receipt|goods_receipt_line) " maos/domain/ap/schema.sql
# 48:CREATE TABLE IF NOT EXISTS supplier (
# 61:CREATE TABLE IF NOT EXISTS purchase_order (
# 73:CREATE TABLE IF NOT EXISTS purchase_order_line (
# 88:CREATE TABLE IF NOT EXISTS goods_receipt (
# 100:CREATE TABLE IF NOT EXISTS goods_receipt_line (
```

而冻结契约 C-R1 里这五张表一张都没有 —— RTV 只引用不重建：

```bash
grep -cE "^CREATE TABLE IF NOT EXISTS (supplier|purchase_order|purchase_order_line|goods_receipt|goods_receipt_line) " review/rtv-contracts.md
# 0
```

**为什么这条要用机器数、不能靠自觉**：`CREATE TABLE IF NOT EXISTS` 撞名的后果
不是报错，是**静默跳过**。重建一份「看起来一样」的定义，两处一旦漂开，症状会离
原因非常远 —— 而且第一天绝对不会红。

### 6.2 RTV 自己新增几张表

契约 C-R1 新增十张表，全部带 `rtv_` 前缀或为本域独有（`credit_note`）：

```bash
grep -c "^CREATE TABLE" review/rtv-contracts.md
# 10
```

依次是 `rtv_schema_version` / `rtv_case` / `rtv_line` / `rtv_disposition` /
`rtv_shipment` / `credit_note` / `rtv_reconciliation` /
`rtv_settlement_observation` / `rtv_compensation_record` / `rtv_business_ref`。

对照 `ap` 域的规模（十五张表，含被复用的那五张）：

```bash
grep -c "^CREATE TABLE" maos/domain/ap/schema.sql
# 15
```

### 6.3 差在哪

| 面 | `ap` 域 | `rtv` 域 | 差别的来源 |
| :-- | :-- | :-- | :-- |
| Skill 数 | 六个（`intake` / `match` / `plan_payment` / `execute` / `observe` / `compensate`） | 六个（C-R4） | 数量巧合，职责一一镜像 |
| 外部系统 | 一个（银行） | 三个（供应商门户 / 承运商 / AP 系统） | 退货要经手物流 |
| ToolPort 数 | 两个（`bank.pay` / `bank.query`） | 五个（C-R5） | 同上 |
| 角色数 | 四个 | 五个（C-R7） | 多一个物流角色 |
| 写权限 | 银行两个 port 一写一读 | 五个 port 里**三个只读** | `ap.adjust_query` 只读是硬约束 |

`ap` 域的 skill 与角色自己数：

```bash
ls maos/skills/builtin/ap/ | grep -c '^[a-z].*\.py$'
# 6                    ← 六个 skill 模块（下划线开头的 __init__.py / _common.py 不计）
ls maos/agents/ap/*_agent.py | wc -l
# 4
```

---

## 7. 不吹的部分（本域**没有**做什么）

这一节的写法照 [`domain-portability.md`](domain-portability.md) §5。空着比没有更糟。

1. **三个模拟器都不是真系统。** `MockSupplier` / `MockCarrier` / `MockApSystem`
   是进程内模拟器，一行真网络都不打。它们证明的是「编排与权威边界在这套形状下
   成立」，**不是**「已经对接了供应商门户」。真门户有认证、有限流、有它自己的
   状态命名 —— 那些一件都没做。
2. **`exchange` 与 `replacement` 只做到裁定为止。** 契约 C-R4 的六个 skill 里
   没有任何一个处理换货单的发出、换回来那批货的收货与验收。状态机主干
   （`disposed → shipped → credited → settled`）演的是 `credit` 那条线；换货
   在真实系统里要再挂一张新的收货单，本域没有这一段。裁定判成 `exchange` 之后
   的流程，当前是**空的**。
3. **场景演示用的是契约级 stub。** T64 的演示场景按契约的 skill 名注册 stub
   skill、按契约的返回值形状造 stub 工具、表结构自己按 C-R1 建 —— 因为四轨
   并行时上游代码互相不在对方基线里。整合期才换成真的。哪些是 stub、整合期换
   成什么，写在场景文件头。
4. **演示场景不进缺省证据束。** 它不进 `ALL_SCENARIOS`、不进 `run.py`
   —— 缺省证据束仍是既有的八束，本域没有往里加东西。
5. **本文的 RTV 侧断言，源头是契约文本不是运行中的代码。** 成稿时 T61–T64
   尚未合入，本文所有带 C-R 编号的引用都指向冻结契约的**文字**。代码与契约
   是否逐条对上，**待整合期核验** —— 核验方法见 §8。
6. **四个出处 URL 没有做在线校验。** 只做字符串断言（`test_rtv_sop_doc.py`）。
   本仓库守不住外部站点的可达性，装作守得住是吹牛。
7. **两个权威源都还是模拟出来的。** 「多一个权威源」这件事在**结构**上成立
   （两个终态、两套判据、同增同减的机器强制），在**对接**上一个都没真接。

---

## 8. 待整合期核验的清单（本文作者跑不了的那些）

T61–T64 合入后，下面这些命令必须逐条跑绿，本文才从「按契约写的」升级为
「按代码核过的」：

```bash
# ① 表结构：十张新表，且不重建 ap 域那五张
grep -c "^CREATE TABLE" maos/domain/rtv/schema.sql                 # 期望 10
grep -cE "^CREATE TABLE IF NOT EXISTS (supplier|purchase_order|purchase_order_line|goods_receipt|goods_receipt_line) " maos/domain/rtv/schema.sql
                                                                    # 期望 0
# ② 状态机：七状态九边，与本文 §4 逐字对上
grep -n "BIZ_STATUS_FLOW" maos/domain/rtv/guard.py
# ③ 权威终态：两个，且判据同增同减
grep -n "AUTHORITATIVE_STATES\|AUTHORITATIVE_RECEIPT_STATE" maos/domain/rtv/guard.py
# ④ 跨域零 import：rtv 域不许 import ap 域
grep -rn "from maos.domain.ap" maos/domain/rtv/                     # 期望空
# ⑤ 内核零改动（这是「换域只换 Skill/ToolPort/业务对象」的落点）
git diff --shortstat <整合前 sha> <整合后 sha> -- maos/contracts/ maos/core/
                                                                    # 期望空
```

第 ⑤ 条是 [`domain-portability.md`](domain-portability.md) 新增那一节的主张，
它在本文成稿时**还没有被跑过** —— 那一节里如实写着「待整合期核验」，没有先
把结论写下来再补证据。
