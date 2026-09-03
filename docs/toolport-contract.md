# ToolPort 契约

<!-- 本文件由 scripts/gen_docs.py 从运行时代码生成，**请勿手改**。
     改了代码就重跑 `python3 scripts/gen_docs.py`；
     `python3 scripts/gen_docs.py --check` 不一致即非零退出。 -->

工具是 Agent 唯一能碰外部世界的地方，所以声明比 Skill 更严。`ToolPort` 是九要素 dataclass（maos/tools/port.py:22，冻结契约附录 A-6），当前扫到 **11 个**已实现工具，分布在 `ap`、`investigation`、`gateway`、`git_tool`、`claim`、`sandbox` 六处。

## 九要素

| # | 字段 | 含义 | 为什么必填 |
| :-- | :-- | :-- | :-- |
| 1 | `name` | ① 名称 | 审计行按它归集；与 Identity 的 `allowed_tools` 是同一套名字 |
| 2 | `purpose` | ② 用途 | 调用方读它决定要不要调，不读实现 |
| 3 | `entry` | ③ 入口 | 真实可调用对象 —— 契约与实现不许分家 |
| 4 | `params_schema` | ④ 入参 | 入参形状；`invoke_tool` 落审计时对它取摘要 |
| 5 | `returns_schema` | ⑤ 出参 | 出参形状；决定上层能不能不看实现就接住返回 |
| 6 | `failure_modes` | ⑥ 失败形态 | **评审会逐条对**：失败被吞掉等于没有边界 |
| 7 | `security_boundary` | ⑦ 安全边界 | **评审会逐条对**：这条是「agent 干不了什么」的答案 |
| 8 | `rate_limit` | ⑧ 限流 | 空 = 未设限，也是一种明确声明，不是遗漏 |
| 9 | `owner` | ⑨ 属主 | 出事找谁；跨轨改动时的责任面 |

## 审计：调用一律走 invoke_tool()

`invoke_tool(port, params, *, store, extras)`（maos/tools/port.py:43）调 `port.entry(**params)` 并**无论成败**落一条 `ToolInvoked` event_log 行：`detail = {tool, status, duration_ms, params_digest, error}`。工具抛异常时**先落审计再原样抛出** —— 失败要被状态机接住，不能在这里吞成 `None`。

直接调 `port.entry` 就没有审计行，出事查不到是谁、什么参数、跑了多久。`params_digest` 走 sha256（maos/tools/port.py:35），落的是摘要不是明文，入参里的业务字段不进证据束。

## 已实现工具契约

### `bank.pay`

声明：`maos/tools/ap.py:344`（`BANK_PAY_PORT`）　入口实现：`maos/tools/ap.py:335`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `name` | ① 名称 | bank.pay |
| `purpose` | ② 用途 | 向银行发出一条付款指令；返回受理回单，**永远不是终态** |
| `entry` | ③ 入口 | `maos.tools.ap._pay` |
| `params_schema` | ④ 入参 | `bank`: BankPort（进程内按名取到的银行实例，见 skills/builtin/ap/_common.py）<br>`instruction`: PaymentInstruction（金额为字符串，付款方式取 UNCL4461） |
| `returns_schema` | ⑤ 出参 | `instruction_id`: str（银行侧指令 id，query 用它）<br>`idempotency_key`: str<br>`status`: accepted —— 受理态，永不为 settled/failed<br>`is_terminal`: bool（恒 False）<br>`amount`: str<br>`currency`: str<br>`payment_means_code`: str（UNCL4461）<br>`payment_means_name`: str（码表里的官方名称）<br>`poll_count`: int（恒 0，受理不算一次观察） |
| `failure_modes` | ⑥ 失败形态 | · 幂等键为空 -> ValueError：没有幂等键就挡不住第二笔付款<br>· 同一幂等键上参数不一致 -> DuplicateInstruction，**不静默收下也不静默丢弃**<br>· 付款方式码不在 UNCL4461 内 -> KeyError（PaymentInstruction 构造时即抛）<br>· 银行不可达 / 超时 -> 由适配器抛，经 invoke_tool 落审计后原样上抛，上层按「未知外部状态」处置，**不许推断成失败** |
| `security_boundary` | ⑦ 安全边界 | 只发指令，不判成败：本 port 的返回值永远不是终态，任何据此写 settled 的代码都会被 maos/domain/ap/guard.py 抛回来（铁律 8）。金额一律字符串，不进浮点。幂等键由 (tenant, invoice) 唯一确定，一张发票只允许有一笔付款指令 |
| `rate_limit` | ⑧ 限流 | （未设限） |
| `owner` | ⑨ 属主 | ap_treasury |

