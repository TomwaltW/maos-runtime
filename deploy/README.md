# 容器内跑 MAOS —— 从零到 `RESULT: 7/7 PASS`

写给**只装了 docker、没读过这个仓库**的人。照着敲，三条命令。

本文里所有耗时、版本、体积都是 2026-08-29 在一台机器上**实跑量出来的**，
不是估的；哪一项没量到，下面会直说「未实测」，不拿「大约」糊过去。

---

## 0. 前置

| 项 | 要求 / 本机实测值 | 怎么来的 |
| :-- | :-- | :-- |
| Docker Engine | 实测 **29.6.1** | `docker --version` |
| Docker Compose | 实测 **v5.3.0**；**至少 v2.22** | `docker compose version` |
| CPU / 内存 | 手册要求 ≥2 核 4GB；实测机器给了 docker **10 核 / 7.75 GiB** | `docker info` |
| 磁盘 | 镜像 **390MB**（基础镜像 215MB + git 与 pytest 175MB）；再加 pgvector 镜像 | `docker images` |

**为什么至少 Compose v2.22**：`docker-compose.yml` 用了 `build.dockerfile_inline`，
v2.22 才有。版本不够会报 `additional properties 'dockerfile_inline' not allowed`。

**首次拉镜像要多久？—— 本机没量到，如实说明。** 量这个要求本地没有
`python:3.11-slim`，而实测机器上它早就在了（215MB），也不为取个读数去删镜像
（同一台机器上还有别的会话在用 docker）。能给的实数是**构建**那一段：见 §2 的表，
`--no-cache` 冷构建 **47s**，其中 42.9s 花在 `apt-get install git` + `pip install pytest`
的下载上。真正的首跑 = 拉 215MB 的时间 + 这 47s。

---

## 1. 三条命令

在**仓库根目录**执行（不是 `deploy/` 里）：

```bash
# ① 容器内跑一个场景（端到端，无需任何 API key）
docker compose -f deploy/docker-compose.yml up --exit-code-from maos

# ② 容器内一条命令核验 —— 这条是给评委的答案
docker compose -f deploy/docker-compose.yml run --rm verify

# ③ 向量库（P5 预备，可选，缺省不起）
docker compose -f deploy/docker-compose.yml --profile pg up -d pgvector
```

### ⚠️ `--exit-code-from` 不是可选的装饰

裸 `docker compose up` **在容器崩溃时也返回 0**。实测：

```
up --exit-code-from maos -> exit=1        # 容器真崩了
up (裸)                   -> exit=0        # 同一次崩溃，却报成功
```

所以任何拿 `docker compose up; echo $?` 当「跑通了没有」判据的脚本都是**假判据**。
本文和 compose 抬头一律写死 `--exit-code-from maos`。

---

## 2. 三条命令各干什么、各要多久（全部实测）

| 命令 | 实测耗时 | 退出码 | 干了什么 |
| :-- | :-- | :-- | :-- |
| `build --no-cache`（冷） | **47s** | 0 | 装 git + pytest，产出 `maos-runtime:local` 390MB |
| `build`（buildkit 有层缓存） | **1s** | 0 | 命中缓存，什么都不做 |
| ① `up --exit-code-from maos` | **0–1s**（镜像已在） | 0 | 场景 1 端到端；沙箱回归 **5 过 / 0 挂 / 0 错** |
| ② `run --rm verify` | **4–5s** | 0 | 现产 8 束证据 + 重放校验 → `RESULT: 7/7 PASS` |
| ③ `--profile pg up -d pgvector` | **0s**（镜像已缓存） | 0 | PostgreSQL 16.15 + vector 0.8.6，健康检查立即 healthy |

首次跑 ① 或 ② 时 compose 会自动先构建，所以第一条命令实际要等 47s + 拉镜像的时间；
之后每次都是上表那个数。

> ①② 各跑了 4 次（本仓库工作区 2 次 + 两份**全新 clone** 各 1 次），
> 上表给的是观察到的区间，不是某一次的单点。两份 clone 都是**零环境变量**、
> 完全照本节命令敲的，两次都跑到 `RESULT: 7/7 PASS`。

### ① `up` 跑完长这样

