# IM 渠道接入 —— 配置与联调

系统最前面的一层：飞书 / 企业微信 / 微信客服里的一句话进来，一次真实处置出去。
实现在 `maos/ingress/`，入口 `scripts/run_ingress.py`，设计取向见
`maos/ingress/__init__.py` 的模块抬头。

## 0. 先在本机跑通（零凭证）

```bash
python3 scripts/run_ingress.py --status
MAOS_APPROVERS=ou_demo python3 scripts/run_ingress.py \
  --simulate "/refund ORD-2026-0001 质量问题" "/approve RC-ORD-2026-0001"
```

`--simulate` 绕开 HTTP 与签名，直接把一句话喂给 router，跑的是**同一条**处置链路。
业务侧先用它跑通，联调时就只剩下「回调配没配对」一个变量。

发照片这条链路也能零凭证验，`--photo` 的字节从本机文件来，落盘链路与真渠道完全相同：

```bash
MAOS_APPROVERS=ou_demo python3 scripts/run_ingress.py \
  --photo 破损1.jpg 质检报告.pdf \
  --simulate "/refund ORD-2026-0001 质量问题" "/approve RC-ORD-2026-0001"
```

## 1. 命令面

| 命令 | 谁能用 | 动不动钱 |
|---|---|---|
| `/refund <订单号> <诉求类型> [金额] [日期]` | 所有人 | **不动**。只读预检：读政策、算窗口、出裁定，挂一条待办 |
| `/approve <case_id>` | `MAOS_APPROVERS` 名单内，且限内部渠道 | **动**。真跑处置（核算 → 付款 → 观察） |
| `/reject <case_id> [原因]` | 同上 | 不动。撤掉待办 |
| `/approve <task_id>` | 同上 | 转 `RoomApprovalBridge`，任务级审批 |
| `/pending` | 内部渠道 | 不动。列待办与等人审批的任务 |
| `/team` | 所有人 | 不动。报一遍圆桌有哪几岗、各自用哪个账号说话、装了哪些 skill（见 §4.8） |
| `/help` | 所有人 | 不动 |

两步走是刻意的：一条群消息不该直接触发一次付款 —— 发命令的人可能打错订单号、
可能不是审批人、可能只是想问「这单能退多少」。

**待办只活在内存里**，进程重启即失效（重发一条 `/refund` 即可重建），且
24 小时过期 —— 预检结论是按当时的政策与日期算的，隔天再批就是拿旧结论退新钱。

## 2. 环境变量

密钥**只读环境变量**，禁止写进任何文件（铁律 6）。放 `~/.maos.env` 然后
`source ~/.maos.env` 前缀执行。

### 通用

| 变量 | 说明 |
|---|---|
| `MAOS_APPROVERS` | 逗号分隔的审批人。与 Matrix 房间**共用同一份**，所以里面会同时躺着飞书 `ou_xxx` 和 Matrix `@u:server` —— 那是同一批人在不同平台上的身份 |

### 飞书

| 变量 | 从哪来 |
|---|---|
| `MAOS_FEISHU_APP_ID` | 开放平台 → 应用 → 凭证与基础信息 |
| `MAOS_FEISHU_APP_SECRET` | 同上 |
| `MAOS_FEISHU_VERIFICATION_TOKEN` | 事件订阅 → Verification Token |
| `MAOS_FEISHU_ENCRYPT_KEY` | 事件订阅 → Encrypt Key（**生产必填**，见下） |

应用需要的权限：`im:message`（收）、`im:message:send_as_bot`（发）。
订阅事件：`im.message.receive_v1`。

**Encrypt Key 是一个安全决策，不是配置口味。** 留空 = 明文回调，此时飞书
**不发签名头**，能校的只有 body 里那个 `token` —— 它是长期不变的共享秘密，
且随每个请求原样送达，泄了之后任何人都能构造出「合法」的回调。配上它才有
`sha256(timestamp + nonce + key + body)` 签名与时间戳窗口。留明文这条路只是
为了让没装 `cryptography` 的机器也能当场跑通链路。

