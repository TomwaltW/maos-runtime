# Nacos 配置治理 —— 真跑实测记录

这份文档只回答一个问题：**「四个治理旋钮搬上 Nacos」这件事，在一台真跑起来的
Nacos 上，到底哪些是跑过的、哪些没跑过。**

它存在的理由与 `deploy/polardb-live.md` 一样：接口写好了、测试绿了，不等于
「动态治理成立了」。本文件负责把「理论上可以」换成一条一条可核对的记录，
**包括没跑通的那些**。

## 与 `deploy/nacos.md` 的分工（别把两份混起来）

| 文件 | 回答什么 | 天花板 |
| :-- | :-- | :-- |
| [`deploy/nacos.md`](nacos.md)（T28 轨） | MAOS 的配置面怎么接、怎么配、怎么降级、审计落在哪 | 接口与口径 |
| **本文件**（T28 轨） | **上面那些实际跑通了哪几条** | 本机 Docker `nacos-server:v2.4.3`，**没有云上实例** |

🔴 **一条硬纪律：本文件里「本机容器跑通」和「阿里云 MSE 跑通」是两件事。**
后者**一次都没跑过**，见 §2。别把前者念成后者。

**实测环境**：macOS arm64 / Docker 29.6.1 / `nacos/nacos-server:v2.4.3`（standalone，
内嵌 derby，鉴权开启，端口只绑 `127.0.0.1`）/ `nacos-sdk-python` 3.2.0 装在隔离 venv /
仓库基线 `d98b9d1`。测量日期 **2026-08-30**。

---

## 一、已实测

### 1.1 容器起来了，鉴权是开着的 ✅

```
$ docker ps --filter name=maos-nacos --format '{{.Names}} {{.Status}} {{.Ports}}'
maos-nacos Up 4 minutes (healthy) 127.0.0.1:8848->8848/tcp, 127.0.0.1:9848->9848/tcp

$ (readiness 探针)
http=200 body='OK'
```

容器启动参数里 `-Dnacos.core.auth.enabled=true`（`docker logs` 可见），
鉴权不是关着的 —— 这一点对一个装着审批人名单的配置面是前提，不是加分项。

### 1.2 🔴 Nacos 2.4.x 不预置 admin，第一次登录必然 500

这是接这套东西第二个会踩的坑（第一个是顶层模块名 `v2`）。原始输出：

```
1) 登录（初始化前）      -> (500, 'caused: User nacos not found;')
2) admin 是否已初始化    -> (404, '{"status":404,"error":"Not Found",
                                    "path":"/nacos/v1/auth/admin/state"}')
3) 初始化 admin          -> 200（响应正文**刻意不贴**，见下）
4) 登录（初始化后）      -> 200, accessToken 长度 129, globalAdmin=True
```

第 2 条顺带答掉一个问题：**`/nacos/v1/auth/admin/state` 在 2.4.3 上不存在**（404），
所以没法用它先探「初始化过没有」，只能直接打初始化接口。

⚠️ 第 3 条的响应形如 `{"username":"...","password":"..."}` —— **它把口令原样回显**。
本文件刻意不贴它的正文（铁律 6）。照抄命令的人也别把它重定向进任何文件。

### 1.3 四个旋钮真的从 Nacos 取，缺项回落 env ✅

判据是 `maos/tests/test_config_source.py::test_live_nacos_serves_the_four_knobs`：
Nacos 里有 `MAOS_MAX_REPLAN=7` 而 env 里没有 → 取到 `7`、`explain()` 说 `nacos`；
`MAOS_FINANCE_THRESHOLD` 只在 env 里有（`9999`）→ 取到 `9999`、`explain()` 说 `env`。

```
$ <venv>/bin/python -m pytest maos/tests/test_config_source.py -v -k live
test_live_nacos_serves_the_four_knobs PASSED                             [ 75%]
test_live_approver_list_change_takes_effect_without_restart PASSED       [100%]
====================== 4 passed, 78 deselected in 12.02s =======================
```

### 1.4 ✅ 动态治理演示：不重启进程，改完名单下一条审批就按新名单判

**这是本轨的主判据**，也是「灰度 / 动态治理」这个 30% 维度弱项的唯一实证。