```
    s1-test  AWAITING_REVIEW  -> DONE             [gate_pass]
  沙箱回归（真跑）: 沙箱回归：5 过 / 0 挂 / 0 错｜[沙箱降级] 本次未走容器，退化为裸 subprocess…
    · tests.test_isolation_probe::test_no_network      passed
    · tests.test_isolation_probe::test_no_host_secrets passed
    · tests.test_isolation_probe::test_no_home_access  passed
    · tests.test_session::test_valid_session           passed
    · tests.test_session::test_expired_session         passed
  Reviewer 语义审查: status=ok 结论=契约与补丁名实相符，回归全过，可放行
maos-1 exited with code 0
```

那句「沙箱降级」是**如实自述**，不是故障：容器里没有 docker daemon，沙箱跑的是
裸子进程而不是嵌套容器，所以 `--read-only` / `--user 1000:1000` 两项确实没生效。
断网那一项**生效了** —— compose 给这两个服务设了 `network_mode: none`，
子进程继承容器的网络命名空间，`test_no_network` 因此是真的过，不是 skip。

### ② `verify` 跑完最后两行

```
RESULT: 7/7 PASS
证据来源：scenario-1, scenario-2, scenario-3, scenario-4, scenario-5, scenario-6, scenario-7, scenario-R5
```

**它做的是「从零复现一遍再校验」，不是「校验仓库里躺着的那份证据」。**
仓库里的 `evidence/` 是**没有库**的（`.gitignore` 排掉了 `*.db`），直接校验必然
`[FAIL] 缺数据库`。所以 `verify` 服务先跑 `make_evidence.py` 现产一份带库的证据，
再跑 `verify.py` 重放校验它 —— 这正是评委该要的：不是信这份证据，是自己再产一份。

### ⚠️ 从 git worktree 挂载时，②会失败

```
[FAIL] 取不到 git sha，证据没有出处，拒绝生成: Command '['git', 'rev-parse', 'HEAD']' returned non-zero exit status 128.
```

**普通 clone 不会遇到这个**（实测容器内只读跑 `git rev-parse` 与
`git status --porcelain -uno` 都 rc=0）。只有 worktree 会：worktree 的 `.git`
是个指向宿主机绝对路径的**指针文件**，容器里那个路径不存在。

绕法是在宿主机上把 sha 传进去，一行：

```bash
MAOS_EVIDENCE_PINNED_SHA=$(git rev-parse HEAD) \
  docker compose -f deploy/docker-compose.yml run --rm verify
```

---

## 3. 宿主机的 `evidence/` 会不会被弄脏？—— 不会

**实测：容器跑完 ①②③ 全部三条之后，`git status --porcelain -- evidence/` 是 0 行。**

原因是 compose 没有把宿主机的 `evidence/` 挂进容器，而是另挂了一个**命名卷**
`maos_evidence` 到容器里的 `/evidence`：

- 源码 `..:/app:ro` —— 容器**改不动**宿主机的源码树
  （实测容器内 `touch /app/…` 报 `Read-only file system`）；
- `evidence:/evidence` —— 唯一可写面，证据、临时目录、SQLite 库都落这里；
- 卷**起始为空**，所以容器里核验的是这一次现产的证据，不是仓库里躺着的那份。

所以不需要「跑完记得还原」这一步 —— 宿主机压根没被写过。

> **为什么落在 `/evidence` 而不是 `/app/evidence`。** 后者看起来更自然（正好是
> `make_evidence.py` 的缺省输出路径），但它会把仓库里已跟踪的那 58 个证据文件
> 整个遮住 —— 于是容器内 `git status --porcelain -uno` 把它们全看成「被删」，
> 而 `make_evidence.py` 正是拿这条判断要不要给出处 sha 加 `-dirty`。
> 实测：在一份工作区干净的 clone 里跑，产出的证据头照样带 `-dirty` 后缀 ——
> **代码明明一个字节都没改**。
> 那个后缀本该指示「跑这批证据的代码与 HEAD 不一致」，挂在 `/app/evidence` 上
> 会让它恒为真，也就再指示不了任何东西。挪到 `/evidence` 之后，容器内 git 看到
> 干净工作区，出处 sha 就是真的 sha。代价是落盘路径不再是缺省值，
> 所以 `verify` 服务显式传了 `--out /evidence` / `--evidence /evidence`。

> 如果你反过来想让证据落到宿主机（比如要把它提交上去），把 compose 里
> `evidence:/evidence` 那一行改成 `- ../evidence:/evidence`。**代价先知道**：
> 容器跑一次就会往宿主机 `evidence/` 里写进 50+ 个文件，`git status` 立刻脏一屏，
> 整合与录制都会被这个绊到。缺省不这么干是有意的。