### 企业微信（自建应用）

| 变量 | 从哪来 |
|---|---|
| `MAOS_WECOM_CORP_ID` | 我的企业 → 企业 ID |
| `MAOS_WECOM_AGENT_ID` | 应用 → AgentId |
| `MAOS_WECOM_SECRET` | 应用 → Secret |
| `MAOS_WECOM_TOKEN` | 应用 → 接收消息 → Token |
| `MAOS_WECOM_AES_KEY` | 应用 → 接收消息 → EncodingAESKey（43 位） |

还要把本机的**公网出口 IP** 加进「企业可信 IP」，否则 `gettoken` 会报 60020。

### 微信客服

企业微信下面的能力，**与自建应用共用 `MAOS_WECOM_CORP_ID`**，其余三项独立：

| 变量 | 从哪来 |
|---|---|
| `MAOS_WECHAT_KF_SECRET` | 微信客服 → API → Secret |
| `MAOS_WECHAT_KF_TOKEN` | 微信客服 → 回调配置 → Token |
| `MAOS_WECHAT_KF_AES_KEY` | 同上，EncodingAESKey |

> **个人微信没有合规的官方消息 API。** 任何「直连个人微信」的方案都是逆向
> （封号 + 不合规）。要让外部微信用户能进来，官方路径就是这里的「微信客服」，
> 或另开一个微信公众号（服务号）。

企微与微信客服的回调**强制加密**，需要 AES：

```bash
python3 -m pip install 'cryptography'      # 或 pycryptodome
```

不装它，这两个渠道会显式抛 `ChannelDepMissing`（而不是静默把密文当明文解析失败、
再归入「不是消息事件」丢掉 —— 那个症状是「群里发了没反应」）。飞书不吃这条依赖。

## 3. 回调地址

| 路径 | 渠道 |
|---|---|
| `/ingress/feishu` | 飞书 |
| `/ingress/wecom` | 企业微信自建应用 |
| `/ingress/wechat-kf` | 微信客服 |
| `/healthz` | 健康检查 |

三个平台都只接受**公网 HTTPS**，而本进程只讲 HTTP、默认只听 `127.0.0.1`
（它自己不做 TLS，直接摆到公网上等于把一个能触发真金白银的口子裸奔）。
正确部署是前面放一层 nginx / frp：

```nginx
location /ingress/ {
    proxy_pass http://127.0.0.1:8737;
    proxy_set_header Host $host;
    proxy_set_header X-Lark-Signature $http_x_lark_signature;
    client_max_body_size 1m;
}
```

签名是对**原始字节**算的 —— 反代不许改写 body（别开 gzip 重编码、别加 BOM），
改一个字节签名就对不上，而症状是「回调地址配得通、真消息全被 401」。

起服务：

```bash
source ~/.maos.env && python3 scripts/run_ingress.py --host 127.0.0.1 --port 8737
```

## 4. 配回调时的顺序

1. 先起本进程，`--status` 确认目标渠道显示 `configured`（未配置的渠道对回调回
   **503** 而不是 404 —— 地址是对的，是我方没配好）。
2. 反代通了之后再去平台后台填地址。平台会先来一次 URL 验证：
   飞书回 `{"challenge": ...}`，企微要求把解密后的 `echostr` **原样**回去
   （加引号或包 JSON 都配不通）。
3. 群里发 `/help`。没反应就看本进程日志：401 = 签名/token 不对；
   一片安静 = 消息根本没到（查反代与平台后台的失败计数）。

## 4.5 发照片当证据

群里甩一张照片，系统会取件、落盘、回一句回执，等下一句 `/refund` 认领：

