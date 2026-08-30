# 把四个治理旋钮搬上 Nacos

这份文档回答一个问题：**MAOS 这侧怎么接 Nacos**（怎么配、配置文档长什么样、
连不上怎么降级、变更怎么落审计）。

它的实测天花板是**本机 Docker `nacos/nacos-server:v2.4.3`**。
真跑通了哪几条、哪几条没跑通，在 [`deploy/nacos-live.md`](nacos-live.md)。

## 与 `deploy/nacos-live.md` 的分工（别把两份混起来）

| 文件 | 回答什么 | 天花板 |
| :-- | :-- | :-- |
| **本文件** | MAOS 的配置面怎么接、怎么配、怎么降级、审计落在哪 | 本机 Docker `nacos-server:v2.4.3` |
| [`deploy/nacos-live.md`](nacos-live.md) | 上面这些**实际跑通了哪几条**，含没跑通的 | 同一台本机容器，只记观察 |

同 `polardb.md` / `polardb-live.md` 的分工（README §9 有红字讲为什么必须分两份）。
只读本文件容易把「接口写好了」误当成「跑通了」—— 那是两件事。

---

## 1. 为什么要做这件事

复赛 30% 维度（工程落地与安全审计）明确列了「版本、灰度」与「审计日志能追溯
**谁**在**什么时候**做了**什么操作**」。

在此之前，四个治理旋钮全是**进程启动时读一次环境变量**：

| 旋钮 | 干什么 | 改一次的代价 |
| :-- | :-- | :-- |
| `MAOS_MAX_REPLAN` | replan 上限，超限转人工 | 重启进程 |
| `MAOS_FINANCE_THRESHOLD` | 第六道闸的财务复核阈值 | 重启进程 |
| `MAOS_SANDBOX_TIMEOUT` | 单次沙箱执行超时秒数 | 重启进程 |
| `MAOS_APPROVERS` | **审批权限名单** | 重启进程 |

而且**没有任何一条记录说明是谁改的**。

`MAOS_APPROVERS` 尤其对味 —— 它决定谁能批一笔钱，动它属于安全事件。
接上 Nacos 之后这两件事都成立了：改名单不重启，且每一次变更落一条 `event_log`。

---

## 2. 🔴 一条压倒一切的约束：缺省路径一个字节都不变

本仓库的卖点是「无需任何 key、裸 clone 7 秒跑到 `8/8 PASS`、核心零依赖」。
`nacos-sdk-python` 是 **63 个包 / 135MB**（会拉进 `alibabacloud_kms`、`a2a-sdk`、
`google-api-core`、`grpcio`）。把它塞进主依赖，「核心零依赖」当场作废，
`clone → pytest` 从秒级变成分钟级 —— 直接撞上 30% 维度红线「无法在合理环境中复现」。

所以：

1. **`nacos-sdk-python` 不在 `pyproject.toml` 里**，只在本文档 §7 的可选依赖一节。
   `maos/tests/test_config_source.py::test_sdk_is_not_a_declared_dependency` 钉着这条。
2. `MAOS_CONFIG_SOURCE` 未设 = `EnvConfigSource`，`get()` 就是 `os.environ.get`，
   四个读取点的取值与接 Nacos 之前**逐字节一致**。
3. `import` 是**两层惰性**的：`maos.config` 不 import `maos.config.nacos_source`，
   后者不 import `v2` —— 只有真的要用 Nacos 时才碰 SDK。
   `test_importing_maos_config_does_not_import_the_sdk` 起子进程验这条。
4. 没装 SDK / 没起 Nacos 的机器上，新增测试要么全绿要么 skip。

这与 PolarDB（`maos/store/`）、Matrix（`hiclaw/`）是同一个模式：**可选后端 + 缺项自动
降级**，不是本轮的发明。

---

## 3. 三步接上

### 第 1 步 · 起一个 Nacos