跑法：真 `RoomApprovalBridge` + 真 `SqliteStore` + 真 Nacos，
经 **OpenAPI**（与人在控制台点「发布」同一条路径）改 `MAOS_APPROVERS`。
进程不重启、bridge 不重建、`MatrixBusConfig` 快照不换 —— **变的只有 Nacos 里那一行**。

三轮实测，逐轮原始输出（第 3 轮）：

```
--- 第 3 轮（dataId=maos-governance-demo-3503686）---
  改之前：@cfo:maos.local -> '无审批权限：@cfo:maos.local 不在 MAOS_APPROVERS 名单内'
  改之前：@boss:maos.local 能批 = True
  改之后：@cfo:maos.local 能批 = True
  改之后：@boss:maos.local -> '无审批权限：@boss:maos.local 不在 MAOS_APPROVERS 名单内'
  生效延迟 = 5.02s    审计延迟 = 5.09s
  审计条数 = 1
```

三轮汇总：

| 轮次 | 生效延迟 | 审计延迟 |
| :-- | :-- | :-- |
| 1 | 5.04s | 5.11s |
| 2 | 5.01s | 5.11s |
| 3 | 5.02s | 5.09s |

**改之前谁能批**：`@boss:maos.local`（`@cfo` 被拒并落 `ApprovalDenied`）。
**改之后谁能批**：`@cfo:maos.local`；**`@boss` 当场批不动了** —— 后半句同样重要，
它证明的是「名单被替换」而不是「名单被追加」。

那条真实审计行（`event_log`，原样）：

```json
{
  "seq": 3,
  "event_id": "",
  "trace_id": "",
  "plan_id": "plan_demo",
  "task_id": null,
  "event_type": "ConfigChanged",
  "from_state": "",
  "to_state": "",
  "reason": "MAOS_APPROVERS: '@boss:maos.local' -> '@cfo:maos.local'",
  "detail": {
    "key": "MAOS_APPROVERS",
    "old": "@boss:maos.local",
    "new": "@cfo:maos.local",
    "origin": "nacos",
    "actor": "nacos@172.20.0.1",
    "actor_source": "nacos-history-api",
    "at": "2026-08-30T05:40:32.525111+00:00",
    "server": "127.0.0.1:8848",
    "namespace": "",
    "group": "DEFAULT_GROUP",
    "data_id": "maos-governance-demo-3503686"
  },
  "created_at": "2026-08-30T05:40:32.525186+00:00"
}
```

#### 生效延迟为什么是 ~5.0s（这不是网络延迟）

三轮都卡在 5.0s 上，太规整了，不像网络。查了 SDK 源码，原因是**客户端的配置监听
是 5 秒一跳的长轮询**，不是服务端主动推：

```python
# v2/nacos/config/remote/config_grpc_client_proxy.py:195
await asyncio.wait_for(self.execute_config_listen_channel.get(), timeout=5)
```

那个队列只被两处喂：`add_listener` 时喂一次、以及**它自己检测到变更后**再喂一次。
服务端的变更通知不进这个队列。所以一次改动会在**下一个 5 秒刻度**被发现 ——
延迟区间是 **0~5s**，期望值 2.5s，实测因为发布时机固定而稳定落在 5.0s 附近。

第一次 spike 时量到过 **3.97s**，正是同一个机制在窗口另一个位置的取值。

**这是 SDK 的节拍，不是 MAOS 的实现慢。** 演示时说「秒级生效」是准确的，
说「实时生效」就说过头了。

#### 审计比生效晚 ~0.07s，这是设计使然

`_apply` 先换快照（于是审批立刻按新名单判），**再**去查一次操作人（HTTP，慢），
最后才落审计。所以生效永远不晚于审计。`test_live_approver_list_change_takes_effect_without_restart`
里 `assert effect_latency <= audit_latency` 把这个先后关系钉成了断言。

### 1.5 🔴 「谁改的」只有走控制台 / OpenAPI 才追得到人

实测对比同一个 dataId 的三条历史（`/nacos/v1/cs/history`）：

```
{'id': '3', 'srcUser': 'nacos', 'srcIp': '172.20.0.1', 'opType': 'U'}   ← OpenAPI 带 accessToken 改的
{'id': '2', 'srcUser': '',      'srcIp': '10.100.158.135', 'opType': 'U'}  ← SDK publish_config 改的
{'id': '1', 'srcUser': '',      'srcIp': '10.100.158.135', 'opType': 'I'}  ← SDK publish_config 建的
```