### `bank.query`

声明：`maos/tools/ap.py:379`（`BANK_QUERY_PORT`）　入口实现：`maos/tools/ap.py:340`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `name` | ① 名称 | bank.query |
| `purpose` | ② 用途 | 问一次银行回单 —— 应付账款域**唯一**能取得付款终态的途径 |
| `entry` | ③ 入口 | `maos.tools.ap._query` |
| `params_schema` | ④ 入参 | `bank`: BankPort（进程内按名取到的银行实例）<br>`instruction_id`: str（bank.pay 返回的银行侧指令 id） |
| `returns_schema` | ⑤ 出参 | `status`: accepted\|pending\|unknown\|settled\|failed<br>`is_terminal`: bool（只有 settled / failed 为 True）<br>`poll_count`: int（问了几次 —— 终态是问出来的证据）<br>`bank_reference`: str（仅 settled 才有：银行流水号，钱确实走了的外部凭据）<br>`value_date`: str（仅终态才有：起息日）<br>`payment_means_code`: str（UNCL4461） |
| `failure_modes` | ⑥ 失败形态 | · 指令 id 不存在 -> LookupError<br>· 轮询到顶仍非终态 -> **如实返回非终态回单**，不许改判成失败：「我问累了」和「银行说没付成」是两回事<br>· status=unknown -> 该笔**可能已经划出**，不许重发指令，只能继续问或转人工 |
| `security_boundary` | ⑦ 安全边界 | 只读。本 port 是 ap.observe 取得权威事实的唯一入口，而 ap.observe 是全系统唯一写得进 settled 的 actor（maos/domain/ap/guard.py）。非终态回单一律不推进业务状态 |
| `rate_limit` | ⑧ 限流 | （未设限） |
| `owner` | ⑨ 属主 | ap_treasury |

### `clearing.cancel`

声明：`maos/tools/investigation.py:494`（`CLEARING_CANCEL_PORT`）　入口实现：`maos/tools/investigation.py:474`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `name` | ① 名称 | clearing.cancel |
| `purpose` | ② 用途 | 向清算方发出 camt.056 撤销请求；返回受理回执，**不返回决议**（决议须经 clearing.resolution 问询，资金证据更须等 pacs.004） |
| `entry` | ③ 入口 | `maos.tools.investigation.clearing_cancel` |
| `params_schema` | ④ 入参 | `clearing`: ClearingHousePort<br>`original_msg_id`: str<br>`end_to_end_id`: str<br>`amount`: str（金额不进浮点）<br>`currency`: str<br>`reason_code`: str（ExternalCancellationReason1Code）<br>`idempotency_key`: str（camt.056 的 Assgnmt/Id）<br>`case_id`: str（可选） |
| `returns_schema` | ⑤ 出参 | `request_id`: str<br>`message_type`: camt.056.001.08（受理，非决议）<br>`resolution`: pending（发出去的那一刻不可能有结论）<br>`funds_settled`: bool（恒 False）<br>`request_resolved`: bool（恒 False）<br>`is_terminal`: bool（恒 False）<br>`source`: str（码表出处） |
| `failure_modes` | ⑥ 失败形态 | · ValueError: 缺 idempotency_key（对应 camt.056 的 Assgnmt/Id）<br>· UnknownCodeError: reason_code 不在 ExternalCancellationReason1Code 里 ——**不许兜底**，编造的原因码发出去就是一份不合规报文<br>· DiscordantCancellationRequest: 同指派号参数不一致；不许发第二份 camt.056，清算方会把它当成第二个 case，而资金只有一笔<br>· NotImplementedError: 用了 SwiftNetworkAdapter 而清算网络未接通 |
| `security_boundary` | ⑦ 安全边界 | MAOS 不持有撤销与资金的权威事实（铁律 8），本工具只产生**观察记录**：send 永不返回决议，决议一律经 clearing.resolution 取得；同一 idempotency_key 不发第二份 camt.056；原因码取自 iso20022_codes.json 的已核对官方表，未知码抛 UnknownCodeError 不兜底 |
| `rate_limit` | ⑧ 限流 | （未设限） |
| `owner` | ⑨ 属主 | task-t38 |

