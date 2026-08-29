# 贡献指南

MAOS 是一个**协议先行、证据先行、最小权限**的多 Agent 协作运行时。
这三条不是口号，是仓库里有机器守卫和测试盯着的东西 —— 下面每一条规则都能在
[`CLAUDE.md`](CLAUDE.md)（全局铁律，每个会话自动加载）或
[`docs/EXECUTION.md`](docs/EXECUTION.md)（执行手册）里找到出处。

> 本仓库是 **Python**。历史上有过一版 TypeScript 实现，现封存在 `legacy-ts/`，
> **权威以 `maos/` 为准**。任何让你跑 Node 包管理器或 `tsc` 的材料都是过期的，
> 本文件里每一条命令都是当前真实可跑的。

---

## 1. 先跑起来（不需要任何 API key）

核心零依赖，只要 Python ≥ 3.10。本机没有 `python` 命令时一律敲 `python3`
（README 与本文件里的每条命令都已经是 `python3`）：

```bash
git clone -b goai-restructure <本仓库地址> maos && cd maos
python3 -m pytest maos/tests -q     # 全量测试
python3 run.py                      # 场景 1-7 端到端，exit=0
python3 scripts/make_evidence.py    # ① 生成证据束
python3 scripts/verify.py           # ② RESULT: 7/7 PASS
```

🔴 **`-b goai-restructure` 不能省。** 仓库的 GitHub 默认分支目前是 `main`，
而 `main` 上是已封存的早期 TypeScript 实现 —— 裸 clone 会拿到一份没有 `maos/` 包、
没有 `scripts/` 的骨架，上面这些命令一条都跑不了，而 `git clone` 本身会安静地成功。
（这条已记进 [`docs/BACKLOG.md`](docs/BACKLOG.md) 的 `## task-T8`，
待人类在仓库设置里把默认分支改过来。）

**①② 缺一不可、顺序不能换**：`*.db` 不入库，核验器要的是库不是快照，
直接跑 ② 会报「缺数据库」并退出 2 —— 这是设计行为，不是故障。
跑完 `git status` 会有 50 行 `evidence/` 改动，也是预期；
**不要用 `git checkout -- evidence/` 单独「收拾干净」**，
那会让新库配旧快照、核验掉到 3/7，看上去像证据被伪造。
两条实测过的出路见 [README §3](README.md#3-一条命令核验这一节是给评委的)。

不清楚跑出来的数字对不对，跑一次演示前置检查，它把期望值写死在里面：

```bash
bash scripts/demo_preflight.sh
```

## 2. 提交前必须跑的三条

```bash
python3 -m pytest maos/tests -q       # 必须全绿：存量 + 你新增的
python3 scripts/gen_docs.py --check   # 代码生成的三份文档与代码一致
git diff --stat maos/contracts/       # 必须空输出（冻结契约未被动过）
```

三条都绿才许 commit（铁律 2、铁律 5）。改了代码而没重新生成文档，第二条会告诉你哪份漂了。

## 3. 碰不得的面

这些不是「建议不要动」，是**动了就是 bug**：

| 面 | 规则 | 出处 |
| :-- | :-- | :-- |
| `maos/contracts/events.py`、`maos/contracts/states.py` | **禁止任何修改**。新业务域必须用现有状态机跑通 —— 这是「领域无关」的证明，做不到说明抽象错了，停下来讨论抽象，不要加状态 | 铁律 1、铁律 9 |
| `maos/core/store.py` 现有表结构 | **禁改，只允许新增表**。新业务域的表都是新增表 | 铁律 1 |
| `evidence/**` | **只放真实命令输出**。每个文件首行必须是 `# generated at <ISO8601> from <git sha>`，由生成脚本自动写入。**禁止手写或编造**，也禁止把旧读数改写成当前值 —— 那等于伪造那一次的运行结果 | 铁律 3 |
| 业务对象的权威状态 | MAOS **不持有权威事实**，只持有观察与推断。订单、支付、库存的权威状态永远归属外部系统。任何把外部状态直接写死为终态的代码都是 bug | 铁律 8 |
| 密钥 | **只读环境变量**，禁止写进任何文件，也禁止让它出现在 `evidence/` 的任何输出里 | 铁律 6 |

契约面由三重机制强制，不是口头约定：仓库配置的 deny 规则、一个 PreToolUse 守卫脚本
（封 shell 侧路）、以及 `maos/tests/test_contracts_frozen.py` 的指纹校验。
指纹存在 `.contracts.lock`，那个文件和守卫脚本本身也在禁改面里。

## 4. 提交规范

```
<type>(p<N>): <一句话>
```

`<type>` 用 `feat` / `fix` / `docs` / `chore`，`p<N>` 是所属 Phase（见 `docs/phases/`）。
例：`feat(p3): refund domain objects, 6 skills, gateway toolport, settled guard`。

- 一个 Phase 至少一个 commit，**验收全绿才许 commit**（铁律 5）。
- `git add` 逐文件点名，不要 `git add -A` / `git commit -a` —— 证据束和两本账
  经常有并行改动，一把梭会把别人的东西带进你的提交。
- **推送由人做**：自动化会话只许本地 commit，禁止 push（铁律 5）。

## 5. 两本账：发现的问题往哪写

**不做范围外的「顺手优化」**（铁律 4）。这条比它看起来重要 —— 顺手改掉的东西
没有测试守着，也没人知道为什么改。

- 发现了问题但不当场改 → 追加一行到 [`docs/BACKLOG.md`](docs/BACKLOG.md)
- 做了手册没覆盖的判断、或有意偏离了既定做法 → 追加一行到
  [`docs/DECISIONS.md`](docs/DECISIONS.md)，格式 `<日期> | Phase N | 情境 | 选择 | 理由`（铁律 7）

两本账都**保留在仓库里**，不要为了好看删掉 ——「知道自己哪里没做完」本身是可信度的一部分。

## 6. 写测试

先写会失败的测试，再写实现。测试放 `maos/tests/`，不要写进源码树。
涉及沙箱的测试用 `MAOS_SANDBOX_FORCE_SUBPROCESS=1` 可以在没有 Docker 的机器上跑。
模拟适配器里**不许**出现生产凭证、客户数据或真实网络副作用。

## 7. 安全

发现安全问题**不要开公开 issue**，按 [`SECURITY.md`](SECURITY.md) 私下联系。