**经 SDK 的 gRPC `publish_config` 写入，Nacos 记不下操作人。** 这是 Nacos + SDK 的
行为，MAOS 这侧补不了 —— 我们能做的只有「查到就记、查不到就留空并写明为什么」，
`detail.actor_source` 里那句话就是干这个的。

所以：**演示与答辩时不要说「所有配置变更都能追溯到人」**。准确的说法是
「经控制台改的变更能追溯到人；经程序改的只追得到来源 IP」。

### 1.6 ✅ Nacos 中途挂掉：沿用最后一份配置，审批名单不会被清空

治理面最该被问的一题。实测（停容器 → 读 → 起容器 → 改 → 读）：

```
① 连上后          approvers = ['@boss:maos.local']
   degraded = False

② 停掉 Nacos 容器 -> 0
   停掉后 approvers = ['@boss:maos.local']
   boss 还能批 = True
   degraded = False | explain = nacos

③ 起回 Nacos 容器 -> 0
   readiness 回来了
   publish v2 -> true
   恢复后收到新名单耗时 = 0.4s
   approvers = ['@cfo:maos.local']
   cfo 能批 = True
```

两条结论：

* **配置中心挂了不会让审批名单清空**，沿用最后一次拿到的那份（last-known-good）。
  这是对的行为 —— 反过来（挂了就清空）等于「配置中心一抖动，所有人都批不了钱」。
* **恢复后 0.4s 就收到了新名单**，比稳态的 5s 还快：重连后 SDK 会立刻跑一轮监听，
  不用等下一个刻度。

⚠️ **但有一条要照实记（见 §3.2）**：② 里 `degraded` 仍然是 `False`。
MAOS 这侧**看不见配置中心已经挂了**。

### 1.7 ✅ 评委环境（没装 SDK、没有 Nacos）不受任何影响

```
$ python3 -m pytest maos/tests -q
1015 passed, 31 skipped in 19.53s          # 基线 935 passed / 29 skipped
$ python3 run.py > /dev/null; echo $?
0
$ python3 scripts/gen_docs.py --check; echo $?
0
```

新增 82 条里 **80 条在没有 SDK 的机器上照跑**，只有真连那 2 条 skip。
skip 的理由是可读的，不是一句「skipped」：

```
Nacos 真连不可用：未装 nacos-sdk-python（63 个包 / 135MB 的可选依赖，见 deploy/nacos.md）
```

---

## 二、未实测（这一栏不许省）

| # | 没验的东西 | 为什么没验 | 影响 |
| :-- | :-- | :-- | :-- |
| 2.1 | **阿里云 MSE 托管 Nacos** | 手上没有实例 | 本文件全部结论的天花板就是本机容器。「兼容 Nacos 协议所以能跑」是**推理不是实测** —— 与 PolarDB 那一轨补跑之前的处境完全一样 |
| 2.2 | **命名空间（namespace）隔离** | 只用了 public | 代码里 `MAOS_NACOS_NAMESPACE` 通路是写好的，一次都没有非空跑过 |
| 2.3 | **Nacos 的灰度（beta）发布** | 30% 维度提到「灰度」，但 beta 发布按 IP 白名单推送，与本轨的「四个旋钮」不是一个粒度 | 现在的「灰度」只到「不重启改配置」这一层，**没有按实例分批** |
| 2.4 | **多实例同时监听同一个 dataId** | 单进程演示 | 多个 MAOS 进程各自落一条 `ConfigChanged`，会不会重复计数没验 |
| 2.5 | **配置回滚（历史版本一键回退）** | 没做 | Nacos 控制台自带，MAOS 这侧只会看到又一次推送 |
| 2.6 | ~~`MAOS_KB_ENABLED` / `MAOS_KB_WEIGHTS`~~ **T35 已接读取点** | T28 当时 `maos/kb/**` 归 T24 / T25，同轮并行改同一个文件必冲突 | 六个旋钮的**读取点全接完了**（`kb_enabled` / `load_weights`）。但那两个键**没进 `GOVERNED_KEYS`** —— 于是它们「能治理、变更不落审计」，理由与后续接法见 `docs/BACKLOG.md` 的 `## task-T35`。**仍未在真 Nacos 上跑过** |
| 2.8 | **探活心跳（T35）在真 SDK + 真 Nacos 上的表现** | T35 那台机器没装 `nacos-sdk-python`，也没有 Nacos 容器 | `server_health()` 那一支是用桩验的：SDK 3.2.0 到底有没有这个方法、是同步还是协程、返回什么，**一次都没验过**。探不到时会退到就绪端点兜底（那一支验过），所以最坏情况是「用的是兜底那条」，不是心跳失效 —— 但这句话本身也没在真 SDK 上验过。判据见 §3.2 |
| 2.7 | **真 Matrix 房间 + Nacos 联合演示** | 本机没有 Synapse 在跑 | §1.4 走的是真 `RoomApprovalBridge`，但审批命令是直接调 `handle_message` 送进去的，不是从 Element 里打出来的 |

