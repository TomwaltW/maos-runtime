# 批量语料 —— 60 单底账、三张申请表、一份分类评测集、12 张聊天截图

`scenarios/custom/` 那份演的是**剧情**（六单，一单一种结局）。这里演的是**分布**：
一天几十单真进件进来，词表认得出多少、多少单要转人工、钱停在哪几道闸上。

**一个字都不碰 `scenarios/custom/`。** `run_requests.py` 与 `room_team_smoke.py` 都有
`--ledger`，所以另起一份底账就够了 —— 老三单那条逐字节比对的判据
（`maos/tests/test_room_team_fixture.py`）完全不受影响。

## 三条命令

```bash
# 1. 60 单批量跑，看分布
python3 scripts/run_requests.py scenarios/bulk/refund-requests-bulk.csv \
        --ledger scenarios/bulk/ledger-bulk.json

# 2. 诉求类型分类评测，看词表认得出多少
python3 scenarios/bulk/eval_reason.py

# 3. 12 单五岗圆桌 + 聊天截图当随案证据
python3 scripts/room_team_smoke.py scenarios/bulk/refund-requests-chat.csv \
        --ledger scenarios/bulk/ledger-bulk.json --evidence scenarios/bulk/chat
```

## 目录里有什么

| 文件 | 是什么 | 规模 |
| :-- | :-- | :-- |
| `ledger-bulk.json` | 底账：租户/渠道/商品/订单/政策/客户/退款历史 | 60 单、12 SKU、5 条政策、17 条退款历史 |
| `refund-requests-bulk.csv` | 申请表，诉求类型写**标准词** | 60 行，全部跑得通 |
| `refund-requests-raw.csv` | 同一批单子，诉求类型写**客户原话** | 60 行，**跑不满是预期** |
| `refund-requests-chat.csv` | 只留有聊天截图的那 12 单 | 12 行 |
| `reason-eval.csv` | 客户说法 → 期望诉求类型 | 114 条，四个分组 |
| `chat/*.png` | 客服会话截图，文件名前缀配单 | 12 张，750px 宽 |
| `generate.py` | 前四份的生成器 | `--check` 校验确定性 |
| `gen_chat.py` | 聊天截图渲染器 | `--one <订单号>` 只出一张 |
| `lexicon_corpus.py` | 客户原话语料，**手写不是模板拼的** | 分组判据写在模块头 |

## 实跑数字（2026-09-10）

### 60 单批量

```text
共 60 单：批准 53、驳回 7；已到账 53 单合计 1966215.54 元；期间 65 次停下来等人放行。
```

诉求分布 质量问题 30 / 七天无理由 20 / 发错货 10；22 单金额留空（按订单实付）。
7 单驳回全部是无理由超窗，其中既有 `AS-001@v1` 的 30 天窗口，也有 `AS-001@v2` 的 15 天 ——
**同一条规则两个版本同时在跑**，因为每单锁的是下单当时那一版。这是本目录相对
`scenarios/custom/` 唯一新增的机制，见下节。

「转人工」那列几乎每单都是 1：真把钱退出去的任务过了闸也停在 `BLOCKED` 等人放行，
与金额无关。**3 次**的那几单才是金额惹的 —— 裁定 reject 却报了超阈值金额，
第六道闸判「没排核算」再转一次（阈值 5000，`maos/runtime/gate.py`）。

### 分类评测（114 条）

```text
分组       条数  判对  判错  弃权  未判(无模型)  准确率
lexicon    14    14    0     0     0             100.0%
real       70    64    0     6     0             91.4%
unknown    25    24    1     0     0             96.0%
ambiguous  5     4     1     0     0             80.0%
```

🔴 **这张表里只有 `lexicon` 和 `unknown` 两组是可复现的**，它们不调模型。
`real` 与 `ambiguous` 两组每跑一次数字都会变一点 —— 同一条说法模型这次判出来、
下次弃权，两次实跑就差了 1 条。**别把它们写进任何回归判据**，也别拿去当承诺。

三个读法：

- **词表只有 14 个词**（3 个 code）。`real` 那 70 条真实说法**词表一条都判不出**，
  全靠模型兜底。没配 key 时它们整组落 `unknown`，那是设计好的退化路径，
  不是 0 分 —— 脚本单列一栏「未判(无模型)」并剔出分母。
- **弃权 6 条留在分母里**。模型说「不知道」是判过了，那一单要人接手，是实打实的
  成本；剔出去准确率会虚高成 100%，而真实情况是每十几条里有一条要人工。
- **`unknown` 组漏的那 1 条是唯一会赔钱的错**：「漏发了两个」被判成 `wrong_item`。
  这条恰好是 `maos/ingress/classify.py` 模块头拿来当「判不准」的标准例子 ——
  判出个 code 就会套上政策一路跑到批款。词表和模型都拦不住它。

### 五岗圆桌 + 聊天截图

12 单全部跑完，12 张图**全部按文件名前缀配上了单**：

