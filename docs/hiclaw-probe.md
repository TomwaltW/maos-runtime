# HiClaw / Matrix 房间来源探测记录（task-C1）

`docs/EXECUTION.md` Phase 4 第 4 步点名要一行：**最终选了哪档、为什么**。这份文档就是那一行
的展开，外加每一步的真实命令与真实输出，供后来人复现和排错。

日期 2026-08-29 ｜ 基线 `f42ea83` ｜ 执行机 macOS 15.5（darwin 25.5.0），Docker 29.6.1

> 🔴 全文所有 access_token 与账号口令一律写作 `<redacted>`。真值只在
> `~/.maos-matrix/room.env` 与 `~/.maos-matrix/creds.txt` 里（`chmod 600`，仓库外）。

---

## 1. 结论：选 C 档（自建 Synapse），没退 matrix.org

**C 档达成，房间是本机自建 Synapse 上的真房间，收发已自证。** 没有动用「真起不来退
matrix.org 私密房间」那条后路。

自建这条路上唯一真正的阻力不是 Synapse 本身（起停 + 建房 + 注册全程可脚本化，十几分钟的
事），而是**本机 docker daemon 拉不到境外镜像**。这一条解决之后，其余步骤没有一处需要人类
点 GUI。

### 顺带纠正一条错误前提

`review/paste-W4.md` 那份派单（从未粘贴执行）把卡点写成「**需人类先注册 Synapse 账号**」，
于是整轨从未开工。**这个前提是错的**：`register_new_matrix_user` 用的是 `generate` 阶段
自动写进 `homeserver.yaml` 的 `registration_shared_secret`，`docker exec` 一条命令就能建
账号，全程无需 GUI、无需人类。本轨三个账号全部是脚本注册的：

```
[up] 账号 maos-bot 已注册
[up] 账号 boss 已注册
[up] 账号 intern 已注册
```

---

## 2. 最终用的东西

| 项 | 值 |
| --- | --- |
| Synapse 镜像 | `ghcr.nju.edu.cn/element-hq/synapse:latest` → 实为 **v1.159.0**（gitsha1 `7b10e6b9`） |
| Element 镜像 | `ghcr.nju.edu.cn/element-hq/element-web:latest` → 实为 **v1.12.26** |
| `server_name` | `maos.local` |
| Synapse 端口 | `8008`（容器 `maos-synapse`） |
| Element 端口 | `8080`（容器 `maos-element`） |
| 数据卷 | `maos_synapse` |
| 房间 | `!xfRqhNYVNyuOMitWVs:maos.local`（名「MAOS 审批」，**非加密**） |
| 账号 | `@maos-bot:maos.local` / `@boss:maos.local`（审批人）/ `@intern:maos.local`（越权用例） |

两个镜像的 label 都指向 element-hq 官方仓库：

```
$ docker image inspect ghcr.nju.edu.cn/element-hq/synapse:latest --format '{{json .Config.Labels}}'
{
    "gitsha1": "7b10e6b9bc2dacc33f0974c999f640b55ef831bc",
    "org.opencontainers.image.source": "https://github.com/element-hq/synapse.git",
    "org.opencontainers.image.version": "1.159.0"
}
```

⚠️ **别拿 digest 当一致性证据**：南大镜像站的 `latest` tag 缓存落后于 ghcr.io，同日实测

```
本地（南大源拉到的）    sha256:18db676dd9f1a053edcf3033a78daeff035b773d58e5bc68802e379947332302
ghcr.io 官方 latest     sha256:20ac3981c3477972efdf6be97accb428a1fad999694ed1c7a85c2d86c7fd1fb5
```

两者**不相等**。可信度来自 label 里的 `image.source` 与可复现的行为，不是 digest 相等。

---

## 3. 最大的坑：docker daemon 拉不到境外镜像

派单原话是「先试 `ghcr.io/element-hq/synapse:latest`，拉不到退 `matrixdotorg/synapse:latest`」。
**两条都不通**，第三条路是本轨自己找的。

### 症状

```
$ docker pull ghcr.io/element-hq/synapse:latest
（并行两个 pull 跑满 10 分钟，一层未落地，Images 总体积零增长，被超时杀掉）

$ docker pull matrixdotorg/synapse:latest
Error response from daemon: failed to resolve reference "docker.io/matrixdotorg/synapse:latest":
failed to do request: Head "https://registry-1.docker.io/v2/matrixdotorg/synapse/manifests/latest":
context deadline exceeded
```

### 定位：宿主机有代理，Docker 没走