```bash
export NACOS_AUTH_TOKEN=$(openssl rand -base64 48)     # 原文 >= 32 字节
export NACOS_AUTH_IDENTITY_KEY=<任意串>
export NACOS_AUTH_IDENTITY_VALUE=$(openssl rand -hex 16)
docker compose -f deploy/nacos/docker-compose.yml up -d
```

`deploy/nacos/docker-compose.yml` 把端口**只绑 `127.0.0.1`**：这台机器上跑的是一个
带审批人名单的治理面，绑 `0.0.0.0` 等于把「谁能批钱」这张表挂到局域网上。

🔴 **Nacos 2.4.x 不预置 admin 账号**，第一次登录会 500（`caused: User nacos not found;`）。
先初始化一次：

```bash
curl -X POST "http://127.0.0.1:8848/nacos/v1/auth/users/admin" \
     -d "password=$MAOS_NACOS_PASSWORD"
```

⚠️ 这个接口的**响应会把口令原样回显**。别把它的输出贴进任何文件或证据里（铁律 6）。

### 第 2 步 · 装可选依赖（装进隔离 venv，不要装进系统 python）

```bash
python3 -m venv /tmp/nacos-venv
/tmp/nacos-venv/bin/pip install nacos-sdk-python
```

🔴 **顶层模块名是 `v2`，不是 `nacos`。** `import nacos` 会 `ModuleNotFoundError`——
这是接这个 SDK 第一个会踩的坑。

### 第 3 步 · 两行环境变量

```bash
export MAOS_CONFIG_SOURCE=nacos
export MAOS_NACOS_SERVER=127.0.0.1:8848
export MAOS_NACOS_USERNAME=<用户名>
export MAOS_NACOS_PASSWORD=<口令>          # 铁律 6：只从环境变量读，不写进任何文件
```

全部连接参数：

| 环境变量 | 缺省 | 说明 |
| :-- | :-- | :-- |
| `MAOS_CONFIG_SOURCE` | `env` | `nacos` 才走配置面；别的值回落 `env` 并告警 |
| `MAOS_NACOS_SERVER` | `127.0.0.1:8848` | |
| `MAOS_NACOS_NAMESPACE` | `""`（public） | |
| `MAOS_NACOS_GROUP` | `DEFAULT_GROUP` | |
| `MAOS_NACOS_DATA_ID` | `maos-governance` | 四个旋钮装在同一个 dataId 里 |
| `MAOS_NACOS_USERNAME` / `MAOS_NACOS_PASSWORD` | 空 | 开了鉴权就必填 |
| `MAOS_NACOS_TIMEOUT_MS` | `5000` | |
| `MAOS_NACOS_HEALTH_INTERVAL_S` | `30` | 探活心跳节拍（秒，T35）。**演示才调低，部署里别动**；`<=0` 关掉心跳；低于 `1` 会被抬到 `1` |

---

## 4. 配置文档长什么样

一个 dataId 装四个旋钮。**properties 与 JSON 两种写法都认**：

```properties
# Nacos 控制台里 dataId = maos-governance 的正文
MAOS_APPROVERS=@boss:maos.local,@cfo:maos.local
MAOS_FINANCE_THRESHOLD=5000
MAOS_MAX_REPLAN=2
MAOS_SANDBOX_TIMEOUT=300
```

```json
{"MAOS_APPROVERS": "@boss:maos.local", "MAOS_MAX_REPLAN": 2}
```

写坏了（比如 JSON 少个括号）**按空文档处理，不抛** —— 控制台上一次手滑不该让审批链路
当场停摆；空文档会让每个 key 都走「Nacos 无此项 → 回落 env」，而那一档是有日志的。

**只有 `GOVERNED_KEYS` 里那四个 key 会被 diff 出变更**，文档里的其他行原样忽略。

---

## 5. 降级：三态，每一档都有日志

`get()` **永远不抛**。逐级回落：