---

## 三、已知差异 / 局限

### 3.1 顶层模块名是 `v2`，不是 `nacos`

`pip install nacos-sdk-python` 装完之后 `import nacos` 会 `ModuleNotFoundError`。
`import v2.nacos` 才对。编排侧和本轨各踩了一次。

### 3.2 ~~🔴 MAOS 看不见「配置中心挂了」~~ —— T35 已补，但补的那条**没在真 Nacos 上验过**

**T28 记录（保留原文，这是当时的实测事实）**：§1.6 ② 实测，Nacos 停掉之后
`degraded` 仍然是 `False`、`explain()` 仍然说 `nacos`。原因是 `degraded` 只在
**构造时**连不上才置位；连上之后掉线由 SDK 内部重连兜着，它不告诉上层。于是
「沿用最后一份配置」这件事**在 MAOS 这侧没有任何症状** —— 你以为在读 Nacos，
其实读的是几小时前的快照。

**T35（2026-08-31）补法**：一条低频探活线程（缺省 30s，
`MAOS_NACOS_HEALTH_INTERVAL_S` 可调），把探活结果并进 `degraded`，两个方向的翻转
各落一行日志。口径见 `deploy/nacos.md` §5。

🔴 **但这条补法的验证天花板要说清楚，别把它念成「在真 Nacos 上验过了」**：
T35 那台机器**没装 `nacos-sdk-python`**（63 个包 / 135MB，不在 `pyproject.toml` 里，
装依赖属必须问人类的四类），**也没有 Nacos 容器在跑**。所以验的是两条真代码路径，
不是一次真连：

| 验了什么 | 怎么验的 | 天花板 |
| :-- | :-- | :-- |
| 就绪端点兜底那一支 | 起一个**真的**本机 HTTP server 当就绪端点，`shutdown()` 掉再拉回来 | 探活逻辑、翻转逻辑、日志、线程收尾都是真跑的；**不是真 Nacos** |
| `server_health()` 那一支 | 桩 service 返回一个可翻转的协程 | 证明了「协程形态的返回值能并进 degraded」；**没证明 SDK 3.2.0 真有这个方法、返回什么** |

跑出来的翻转（两条路径各三态）：

```
INFO    配置面探活心跳已起：每 1.0s 一次
>> ① 接通、服务端活着       degraded=False  reason=''
     取值仍走 Nacos 快照：MAOS_MAX_REPLAN='9' origin='nacos'
-- 停掉服务端（等价 docker stop maos-nacos）--
WARNING 配置面降级 env：Nacos 探活失败（就绪端点不可达（URLError））—— server=… ；仍按最后一份快照（2 项）继续跑
>> ② 服务端挂了            degraded=True
     快照照读，last-known-good 不变：MAOS_MAX_REPLAN='9' origin='nacos'
-- 把服务端拉回来 --
INFO    配置面已恢复（就绪端点 HTTP 200）—— 此前降级原因：…
>> ③ 服务端回来了          degraded=False  reason=''
```

**所以 §2 新增一条 2.8**：探活心跳在真 SDK + 真 Nacos 上一次都没跑过。
下一个手上有 Nacos 容器的会话，把 §1.6 那一步照原样重跑一次即可闭合 ——
判据就一条：`docker stop maos-nacos` 之后 30s 内 `degraded` 翻成 `True`。

### 3.3 publish 之后**立刻** get_config 可能读到空串