```
[张三 发了一张图]
→ 已收下 1 份证据（暂存 30 分钟，等一条 /refund 认领）：
    · 破损.jpg（image/jpeg，412 KB，sha256:f91775bd1346）
  这个会话现有 1 份待认领证据。接着发：/refund <订单号> <诉求类型>

[张三] /refund ORD-2026-0001 质量问题
→ 预检 · …… · 随案证据：1 份（本会话上传，f91775bd）
```

**照片先进、命令后到**是刻意支持的顺序 —— 那是人在群里的真实动作。照片自己不带
订单号，所以按 `(渠道, 会话)` 暂存 30 分钟，`/refund` 认领时挂进
`payload["customer_evidence"]`，由 `refund.intake` 落库（那是这张表唯一的写入路径）。
**取走即清空**：不清的话下一句 `/refund ORD-B` 会把上一单的照片再挂一遍。

| 事项 | 口径 |
|---|---|
| 收哪些 | jpeg / png / gif / webp / heic / pdf。**按内容嗅探判**，平台自报的 MIME 不采信 |
| 单份上限 | 20 MB（`MAOS_ATTACHMENT_MAX_BYTES`） |
| 一单最多挂 | 10 份（超出丢最旧的） |
| 存哪 | `var/attachments/<ab>/<sha256>`（`MAOS_ATTACHMENT_DIR`）。**内容寻址**，同一张图重复进来只落一份 |
| 进不进 git | **不进**。`.gitignore` 已排除 —— 客户的照片里可能有身份证、面单，而密钥扫描只认文本、扫不到 PNG |
| 渠道支持 | 飞书（image / file / post 图文混排）、Matrix 房间（`m.image` 与 `m.file`）。企微 / 微信客服**未实现取件**，图片仍安静忽略 |

Matrix 侧要收附件，`listen` 传第二个回调：`channel.listen(on_message, on_attachment)`。
不传就完全不注册附件回调，行为与加这条能力之前逐字一致。**加密房间的媒体不收** ——
拿到的是 AES 密文，落盘会「成功」，只有真去打开那张图的人才发现是乱码。

`listen` 的两个回调都在**工作线程**上跑，不在 nio 的事件循环线程上。这是真房间
撞出来的：nio 在 `sync_forever` 里 `await` 回调，回调里再同步等一个要同一条循环
才能推进的 `send` / `fetch`，就是自己等自己 —— 症状是回帖固定迟到 30s、取件 100%
超时，而回调里不发消息的单元测试全绿。

## 4.6 申请表进群

群里甩一张退款申请表（CSV，格式同 `scripts/run_requests.py`：订单号 / 诉求类型 /
申报金额 / 申请日期），系统**逐行**预检并回一份反馈：

```
申请表 bad-requests.csv：共 6 行，可预检 2 行，有问题 4 行

有问题的行（改好后整张表再发一次）：
  · 第 2 行 ORD-9999-9999：底账里没有订单 ORD-9999-9999
  · 第 3 行 ORD-2026-0001：看不懂的诉求类型 '天上掉馅饼'。可以写：……
  · 第 4 行 ORD-2026-0002：金额 -500 是负数 —— 退款金额不能为负
  · 第 5 行 ORD-2026-0003：看不懂的日期 '2026-13-45'，写成 2026-07-10 这样就行

预检结果（只读，未动任何资金）：
  · 第 7 行 ORD-2026-0001（质量问题）：批准 —— ……；付款至申请 61 天，申报 999999999
      提醒：申报 999999999 超过订单实付 6800，核算时会按实付封顶
      放行：/approve RC-ORD-2026-0001（需 supervisor）
```