| 档 | 触发条件 | 日志 | 之后从哪取值 |
| :-- | :-- | :-- | :-- |
| ① | SDK 没装 | `WARNING 配置面降级 env：nacos-sdk-python 未安装…` | `os.environ` |
| ② | 连不上 / 拉不到 | `WARNING 配置面降级 env：Nacos 不可达（…）server=… dataId=…` | `os.environ` |
| ③ | 该项在 Nacos 没有 | `INFO <key> 在 Nacos（…）无此项，本次取值来自 env` | `os.environ` |
| ④ | **接通之后**服务端挂了（T35） | `WARNING 配置面降级 env：Nacos 探活失败（…）；仍按最后一份快照（N 项）继续跑` | **仍是 Nacos 那份快照**（见下） |

④ 与 ①②③ 有一条关键差别：**它不改变取值来源**。快照仍是最后一份好配置，照读 ——
last-known-good 这个行为本身是对的，反过来等于「配置中心一抖动所有人都批不了钱」。
④ 补的只有**可观测性**：在它之前，「配置中心挂了」这件事在 MAOS 侧完全无症状。

🔴 **降级必须是「静默失败之外的第三态」。** 静默降级会让人以为治理生效了，而实际上
没有 —— 那比不接更坏。所以除了日志，还有两个**可写进断言**的出口：

```python
src.degraded          # 整体降级态（①②）
src.degraded_reason   # 人话原因，没降级是空串
src.explain(key)      # 上一次 get(key) 的来源："nacos" / "env" / "default"
```

光有日志不够 —— 「降级了没有」只能靠读日志眼判的话，静默降级就有地方藏。

### 已经连上之后 Nacos 挂了会怎样

**沿用最后一次拿到的那份配置继续跑**（last-known-good），审批名单不会被清空。
实测记录见 `nacos-live.md` §1.6。

**T28 收工时这里有个洞，T35 补上了**：`degraded` 那时只在**构造那一刻**置位，
连上之后 Nacos 挂掉，MAOS 侧不报错、不变慢、日志里一行都没有，`degraded` 一直是
`False` —— 你以为在读 Nacos，实际读的是几小时前的快照。这类缺陷不会被测试发现，
只会在演示当天发现。

补法是一条**低频探活线程**：缺省 30s 一次（`MAOS_NACOS_HEALTH_INTERVAL_S` 可调），
把结果并进 `degraded`，**两个方向的翻转各落一行日志**：

```
WARNING 配置面降级 env：Nacos 探活失败（就绪端点不可达（URLError））—— server=… ；仍按最后一份快照（2 项）继续跑
INFO    配置面已恢复（就绪端点 HTTP 200）—— 此前降级原因：Nacos 探活失败（…）
```

三条约束刻在代码里：

* **低频**。它是健康探测不是配置轮询 —— 配置怎么生效不归它管，SDK 自己那条 5s
  的长轮询才是配置通路，探得再密也不会让新配置早一秒到。写成秒级买到的只是一个
  没人看的小数点，付出的是给 Nacos 的 N 倍连接压力。
* **心跳自身失败不掀主流程**。探活抛什么都只是「这一轮没探成」。配置中心挂了
  该让 MAOS 降级，不是让 MAOS 陪葬。
* **缺省路径连一个线程都不起**。`MAOS_CONFIG_SOURCE` 未设时
  `NacosConfigSource` 一个实例都不会造。

探活优先用 SDK 的 `server_health()`；SDK 没暴露它就退到 Nacos 自己的就绪端点
`/nacos/v1/console/health/readiness`（与 `deploy/nacos/docker-compose.yml` 的
healthcheck 同一个 URL，走 stdlib `urllib`，不引第二个依赖，请求里不带任何凭据）。
**没有这个兜底的话，SDK 换个版本这条心跳就静默变成空转** —— 那又是同一类
「没有症状的故障」，只是换了个位置重新长出来。

---

## 6. 审计：一次变更一条 `event_log`

每一次旋钮变更落一条 `ConfigChanged`：