第一次 spike 撞到过：`publish_config` 返回 `True`，紧接着 `get_config` 返回 `''`。
换成「配置早就存在、进程后启动」这个真实顺序就正常（§1.3 实测）。

影响只在「同一个进程先发布再立刻读」这种用法上，MAOS 不这么用。

### 3.4 配置历史是异步落库的，查操作人要重试

推送先到、历史后写。一个**全新 dataId** 第一次改动时，推送到达那一刻
`/nacos/v1/cs/history` 的 `pageItems` 还是空的 —— 不重试就会把一条本来查得到操作人的
变更记成「操作人未知」，而那正是审计最不该出错的地方。

`_HistoryLookup.who()` 因此带 5 次 × 0.25s 的有界重试。
`MAOS_NACOS_TEST_DATA_ID` 换一个没用过的名字就能复现这条竞态。

### 3.5 一次变更曾经被上报两遍（已修，记下来是因为它很隐蔽）

实现初版里有两条路都会上报同一次变更：推送回调（`_apply`）和读取时比对（`_notice`）。
后者**没有操作人**，而且因为前者要先查一次历史 API（慢），**后者会抢先落库** ——
于是审计里留下的是 `actor: ''` 那一条，看起来像「查过了，没查到」。

修法是让带推送的源把变更上报权收归推送回调一家（`emits_on_read = False`）。
留在这里是因为这个坑的症状极具误导性：审计**有**记录、条数**也对**，只是操作人是空的。

### 3.6 SDK 关闭路径会喷噪音

`shutdown()` 之后偶尔留下 `Task was destroyed but it is pending!` 与
`fork_posix.cc:71 Other threads are currently calling into gRPC`。
不影响退出码（实测 `exit=0`），但演示时终端会脏。

### 3.7 SDK 是纯 async 的，MAOS 是同步的

`create_config_service` / `get_config` / `add_listener` 全是协程，监听回调还必须是
`async def` 且四个位置参数（`v2/nacos/config/model/config.py:89` 写死）。
接法是把客户端关进一条专用事件循环的守护线程，`get()` 只读内存快照 ——
理由与另外两条路的取舍写在 `maos/config/nacos_source.py` 的模块抬头。

---

## 四、复跑方式

**判据就是那两条真连测试**，不需要另写脚本：

```bash
# 1) 起 Nacos（首次要按 §1.2 初始化 admin）
export NACOS_AUTH_TOKEN=$(openssl rand -base64 48)
export NACOS_AUTH_IDENTITY_KEY=<任意串>
export NACOS_AUTH_IDENTITY_VALUE=$(openssl rand -hex 16)
docker compose -f deploy/nacos/docker-compose.yml up -d

# 2) 隔离 venv 装可选依赖（别装进系统 python3）
python3 -m venv /tmp/nacos-venv && /tmp/nacos-venv/bin/pip install nacos-sdk-python pytest

# 3) 跑
export MAOS_NACOS_USERNAME=<用户名> MAOS_NACOS_PASSWORD=<口令>
/tmp/nacos-venv/bin/python -m pytest maos/tests/test_config_source.py -q
#   -> 82 passed

# 想复现 §3.4 那条竞态：换一个没用过的 dataId
export MAOS_NACOS_TEST_DATA_ID=maos-governance-$RANDOM
```

不做第 1、2 步时，`python3 -m pytest maos/tests/test_config_source.py -q`
的结果是 `80 passed, 2 skipped` —— 那就是评委机器上的样子。

---

## 五、安全声明（铁律 6）

* 本文件**没有任何一行口令**。Nacos 的用户名口令全程只从环境变量读，
  文档里一律写成 `<用户名>` / `<口令>` 或 `$MAOS_NACOS_PASSWORD`。
* §1.2 第 3 条**刻意只写「200」不贴响应正文** —— 那个接口会把口令原样回显。
* 出现的 IP（`172.20.0.1` / `10.100.158.135`）是 Docker 网桥与本机内网地址，
  不是凭证；保留它们是因为 §1.5 的结论正是靠 `srcIp` 与 `srcUser` 的对比得出的。
* Nacos 端口只绑 `127.0.0.1`，compose 里三个 auth 值是 `${VAR:?…}` 形态，不配就起不来。