### `clearing.resolution`

声明：`maos/tools/investigation.py:527`（`CLEARING_RESOLUTION_PORT`）　入口实现：`maos/tools/investigation.py:489`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `name` | ① 名称 | clearing.resolution |
| `purpose` | ② 用途 | 问询清算方对撤销请求的决议（camt.029）与资金退回（pacs.004）——本域终态的唯一合法来源 |
| `entry` | ③ 入口 | `maos.tools.investigation.clearing_resolution` |
| `params_schema` | ④ 入参 | `clearing`: ClearingHousePort<br>`request_id`: str |
| `returns_schema` | ⑤ 出参 | `message_type`: camt.029.001.08 \| pacs.004.001.09<br>`confirmation_code`: str（camt.029 的 ExternalInvestigationExecutionConfirmation1Code）<br>`rejection_code`: str（否定决议时的 ExternalPaymentCancellationRejection1Code）<br>`return_reason_code`: str（pacs.004 的 ExternalReturnReason1Code）<br>`returned_amount`: str（只有 pacs.004 才有）<br>`resolution`: confirmed\|rejected\|pending\|partial\|other（**撤销请求**的下落）<br>`request_resolved`: bool（请求有结论了吗）<br>`funds_settled`: bool（钱回来了吗 —— **只有 pacs.004 为 True**）<br>`poll_count`: int（问过几次，证明结论是问出来的）<br>`is_terminal`: bool（funds_settled 或明确被拒；**CNCL 不算**） |
| `failure_modes` | ⑥ 失败形态 | · KeyError: 未知 request_id<br>· resolution=pending: 清算方还没给结论，继续问，**不许当成失败**<br>· confirmation_code=CNCL 而 funds_settled=False: 清算方说撤销成功了，但资金退回报文（pacs.004）还没到 —— **这一档最危险**，把它当成业务成功就是把外部状态写死为终态（铁律 8）<br>· confirmation_code=RJCR: 撤销请求被拒，随附 rejection_code，终态，转人工或补偿<br>· NotImplementedError: 用了 SwiftNetworkAdapter 而清算网络未接通 |
| `security_boundary` | ⑦ 安全边界 | 只读观察，不改变清算方任何状态；问询次数落在回执的 poll_count 上，审计可证明结论来自观察而非本地推断；funds_settled 是对 message_type 的判定（只有 pacs.004 为真），**不是构造入参** —— 杜绝在调用处手填一个 True |
| `rate_limit` | ⑧ 限流 | （未设限） |
| `owner` | ⑨ 属主 | task-t38 |

### `gateway.query`

