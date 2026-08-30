# 新克隆冒烟报告 —— 无 key、掐表、从零跑到 verify 7/7

> ⏱ **时效声明（2026-08-29 补记）。本报告是历史执行记录，不是当前 HEAD 的实测。**
> 三遍冒烟分别钉死在 `42822fc`（第一遍）/ `d213ef4`（第二遍）/ `5ea6890`（第三遍，整合轮 5），
> 当前主干 HEAD 已是 `c1049c2`，其后仍有多轨在合入。
>
> **已知会随合入变动的读数**：`pytest` 条数、`verify.py` 七项的分子分母、各步耗时。
> 这些数字请以当前 HEAD 实跑为准，**不要直接引用本报告的读数**。
>
> **不随合入变动的是结构性结论**：零出网、无任何 API key 可跑、从全新克隆一次通到
> `RESULT: 7/7 PASS`、跑完工作区 50 行脏、掐表远在 15 分钟预算内。
> 这几条编排侧已在 `c1049c2` 上复跑确认仍成立。
>
> <!-- PENDING-R9: 本报告的读数**只能由一次仓库外全新 clone 的冒烟实跑**产出。
>      整合轮 8 没有做第四遍冒烟，因此**不填** —— 拿仓库内的 703 passed 冒充一次
>      没做过的克隆冒烟，正是本报告 §5 与 DECISIONS 反复禁的那件事。
>      待办已记进 docs/BACKLOG.md 的 ## integrate-round-8。 -->

对应 `docs/submission-checklist.md` A-1 的两条：「新克隆冒烟，严格按 README 从零跑到
`verify.py` 7/7，掐表 ≤ 15 分钟」与「冒烟用的是没有任何 API key 的环境」。
本报告是这两条的执行记录，**三遍冒烟都在仓库外的全新 clone 里做**，本仓库
`evidence/` 一个字节未动。

执行人视角是刻意设定的：**只有这个仓库、没有上下文、没有 key 的评委**。凡是「我知道
该怎么办所以跳过去了」的地方，一律记成一条 README 缺口，不用已有知识救场。

---

## 1. 环境与口径

| 项 | 值 |
| :-- | :-- |
| 克隆命令（第一遍） | `git clone <本仓库地址> /tmp/maos-smoke-z5-r2 && git checkout -q 42822fc` |
| 克隆命令（第二遍） | `git clone -b task/z5-clone-smoke <本 worktree> /tmp/maos-smoke2-z5` |
| 克隆命令（第四遍） | `git clone --single-branch -b integrate/round-6 <本仓库> <目标目录>` |
| 第一遍基线 sha | `42822fc`（钉死，见下） |
| 第二遍基线 sha | `d213ef4`（本轨 README 改动已 commit） |
| `python3 --version` | `Python 3.11.7` |
| `which python` | **不存在** —— 本机没有 `python` 命令，README 里每条命令都必须是 `python3` |
| `docker info` | `exit=0` —— **本机 Docker 可用**，沙箱走容器路径，不是降级路径 |
| 网络 | 全程零出网（判据见 §3 末「没有成为卡点的」表第 2 行） |

**钉基线的说明**：派单要求 `git clone -b goai-restructure` 后应得 `42822fc`，实跑得到的是
`f42ea83` —— 主干在派单写就后又推进了 `c3c30bc` / `f42ea83` 两个 commit。先查增量：
两个 commit 只动 `CLAUDE.md` 与 `docs/ops/ORCHESTRATION.md`，**不碰 `README.md` /
`scripts/**` / `maos/**`**，对冒烟结论零影响。为让本报告的每个读数都能被同一个 sha
一字不差复现，第一遍钉死 `42822fc`。已记 `docs/DECISIONS.md ## task-Z5` 第 1 条。

**环境清洁**。本机 `env | grep -E '^(MAOS_|MATRIX_|ANTHROPIC_|OPENAI_)'` 本来就是空的，
但两遍冒烟仍在脚本开头显式 unset 了全部 20 个变量再校验残留为空，证明结论不依赖本机状态：

```
MAOS_LLM_BASE_URL MAOS_LLM_API_KEY MAOS_LLM_MODEL MAOS_LLM_TIMEOUT MAOS_LLM_TOKEN
MAOS_SANDBOX_FORCE_SUBPROCESS MAOS_SANDBOX_TIMEOUT MAOS_SANDBOX_WORKDIR
MAOS_MAX_REPLAN MAOS_KB_ENABLED MAOS_KB_WEIGHTS MAOS_APPROVERS
MAOS_FINANCE_THRESHOLD MAOS_STORE_BACKEND MAOS_PG_DSN MAOS_PKG MAOS_RELOCK
MATRIX_TOKEN ANTHROPIC_API_KEY OPENAI_API_KEY
```

---

## 2. 逐步耗时表

### 第一遍（旧 README，基线 `42822fc`）

> ⚠️ **这一栏是「修复前」的对照，不是现状。** 下表 3 个 ❌ 记的是**旧 README** 的问题
> （`maos-runtime` 目录名不存在、`verify.py` 作为第一条命令必然失败、跑完 50 行脏未写明），
> **三条都已在第二遍（新 README，`d213ef4`）逐条验证修复** —— 见下一小节，同样的路径全 ✅。
> 保留第一遍是为了看得出改了什么；删掉它，这份报告就成了粉饰。

| # | 命令原文 | 耗时 | exit | 与 README 是否一致 |
| :-- | :-- | --: | --: | :-- |
| 1 | `git clone …` | 0.78s | 0 | ❌ §4 写的是 `git clone <repo> && cd maos-runtime`，**`maos-runtime` 不是任何真实产物的目录名** |
| 2 | `python3 scripts/verify.py` | 0.07s | **2** | ❌ 这是 README **第 10 行、第一条命令**，注释写着「评委的一条命令」，而新克隆上它必然失败 |
| 3 | `python3 scripts/make_evidence.py` | 4.37s | 0 | ✅ 与 §3 ① 一致 |
| 4 | `python3 -m maos.kb.experiment` | 0.51s | 0 | ✅ 与 §3 ② 一致 |
| 5 | `python3 scripts/verify.py` | 0.11s | 0 | ✅ `RESULT: 7/7 PASS`，七行分子分母与 §3 贴的读数**逐字节一致** |
| 6 | `python3 -m pytest maos/tests -q` | 7.71s | 0 | ✅ `521 passed` |
| 7 | `python3 run.py` | 2.55s | 0 | ✅ 场景 1–7 跑满 |
| 8 | `git status --porcelain` | 0.04s | 0 | ❌ **50 行改动**，README 全文没有一个字提到跑完工作区会变脏 |
| | **掐表：clone → `RESULT: 7/7 PASS`** | **6.57s** | | 预算 15 分钟，**用掉 0.7%** |
| | 全程（含 pytest + run.py） | 17.06s | | |

### 第二遍（新 README，基线 `d213ef4`）

严格照改后的 README 从第一屏往下走，一步都不自己补。

