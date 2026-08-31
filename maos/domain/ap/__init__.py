"""应付账款（Accounts Payable）业务对象层。

纯新增目录，`maos/contracts/**` 与 `maos/core/**` 一个字节没动 —— 这正是
`docs/domain-portability.md` §4 那份「换一个新域要做什么」清单第 1 条的样子。

  · `schema.sql`  13 张业务表 + 1 张迁移记账表，全部新增，不碰既有表
  · `objects.py`  读写口径与三单读取；对 `ap_case` 的写入一律拒绝
  · `guard.py`    权威终态守卫：全系统只有 `ap.observe` 写得进 `settled`
"""

from __future__ import annotations
