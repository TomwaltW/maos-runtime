# 自建 Synapse + Element（HiClaw 的 Matrix 后端）

C 轮四轨共用的房间地基。`up.sh` 起完，另外三轨从 `~/.maos-matrix/room.env` 取四键开工。

> 为什么不进 `deploy/docker-compose.yml`：Synapse 必须先 `generate` 生成 `homeserver.yaml`
> 与签名密钥再启动，这一步塞不进 `compose up` 一把梭。所以单独起在本目录，compose 那份
> 保持原样。compose 文件末尾那段 Synapse 注释是更早的手写草稿，与本目录实做有出入
> （镜像源、`-it`、`-a` 管理员标志），以本目录为准。

## 一键起停

```bash
bash deploy/synapse/up.sh          # 幂等，重复跑不炸也不会重复建房
bash deploy/synapse/down.sh        # 停容器，数据卷保留，下次 up.sh 秒起
bash deploy/synapse/down.sh --purge   # 连数据卷一起删（不可逆）
```

`up.sh` 干完这些事：拉镜像 → `generate`（只做一次）→ 起 Synapse → 注册 `maos-bot` /
`boss` / `intern` → 登录取 token → 建**非加密**房并自查 → boss / intern 各自 join →
起 Element → 写握手件。

## 端口 / 容器 / 卷

| 项 | 值 | 说明 |
| --- | --- | --- |
| Synapse | `http://localhost:8008` | 容器 `maos-synapse`，`MAOS_HS_PORT` 可覆盖 |
| Element | `http://localhost:8080` | 容器 `maos-element`，`MAOS_EL_PORT` 可覆盖 |
| server_name | `maos.local` | mxid 形如 `@maos-bot:maos.local`，`MAOS_SERVER_NAME` 可覆盖 |
| 数据卷 | `maos_synapse` | 配置、签名密钥、SQLite 库、全部账号与房间 |

镜像默认走**南大镜像站代理的 element-hq 官方镜像**（`ghcr.nju.edu.cn/element-hq/*`）——
本机 docker daemon 到 `ghcr.io` 与 `docker.io` 都不通，原委见 `docs/hiclaw-probe.md`。
实测拉到的是 Synapse v1.159.0，镜像 label 的 `org.opencontainers.image.source` 指向
`github.com/element-hq/synapse.git`；但南大的 `latest` tag 缓存比 ghcr.io 落后，
两边 digest 不相等，别拿 digest 当一致性证据。网络正常的机器覆盖两个变量即可：

```bash
MAOS_SYNAPSE_IMAGE=ghcr.io/element-hq/synapse:latest \
MAOS_ELEMENT_IMAGE=ghcr.io/element-hq/element-web:latest \
  bash deploy/synapse/up.sh
```

## 产出的握手件（仓库外，`chmod 600`）

```
~/.maos-matrix/room.env     四键 + 三附加键，可 source
~/.maos-matrix/creds.txt    三个账号的口令，供登 Element 截图
~/.maos-matrix/STATUS       READY <ISO8601> | BLOCKED <原因>
```

🔴 token 与口令**只**落这三个文件。禁止进仓库任何文件、任何 commit message、任何截图。

## 两条最容易踩的

**`MATRIX_HOMESERVER` 分宿主机 / 容器两个口径。** 宿主机跑 python 用
`http://localhost:8008`；在容器里跑要换成 `http://host.docker.internal:8008`。写错不会报错，
只会连不上然后静默降级 log-only。

**房间绝不能开 E2EE。** `hiclaw/matrix_bus.py::_NioChannel._verify_room` 一旦查到
`m.room.encryption` 状态事件就当场降级 log-only（本轨不装 `matrix-nio[e2e]`）。加密是
Element **客户端**建房时的默认，用 API 建房不会自动加密 —— `up.sh` 每次都自查一遍，
期望 `M_NOT_FOUND`。

## 房间收发自证

```bash
. ~/.maos-matrix/room.env && ~/.maos-matrix/venv/bin/python deploy/synapse/smoke_send.py
```

发一条 `m.notice` 再读回，打 `SMOKE OK`。它**不 import `hiclaw`** —— 只证明房间这层地基
能收发；`hiclaw` 的镜像 / 审批 / 越权三条假设归 C-2 验。

## 怎么彻底重来

```bash
bash deploy/synapse/down.sh --purge     # 删容器 + 数据卷，STATUS 自动置 BLOCKED
rm -f ~/.maos-matrix/room.env ~/.maos-matrix/creds.txt
bash deploy/synapse/up.sh               # 全新 generate、新账号、新房间
```

新房间意味着新的 `room_id` 和新 token，**下游三轨必须重新 source `room.env`**。
只是想让容器重启，用不带 `--purge` 的 `down.sh` 就够了。

## Agent 账号（退款圆桌）

`up.sh` 管的是地基三个号（`maos-bot` / `boss` / `intern`）。退款圆桌要让**五个岗位各用
自己的 Matrix 账号发言** —— Element 里五个头像、五个显示名，谁说的一眼可见，而不是
五段话都顶着 `maos-bot` 的头像、靠 `【岗位 · 工号】` 前缀区分。那五个号由这个脚本建：

