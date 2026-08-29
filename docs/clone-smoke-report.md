# 新克隆冒烟报告 —— 无 key、掐表、从零跑到 verify 7/7

对应 `docs/submission-checklist.md` A-1 的两条：「新克隆冒烟，严格按 README 从零跑到
`verify.py` 7/7，掐表 ≤ 15 分钟」与「冒烟用的是没有任何 API key 的环境」。
本报告是这两条的执行记录，**两遍冒烟都在仓库外的全新 clone 里做**，本仓库
`evidence/` 一个字节未动。

执行人视角是刻意设定的：**只有这个仓库、没有上下文、没有 key 的评委**。凡是「我知道
该怎么办所以跳过去了」的地方，一律记成一条 README 缺口，不用已有知识救场。

---

## 1. 环境与口径

| 项 | 值 |
| :-- | :-- |
| 克隆命令（第一遍） | `git clone /Users/shensikai/Documents/MAOS /tmp/maos-smoke-z5-r2 && git checkout -q 42822fc` |
| 克隆命令（第二遍） | `git clone -b task/z5-clone-smoke <本 worktree> /tmp/maos-smoke2-z5` |
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

---

**对 15 分钟预算的结论**：三遍都远在预算内（6.57s / 6.44s / 5.4s，约占预算的 0.7%）。
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