```
$ env | grep -i proxy
NO_PROXY=localhost,127.0.0.1,::1
HTTPS_PROXY=http://127.0.0.1:7897
HTTP_PROXY=http://127.0.0.1:7897

$ docker info --format '{{json .HTTPProxy}} {{json .HTTPSProxy}}'
"http.docker.internal:3128" "http.docker.internal:3128"
```

宿主机 shell 走 `127.0.0.1:7897`，docker daemon 走 Docker Desktop 内置的
`http.docker.internal:3128` —— **两套代理，daemon 那套出不了境**。三个交叉验证：

```
# 容器里访问境外 registry：超时
$ docker run --rm python:3.11-slim python3 -c "import urllib.request as u; \
    print(u.urlopen('https://registry-1.docker.io/v2/', timeout=15).status)"
urllib.error.URLError: <urlopen error timed out>

# 容器里访问境内站点：通
$ docker run --rm python:3.11-slim python3 -c "import urllib.request as u; \
    print('cn-site', u.urlopen('https://www.baidu.com', timeout=12).status)"
cn-site 200

# 宿主机（走 7897 代理）访问同一个境外 registry：连上了，只是缺 CA
$ python3 -c "import urllib.request as u; u.urlopen('https://registry-1.docker.io/v2/', timeout=20)"
urllib.error.URLError: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] ...>
```

SSL 握手阶段才报错 = TCP 与代理都通，只是 macOS 的 Python.framework 没装 CA。
**所以不是网络断了，是 Docker 这一侧没有代理。**

> 根治办法是在 Docker Desktop 的 Settings → Resources → Proxies 里把代理指过去。那属于
> 「改 Docker」，按项目铁律要停下来问人类，本轨没动，改走镜像源绕开。已记
> `docs/BACKLOG.md` 的 `## task-C1`。

### 解法：镜像源实测

`docker manifest inspect` 是最快的探针（走 CLI 直连，几秒出结果，不用等 blob）：

```
docker.1ms.run       -> {            （可用）
docker.xuanyuan.me   -> toomanyrequests: 免费节点当前繁忙，请稍后重试。
hub.rat.dev          -> {            （可用）
docker.1panel.live   -> denied: only support mainland China
dockerpull.org       -> unknown: <!doctype html>
docker.unsee.tech    -> failed to configure transport ... EOF
docker.m.daocloud.io -> 403 Forbidden
ghcr.nju.edu.cn      -> {            （可用，✅ 最终采用）
ghcr.1ms.run         -> {            （可用）
ghcr.rat.dev         -> ... EOF
```

⚠️ **manifest 能拿到 ≠ blob 拉得动。** `docker.1ms.run` 和 `hub.rat.dev` 的 manifest 都正常，
但拉 synapse 时两个源都停在 `Pulling fs layer`、Images 总体积**零增长**（element-web 从
`docker.1ms.run` 倒是拉成功了，268MB）。真正把 501MB 的 synapse 拉下来的是
**`ghcr.nju.edu.cn`（南京大学镜像站）**，且它代理的正好是派单首选的 `element-hq` 官方镜像。

判断源是否真的在下载，看 `docker system df` 的 Images 体积会不会涨 —— 非 tty 会话里
`docker pull` 的进度条不刷新，光看日志会误以为在跑。

---

## 4. 逐步实录

### 4.1 generate（只做一次）

```
$ docker run --rm -v maos_synapse:/data \
    -e SYNAPSE_SERVER_NAME=maos.local -e SYNAPSE_REPORT_STATS=no \
    ghcr.nju.edu.cn/element-hq/synapse:latest generate
Creating log config /data/maos.local.log.config
Setting ownership on /data to 991:991
Generating config file /data/homeserver.yaml
Generating signing key file /data/maos.local.signing.key
A config file has been generated in '/data/homeserver.yaml' for server name 'maos.local'.
```

⚠️ **坑 1：`-it` 必须去掉。** `deploy/docker-compose.yml` 末尾注释写的是
`docker run -it --rm ... generate`，在非 tty 的自动化会话里直接失败。`up.sh` 里没有 `-it`。

### 4.2 起服务 + 就绪探测

```
$ docker run -d --name maos-synapse -v maos_synapse:/data -p 8008:8008 \
    ghcr.nju.edu.cn/element-hq/synapse:latest

$ curl -s http://localhost:8008/_matrix/client/versions | head -c 120
{"versions":["r0.0.1","r0.1.0","r0.2.0","r0.3.0","r0.4.0","r0.5.0","r0.6.0","r0.6.1","v1.1","v1.2","v1.3","v1.4","v1.5",
```