声明：`maos/tools/gateway.py:395`（`GATEWAY_QUERY_PORT`）　入口实现：`maos/tools/gateway.py:362`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `name` | ① 名称 | gateway.query |
| `purpose` | ② 用途 | 查询一笔退款在支付网关侧的当前状态 —— 终态的唯一合法来源 |
| `entry` | ③ 入口 | `maos.tools.gateway.gateway_query` |
| `params_schema` | ④ 入参 | `gateway`: GatewayPort<br>`request_id`: str |
| `returns_schema` | ⑤ 出参 | `status`: processing\|unknown\|settled\|failed<br>`poll_count`: int（问过几次，证明终态是问出来的）<br>`outcome`: success\|failed\|unknown<br>`is_terminal`: bool |
| `failure_modes` | ⑥ 失败形态 | · KeyError: 未知 request_id<br>· status 仍为 processing/unknown: 还没到终态，继续轮询，**不许当成失败**<br>· NotImplementedError: 用了 AlipaySandboxAdapter 而沙箱未接通 |
| `security_boundary` | ⑦ 安全边界 | 只读观察，不改变网关侧任何状态；轮询次数落在回执的 poll_count 上，审计可证明终态来自观察而非本地推断 |
| `rate_limit` | ⑧ 限流 | （未设限） |
| `owner` | ⑨ 属主 | task-r3 |

### `gateway.refund`

声明：`maos/tools/gateway.py:367`（`GATEWAY_REFUND_PORT`）　入口实现：`maos/tools/gateway.py:350`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `name` | ① 名称 | gateway.refund |
| `purpose` | ② 用途 | 向支付网关发起退款；返回受理回执，**不返回终态**（终态须经 gateway.query 观察） |
| `entry` | ③ 入口 | `maos.tools.gateway.gateway_refund` |
| `params_schema` | ④ 入参 | `gateway`: GatewayPort<br>`out_trade_no`: str<br>`refund_amount`: str（金额不进浮点）<br>`idempotency_key`: str<br>`reason`: str（可选） |
| `returns_schema` | ⑤ 出参 | `request_id`: str<br>`status`: processing\|unknown（非终态）<br>`code`: str<br>`retriable`: bool<br>`outcome`: success\|failed\|unknown<br>`remedy`: str<br>`source`: str（错误码出处）<br>`is_terminal`: bool |
| `failure_modes` | ⑥ 失败形态 | · ValueError: 缺 idempotency_key（对应支付宝 out_request_no）<br>· status=unknown: 网关说不清结果（ACQ.SYSTEM_ERROR / code 20000）——**不许在本地推断成败**，必须 gateway.query<br>· status=failed: 明确失败（ACQ.TRADE_NOT_EXIST / ACQ.SELLER_BALANCE_NOT_ENOUGH 等）<br>· code=ACQ.DISCORDANT_REPEAT_REQUEST: 同幂等键参数不一致，前一笔下落未知<br>· NotImplementedError: 用了 AlipaySandboxAdapter 而沙箱未接通 |
| `security_boundary` | ⑦ 安全边界 | MAOS 不持有退款的权威事实（铁律 8），本工具只产生**观察记录**：refund 永不返回终态，终态一律经 query 取得；同一 idempotency_key 不产生第二笔退款；错误码判据全部取自 gateway_codes 的已核对官方表，未知码抛 KeyError 不兜底 |
| `rate_limit` | ⑧ 限流 | （未设限） |
| `owner` | ⑨ 属主 | task-r3 |

### `git-mcp`