| # | 命令原文 | 耗时 | exit | 脏行 | 与 README 是否一致 |
| :-- | :-- | --: | --: | --: | :-- |
| 1 | `git clone <地址> maos && cd maos` | 0.91s | 0 | 0 | ✅ 目录名由命令自己给定，不再写死 |
| 2 | `python3 scripts/make_evidence.py`（抬头 ①） | 4.48s | 0 | 43 | ✅ |
| 3 | `python3 -m maos.kb.experiment`（抬头 ②） | 0.57s | 0 | 50 | ✅ |
| 4 | `python3 scripts/verify.py`（抬头 ③） | 0.11s | 0 | 50 | ✅ `RESULT: 7/7 PASS` |
| 5 | `python3 -m pytest maos/tests -q` | 7.94s | 0 | 50 | ✅ `521 passed` |
| 6 | `python3 run.py` | 2.45s | 0 | 50 | ✅ |
| 7 | `python3 run.py --scenario 7` | 0.28s | 0 | 50 | ✅ |
| | **掐表：clone → `RESULT: 7/7 PASS`** | **6.44s** | | | **一次通到 7/7，零卡点、零非零退出** |
| | 全程（README §4 全部命令跑满） | 17.37s | | | |

### 第三遍（整合轮 5，基线 `5ea6890`）—— Y-3 收敛成两条命令之后

Y-3 合并后 `make_evidence.py` 缺省一并产出 `scenario-R5`，复现路径从三条命令变两条。
**全新克隆 + 显式 unset 全部 `MAOS_*` / `MATRIX_*` / key 变量**，逐步实测：

| # | 命令原文 | 耗时 | exit | 脏行 |
| :-- | :-- | --: | --: | --: |
| 1 | `git clone …` | 0.81s | 0 | 0 |
| 2 | `python3 scripts/make_evidence.py`（①） | 6.15s | 0 | 50 |
| 3 | `python3 scripts/verify.py`（②） | 0.10s | 0 | 50 |
| 4 | `python3 -m pytest maos/tests -q` | 9.17s | 0 | 50 |
| 5 | `python3 run.py` | 2.28s | 0 | 50 |
| 6 | `python3 run.py --scenario 7` | 0.26s | 0 | 50 |
| 7 | `python3 scripts/gen_docs.py --check` | 0.14s | 0 | 50 |
| | **掐表：clone → `RESULT: 7/7 PASS`** | **5.4s**（另一次独立复跑） | | |
| | 全程（七条跑满） | **18.3s** | | |

三处与前两遍不同：

- **少敲一条命令**。原来的 ②（`python3 -m maos.kb.experiment`）不再需要单独敲，
  「只跑 ①③ 会卡在缺 `scenario-R5/maos.db`」那个坑随之消失。
- **`pytest` 从 `521 passed` 变 `571 passed`**（Y 轮三轨各带新测试进来）。
- **`make_evidence.py` 从 4.4s 涨到 6.2s** —— 它现在多跑一个 R5 场景，
  原来那 0.5s 是单独敲第二条命令花的，账没变多，只是并到一条里了。

**没变的**：`git status` 跑完仍是 **50 行 M**；`scenario-R5` 的 7 个文件首行 sha
**仍带 `-dirty`**（见 §3 卡点 6 的补记）。

### 第四遍（整合轮 6，基线 `e6075e5`）—— 合 D-1 + D-2 之后

D-1（rework 第三出口）与 D-2（第六道闸 plan 级判据）并入后，七项证据里有四项的
分母涨了、`pytest` 从 596 涨到 645。**这一遍是来验「合并之后，一个只有仓库、
没有 key 的人还能不能从零跑到 7/7」的**，不是来刷秒数的。

| # | 命令原文 | 耗时 | exit | 脏行 |
| :-- | :-- | --: | --: | --: |
| 1 | `git clone --single-branch -b integrate/round-6 …` | 1.76s | 0 | 0 |
| 2 | `python3 scripts/verify.py`（**出厂态直接跑 ②**） | 0.04s | **2** | 0 |
| 3 | `python3 scripts/make_evidence.py`（①） | 5.04s | 0 | 50 |
| 4 | `python3 scripts/verify.py`（②） | 0.10s | 0 | 50 |
| 5 | `python3 -m pytest maos/tests -q` | 10.50s | 0 | 50 |
| 6 | `python3 run.py` | 2.52s | 0 | 50 |
| 7 | `python3 run.py --scenario 7` | 0.28s | 0 | 50 |
| 8 | `python3 scripts/gen_docs.py --check` | 0.13s | 0 | 50 |
| | **掐表：clone → `RESULT: 7/7 PASS`** | **6.89s** | | |
| | 全程（八条跑满） | **20.4s** | | |

第 2 行是**刻意跑的**：README 抬头说「直接跑 ② 会报缺数据库并退出 2，这是设计行为」，
这一遍逐字复核了它 —— `[FAIL] 无法开始核验：缺数据库: …/evidence/scenario-1/maos.db
（先跑 python3 scripts/make_evidence.py）`，退出码 **2**，且错误消息自己给出了正确的
下一步。**这条仍然成立**，合并没有把它弄坏。

七项读数与在整合轮 6 工作区内实测的**逐字节一致**，这是这一遍最要紧的一条：

```
[PASS] hash-integrity       86/86
[PASS] business-ref         35/35
[PASS] authoritative-fact   3/3
[PASS] trace-tree           19/19
[PASS] kb-hit               7/7
[PASS] business-outcome     10/10
[PASS] history-case         1/1

RESULT: 7/7 PASS
```

`warn` **12 行 / 3 类**，`pytest` **645 passed**（第三遍是 571，其间隔着 Y-4 的 25 条、
D-1 的 26 条、D-2 的 23 条）。

⚠️ **无 key 这个条件，这一遍是怎么成立的与前几遍不同，值得写清楚。** 第三遍是
**显式 unset 了 20 个**变量；这一遍按同一套规则（所有 `MAOS_*`，以及名字里含
`API_KEY` / `BASE_URL` / `LLM` 或各厂商名的变量）过滤执行环境，实测**抹掉 0 个**
—— 环境里本来就一个都没有。结论（无 key 能跑完全程）不变，但**它这次不是被 unset
保证的，是恰好没有**。谁要在带 key 的机器上复现这一遍，仍须显式 unset，否则这一遍
的执行条件并没有被真正复制。

顺带实测了 §3 卡点 3 给的**乙方案**（`find evidence -name 'maos.db' -delete &&
git checkout -- evidence/`）：跑完后 `git status` **脏行归 0**，确实回到出厂态，
且此后直接跑 ② 就是上面第 2 行那个 `exit=2`。两条出路里的这一条，本轮复核有效。

**没变的**：`git status` 跑完仍是 **50 行 M**；`scenario-R5` 的 7 个文件首行 sha
**仍带 `-dirty`** —— 且这一遍证明了它**不是某个工作区的特产**，全新克隆一样有，
根因见 `docs/BACKLOG.md` 的 `## integrate-round-6` 第 1 条（`make_evidence.py`
同一次运行内部：场景 1-7 先落盘把工作区弄脏，排在最后的 R5 取 sha 时就读到脏状态）。