### 4.3 注册三个账号（无需人类）

```
$ docker exec maos-synapse register_new_matrix_user \
    -u maos-bot -p '<redacted>' --no-admin -c /data/homeserver.yaml http://localhost:8008
```

口令用 `openssl rand -hex 16` 现生成，只落 `~/.maos-matrix/creds.txt`（`chmod 600`）。
⚠️ compose 注释里写的是 `-a`（管理员）；本轨三个账号一律 `--no-admin` —— bot 只需要
建房与发消息，不需要管理员权限。

### 4.4 取 token → 建非加密房 → 自查

```
$ curl -s -XPOST http://localhost:8008/_matrix/client/v3/login \
    -d '{"type":"m.login.password","identifier":{"type":"m.id.user","user":"maos-bot"},
         "password":"<redacted>"}'
（取响应里的 access_token；口令不写进 room.env，代码走 token 鉴权）

$ curl -s -XPOST http://localhost:8008/_matrix/client/v3/createRoom \
    -H "Authorization: Bearer <redacted>" \
    -d '{"name":"MAOS 审批","preset":"private_chat",
         "invite":["@boss:maos.local","@intern:maos.local"]}'
→ !xfRqhNYVNyuOMitWVs:maos.local
```

⚠️ **坑 2：房间绝不能开 E2EE，而且必须自己验一次。**
`hiclaw/matrix_bus.py::_NioChannel._verify_room` 一旦查到 `m.room.encryption` 状态事件就
当场降级 log-only（本轨不装 `matrix-nio[e2e]`），整轮就白做了。加密是 Element **客户端**
建房时的默认，服务端 API 建房不会自动加密 —— 但「不会」不等于「验过了」，所以 `up.sh`
每次都自查：

```
$ curl -s -H "Authorization: Bearer <redacted>" \
    http://localhost:8008/_matrix/client/v3/rooms/!xfRqhNYVNyuOMitWVs:maos.local/state/m.room.encryption
HTTP 404  {"errcode":"M_NOT_FOUND","error":"Event not found."}     ← 期望值
```

三人在房：

```
$ curl -s -H "Authorization: Bearer <redacted>" \
    http://localhost:8008/_matrix/client/v3/rooms/$MATRIX_ROOM_ID/joined_members
HTTP 200  members=["@boss:maos.local", "@intern:maos.local", "@maos-bot:maos.local"]
```

### 4.5 Element web

```
$ docker run -d --name maos-element -p 8080:80 \
    -v <repo>/deploy/synapse/element-config.json:/app/config.json:ro \
    ghcr.nju.edu.cn/element-hq/element-web:latest
```

⚠️ **坑 3：挂载路径必须是绝对路径，且落点是 `/app/config.json`。** `up.sh` 用
`cd "$(dirname "${BASH_SOURCE[0]}")" && pwd` 求脚本自身所在目录，从任何工作目录跑都对。

浏览器实开 `http://localhost:8080` 确认（**不是推理**）：

- 欢迎页标题渲染成 `Welcome to MAOS 审批` → `element-config.json` 的 `brand` 生效，
  挂载成功；
- 登录页 Homeserver 一栏显示 `maos.local` / `http://localhost:8008` →
  `default_server_config` 生效，Element 已指向自建 Synapse，`maos.local` 这个
  `server_name` **没有**卡在 `.well-known` 发现上（不必退回 `localhost`）；
- 页内实测 secure context 与浏览器直连：

```js
{"isSecureContext":true, "hasCryptoSubtle":true, "hasIndexedDB":true,
 "origin":"http://localhost:8080",
 "idbOpen":"ok",              // 真开了一次 IndexedDB，不是看 API 存在性
 "subtleGenerateKey":"ok",    // 真跑了一次 WebCrypto generateKey(AES-GCM 256)
 "homeserverFetch":"200 versions=20"}   // 浏览器视角直连 8008，CORS 通
```

`http://localhost` 确实被当作 secure context，IndexedDB 与 WebCrypto 都没因为缺 HTTPS 罢工。

> **登 boss 看房间这一步留给 C-4。** 服务端前提本轨已全部验过（boss 在房里、房间非加密、
> Element 连得上 homeserver、crypto/IndexedDB 正常），只差填表单。本轨不做交互式登录是
> 为了不让账号口令出现在会话记录与终端回显里（铁律 6）。口令在
> `~/.maos-matrix/creds.txt`，C-4 直接取用。

### 4.6 房间收发自证

