"""身份核验的缺省实现 —— 按绑定表精确匹配「这位客户能不能查这一单」（p13 契约 §1.4 T171）。

``BindingVerifier`` 实现 ``ports.IdentityVerifier``：拿 (租户, 渠道, 客户, 客户打出来的单号)
去 ``cs_order_binding`` 里找一行，**找到才放行**，找不到一律 ``None``（失败即关，前台不查单、
转人工）。绑定是**授权**不是订单事实（铁律 8）：它只回答「能不能查」，不回答「单是什么状态」。

单号比较前只做两件规范化：去首尾空白、全角 ASCII 转半角（客户在中文输入法下打出
``Ａ１００１`` 是常态）。**不做**大小写折叠、不去中间空白、不做 NFKC —— 多做一步就是多放行
一类「长得像」的单号，而这道闸的方向只能是宁可不认。写入侧（``records.upsert_binding`` /
``load_bindings_file``）用同一个函数规范化后再存，两边口径一致。

本模块只读，不落 event_log（契约 §2' R5：订单号、query_key 不进审计行）。
"""

from __future__ import annotations

from typing import Any

from maos.domain.cs import objects
from maos.domain.cs.ports import Binding

#: 全角 ASCII 区（``！`` U+FF01 到 ``～`` U+FF5E）与半角 ``!``..``~`` 的码位差。
_FULLWIDTH_FIRST = 0xFF01
_FULLWIDTH_LAST = 0xFF5E
_FULLWIDTH_OFFSET = 0xFEE0
#: 全角空格（U+3000）。
_IDEOGRAPHIC_SPACE = 0x3000


def normalize_display_no(text: str) -> str:
    """客户看得到的单号的规范形：全角 ASCII 转半角（全角空格转半角空格），再去首尾空白。"""
    out: list[str] = []
    for ch in text or "":
        code = ord(ch)
        if _FULLWIDTH_FIRST <= code <= _FULLWIDTH_LAST:
            out.append(chr(code - _FULLWIDTH_OFFSET))
        elif code == _IDEOGRAPHIC_SPACE:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out).strip()


_SELECT_BINDING = (
    "SELECT tenant_id, channel, external_userid, display_no, system_name, query_key, source,"
    " bound_at FROM cs_order_binding"
    " WHERE tenant_id=? AND channel=? AND external_userid=? AND display_no=?"
)


def _binding_from_row(row: dict) -> Binding:
    return Binding(tenant_id=str(row["tenant_id"]), channel=str(row["channel"]),
                   external_userid=str(row["external_userid"]),
                   display_no=str(row["display_no"]), system_name=str(row["system_name"]),
                   query_key=str(row["query_key"]), source=str(row["source"]),
                   bound_at=str(row["bound_at"]))


class BindingVerifier:
    """``ports.IdentityVerifier`` 的缺省实现：绑定表精确匹配，失败即关。"""

    def resolve(self, store: Any, *, tenant_id: str, channel: str, external_userid: str,
                display_no: str) -> Binding | None:
        """(租户, 渠道, 客户, 规范化单号) 四项精确命中一行才返回绑定，否则 ``None``。

        租户空串（客服账号没映射到租户）、渠道 / 客户 / 单号任一为空，一律 ``None`` ——
        不猜默认租户，也不拿空键去库里碰运气。
        """
        if not tenant_id or not channel or not external_userid:
            return None
        no = normalize_display_no(display_no)
        if not no:
            return None
        objects.ensure_schema(store)
        rows = objects.query(store, _SELECT_BINDING, (tenant_id, channel, external_userid, no))
        return _binding_from_row(rows[0]) if rows else None