---

**对 15 分钟预算的结论**：四遍都远在预算内（6.57s / 6.44s / 5.4s / 6.89s，约占预算的 0.8%）。
**但这个数字本身不说明 README 好用** —— 掐表只量机器时间，而第一遍真正的成本全部
落在「照 README 走不通、要回头猜」上，那部分不体现在秒数里。见下一节。

**关于「卡多久」的口径**：本报告**不编造墙钟时间**。第一遍的执行者（我）带着已知信息，
无法诚实地模拟一个真人卡住的分钟数。所以卡点一律用两个可复核的量代替：
**要跨读 README 几节才能脱困**，以及**照错误提示做会失败几次**。这两个量是客观的。

---

## 3. 卡点清单

### 卡点 1 —— README 第一条命令，在新克隆上必然失败

- **卡在哪**：README 第 9–11 行的代码块，注释明写「评委的一条命令」。新克隆直接跑
  `python3 scripts/verify.py` → `[FAIL] 无法开始核验：缺数据库: evidence/scenario-1/maos.db`，**exit=2**。
- **量**：这是评委看到的**第一屏第一条命令**；脱困要跨读到 §3（约 80 行之后）。
- **没有上下文的人会怎么误解**：「这项目连自己写的第一条命令都跑不起来。」
  README §3 确实解释了「`*.db` 不入库、这是设计行为」，但抬头那条命令上**没有任何指向 §3 的线索**。
- **最小修法**：抬头块换成完整的 ①②③ 三条，并把「为什么直接跑 ③ 会退出 2」压缩成一段
  引用块写在紧挨着的位置。**已做**（README 修正 1）。

### 卡点 2 —— 照错误提示做，会原地打转

- **卡在哪**：卡点 1 的报错提示是「先跑 `python3 scripts/make_evidence.py`」。照做，再 `verify.py`：
  ```
  [FAIL] 无法开始核验：缺数据库: evidence/scenario-R5/maos.db（先跑 python3 scripts/make_evidence.py）
  exit=2
  ```
  提示**一字不差还是那句**。再照做一次，**还是同一个错**。本轨实测跑到第三次确认无出路 ——
  `make_evidence.py` 按 `ALL_SCENARIOS` 跑 1–7，永远不产 `scenario-R5`。
- **量**：照提示做**失败次数无上限**；脱困唯一的路是读到 §3 的 ⚠ 块。
- **没有上下文的人会怎么误解**：「是我漏了什么，还是这脚本坏了？」——而屏幕上没有任何
  信息能区分这两者。这是评委最可能踩的一脚。
- **附带的恶化**：第二次跑 `make_evidence.py` 时工作区已经不干净，证据首行的 sha 变成
  `-dirty`，把原本字节稳定的 `scenario-1..4` 也一并改脏，**脏行数从 43 涨到 50 再往上**，
  观感上像「越修越坏」。
- **最小修法**：文档侧已在抬头块写死「②不能省、只跑 ①③ 会原地打转」（README 修正 1）。
  **根治要改 `scripts/verify.py` 的报错分支，属 Y-3 的面，本轨不碰** ——
  该账 `docs/BACKLOG.md ## task-W5` 第 2 条已详细记过，本轨复现一次后不重复开条。

### 卡点 3 —— 🔴 最伤的一条：把工作区「收拾干净」，证据就变成「伪造的」

- **卡在哪**：跑完 ①②③ 拿到 7/7 之后，`git status` 有 **50 行**改动。一个懂 git 的评委的
  本能反应是 `git checkout -- evidence/` 收拾一下，然后**再核验一遍**。结果：
  ```
  [FAIL] hash-integrity       4/74
  [FAIL] business-ref         0/33
  [FAIL] trace-tree           10/18
  [FAIL] business-outcome     0/10
  RESULT: 3/7 PASS            exit=1
  ```
- **根因**：`evidence/*.json` 入 git 而 `evidence/*/maos.db` **不入 git**。`checkout` 只还原了
  json 快照，现跑出来的新库还在原地 —— 核验器拿**新库**校验**旧快照**，当然对不上。
- **没有上下文的人会怎么误解**：**这是本轨发现的最坏一条。** 卡点 2 的表现是 exit=2 加一句
  「缺数据库」，评委知道自己少跑了一步；这一条的表现是 `hash-integrity 4/74`，而 README §3
  的失败释义表对这一项的原话是「**证据被篡改或事后手写**」。一个刚跑出 7/7、顺手收拾了
  一下、又复核了一遍的评委，拿到的结论是**这个项目的证据束是伪造的** —— 而整件事只是
  两边不同步。这一条比卡点 2 更危险，因为它**不像故障，像结论**。
- **量**：触发它不需要读错任何文档，只需要有 git 习惯；旧 README 里**没有任何一处**提到过
  工作区会变脏，更没有提到不要单独还原。
- **最小修法**：README §3 写明「跑完 50 行是预期」+「不要单独 `git checkout`」+ 两条**实测过**
  的出路。**已做**（README 修正 3）。根治要动 `scripts/`，**已记 `docs/BACKLOG.md ## task-Z5` 第 1 条**。

### 卡点 4 —— §4「5 分钟快速开始」走完，到不了 7/7

- **卡在哪**：§4 只有 `pytest` + `run.py` + `run.py --scenario 7` 三条，**没有 verify**。
  而 verify 在 §3 —— §3 排在 §4 **前面**，且 §3 从不提「先 clone」。于是 README 里
  **不存在一条从头读到尾、能把人从 clone 送到 7/7 的路径**：clone 的说明在 §4，
  到 7/7 的说明在 §3，两段互不引用。评委必须自己把两节拼起来。
- **没有上下文的人会怎么误解**：「快速开始都走完了，评委关心的 7/7 到底在哪跑？」
- **最小修法**：§4 的命令块补上证据链三条并注明「到这里只跑了代码，要看到 7/7 还差这三条」。
  **已做**（README 修正 4）。

### 卡点 5 —— `cd maos-runtime`：目录名对不上任何真实产物

- **卡在哪**：README §4 原文 `git clone <repo> && cd maos-runtime`。仓库目录叫 `MAOS`，
  clone 出来叫什么取决于 URL，**`maos-runtime` 不对应任何真实产物**。照抄这一行 → `cd` 失败。
- **没有上下文的人会怎么误解**：这一条误解成本低（`ls` 一下就知道），但它出现在
  「5 分钟快速开始」的第一行，第一印象是**这份 README 没人照着跑过**。
- **最小修法**：改成 `git clone <本仓库地址> maos && cd maos`，目录名由命令自己给定，
  并补一句「clone 出来的目录名由你给的地址决定」。**已做**（README 修正 4）。

### 卡点 6 —— `kb.experiment --help` 不打用法，直接开跑并改脏工作区