### 证据怎么取出来

```bash
# 看卷里有什么
docker compose -f deploy/docker-compose.yml run --rm --entrypoint ls maos -R /evidence

# 看某一束的出处头（第一行就是 `# generated at <ISO8601> from <git sha>`）
docker compose -f deploy/docker-compose.yml run --rm --entrypoint head maos \
  -1 /evidence/scenario-1/trace.json

# 拷到宿主机的某个目录（不要拷进仓库，会脏化工作区）
docker compose -f deploy/docker-compose.yml run --rm --entrypoint tar maos \
  -cf - -C /evidence . > /tmp/maos-evidence.tar
```

### 想从零复现一次

命名卷是**持久**的，第二次跑 `verify` 会覆盖在上一次的产物上。要真正的白纸：

```bash
docker volume rm maos_evidence      # 只删这一个卷，别用 docker volume prune
```

---

## 4. 收尾与红线

正常收尾 —— ① 的容器跑完自己就退出了，什么都不用做。③ 起的库要手动停：

```bash
docker compose -f deploy/docker-compose.yml --profile pg down
```

### 🔴 三条不许做的

1. **不许 `docker compose down --remove-orphans`。**
   这台机器上可能同时跑着 `maos-synapse` / `maos-element`（房间基建，
   由 `deploy/synapse/up.sh` 起，**不归本 compose 项目管**）。加了
   `--remove-orphans`，compose 会把它认成「孤儿」一并删掉 —— 房间没了，
   Demo 录制那一镜也就没了。

2. **不许 `docker system prune` / `docker volume prune`。**
   会连 `maos_synapse`（Synapse 的签名密钥与数据库都在里面）一起清掉。
   要清就点名清：`docker volume rm maos_evidence`。

3. **不许 `docker rm` 你没亲手起的容器。**
   尤其是 `maos-synapse`、`maos-element`。

### ⚠️ 同一台机器上有多个会话在用 docker 时

本 compose 的 project name 写死是 `maos`，所以**从任何一个 worktree 跑它，
容器都落在同一个 project 里**。这意味着 `docker compose down` 会把别人起的
`maos-pgvector-1` 也一并停掉。要在不打扰别人的前提下验证 pgvector，
用独立 project + 换端口：

```bash
POSTGRES_PORT=15432 docker compose -p maos-t9-pgcheck \
  -f deploy/docker-compose.yml --profile pg up -d pgvector
# 验完只拆自己这个 project，不碰 project maos
POSTGRES_PORT=15432 docker compose -p maos-t9-pgcheck \
  -f deploy/docker-compose.yml --profile pg down
docker volume rm maos-t9-pgcheck_pgdata
```

（上面这段就是本文 §2 表格里 ③ 那一行的实测方式。）

### 端口冲突

pgvector 缺省映射 `5432:5432`。本机已有 postgres 占着 5432 时，**换端口，
不要去杀别人的进程**：

```bash
POSTGRES_PORT=15432 docker compose -f deploy/docker-compose.yml --profile pg up -d pgvector
```

实测换到 15432 后，从宿主机连得上：

```
$ docker run --rm pgvector/pgvector:pg16 pg_isready -h host.docker.internal -p 15432 -U maos -d maos
host.docker.internal:15432 - accepting connections
$ psql … -c "select extname, extversion from pg_extension where extname='vector';"
 extname | extversion
---------+------------
 vector  | 0.8.6
```

---

## 5. Synapse 为什么不在 compose 里

Synapse 必须**先生成** `homeserver.yaml` 与签名密钥才能启动，这一步没法塞进
`up` 一把梭 —— 硬塞进去，「一键起」就变成一串没法一键跑通的前置步骤，
而这个文件存在的全部理由就是「评委敲的第一条命令能跑通」。

所以它单独走：**`deploy/synapse/README.md`**，那里有 `up.sh` / `down.sh`
和建 bot 账号、取 access token 的完整步骤。
`docker-compose.yml` 文件末尾也留了一份不依赖脚本的裸 `docker run` 版本。

**四个 Matrix 键（`MATRIX_HOMESERVER` / `MATRIX_USER` / `MATRIX_TOKEN` /
`MATRIX_ROOM_ID`）缺任何一个，事件总线就降级回进程内总线**，
所以这一整节没做也不影响本文任何一条命令。

---

## 6. `deploy/.env.example` 为什么不在

本该有一份 `deploy/.env.example` 列全所有可配置键。**它写不进去** ——
这个路径被本机的权限 deny 规则拦住（与仓库根的 `.env.example` 同一条规则）。
按约定停在这里报告，**没有绕道换个文件名**糊弄过去。

**键名清单在 `docs/BACKLOG.md` 的 `## task-omega` 一条里列全了。**

