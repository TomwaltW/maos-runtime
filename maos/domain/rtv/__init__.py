"""采购退货退款（Return to Vendor）业务对象层。

纯新增目录，`maos/contracts/**` 与 `maos/core/**` 一个字节没动 —— 这是
`docs/domain-portability.md` §4 那份「换一个新域要做什么」清单第 1 条的样子。

  · `schema.sql`  9 张业务表 + 1 张迁移记账表，全部新增，不碰既有表
  · `objects.py`  读写口径与源单据读取；对 rtv_case 与两张权威事实表的写入一律拒绝
  · `guard.py`    权威终态守卫：全系统只有 `rtv.observe` 写得进 `credited` 与 `settled`
  · `fixtures.py` 靶场数据的唯一构造路径

## 本域与 `ap` 域的关系：镜像，且**只在数据层**相交

RTV 是 `ap` 域的镜像 —— ap 是「我付供应商」，RTV 是「供应商退我」。两者共用
`supplier` / `purchase_order` / `purchase_order_line` / `goods_receipt` /
`goods_receipt_line` 五张源单据表：**复用既有定义，一张都不重建**。

但**代码层零相交**：本包不 `import maos.domain.ap` 的任何东西。`money()` /
`attach_business_ref()` / 迁移机制这些是**照抄口径**（各写一份），不是共享实现。
理由与 `maos/domain/ap/objects.py` 抬头那段一样：抽成公共基类之后，那个基类就成了
几个域共同持有的面，动它就等于动所有域，而 `docs/domain-portability.md` §1
那张表里 `maos/domain/` 一行标的是 ❌「按域实现」。

那五张表的**建表责任在 ap 域**，本域只引用。见 `objects.require_upstream_tables()`。
"""

from __future__ import annotations