- **卡在哪**：README 抬头与 §3 把两条命令并列成 ①②，评委很自然会对两条都敲 `--help`。
  实测：
  - `python3 scripts/make_evidence.py --help` → 正规 argparse，打印 `usage: make_evidence [-h] [--out OUT] …`，**不写任何文件**，脏行 0。
  - `python3 -m maos.kb.experiment --help` → **无视参数直接开跑**，落盘 `evidence/scenario-R5/` 七个文件，**脏行 7**，exit=0。
- **没有上下文的人会怎么误解**：踩到的人不会意识到是自己触发的，只会看到 `git status`
  又多了 7 行。与卡点 3 叠加时尤其糟 —— 他此刻正在琢磨「工作区怎么又脏了」。
- **最小修法**：给 `maos/kb/experiment.py` 的 `__main__` 补一个真 argparse。
  **`maos/kb/**` 不在本轨可改面内，已记 `docs/BACKLOG.md ## task-Z5` 第 2 条。**

### 没有成为卡点的（逐条验过，全部属实）

| 验了什么 | 结论 |
| :-- | :-- |
| README 里有没有裸 `python` 命令 | `grep -nE '(^|[^3a-zA-Z_])python([^3a-zA-Z_]|$)' README.md` → **零命中**，改前改后都是零。本机 `which python` 不存在，这一条 README 是干净的 |
| 无 key 能不能跑完全程 | **能**。20 个变量全 unset 后，clone→7/7→pytest→run.py 全部 exit=0。代码侧判据：`maos/model/client.py:select_model_client` 在 `MAOS_LLM_BASE_URL/API_KEY/MODEL` 三者缺任一时降级 `ScriptedModelClient` 并只记缺失变量名。**口径说明**：这是「代码路径 + 实跑」两重判据，本轨**没有做物理断网**测试，不宣称做过 |
| §3 贴的七行实跑读数还准不准 | **准**。基线 `42822fc` 实跑与 README 贴的逐字节一致（`74/74`、`33/33`、`3/3`、`18/18`、`4/4`、`9/9`、`1/1`）。只有「基线 `df96fa8`」这个 sha 标注过期了，已刷 |
| `gen_docs.py --check` 在新克隆上过不过 | **过**，exit=0，三份代码生成文档与代码逐字节一致 |
| `run.py` 会不会也改脏 `evidence/` | **不会**。逐步实测：clone 0 行 → ① 43 行 → ② 50 行 → ③ 50 行 → `run.py` 50 行 |
| 跑完会不会有 `*.db` 出现在 `git status` 里 | **不会**，`.gitignore` 挡得住，50 行全是 `evidence/**/*.json` 与 `run.log` |

---

## 4. README 修正清单

| # | 位置 | 改前 | 改后 | 对应卡点 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | 抬头第 9–11 行 | 单条 `python3 scripts/verify.py  # 评委的一条命令` | 完整 ①②③ 三条 + 一段引用块：「三条一条都不能省；直接跑 ③ 退出 2；只跑 ①③ 会卡在 `scenario-R5/maos.db` 且照提示做会原地打转」 | 1、2 |
| 2 | §3「本机实跑」标注 | 基线 `df96fa8` | 基线 `42822fc`，并注明是**全新克隆 + 无任何 API key**，附本报告链接 | — |
| 3 | §3 ⚠ 块之后（新增一段） | 无 | 「跑完 `git status` 有 50 行是预期」+ 🔴「不要用 `git checkout -- evidence/` 收拾」+ 两条**实测过**的出路（甲：重跑 ①② 回 7/7；乙：`find evidence -name 'maos.db' -delete && git checkout -- evidence/` 回出厂态）+ 一句「只 checkout 不删库是唯一会得出错误结论的那条路」 | 3 |
| 4 | §4 命令块 | `git clone <repo> && cd maos-runtime` + 三条代码命令 | `git clone <本仓库地址> maos && cd maos` + 三条代码命令 + **证据链三条**（注明「到这里只跑了代码，要看到 7/7 还差这三条」）+ 一句「clone 出的目录名由你给的地址决定」 | 4、5 |
| 5 | §4 块尾（新增） | 无 | 实测读数：全部跑完约 17 秒，最短路径约 7 秒；跑完 50 行脏属预期，别单独 `git checkout`，指向 §3 | 3 |

**README 救不了、已记 BACKLOG 的**（`docs/BACKLOG.md ## task-Z5`，两条）：

1. `evidence/*.json` 入库而 `maos.db` 不入库导致的失同步 —— 根治要改 `scripts/verify.py`
   的失配处置（建议：先比对库与快照的生成时间戳，报「不同步」而不是判 FAIL）。**卡点 3 的根因。**
2. `maos/kb/experiment.py` 的 `__main__` 缺 argparse —— **卡点 6 的根因。**

**已有账、本轨不重复开条**：卡点 2 的死循环属 `docs/BACKLOG.md ## task-W5` 第 2 条，
本轨在全新克隆上复现一次，结论与出路与该条完全一致，只补了一条实测读数（照提示重跑
会让脏行数上涨，观感像「越修越坏」）。

---

## 5. 结论

- **A-1 两条都过了**：第二遍冒烟从全新克隆一次通到 `RESULT: 7/7 PASS`，零卡点、
  零非零退出，掐表 **6.44 秒**，远在 15 分钟预算内；全程在 **20 个环境变量全部 unset**
  的无 key 环境里完成。
- **但掐表这个指标本身没有区分力**：第一遍的机器时间同样只有 6.57 秒，而它有 6 处卡点，
  其中一处会让评委得出「证据是伪造的」结论。**验收单 A-1 建议把「≤ 15 分钟」补一句
  「且全程零非零退出、不需要跨节拼路径」** —— 否则旧 README 也能「通过」这一条。
- 🔴 **Y-3 合并后，本轨的冒烟结论必须重跑一遍。** Y-3 正在把 ①② 合并成一条命令，
  届时 README 抬头块、§3、§4 的三条链**全部会变**，本报告的两张耗时表、卡点 2 与
  卡点 6 的表述都要跟着重做。**在 Y-3 合并前，不要把本报告当作最终版引用。**

---

## 待整合轮 5 回填

| # | 触发轨 | 要回填什么 | 落点 |
| :-- | :-- | :-- | :-- |
| 1 | **Y-3** | ①② 合并成一条命令后，README **抬头块**（第 9–11 行区域）、**§3 的 ①②③ 命令块**、**§4 块尾的三条**全部改写成新的单条形式；两处 `<!-- Y轮易变（Y-3）… -->` HTML 注释一并删掉（`grep -n 'Y轮易变' README.md` 可定位） | `README.md` 抬头 / §3 / §4 |
| 2 | **Y-3** | **本报告的两张耗时表与卡点 2、卡点 6 需按新命令重跑重写**；§5 结论段的「Y-3 合并后必须重跑」一句届时删除 | `docs/clone-smoke-report.md` §2 §3 §5 |
| 3 | **Y-3** | 卡点 2 引用的 `## task-W5` 第 2 条若被 Y-3 消解，BACKLOG 该条应标记为已解；`## task-Z5` 第 2 条（`kb.experiment` 缺 argparse）随之自动消解 | `docs/BACKLOG.md` |
| 4 | **Y-1** | `verify.py` 的 4 条 warn 自动消失后，README §3 里「两项会附若干 `warn:` 行」那段要删或改写。**本轨没有在 README 里新增任何 warn 相关文字**，只需处理原有那一段 | `README.md` §3 |
| 5 | **Y-2 / Y-4** | 场景 6 的 RAG 检索、场景 7 的换渠道重试有话可说之后，README §5 七个场景表的对应两行描述要刷。**本轨未动 §5** | `README.md` §5 |
| 6 | 编排侧 | 本报告第一遍钉的基线 `42822fc` 与第二遍的 `d213ef4` 在后续合并后都会成为历史 sha；若要重跑，先刷 §1 的两行克隆命令 | `docs/clone-smoke-report.md` §1 |