```bash
bash deploy/synapse/add_agents.sh                        # 建号 + 设显示名 + 进房 + 写 agents.env
bash deploy/synapse/add_agents.sh --dry-run              # 只打计划，一个请求都不发
bash deploy/synapse/add_agents.sh --room '!x:maos.local' # 覆盖房间（缺省取 room.env 的 MATRIX_ROOM_ID）
```

它对 `maos-intake` / `maos-policy` / `maos-evidence` / `maos-risk` / `maos-finance` 逐个：
注册（`--no-admin`）→ 取 token → 用该号自己的 token 设显示名（申请受理岗 / 规则审核岗 /
证据核验岗 / 风险反欺诈岗 / 财务执行岗）→ `maos-bot` 邀请、该号自己 join → **join 之后**
再用该号自己的 token 自查房间非加密。前提是 `up.sh` 已经跑过（要 `room.env` 与容器）。

> 顺序不能反。未 join 的号去查 `m.room.encryption` 拿到的是 403，而 matrix-nio 会把非 404
> 的错误体原样包成「成功」响应 —— 于是「号还没进房」被念成「房间开了加密」，降级日志里
> 留一个假原因。原委写在 `hiclaw/matrix_bus.py::encryption_verdict`。

### 产物：`~/.maos-matrix/agents.env`

`chmod 600`、仓库外、永不入库，`export` 形式可直接 source。每岗三键 + 末尾一个名单，共 16 行：

```
MAOS_AGENT_<AGENT_KEY>_USER        # mxid，如 @maos-intake:maos.local
MAOS_AGENT_<AGENT_KEY>_PASSWORD    # 口令（<只在文件里>）
MAOS_AGENT_<AGENT_KEY>_TOKEN       # access_token（<只在文件里>）
MAOS_ROOM_BOTS                     # 五个 mxid 逗号连接
```

`<AGENT_KEY>` = `agent_id.upper().replace("-", "_")`，五个值是 `REFUND_INTAKE` / `REFUND_POLICY` /
`REFUND_EVIDENCE` / `REFUND_RISK` / `REFUND_FINANCE`。推导函数是 `hiclaw/room_voices.py::env_keys_of`，
全仓只此一份（这个脚本是 shell、没法 import，那份字面量与它一起改）。

`MAOS_ROOM_BOTS` 给的是**监听侧**：`hiclaw/matrix_bus.py::open_channel` 现读它，进
`should_deliver` 的忽略名单 —— 一个房间只该有一个监听者，否则 `maos-bot` 会去接岗位号的
发言、岗位号下一轮再接，两个机器人互相接龙刷屏。

🔴 **和 `room.env` 是两个文件，不是一个。** `up.sh` 每次跑都会**整份重写** `room.env`
（见它的第 8 节），所以岗位账号的凭证必须独立存放；反过来这个脚本也一个字不碰
`room.env` 与 `creds.txt`。跑房间时两份都要 source：

```bash
set -a; . ~/.maos-matrix/room.env; . ~/.maos-matrix/agents.env; set +a
```

### 限流：为什么重跑必须零登录

Synapse 的 `rc_login.address` 默认 **`burst_count=5` / `per_second=0.003`**（实测于容器内
`synapse/config/ratelimiting.py`，本机 `homeserver.yaml` 里一个 `rc_` 键都没写，全是缺省）。
五个号首跑正好把桶用满，补回一个令牌要 333 秒。所以：

- **口令能沿用就沿用，token 能用就不重登** —— 脚本先 `. agents.env`，再拿旧 token 打一次
  `/account/whoami`，认得出就完全跳过登录。实测第二次跑**零登录** `exit=0`。
- 真要登的那几个号，**两次登录之间歇 3 秒**（口径同 `docs/BACKLOG.md` 里那条「一条命令
  一次登陆在 Synapse 上会卡死」）。
- 撞 429 就按响应体的 `retry_after_ms` 退避重试，**不改 `homeserver.yaml` 放宽限流**
  （`docs/DECISIONS.md` 2026-08-29 那条已定：放宽限流是把测试环境调成和生产不一样）。
- **绝不重登 `maos-bot`**，它的 token 归 `up.sh` 管。

撞满限流的症状是进程**静止**而不是报错（nio 会 sleep 几十秒重试，而 429 响应体里没有
`user_id`，schema 校验再报一次错），排查方向会指向房间或网络 —— 所以这几条不是省事，是必须。

### 怎么重来

```bash
rm -f ~/.maos-matrix/agents.env      # 口令与 token 一起丢掉，下次跑重新生成、重新登录
bash deploy/synapse/add_agents.sh
```

账号本身不会被删（它们在数据卷里）。真要连账号一起清，只能 `down.sh --purge` 重建整个卷。
🔴 删了 `agents.env` 就是五次登录，正好顶满 `burst_count` —— 别在演示前十分钟做这件事。