声明：`maos/tools/mcp/git_tool.py:66`（`GIT_MCP_PORT`）　入口实现：`maos/tools/mcp/git_tool.py:48`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `name` | ① 名称 | git-mcp |
| `purpose` | ② 用途 | 经 MCP（stdio / JSON-RPC 2.0）做只读 git 查询：仓库基线、文件清单、单文件内容 |
| `entry` | ③ 入口 | `maos.tools.mcp.git_tool.git_mcp` |
| `params_schema` | ④ 入参 | `op`: str（baseline / ls_files / show_file）<br>`root`: str（仓库根，同时是路径关押边界；相对路径按仓库根解析，不按 CWD）<br>`path`: str（仅 show_file：相对 root 的路径）<br>`prefix`: str（仅 ls_files：路径前缀过滤，可空） |
| `returns_schema` | ⑤ 出参 | `baseline`: {repo_root:str, repo_name:str, head:str, head_short:str, branch:str, dirty:bool, dirty_count:int, tracked_count:int}<br>`ls_files`: {files:list[str], count:int}<br>`show_file`: {path:str, content:str, bytes:int, truncated:bool} |
| `failure_modes` | ⑥ 失败形态 | · McpError: 拉起 server 失败 / 对端提前退出（stderr 尾三行随异常一起带出）<br>· McpError: 握手协议版本不一致 —— 停，不猜，不按任一版继续跑<br>· McpError: 等待响应超时（MAOS_MCP_TIMEOUT，默认 15s），子进程已被杀掉，不留孤儿<br>· McpError: 工具级失败（root 不是 git 仓 / 文件不在 HEAD 里 / 路径越出 root）<br>· ValueError: op 不在 OPS 里 —— 调用点写错了，不是对端的问题<br>· 以上全部原样抛出，不降级回本地 git：悄悄降级会让「这一步走没走 MCP」在证据里查不出来 |
| `security_boundary` | ⑦ 安全边界 | ① 全部工具只读：不 commit / 不 apply / 不 checkout，写操作归沙箱，两处都能改仓库会让「谁改的」失去唯一答案；② 路径按 --root 关押，show_file 的 path 先 resolve 再用 Path.relative_to 判定（不用 startswith，后者会把 /w-evil 判成 /w 的子路径）；③ 不打网络：只 fork git 子进程跑本地查询子命令，不跑 fetch/push/clone；④ 子进程 env 按白名单重建，只放行 PATH/LANG + 自算的 PYTHONPATH，按名放行而非按名拦截，新增 *_TOKEN 变量不需要有人记得来加拦截；⑤ 单帧上限 64KiB，超出显式标 truncated —— 静默截断等于伪造文件内容 |
| `rate_limit` | ⑧ 限流 | （未设限） |
| `owner` | ⑨ 属主 | task-mcp |

### `payer.query`

声明：`maos/tools/claim.py:462`（`PAYER_QUERY_PORT`）　入口实现：`maos/tools/claim.py:421`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `name` | ① 名称 | payer.query |
| `purpose` | ② 用途 | 查询一笔赔付在赔付方侧的当前状态 —— paid 这个终态的唯一合法来源 |
| `entry` | ③ 入口 | `maos.tools.claim.payer_query` |
| `params_schema` | ④ 入参 | `payer`: PayerPort<br>`request_id`: str |
| `returns_schema` | ⑤ 出参 | `status`: processing\|unknown\|paid\|denied<br>`poll_count`: int（问过几次，证明终态是问出来的）<br>`carc_code`: str<br>`group_code`: str<br>`is_terminal`: bool |
| `failure_modes` | ⑥ 失败形态 | · KeyError: 未知 request_id<br>· status 仍为 processing/unknown: 还没到终态，继续轮询，**不许当成拒付**<br>· NotImplementedError: 用了 RealPayerAdapter 而真实赔付方未接通 |
| `security_boundary` | ⑦ 安全边界 | 只读观察，不改变赔付方侧任何状态；轮询次数落在回执的 poll_count 上，审计可证明终态来自观察而非本地推断 |
| `rate_limit` | ⑧ 限流 | （未设限） |
| `owner` | ⑨ 属主 | task-T37 |

### `payer.submit`