---

## 整合轮 6 冒烟收口（2026-08-29）

上表第 6 条本轮**已执行**：合入 D-1 + D-2 之后重跑了一遍全新克隆冒烟，见 §2 第四遍，
§1 的克隆命令表已补上第四遍那一行。

🔴 **前三遍的读数一个字都没改。** 它们是各自那次跑出来的事实，带各自的基线 sha；
把 `521 passed` / `571 passed` / `4/74` 改写成当前值，等于伪造那几次冒烟的结果。
本报告的口径从本轮起明确为：**历史快照，只增不改** —— 要新读数就另起一节，
不回头改旧节。已记进 `docs/DECISIONS.md` 的 `## integrate-round-6`。

因此 §3 的卡点清单、§4 的 README 修正清单、§5 的结论段也**全部按原样保留**，
包括 §5 里那句「Y-3 合并后必须重跑」—— 它在写下的那一刻是对的，第三遍就是它的执行。

本轮唯一需要下一轮接手的：**A-1 那条判据仍然只量机器时间**。四遍的秒数分别是
6.57 / 6.44 / 5.4 / 6.89 秒，而第一遍有 6 处卡点、第四遍零卡点，秒数却看不出差别。
§5 早就提过「建议把『≤ 15 分钟』补一句『且全程零非零退出、不需要跨节拼路径』」，
这条建议至今没有落到 `docs/submission-checklist.md` 的 A-1 里。

---

## 第五遍冒烟（T8 轨，2026-08-29）—— 基线 `4cfef38`，本地源 + 远端源各跑一遍

> 📌 **本节记的是 `4cfef38`（整合轮 10，T1–T6 六轨已全部合入，`802 passed`）。
> 上面几节记的是各自当时的基线，两者不可互相覆盖。** 本节一个字都没动前四遍的读数
> —— 那是本报告从整合轮 6 起定死的口径：**历史快照，只增不改**（见上一节）。
> 想比较的话对着看，不要合并成一张表。

| 项 | 值 |
| :-- | :-- |
| 基线 sha | `4cfef38fa0c2deb824e2293285d9642a2204c6b3` |
| 跑的日期 | 2026-08-29 |
| 克隆源 A（本地） | `git clone --single-branch -b goai-restructure <本 worktree 路径> maos` |
| 克隆源 B（远端） | `git clone --single-branch -b goai-restructure https://github.com/TomwaltW/maos-runtime.git maos` |
| 两遍的 clone HEAD | **都是 `4cfef38…`**，与基线逐字符一致 |
| `python3 --version` | `Python 3.11.7`；`which python` 仍不存在 |

**为什么两个源都跑**：本地源快且不依赖网络，但它只能证明「本机这份代码是好的」；
**只有远端 URL 才验得了「评委 clone 到的东西对不对」**。两遍结果若不同，
那个差本身就是最重要的发现 —— 这一遍确实发现了一个，见 §卡点 7。

**环境清洁**：先显式 unset 27 个变量（历史节那 20 个，加上代码里实际读到的
`MAOS_EVIDENCE_PINNED_SHA` / `MAOS_PROBE_*` / `MATRIX_HOMESERVER` / `MATRIX_USER` /
`MATRIX_ROOM_ID` / `MATRIX_ROOM_ID_ENCRYPTED`），再按通配规则
（`MAOS_*` / `MATRIX_*` / 含 `API_KEY` / `BASE_URL` / `LLM`）兜一遍，最后校验残留为空。

🔴 **和第四遍一样，实测抹掉 0 个 —— 环境里本来就一个都没有。**
结论（无 key 能跑完全程）不变，但**它这次同样不是被 unset 保证的，是恰好没有**。
带 key 的机器上要复现本节，仍须显式 unset。

顺带一条值得记的：`make_evidence.py` 开跑时打印
`脱敏哨兵：['CLAUDE_CODE_MESSAGING_TOKEN']（值不打印）` ——
环境里**确实有**一个名字带 `TOKEN` 的变量，它不是 MAOS 读的任何一个，
而出口脱敏机制**已经把它纳入哨兵反查**。这正是那道闸该有的样子：
它防的不是「我们自己的 key」，是「跑证据的那台机器上碰巧有的任何密钥」。

### 逐步耗时表 —— 源 A（本地）

| # | 命令原文 | 耗时 | exit | 脏行 | 与 README 是否一致 |
| :-- | :-- | --: | --: | --: | :-- |
| 1 | `git clone …` | 1.888s | 0 | 0 | ✅ |
| 2 | `python3 scripts/verify.py`（**出厂态直接跑**） | 0.045s | **2** | 0 | ✅ 与 README 抬头写的「直接跑会报缺数据库并退出 2」逐字相符 |
| 3 | `python3 -m pytest maos/tests -q` | 13.669s | 0 | 0 | ✅ `802 passed`，与 README §4 写的条数一致 |
| 4 | `python3 run.py` | 2.583s | 0 | 0 | ✅ 场景 1–7 跑满；**跑完仍 0 行脏**（它不产证据） |
| 5 | `python3 run.py --scenario 7` | 0.273s | 0 | 0 | ✅ |
| 6 | `python3 scripts/make_evidence.py`（①） | 5.228s | 0 | **50** | ✅ 「8 场景落盘，0 场景缺模块」 |
| 7 | `python3 scripts/verify.py`（②） | 0.099s | 0 | 50 | ✅ `RESULT: 7/7 PASS` |
| 8 | `python3 scripts/gen_docs.py --check` | 0.148s | 0 | 50 | ✅ 三份代码生成文档与代码逐字节一致 |
| | **掐表：clone → `RESULT: 7/7 PASS`** | **6.972s** | 0 | | 独立复跑的最短路径（clone + ① + ②） |
| | 全程（八条跑满） | **23.9s** | | | |

### 逐步耗时表 —— 源 B（远端 URL）