```json
{
  "event_type": "ConfigChanged",
  "reason": "MAOS_APPROVERS: '@boss:maos.local' -> '@cfo:maos.local'",
  "detail": {
    "key": "MAOS_APPROVERS",
    "old": "@boss:maos.local",
    "new": "@cfo:maos.local",
    "origin": "nacos",
    "actor": "nacos@172.20.0.1",
    "actor_source": "nacos-history-api",
    "at": "2026-08-30T05:40:32.525111+00:00",
    "server": "127.0.0.1:8848", "namespace": "", "group": "DEFAULT_GROUP",
    "data_id": "maos-governance-demo-3432498"
  }
}
```

接线一行：

```python
from maos.config import attach_config_audit
detach = attach_config_audit(store, plan_id=plan_id)   # 返回取消订阅的函数
```

`hiclaw/room_demo.py` 已经这么接了，落在那次演示的 `plan_id` 上 —— 于是
`list_event_log(plan_id)` 一把捞得出「谁在什么时候把名单从 X 改成 Y」，
与状态迁移在同一条时间线上。

### 为什么这不算「扩事件契约」

`ConfigChanged` 是 **`event_log` 的自由 `event_type`，不进 `contracts/events.py`**。
这不是本轮的发明，是仓库里已经成文的写法 —— `maos/agents/testing.py:50`：

> 走 `append_event_log` 的自由 `event_type`，**不进 contracts/events.py 的 Topic**
> （铁律 1）—— `SkillInvoked` / `ToolInvoked` / `AuthoritativeFactViolation`
> 都是这么加的。

`maos/kb/retriever.py:571` 的 `KbRetrieved` 是这条路上最近的先例。
`ConfigChanged` 是下一个：**一个字节的冻结契约都没动，一张新表都没建。**
`test_audit_does_not_extend_the_frozen_contract` 钉着这条。

### 「谁改的」不许编

SDK 的推送回调只给 `(tenant, group, data_id, content)` 四个参数，**里面没有操作人**。
所以操作人是事后从 Nacos 自己的配置历史 API（`/nacos/v1/cs/history`）查回来的：

* 查到就记 `srcUser@srcIp`，`actor_source` 写 `nacos-history-api`；
* 查不到就**留空**，`actor_source` 里写明为什么留空。

🔴 **实测差别（`nacos-live.md` §1.5）**：经**控制台 / OpenAPI** 改的配置，
Nacos 记得下 `srcUser`；经 **SDK 的 gRPC `publish_config`** 改的，`srcUser` 是空的。
所以「谁改的」这件事，只有走控制台那条路才追得到人 —— 这是 Nacos 的行为，
不是 MAOS 能补的。照实写在这里，别在演示时说成「全都能追溯」。

---

## 6.5 写入侧闸门：`MAOS_APPROVERS` 不许被一次手滑清空（T35）

审批名单是安全面。控制台上一次手滑把它写成空串、或只剩逗号空格，采用了就是
**所有人当场批不动**。落审计只解决「事后查得到」，不解决「当场就错了」。

于是 `NacosConfigSource._apply` 里有一道**只针对这一个键**的闸门：

| 旧名单 | 新名单 | 结果 |
| :-- | :-- | :-- |
| 非空 | 解析后一个人都不剩 | **拒绝采用**，快照沿用旧值，落一条**告警级**审计 |
| 非空 | 非空（换人、增删） | 照常采用 |
| 空 | 空 | 照常采用（没有「上一份好名单」可沿用，拦下来只会更难查） |
| 空 | 非空 | 照常采用 |

**闸门只拦「清空」这一个方向。** 它要挡的是一次手滑，不是「配置面说了不算」。

判据只有一条：**这份名单解析完还剩不剩人**（口径与
`hiclaw.matrix_bus.parse_approvers` 等价：逗号分隔、空白项丢弃）。
**不判「这个人该不该有权限」** —— 那件事机器判不了，只能靠审计事后追。
也**不判 Matrix ID 形态** —— `parse_approvers` 明写不做格式校验，在这里补一道
会让一份合法但不走 Matrix 的名单被拒，那是把一个没有的洞堵成一个新的洞。