声明：`maos/tools/claim.py:426`（`PAYER_SUBMIT_PORT`）　入口实现：`maos/tools/claim.py:409`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `name` | ① 名称 | payer.submit |
| `purpose` | ② 用途 | 向赔付方发起赔付指令；返回受理回执，**不返回 paid**（到账须经 payer.query 观察） |
| `entry` | ③ 入口 | `maos.tools.claim.payer_submit` |
| `params_schema` | ④ 入参 | `payer`: PayerPort<br>`claim_ref`: str<br>`amount`: str（金额不进浮点）<br>`idempotency_key`: str<br>`payee`: str（可选，收款方；属幂等比对面）<br>`memo`: str（可选） |
| `returns_schema` | ⑤ 出参 | `request_id`: str<br>`status`: processing\|unknown\|denied（**不含 paid**）<br>`carc_code`: str（X12 CARC，到账/在途时为空）<br>`group_code`: str（CO\|PR\|OA\|PI，这笔调整由谁承担）<br>`remark_codes`: list[str]（RARC，16/96/252 强制要求至少一条）<br>`effect`: denied\|reduced\|patient_share（MAOS 侧口径）<br>`recourse`: none\|resubmit_after_fix\|route_other_payer\|human_appeal<br>`source`: str（码表出处 URL）<br>`fetched_at`: str（码表核对日期）<br>`is_terminal`: bool |
| `failure_modes` | ⑥ 失败形态 | · ValueError: 缺 idempotency_key<br>· DuplicateClaimPayment: 同幂等键但金额/案件号/收款方不一致 —— 不静默收下<br>· ValueError: CARC 16/96/252 的回执缺 RARC（X12 原文要求至少一条）<br>· status=unknown: 赔付方说不清结果 —— **不许在本地推断成败**，必须 payer.query<br>· status=denied: 明确拒付，回执带 CARC + Group Code（如 96 Non-covered charge(s)）<br>· KeyError: 未知 CARC / 未知 Group Code（码表不兜底，见 claim_codes.lookup）<br>· NotImplementedError: 用了 RealPayerAdapter 而真实赔付方未接通 |
| `security_boundary` | ⑦ 安全边界 | MAOS 不持有赔付的权威事实（铁律 8），本工具只产生**观察记录**：submit 永不返回 paid，到账一律经 payer.query 取得；同一 idempotency_key 不产生第二笔赔付；码值判据全部取自 claim_codes 的已核对 X12 官方表，未知码抛 KeyError 不兜底；回执挂在 artifact 的 payer_receipt 键上，不占用 receipt —— 那个键归第七道闸的支付宝码表，两张码表不许混查 |
| `rate_limit` | ⑧ 限流 | （未设限） |
| `owner` | ⑨ 属主 | task-T37 |

### `sandbox.git_apply`

声明：`maos/tools/sandbox.py:723`（`GIT_APPLY_PORT`）　入口实现：`maos/tools/sandbox.py:439`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `name` | ① 名称 | sandbox.git_apply |
| `purpose` | ② 用途 | 在沙箱工作目录内应用或回滚补丁集，落盘前完成三重路径校验 |
| `entry` | ③ 入口 | `maos.tools.sandbox.sandbox_git_apply` |
| `params_schema` | ④ 入参 | `patch_set`: dict<br>`workdir`: str<br>`reverse`: bool（keyword-only）<br>`check_only`: bool（keyword-only） |
| `returns_schema` | ⑤ 出参 | `ok`: bool<br>`error`: {stage,path,hunk,message} \| None |
| `failure_modes` | ⑥ 失败形态 | · validate: 补丁集为空或 files 项缺 path/diff<br>· prepare: workdir 不存在或不是目录<br>· path_check: 触碰 infra/.github/secrets/tests 任一段<br>· conftest_guard: 任意层级的 conftest.py 新增或修改<br>· path_escape: 规范化后落在 workdir 之外<br>· apply: git apply 拒绝（error.hunk 带 git 报的行号） |
| `security_boundary` | ⑦ 安全边界 | 补丁只落在传入的 workdir 内；声明路径与 diff 正文里的路径都要过三条校验（受保护目录分段相等 / conftest.py 任意层级禁改 / workdir 内含性），任一条不过即拒，不重试、不降级 |
| `rate_limit` | ⑧ 限流 | （未设限） |
| `owner` | ⑨ 属主 | task-b |