| # | 命令原文 | 耗时 | exit | 脏行 |
| :-- | :-- | --: | --: | --: |
| 1 | `git clone …`（走网络） | 3.659s | 0 | 0 |
| 2 | `python3 scripts/verify.py`（出厂态直接跑） | 0.060s | **2** | 0 |
| 3 | `python3 -m pytest maos/tests -q` | 13.746s | 0 | 0 |
| 4 | `python3 run.py` | 3.154s | 0 | 0 |
| 5 | `python3 run.py --scenario 7` | 0.322s | 0 | 0 |
| 6 | `python3 scripts/make_evidence.py`（①） | 5.102s | 0 | 50 |
| 7 | `python3 scripts/verify.py`（②） | 0.100s | 0 | 50 |
| 8 | `python3 scripts/gen_docs.py --check` | 0.155s | 0 | 50 |
| | **掐表：clone → `RESULT: 7/7 PASS`** | **8.690s** | 0 | |
| | 全程（八条跑满） | **26.3s** | | |

**两个源逐条对齐**：exit code 全同、脏行数全同、七项分子分母全同、`802 passed` 全同。
唯一的差是网络带来的 clone 耗时（1.888s → 3.659s）与随之抬高的掐表读数。
**远端那份代码与本地这份是同一个东西 —— 只要你指定了分支。**

### 七项读数（两遍逐字节一致）

```
[PASS] hash-integrity       86/86
[PASS] business-ref         35/35
[PASS] authoritative-fact   3/3
[PASS] trace-tree           19/19
[PASS] kb-hit               7/7
[PASS] business-outcome     10/10
[PASS] history-case         1/1

RESULT: 7/7 PASS
```

`warn` **12 行 / 3 类**（A 6 行 / D 4 行 / E 2 行），与 `docs/submission-checklist.md` A-2
写的判据一致。`pytest` **802 passed**（第四遍是 645，其间隔着整合轮 8 的 58 条、
整合轮 9 的 46 条、整合轮 10 的 53 条）。

**没变的**：`git status` 跑完仍是 **50 行 M**；`git status` 里**一个 `*.db` 都没有**。
**变了的**：`scenario-R5` 的首行 sha **不再带 `-dirty`** —— 八个场景全干净，
整合轮 9 的 H-7 修复在全新克隆上同样成立，它不是某个工作区的特产。

### 卡点 7 —— 🔴 本遍唯一的新发现：裸 `git clone` 落在 `main` 上，而 `main` 是 TypeScript 时代的骨架

> ✅ **已于 2026-08-30 转绿（整合轮 13 前）**：人类把 GitHub 默认分支改成了 `goai-restructure`，
> `git ls-remote --symref <地址> HEAD` 现在给 `ref: refs/heads/goai-restructure`。
> **下面整节按惯例保留原样，是当时的实录**，不是仓库当前状态。

前四遍的克隆命令**都显式带了 `-b <分支>`**，于是这个问题四遍都没被看见。
这一遍多做了一步：**不带 `-b`，照评委最可能的敲法裸 clone 一次。**

```
$ git ls-remote --symref https://github.com/TomwaltW/maos-runtime.git HEAD
ref: refs/heads/main    HEAD
3f2d5d12ac73d2a1d2668fa71609ac770f99afa1        HEAD
4cfef38fa0c2deb824e2293285d9642a2204c6b3        refs/heads/goai-restructure
```

**仓库的 GitHub 默认分支是 `main`，HEAD 停在 `3f2d5d1`。** 裸 clone 拿到的是：

| 项 | 裸 clone（`main` / `3f2d5d1`） | 指定分支（`goai-restructure` / `4cfef38`） |
| :-- | :-- | :-- |
| 入库文件数 | **44** | **279** |
| 顶层长什么样 | `package.json`、`pnpm-lock.yaml`、`pnpm-workspace.yaml`、`tsconfig.json`、`src/`、`tests/*.test.ts`、`python/` | `maos/`、`scripts/`、`evidence/`、`run.py`、`deploy/`、`scenarios/`… |
| 有 `maos/` 包吗 | **没有** | 有 |
| 有 `scripts/verify.py` 吗 | **没有** | 有 |
| README 里的命令 | 英文 TS 版 MVP 说明 | 当前这一份 |

- **卡在哪**：README §4 第一行写的是 `git clone <本仓库地址> maos && cd maos`。
  「本仓库地址」这个占位符**掩盖了分支这件事**。照抄它，落地的是一份
  没有 `maos/` 包、没有 `scripts/`、没有 `evidence/` 的 TypeScript 骨架 ——
  README 里**此后的每一条命令都会 command-not-found 或 No such file**。
- **量**：这是评委的**第 0 条命令**。前四遍的六个卡点全都发生在 clone 之后，
  这一条发生在 clone 那一刻，且没有任何输出提示他走错了分支 ——
  `git clone` 会安安静静地成功。
- **没有上下文的人会怎么误解**：他会打开 README，看到里面讲的东西
  在自己的目录里一个都找不到，然后合理地推断「这份 README 描述的不是这个仓库」。
  **这比前四遍任何一个卡点都靠前、都致命**：它发生在他还没跑过一条命令的时候。
- **顺带解释了一处遗留**：`main` 上的 `CONTRIBUTING.md` 与 `goai-restructure` 上的
  **一字不差**（都写着 `pnpm test` / `pnpm typecheck`）。它就是 TS 时代留下来的，
  从没跟着 Python 重构改过 —— 本轨已重写（见该文件）。
- **最小修法**：两条路，**都不在本轨可改面内**。
  甲：在 GitHub 上把默认分支改成 `goai-restructure`（一次性设置，最省事，且评委
  裸 clone 就对）。乙：README §4 那行写死 `git clone -b goai-restructure <地址> maos`。
  甲乙都做最稳。**已记 `docs/BACKLOG.md ## task-T8`。**
  `SECURITY.md` 本轨已先补上一段显式警告（安全报告人对着 `main` 报，报的是一份没在跑的实现）。

### 与 README 不符的其它两处（都属数字过期，不属故障）

| # | README 位置 | 写的 | 实测（`4cfef38`） |
| :-- | :-- | :-- | :-- |
| 1 | 第 16 行「clone + 这两条共约 **5 秒**」 | 5 秒 | **6.97s**（本地源）/ **8.69s**（远端源） |
| 2 | 第 179 行「以上全部跑完约 **18 秒**，最短路径约 **5 秒**」 | 18 秒 / 5 秒 | **23.9s**（本地）/ **26.3s**（远端）；最短路径同上 |

两处都是整合轮 5（`571 passed`）留下的读数，`pytest` 涨到 802 条之后自然变长
（9.17s → 13.7s，占了涨幅的绝大部分）。**这不是回归，是数字过期。**
`README.md` 是整合轮的面，本轨不改，**已记 `docs/BACKLOG.md ## task-T8`**。

### 五条结构性结论逐条复核

上面第 10–11 行列的那五条，在 `4cfef38` 上逐条实测：