所有键都可缺省（缺省即降级为确定性本地实现），**空环境就应当跑得通** ——
本文 §2 那张表就是在空环境下量的，一个 key 都没配。

---

## 7. 跑不通怎么办

**先看退出码，而且必须是 `--exit-code-from` 那个退出码**（§1 的坑）。
这套链路的失败有几种是安静的，不要先怀疑「卡住了」。

### 7.1 第一条命令看起来卡住不动了

- **症状**：`up` 之后长时间没有任何输出
- **原因**：多半是在拉 `python:3.11-slim`（215MB）或在跑 `apt-get install git`。
  实测冷构建 47s，加上拉镜像，几分钟是正常的
- **下一步**：等。想看进度就另开一个终端 `docker compose -f deploy/docker-compose.yml build`，
  它会把构建日志打在前台

### 7.2 `additional properties 'dockerfile_inline' not allowed`

- **原因**：Docker Compose 低于 v2.22
- **下一步**：升级 Docker Desktop。真升不了的退路是把 compose 里的 `build:` 整段
  换成 `image: python:3.11`（**完整版**，不是 `-slim`）—— 完整版自带 git，
  但**不带 pytest**，还得自己想办法把 pytest 装进去，否则会撞上 7.4。
  ⚠️ 这条退路**本机没实测过**（实测机器的 Compose 是 v5.3.0，用不着走它），
  写在这里是给版本卡死的人一个方向，不是一条验证过的步骤

### 7.3 `FileNotFoundError: ... 'git'`

- **症状**：场景还没开始跑就炸在 `maos/flows/common.py` 的 import 期
- **原因**：跑的是**没装 git 的镜像**（旧版 compose 直接用 `python:3.11-slim`）
- **下一步**：`docker compose -f deploy/docker-compose.yml build --no-cache`，
  确认用的是本仓库当前这份 compose

### 7.4 `pytest 没有产出 junit 报告，多半根本没跑起来`

- **症状**：场景跑到一半，Gate 两轮打回，最后 `AssertionError` 在
  `scenario_1.py:143`（断言 plan 终态是 DONE 那行）
- **原因**：镜像里没有 pytest。沙箱回归走
  `python -m pytest --junitxml=…`，没有 pytest 就没有报告，Gate 判 `tool_error`
- **下一步**：同 7.3，重新构建

### 7.5 `FAILED tests/test_isolation_probe.py::test_no_network - Failed: DID NOT RAISE OSError`

- **症状**：同 7.4 的终局（Gate 打回 → plan FAILED），但报的是这条
- **原因**：**容器有网**。靶场的隔离探针拿 `/.dockerenv` 在不在判「我是不是在沙箱里」，
  MAOS 自己跑在容器里时这个标记也在，于是它对着裸子进程要求断网
- **下一步**：确认 compose 里那两个服务有 `network_mode: none`。
  **这不是把红的调绿** —— 探针断言的是「沙箱没有网」，加上这行之后这句话真的成立了

### 7.6 `[FAIL] 取不到 git sha，证据没有出处，拒绝生成`

- **原因**：从 git worktree 挂载，容器里看不到那个 gitdir
- **下一步**：§2 那条 `MAOS_EVIDENCE_PINNED_SHA=$(git rev-parse HEAD)`

### 7.7 `[FAIL] 缺数据库`

- **症状**：直接跑 `verify.py` 而没先跑 `make_evidence.py`
- **原因**：`.gitignore` 排掉了 `*.db`，仓库里那份证据没有库
- **下一步**：用 `run --rm verify` 服务，它就是「先产再验」两步一起跑的

### 7.8 `Error response from daemon: ... port is already allocated`

- **原因**：5432 被别的 postgres 占了
- **下一步**：`POSTGRES_PORT=15432 …`（§4）。**不要去杀占端口的进程**，
  那可能是别人正在用的库

### 7.9 房间容器不见了

- **原因**：跑过 `down --remove-orphans` 或 `system prune`（§4 的红线）
- **下一步**：`bash deploy/synapse/up.sh` 重建。签名密钥在 `maos_synapse`
  卷里，卷还在的话房间数据不会丢；卷也被 prune 掉了就得重新建房、重取 token