```
$ . ~/.maos-matrix/room.env && ~/.maos-matrix/venv/bin/python deploy/synapse/smoke_send.py
sent event_id=$VGmkV8kLV9rscBEuaVpaLOEsbo0Oc7Xy0CFUYmhTXLI
echo   body=[smoke] MAOS 房间地基自证 2026-08-29T05:42:17+00:00
SMOKE OK
```

发一条 `m.notice` 再按 event_id 读回，两边 body 一致才算数。这个脚本**不 import
`hiclaw`** —— 它证明的是房间这层地基能收发；`hiclaw` 的镜像 / 审批 / 越权三条假设归 C-2 验。

---

## 5. `localhost:8008` 与 `host.docker.internal:8008` 的口径差

**两个都对，用错场合就是连不上，然后静默降级 log-only —— 不报错，最难查。**

| 谁在跑 | `MATRIX_HOMESERVER` 该写什么 |
| --- | --- |
| 宿主机上的 python（`run.py --matrix`、`smoke_send.py`、C-2/C-3 的测试） | `http://localhost:8008` |
| 跑在**容器里**的 MAOS（`deploy/docker-compose.yml` 那条路径） | `http://host.docker.internal:8008` |
| 浏览器里的 Element | `http://localhost:8008`（`element-config.json` 里就是这个） |

`~/.maos-matrix/room.env` 里落的是**宿主机口径**（`http://localhost:8008`），因为 C 轮四轨
全部在宿主机上跑 python。`deploy/docker-compose.yml` 末尾注释写的
`http://host.docker.internal:8008` 是容器口径，那份注释没错，只是场合不同。

症状识别：口径写错时 `MatrixBusConfig.from_env` 四键俱全、不会报缺配置，而是在
`_NioChannel` 构造时连接失败，打

```
WARNING maos.matrix  Matrix 房间连接失败（...），降级 log-only
```

—— 与「压根没配」的降级日志长得不一样，看到「连接失败」四个字先怀疑口径。

---

## 6. 其余坑

**坑 4：Synapse 的 `rc_login` 限流会打穿幂等。** `up.sh` 第一版每次跑都无条件登三个账号，
第二次重跑当场炸：

```
[up] 失败：登录 intern 失败：Too Many Requests
up.sh(第二次) exit=1
```

Synapse 默认 `rc_login.failed_attempts/account` 的 `burst_count` 是 3，第一次跑正好用满。
**改法不是调 homeserver.yaml 放宽限流，而是别登那么多次**：bot 的 token 先拿
`/account/whoami` 验一次能用就沿用；boss / intern 先查 `joined_members`，已在房里就
完全不登录。改完第二次跑零登录，`exit=0`。

**坑 5：守卫 hook 会拦含 heredoc 的 Bash 命令。** 用 `cat > file <<EOF` 写 Python 脚本时
被拦：`blocked: 该操作触碰受保护面 <命令无法解析: No closing quotation>（解析失败）`
—— Python 代码里的单引号破坏了守卫的 `shlex` 解析。解析失败判「拦」是守卫的正确姿势，
不该拆词规避；改用 Write 工具落文件即可（与 `DECISIONS.md` 里 `## integrate-round-4`
第 4 条同一类处置）。

**坑 6：`deploy/docker-compose.yml` 末尾那段 Synapse 注释与本轨实做对不上**（镜像源、
`-it`、`-a` 管理员标志、四键口径）。那个文件是 Ω 的面，本轨**记账不改**，见
`docs/BACKLOG.md` 的 `## task-C1`。

---

## 7. 交给下游的握手件

```
~/.maos-matrix/room.env     chmod 600，7 个键，可 source
~/.maos-matrix/creds.txt    chmod 600，三个账号口令（供 C-4 登 Element）
~/.maos-matrix/STATUS       READY <ISO8601>
```

`room.env` 的键名（值一律不入库）：

```sh
export MATRIX_HOMESERVER=<redacted>   # http://localhost:8008，宿主机口径
export MATRIX_USER=<redacted>         # @maos-bot:maos.local
export MATRIX_TOKEN=<redacted>        # bot 的 access_token
export MATRIX_ROOM_ID=<redacted>      # !xfRqhNYVNyuOMitWVs:maos.local
export MAOS_APPROVERS=<redacted>      # @boss:maos.local
export MAOS_MATRIX_OUTSIDER=<redacted># @intern:maos.local，越权用例用的非审批人
export MAOS_ELEMENT_URL=<redacted>    # http://localhost:8080
```

起停见 `deploy/synapse/README.md`。