| # | 结论 | 是否仍成立 | 实测 |
| :-- | :-- | :-- | :-- |
| 1 | 零出网 | ✅ 成立 | 口径与前几遍一致：**代码路径 + 实跑**两重判据（`maos/model/client.py:select_model_client` 在三个 LLM 变量缺任一时降级 `ScriptedModelClient`；无 key 环境下 clone→7/7→pytest→run.py 全部 exit=0）。**本轨同样没有做物理断网测试，不宣称做过。** |
| 2 | 无任何 API key 可跑 | ✅ 成立 | 27 个变量 unset + 通配兜底 + 残留校验为空；实测抹掉 0 个（环境本来就没有），见上文口径说明 |
| 3 | 从全新克隆一次通到 `RESULT: 7/7 PASS` | ⚠️ **有条件地成立** | 指定 `-b goai-restructure` 时两个源都一次通过、零卡点、零非零退出（那条 `exit=2` 是刻意跑的设计行为）。**裸 clone 不成立** —— 落在 `main` 上，一条命令都跑不了，见卡点 7 |
| 4 | 跑完工作区 50 行脏 | ✅ 成立 | 两个源都是 50 行，全是 `evidence/**` 的 `M`，`*.db` 一个都没混进来 |
| 5 | 掐表远在 15 分钟预算内 | ✅ 成立 | 6.972s / 8.690s，约占预算的 **0.8% / 1.0%** |

**第 3 条从「成立」降到「有条件地成立」，是本遍最要紧的一行。**
前四遍写下这条时，执行者每次都自己带了 `-b`，于是这个条件一直是隐含的、没被写出来的。
把它写出来之后，修法就很显然了（改默认分支）—— 隐含条件的代价从来不是它难修，
是**没人知道它存在**。

### 对 15 分钟预算的结论（第五遍）

五遍的掐表读数：6.57 / 6.44 / 5.4 / 6.89 / **6.97（本地）· 8.69（远端）** 秒。
上一节说「A-1 那条判据仍然只量机器时间」—— 本遍再添一个证据：
卡点 7 让评委根本跑不到第一条命令，而**它对掐表读数的影响是 0**，
因为掐表是从「clone 对了分支」之后才开始的。
判据里那句「且全程零非零退出、不需要跨节拼路径」应该再补一句
**「且用评委最可能敲的那条命令 clone」**。已记 `docs/BACKLOG.md ## task-T8`。

---

## 第六遍冒烟（T19 轨，2026-08-30）—— 基线 `f15e5dd`，`860 passed`，本地源 + 远端源各跑一遍

> 📌 **本节记的是 `f15e5dd`（整合轮 11，T1–T14 十四轨已全部合入，`860 passed`）。
> 前五节记的是各自当时的基线，两者不可互相覆盖。** 本节一个字没动前五遍的读数
> —— 沿用本报告从整合轮 6 起定死的口径：**历史快照，只增不改**。

| 项 | 值 |
| :-- | :-- |
| 本地基线 sha | `f15e5dd1348ee6b55be2138f0e96bcb6214f5ef2` |
| 跑的日期 | 2026-08-30 |
| 克隆源 A（本地） | `git clone --single-branch -b goai-restructure <本 worktree 路径> maos` |
| 克隆源 B（远端） | `git clone --single-branch -b goai-restructure https://github.com/TomwaltW/maos-runtime.git maos` |
| 两遍的 clone HEAD | 🔴 **不一致**：A = `f15e5dd…`，B = **`4cfef38…`** —— 见卡点 8 |
| `python3 --version` | `Python 3.11.7`；`which python` 仍不存在 |

**环境清洁**：与第五遍同一套口径（27 个具名变量 unset + 通配兜底
`MAOS_*` / `MATRIX_*` / 含 `API_KEY` / `BASE_URL` / `LLM`，再校验残留为空）。
🔴 **实测又是抹掉 0 个 —— 环境里本来就一个都没有**，结论（无 key 能跑完全程）
不变，但它仍然不是被 unset 保证的，是恰好没有。`make_evidence.py` 照旧打印
`脱敏哨兵：['CLAUDE_CODE_MESSAGING_TOKEN']（值不打印）`，与第五遍一致。

### 卡点 8 —— 🔴 本遍唯一的新发现：远端 `goai-restructure` 落后本地 21 个 commit

第五遍的结论里有一句「**远端那份代码与本地这份是同一个东西 —— 只要你指定了分支**」。
**这一遍它不成立了。** 两个源指定同一个分支名，clone 出来的 HEAD 不是同一个：

```
$ git ls-remote --heads origin
7c80ca30fb2721d4b0e3503ad3273960c9b73a6b	refs/heads/agent/add-python-maos-skeleton
4cfef38fa0c2deb824e2293285d9642a2204c6b3	refs/heads/goai-restructure
3f2d5d12ac73d2a1d2668fa71609ac770f99afa1	refs/heads/main

$ git rev-parse goai-restructure          # 本地
f15e5dd1348ee6b55be2138f0e96bcb6214f5ef2

$ git log --oneline 4cfef38..f15e5dd | wc -l
21
```

远端的 `goai-restructure` 还停在 **`4cfef38`（整合轮 10）**，也就是第五遍那个基线。
整合轮 11 连同 T7–T14 八轨的 21 个 commit **从未推送**（全局铁律 5：只许本地 commit，
push 由人类手动做 —— 这个卡点正是那条铁律的必然副作用，不是谁做错了）。

**后果，按评委实际会遇到的顺序**：

1. 他照改好的 README 敲 `git clone -b goai-restructure`，**成功**，分支名也对得上；
2. 他敲 `python3 -m pytest maos/tests -q`，得到 **`802 passed`**，而 README 白纸黑字写着
   **`860 passed`** —— 差 58 条；
3. 他敲 ①②，得到 `RESULT: 7/7 PASS`，但 `trace-tree` 是 **19/19**，而 README §3 贴的是
   **29/29**。

**七项全 PASS、退出码全 0 —— 没有任何一条命令报错。** 这正是它难缠的地方：
卡点 7（裸 clone 落在 `main`）会让人**立刻**发现不对，卡点 8 不会。
评委拿到的是一份「跑得通、但和 README 对不上数」的仓库，
而对不上数的最自然解释是**「这份 README 的数字是编的」** —— 这恰好打在
本仓库最不能被怀疑的地方（全局铁律 3：证据必须真实）。

**修法只有一条，且单轨做不了**：人类把本地 `goai-restructure` 推到远端。
🔴 **它必须发生在提交之前**，否则卡点 7 修好了也没用 —— 评委分支敲对了，
拿到的仍然是整合轮 10 的代码。已记 `docs/BACKLOG.md ## task-T19`。

### 逐步耗时表 —— 源 A（本地，`f15e5dd`，`860 passed`）