```text
随案证据 chat/：ORD-2026-1000 配 1 份；ORD-2026-1003 配 1 份；……（12 单各 1 份）
```

配上证据的单子，证据核验岗给 `complete`（规则要求 image、最少 1 份，实收 1 份）。

## 这份底账多了什么机制

`scenarios/custom/ledger.json` 的三条政策都只有 v1，所以「政策版本锁在订单上」
这句话在那份数据上**看不见** —— 改不改版本号跑出来一模一样。

这里给 `AS-001` 加了 v2（2026-06-01 生效，窗口从 30 天收到 15 天、退款比例 0.98），
订单的 `policy_version_at_order` 按付款时间自动落在 v1 或 v2。于是同一张表里：

```text
ORD-2026-1043  七天无理由  批准  AS-001@v2     第 5 天申请，5 ≤ 15
ORD-2026-1047  七天无理由  驳回  AS-001@v1     第 33 天申请，33 > 30
ORD-2026-1058  七天无理由  驳回  AS-001@v1     第 67 天申请，67 > 30
```

`ORD-2026-1047` 那条聊天截图演的就是这一幕：客户问「怎么改成十五天了」，
客服答「6 月 1 日起新单适用 v2，您这单下单时就锁定了这一版」。

## 聊天截图：画的是文字，不是照片

`scenarios/custom/evidence/` 那两张是 96×96 噪点图，理由是证据核验岗核的是
「有没有、digest 空不空、类型对不对」，不是图里画了什么，用真照片只会给仓库
添一份要脱敏的东西。**那个理由这里依然成立 —— 这 12 张也没有一张真照片。**

不同的是它们画的是**编出来的对话**。编的文字不需要脱敏，而比噪点多给两样：

1. 答辩/演示时能投出来给人看，噪点图投出来只能说「这里有张图」；
2. 将来接视觉抽取时有考题 —— AP 那条通道已经在做「发票图片 → 抽取 → 待复核表」
   （`docs/ap-entry.md`），聊天截图是退款域同形的那一道。

**确定性只到本机**：对话逐条写死、渲染不含随机，同一台机器跑两次逐字节一致；
换台机器字体版本不同、字形就不同。这与噪点图那条「纯 stdlib 跨机可复现」是
真实差别，别混着承诺。

字体走 `Hiragino Sans GB` → `STHeiti` → `Songti`，一个都找不到就**当场停**：
退到 PIL 默认位图字体只会画出一版全是方框的图，比报错更难查。

## 怎么改、怎么加量

**别手改落盘的四份文件**，改生成器再重跑 —— 手改的那一行下次重跑就没了。

```bash
python3 scenarios/bulk/generate.py           # 重新生成四份
python3 scenarios/bulk/generate.py --check   # 校验：与重跑结果逐字节一致
python3 scenarios/bulk/gen_chat.py           # 重新渲染 12 张图
```

| 想要 | 改哪 |
| :-- | :-- |
| 更多单子 | `generate.py::ORDER_COUNT`（订单号跟着 `ORD-2026-1000` 往后排） |
| 换一批数据 | `generate.py::SEED` |
| 更多客户原话 | `lexicon_corpus.py::REAL_SAYINGS`，加完直接进评测集 |
| 新的商品/质保档 | `generate.py::PRODUCTS` |
| 加一段对话 | `gen_chat.py::CONVERSATIONS` **和** `generate.py::CHAT_ORDERS`，两处 |

最后一行的两处有断言钉着：对不上时 `gen_chat.py` 当场抛。不钉的话症状是哑的 ——
图落了盘、`--evidence` 报一行「认不出订单号」就跳过、圆桌照跑，只是每一单的
证据核验都判 missing，而那正是配了图要演的反面。

## 与守卫的关系

`ledger-bulk.json` 有非空 `policy_rule`，所以被
`maos/tests/test_refund_corpus_rule_no.py` 扫到，已登记进该文件的 `COVERED`。
抬头那句租户作用域说明由生成器写进 `_rule_no_scope`（三个固定措辞 + 点名 `tnt-demo`）。

> 本文件的 `AS-00x` 编号只在租户 `tnt-demo` 内有意义。
> 同一个编号在别的租户语料里指的是另一条规则 —— **跨语料引用规则号前先看租户**。

删掉本目录时，记得把 `COVERED` 里那一行一起删，否则那条守卫会报「缺失」。

## 边界

- **没有判据**。这批数据不比对任何期望值，`reason-eval.csv` 除外（它每行自带期望
  code）。要看「同一份数据两种判法结论相反」的对照实验，跑 `python3 run.py --contrast`。
- **`refund-requests-raw.csv` 跑不满 60 单是对的**。判不准的单子被挑出来等人工，
  不进入处置 —— 那是安全出口，不是失败（`maos/ingress/classify.py`「判不准 ≠ 填错了」）。
- **跑 `eval_reason.py` 和原话表会调真模型**（配了 key 时），一条说法一次调用。
  114 条评测集跑一轮就是 114 次。没配 key 时不出网，`real` 组整组落未判。