| 事项 | 口径 |
|---|---|
| 认表 | **按内容**：能解码成文本且表头有订单号那一列。改名成 .csv 的二进制到不了这里，照旧走白名单被拒 |
| 编码 | utf-8-sig（吃掉 Excel 的 BOM）→ gbk（中文 Windows 上 Excel 另存 CSV 的默认）。用了 gbk 会在回帖里说 |
| 逐行独立 | 一行填错不挡其余行；一行里的几处错一次说完。与 `run_requests.py` 的 fail-fast 刻意不同 —— 群里的人是改完整张表再发 |
| 行号 | 按记录数（表头第 1 行），空行也占号，单元格里的换行不算新行 —— 对得上 Excel 左边那列 |
| 只读 | 合法的行只做预检、挂待办（同 `/refund`）。放行仍要审批人逐单 `/approve <case_id>` |
| 与照片的关系 | 表**不认领**会话暂存的照片 —— 十行申请配三张图，没法知道图是谁的。要挂证据就单发 `/refund` 那一单 |
| 覆盖待办 | 表里的行与 `/refund` 一样按 case_id 覆盖已有待办；换掉的是别人的、或原来挂着证据，回帖里那一行会说 |
| 上限 | 一张表 50 行（回帖要装得进一条消息），超出的行数**说出来**；回显的每个字段最多 40 字；附件本身走 20 MB 体积闸（取件前按自报 size、取件后按实际字节各拦一道） |
| 金额 | 负数、0、`nan` / `inf` 在入口拒；`/refund` 命令同一道闸 |

## 4.7 Matrix 房间：退款助手常驻

本机演示房（Element `MAOS 审批`）里跑一个常驻进程，把房间接到 ingress 这条链路：

```bash
set -a; . ~/.maos.env; . ~/.maos-matrix/room.env; set +a
~/.maos-matrix/venv/bin/python -m hiclaw.room_ingress
```

进房间之后：

| 房间里做什么 | 系统怎么回 |
|---|---|
| 打一句话（不是命令） | 有真模型（`MAOS_LLM_*` 配齐）由它接一句，只依据本进程算好的事实（命令面、底账订单、本会话待办 / 证据 / 上一张表）；没真模型回固定话术。**不会沉默** |
| 拖一张申请表 CSV | §4.6 那份逐行反馈 |
| 拖一张照片 / PDF | 收下当证据，等一句 `/refund` 认领（§4.5） |
| `/refund` `/approve` `/reject` `/pending` `/help` | 与飞书群同一套；Matrix 是内部审批房，`/approve` 在这里能落 |

回话器是**可选件**：`IngressRouter(chat=...)` 缺省不装，飞书 / 企微群里闲聊照旧
一声不吭（人来人往的群里对每句闲聊回话是骚扰）；只有这种对面只有机器人的审批房才装。
`ScriptedModelClient` 视作没模型 —— 它未命中脚本返回字面量 `{}`，房间里刷一句
`{}` 比不回还糟。模型调用失败（网关 5xx、超时）也退回固定话术并记 WARNING。

与 `hiclaw.ap_room` 相反，这里没真模型**也跑**：申请表反馈与命令面是规则代码，
一个 token 都不花；模型只在闲聊这一处出场，是配菜不是主菜。

不接长驻运行时：任务级 `/approve <task_id>` 在本进程无处可落（router 会说明）。
那是 `room_demo` / `ap_room` 的地盘，各起各的进程 —— 别在一个房间里两个 bot 抢答。

## 4.8 退款圆桌：五岗依次发言

同一个房间、同一条链路，再挂一组**旁路观察者**：预检 / 申请表 / 放行这三处各让
五个岗位（申请受理 → 规则审核 → 证据核验 → 风险反欺诈 → 财务执行）依次说一句。
它们**不进** `run_payload` 的 DAG、不落库、不改回帖 —— 处置结论一个字不动，
圆桌只把「这一单为什么这么判」摊开给房间里的人看。

怎么起（比 §4.7 多 source 一份岗位账号）：

```bash
set -a; . ~/.maos.env; . ~/.maos-matrix/room.env; . ~/.maos-matrix/agents.env; set +a
~/.maos-matrix/venv/bin/python -m hiclaw.room_ingress
```

