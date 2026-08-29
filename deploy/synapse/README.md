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