### `sandbox.pytest_run`

声明：`maos/tools/sandbox.py:747`（`PYTEST_RUN_PORT`）　入口实现：`maos/tools/sandbox.py:636`

| 要素 | 含义 | 值 |
| :-- | :-- | :-- |
| `name` | ① 名称 | sandbox.pytest_run |
| `purpose` | ② 用途 | 在容器沙箱里跑 workdir 的测试，产出结构化 test_report |
| `entry` | ③ 入口 | `maos.tools.sandbox.sandbox_pytest_run` |
| `params_schema` | ④ 入参 | `workdir`: str |
| `returns_schema` | ⑤ 出参 | `passed`: int<br>`failed`: int<br>`errors`: int<br>`cases`: list[{id,status,msg}]<br>`duration`: float<br>`tool_error`: str \| None<br>`summary`: str<br>`sandbox_mode`: container \| subprocess \| not-run<br>`degraded_reason`: str \| None |
| `failure_modes` | ⑥ 失败形态 | · tool_error: workdir 不可用 / docker run 起不来 / 超时（容器已强制清除）<br>· tool_error: pytest 退出码 ≥2（中断、内部错、用法错、零用例收集）<br>· tool_error: 没产出 junit 报告或报告解析失败<br>· failed>0: 用例真的挂了 —— 这不是工具失败，Gate 逐条转 findings<br>· sandbox_mode=subprocess: 跑成了，但容器隔离本次未生效（degraded_reason 说明原因） —— 这不是失败，是一份可信度更低的通过 |
| `security_boundary` | ⑦ 安全边界 | 主路径容器：--network none --read-only --user 1000:1000 --memory 512m --cpus 1 --pids-limit 128，不继承宿主 env；降级路径裸 subprocess，env 按白名单重建（只放行 PATH/LANG，HOME 指向一次性空目录）；超时由宿主侧 MAOS_SANDBOX_TIMEOUT（默认 300s）兜底并 docker rm -f 清场 |
| `rate_limit` | ⑧ 限流 | （未设限） |
| `owner` | ⑨ 属主 | task-b |

## 迁移到 MCP

**迁移到 MCP = 换 entrypoint 的传输层，schema 与审计不变。**

九要素里只有 `entry` 是本地可调用对象；把它换成一个 MCP client stub（同样的 `params_schema` 入、同样的 `returns_schema` 出），其余八项一字不改。`invoke_tool` 与 `ToolInvoked` 审计行在调用点之上，不关心 entry 背后是本地函数、子进程还是一个 MCP server —— 所以迁移之后，证据束里那条审计行的形状、`scripts/verify.py` 的第 1 项校验、Identity 的 `allowed_tools` 白名单，全部原样成立。

**这句话已经不是推论了。** `git-mcp` 这个工具的 `entry` 就是一次 MCP stdio 往返（JSON-RPC 2.0，`maos/tools/mcp/`：拉起 server → `initialize` 握手 → `tools/call` → 收尸），而它落进 `event_log` 的 `ToolInvoked` 行与本地工具的**逐字段同形**。

可当场核验：

```bash
python3 -m maos.tools.mcp.server --root scenarios/fixture-repo  # 手工起 server
python3 -m pytest maos/tests/test_mcp_transport.py maos/tests/test_mcp_git_tool.py -q
```

其余 10 个工具的 `entry` 仍是进程内函数 —— **这是刻意的，不是没来得及**：`sandbox.*` 的隔离论证（容器 `--network none --read-only`）独立成立，换传输层要重新论证一遍等价性而收益为零；`gateway.*` 则把 `GatewayPort` 活对象当参数传，跨进程前必须先重构成「server 侧持有 gateway」。两条都记在 `docs/BACKLOG.md`。