| # | 命令原文 | 耗时 | exit | 脏行 | 与 README 是否一致 |
| :-- | :-- | --: | --: | --: | :-- |
| 1 | `git clone …` | 1.990s | 0 | 0 | ✅ |
| 2 | `python3 scripts/verify.py`（**出厂态直接跑**） | 0.045s | **2** | 0 | ✅ 与 README 抬头写的「直接跑会报缺数据库并退出 2」逐字相符 |
| 3 | `python3 -m pytest maos/tests -q` | 18.150s | 0 | 0 | ✅ `860 passed, 22 skipped`，与 README §4 写的条数一致 |
| 4 | `python3 run.py` | 2.442s | 0 | 0 | ✅ 场景 1–7 跑满；跑完仍 0 行脏 |
| 5 | `python3 run.py --scenario 7` | 0.251s | 0 | 0 | ✅ |
| 6 | `python3 scripts/make_evidence.py`（①） | 4.714s | 0 | **50** | ✅ 「8 场景落盘，0 场景缺模块」 |
| 7 | `python3 scripts/verify.py`（②） | 0.095s | 0 | 50 | ✅ `RESULT: 7/7 PASS`，`trace-tree 29/29` |
| 8 | `python3 scripts/gen_docs.py --check` | 0.134s | 0 | 50 | ✅ 三份代码生成文档与代码逐字节一致 |
| | **掐表：clone → `RESULT: 7/7 PASS`** | **6.791s** | 0 | | 独立复跑的最短路径（clone + ① + ②） |
| | 全程（八条跑满） | **27.8s** | | | |
| | 其中 README §4 那六条 | **27.6s** | | | 回填 README 用的就是这个数 |

### 逐步耗时表 —— 源 B（远端 URL）🔴 **注意：它跑的不是本轮基线**

**这张表测的是 `4cfef38`（整合轮 10），不是 `f15e5dd`。** 因为远端还停在那里（卡点 8）。
它**不能**用来回填 README 的秒数 —— 那会把整合轮 10 的读数写成整合轮 11 的。
留在这里是因为它本身就是卡点 8 的实证。

| # | 命令原文 | 耗时 | exit | 脏行 |
| :-- | :-- | --: | --: | --: |
| 1 | `git clone …`（走网络） | 3.025s | 0 | 0 |
| 2 | `python3 scripts/verify.py`（出厂态直接跑） | 0.042s | **2** | 0 |
| 3 | `python3 -m pytest maos/tests -q` | 12.812s | 0 | 0 |
| 4 | `python3 run.py` | 2.710s | 0 | 0 |
| 5 | `python3 run.py --scenario 7` | 0.273s | 0 | 0 |
| 6 | `python3 scripts/make_evidence.py`（①） | 4.783s | 0 | 50 |
| 7 | `python3 scripts/verify.py`（②） | 0.101s | 0 | 50 |
| 8 | `python3 scripts/gen_docs.py --check` | 0.134s | 0 | 50 |
| | **掐表：clone → `RESULT: 7/7 PASS`** | **7.769s** | 0 | |
| | 全程（八条跑满） | **23.9s** | | |

**两个源逐条对齐的结果**：exit code 全同、脏行数全同（都是 50）、`RESULT: 7/7 PASS` 全同。
**不同的三处，全部由卡点 8 解释**：`pytest` **860 vs 802**（差 58 条）、
`trace-tree` **29/29 vs 19/19**、`pytest` 耗时 18.150s vs 12.812s。
第五遍那句「远端那份代码与本地这份是同一个东西 —— 只要你指定了分支」，
**现在要再加一个条件：只要主干推上去了**。

### 七项读数 —— 源 A（`f15e5dd`）

```
[PASS] hash-integrity       86/86
[PASS] business-ref         35/35
[PASS] authoritative-fact   3/3
[PASS] trace-tree           29/29
[PASS] kb-hit               7/7
[PASS] business-outcome     10/10
[PASS] history-case         1/1

RESULT: 7/7 PASS
```

与 README §3 贴的那段逐字一致（`trace-tree` 29/29 是整合轮 11 的数）。
源 B 除 `trace-tree 19/19` 外其余七项相同。

### 与第五遍的差 —— 秒数为什么涨

| 读数 | 第五遍（`4cfef38`，802 条） | 第六遍（`f15e5dd`，860 条） | 差 |
| :-- | --: | --: | --: |
| 掐表最短路径（本地源） | 6.972s | **6.791s** | −0.18s（基本持平） |
| 全程八条（本地源） | 23.9s | **27.8s** | +3.9s |
| 其中 `pytest` | 13.669s | **18.150s** | **+4.48s** |

**全程的涨幅几乎全部来自 `pytest`**（802 → 860 条，+58 条，+4.48s），其余七条合计只差 −0.6s。
掐表路径不含 `pytest`，所以基本没动 —— 这也解释了为什么 README 那两处秒数
一处要从 18 改到 28、另一处只从 5 改到 7。

⚠️ **`pytest` 耗时有机器噪声**：同一份代码同一台机器，本轨另外两次裸跑分别是
21.32s 和 20.07s，本表这次是 18.150s。回填 README 用的是「约 28 秒」这个量级说法，
不是把 27.6 写死 —— 量级稳，小数点后不稳。

### README 三处秒数的回填（本轨已做）

| # | README 位置 | 原值（802 条时代） | 回填为 | 依据 |
| :-- | :-- | :-- | :-- | :-- |
| 1 | 第 16 行「clone + 这两条共约 X 秒」 | 5 秒 | **7 秒** | 源 A 掐表 6.791s |
| 2 | §4「以上全部跑完约 X 秒」 | 18 秒 | **28 秒** | 源 A README §4 六条 27.6s |
| 3 | §4「最短路径约 X 秒」 | 5 秒 | **7 秒** | 同 1 |

**三处都取源 A**，理由见上面源 B 那张表的抬头：源 B 测的是整合轮 10 的代码。
第五遍记的 6.97/8.69 是 802 条时代的读数，**本轨没有照抄，是重量的**。

### 五条结构性结论逐条复核（第六遍）

| # | 结论 | 是否仍成立 | 实测 |
| :-- | :-- | :-- | :-- |
| 1 | 零出网 | ✅ 成立 | 口径与前几遍一致（代码路径 + 实跑两重判据）。**本轨同样没做物理断网测试，不宣称做过。** |
| 2 | 无任何 API key 可跑 | ✅ 成立 | 27 个变量 unset + 通配兜底 + 残留校验为空；实测抹掉 0 个（环境本来就没有） |
| 3 | 从全新克隆一次通到 `RESULT: 7/7 PASS` | ⚠️ **有条件地成立，且条件比第五遍多了一个** | 条件一：指定 `-b goai-restructure`（卡点 7，README 本轨已修）。条件二：**主干已推送**（卡点 8，本轨修不了）。两条都满足时两个源都一次通过 |
| 4 | 跑完工作区 50 行脏 | ✅ 成立 | 两个源都是 50 行，全是 `evidence/**` 的 `M`，`*.db` 一个都没混进来 |
| 5 | 掐表远在 15 分钟预算内 | ✅ 成立 | 6.791s / 7.769s，约占预算的 **0.75% / 0.86%** |

**第 3 条的条件从一个变成两个，是本遍最要紧的一行。** 和第五遍同样的道理：
执行者每次都在本地跑，于是「主干已推送」这个条件一直是隐含的、没被写出来的。
隐含条件的代价从来不是它难修 —— 卡点 8 的修法只是一条 `git push` ——
是**没人知道它存在**，于是没人会去做那一条。
