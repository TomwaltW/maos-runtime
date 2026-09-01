"""MCP（Model Context Protocol）接入层 —— 只做传输，不做语义。

本包回答的是一个很窄的问题：**ToolPort 的 `entry` 换成跨进程调用之后，
九要素、`ToolInvoked` 审计行、`allowed_tools` 白名单还成不成立。**
答案是成立，而且这里是它被真跑通的地方，不再是文档里的推论。

三个模块：

* ``protocol`` —— JSON-RPC 2.0 + 换行分帧，客户端与服务端共用一份线格式
* ``server``   —— 最小 MCP server，暴露**只读** git 查询，路径按 ``--root`` 关押
* ``git_tool`` —— ``git-mcp`` 这个 ToolPort 的九要素声明，``entry`` 走 client

刻意不做的事（写在这里，免得下一个人以为是漏了）：

1. **不接管沙箱**。``sandbox.git_apply`` / ``sandbox.pytest_run`` 的隔离论证
   （容器 ``--network none --read-only``）是独立成立的，换成 MCP 传输要重新论证
   一遍等价性，收益却是零 —— 它们本来就已经是真调用。
2. **不做写操作**。本 server 全部工具只读：不 commit、不 apply、不 checkout。
   写操作的安全边界归沙箱，两处都能改仓库会让「谁改的」失去唯一答案。
3. **不让模型自己选工具**。那要改 ``ModelClient.complete`` 的冻结契约 A-12，
   并重写所有 agent 的脚本化 ``run()`` 控制流，不在本次范围内。
"""

from maos.tools.mcp.protocol import PROTOCOL_VERSION, McpError

__all__ = ["PROTOCOL_VERSION", "McpError"]