被拒的那一次**照样进审计**，否则就成了另一种静默：

```
WARNING 拒绝采用配置变更 MAOS_APPROVERS：解析后一个审批人都不剩 —— 采用它等于所有人当场批不动 —— 沿用旧值 '@boss:example.org,@cfo:example.org'（操作人 未知）
```

```json
{
  "event_type": "ConfigChanged",
  "reason": "MAOS_APPROVERS: '@boss:example.org,@cfo:example.org' -> ''（已拒绝采用，沿用旧值）",
  "detail": {
    "severity": "warning",
    "rejected": true,
    "reject_reason": "解析后一个审批人都不剩 —— 采用它等于所有人当场批不动",
    "attempted": "",
    "effective": "@boss:example.org,@cfo:example.org"
  }
}
```

同一次推送里**别的键照常采用，不连坐** —— 一次保存里同时改了阈值和名单时，
阈值该生效还是生效。

> **「拒绝采用」还是「采用并告警」是人类拍的板**（2026-08-31）。
> 另一条路（采用并告警）符合「配置中心是权威」的直觉，代价是空名单一旦生效，
> `_effective_approvers()` 会回落到构造时那份快照 —— 而真部署里那份同样可能是空的，
> 于是审批全线阻塞，且现场没人知道是这次配置改动造成的。取舍记在
> `docs/DECISIONS.md` 的 `## task-T35`。

---

## 7. 可选依赖（**不进 `pyproject.toml`**）

```
nacos-sdk-python==3.2.0        # 顶层模块名是 v2；63 个包 / 135MB
```

装了它才有 `MAOS_CONFIG_SOURCE=nacos` 这条路；不装则 `EnvConfigSource` 照常跑，
全量测试与 `python3 run.py` 一个字节都不受影响。

---

## 8. 已实测 / 未实测

**已实测**（详见 `nacos-live.md`，每条都有原始输出）：
本机 Docker `nacos-server:v2.4.3`（arm64）+ 鉴权开启；四个旋钮真从 Nacos 取；
不重启改审批人名单、下一条审批按新名单判；变更落审计且带操作人；
Nacos 停机时沿用最后一份配置；三档降级。

**T35 补测**（无真 Nacos / 无 SDK 的机器上，见 `nacos-live.md` §3.2）：
接通之后服务端不可达时 `degraded` 会翻成 `True`、服务端回来会翻回 `False`，两个方向
各落一行日志；空审批名单被闸门拒绝采用且照样落审计；`MAOS_KB_ENABLED` /
`MAOS_KB_WEIGHTS` 两个旋钮已接上配置面（六个旋钮**读取点全接完**）。

**未实测**：阿里云 MSE 托管 Nacos、命名空间隔离、Nacos 的灰度（beta）发布、
多实例同时监听、**探活心跳在真 SDK 3.2.0 + 真 Nacos 上的表现**（T35 那台机器没装
SDK，`server_health()` 那一支是用桩验的）。

---

## 9. 安全声明（铁律 6）

* Nacos 的用户名口令**只从环境变量读**，不写进本仓库任何文件。
  `deploy/nacos/docker-compose.yml` 里三个 auth 值全是 `${VAR:?…}` 形态，
  不配就起不来 —— 「不配就是不安全」写在脸上，而不是藏在一个像密钥的字面量里。
* `deploy/nacos-live.md` 里没有任何一行口令，且**刻意不贴** admin 初始化接口的响应
  （那个接口会回显口令）。
* 审计行里的值过一层脱敏：key 名含 `KEY` / `TOKEN` / `SECRET` / `PASSWORD` / `DSN` /
  `CREDENTIAL` 的只落长度不落内容（`maos/config/source.py::redact`）。
  本轮四个旋钮都不是密钥，这条是护栏不是功能。
* 探活心跳（T35）的兜底请求打的是 `/nacos/v1/console/health/readiness`，
  **不带任何凭据**，响应只读前 64 字节且不落任何文件。
* Nacos 端口只绑 `127.0.0.1`。