`--no-team` 关掉圆桌，退回 §4.7 那个单机器人形态（命令面与申请表照常）。
岗位账号写在 `~/.maos-matrix/agents.env`（仓库外、`chmod 600`、永不入库），
由建号脚本生成；一岗都没配也能起，见下面的退化表。

`/team` 报一遍圆桌有哪几岗：每岗一行「岗位名（工号）· 用哪个账号说话」，跟着一行
职责，再每个 skill 一行 `name@version — purpose`。**只读、不判渠道、不调模型** ——
名单全是代码里的常量与 skill 注册表，让模型复述一遍只会多一次编造的机会（铁律 8）。
没接圆桌时它明说「本进程没接圆桌（单机器人模式）」，不装作有。

房间里一单走完是这个顺序：

| 步骤 | 房间里看到什么 |
|---|---|
| `/refund <订单号> <诉求类型>`，或拖一张申请表进来 | 先是那张只读**预检卡**（裁定 / 依据 / 申报金额 / 怎么放行）。回帖**发完之后**五岗才依次发言 —— 结论不等五次模型调用 |
| 五岗各一条 | 受理岗报案子要素、规则岗报裁定依据与窗口天数、证据岗与风险岗报各自结论、财务岗报**核算预演**（未落账）并给出 `/approve <case_id>` |
| `/approve <case_id>` | 审批人放行，真跑处置，回帖是那张执行卡 |
| 放行之后 | 财务执行岗再说一句：核准金额、政策版本、业务状态、**到账观察几条** —— 0 条就说「已受理、未到账」（铁律 8） |

退化形态（三档都照常起，只是少几句发言）：

| 缺什么 | 房间里的样子 |
|---|---|
| 没配 `MAOS_LLM_*` | 五岗照发，但发的是**事实卡**原文（规则代码算出来的那份），不是模型复述的人话。不沉默、也不刷一句 `{}` |
| 岗位账号没建 / token 失效 / 号没进房 | 那一岗改由 `maos-bot` 代言，带 `【岗位名 · 工号】` 名牌。启动那一行说清哪几岗独立、哪几岗代言，**只报 mxid 不报 token** |
| 圆桌引擎（`maos.roundtable`）或发声面模块没装载 | 退回单机器人模式，启动打印一行「圆桌：未装载」。命令面、申请表、闲聊一个字不受影响 |

## 5. 已知边界

- **群里起的单，Plan 内的任务级审批仍由处置流程代跑。** 群里那次 `/approve`
  决定的是「这一单要不要办」；跑起来之后闸门拦下的任务级审批点（如核算分录）
  由 `custom_case.run_payload` 按 CLI 口径代跑。回帖会把它们逐条列出并标明
  「与群里这次放行不是同一层」。要让这两层合一，需要在 `run_payload` 的审批段
  开一个注入口 —— 那是既有文件，未动。
- **证据进了库，但没有一条政策规则会因为「有没有图」而改变裁定。**
  `case_r4a.json` 的 AS-003 body 里写着 `requires_evidence_kinds` /
  `min_evidence_count`，而这三个键在 `maos/**/*.py` 里**零命中** —— 政策引擎只读
  `refund_ratio` / `deduct_fee`。所以交一张图和不交，结论一模一样，且不报错。
  已记 `docs/BACKLOG.md`（2026-09-02）。另注意：`scenarios/custom/ledger.json`
  的 AS-003 是「发错货全额退」，与证据无关，**和 r4a 那条同号不同规则**。
- **附件暂存与待办一样不落库**：进程重启后未认领的照片就没了（重发一张即可）。
- **`var/attachments/` 只写不删**，没有清理机制，见 BACKLOG。
- **待办不落库**，见 §1。
- **微信客服的游标**默认只在进程内存着。重启后 `sync_msg` 可能从头给，靠
  `KF_MAX_AGE`（10 分钟）兜底丢弃超龄消息 —— 不然会把三个月前的单子重跑一遍。
  要持久化就给 `WeChatKfAdapter` 传 `load_cursor` / `save_cursor`。
