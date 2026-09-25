"""身份核验的缺省实现 —— 按绑定表精确匹配「这位客户能不能查这一单」（p13 契约 §1.4 T171）。

``BindingVerifier`` 实现 ``ports.IdentityVerifier``：拿 (租户, 渠道, 客户, 客户打出来的单号)
去 ``cs_order_binding`` 里找一行，**找到才放行**，找不到一律 ``None``（失败即关，前台不查单、
转人工）。绑定是**授权**不是订单事实（铁律 8）：它只回答「能不能查」，不回答「单是什么状态」。

单号比较前只做三件规范化：去首尾空白、全角 ASCII 转半角（客户在中文输入法下打出
``Ａ１００１`` 是常态）、去掉打在单号前面的**前缀**（p15 契约 §1 C7：``#`` / ``No.`` / ``NO:`` /
``№`` / ``单号：`` / ``订单号`` / ``order #`` 一类，拉丁字母不分大小写，见 :data:`_PREFIX_RE`）。
前缀只认「前缀词 + 分隔符」或中文前缀词本身 —— ``NO1001``、``ORDER123`` 这种字母直接贴着数字的
不动（那可能就是单号本身）。**不做**大小写折叠、不去中间空白、不做 NFKC —— 多做一步就是多放行
一类「长得像」的单号，而这道闸的方向只能是宁可不认。写入侧（``records.upsert_binding`` /
``load_bindings_file``）用同一个函数规范化后再存，两边口径一致；理解层抽出来的单号
（``understand.extract_order_no``）也过这个函数，所以前缀在三处是同一个口径。

本模块只读，不落 event_log（契约 §2' R5：订单号、query_key 不进审计行）。
"""

from __future__ import annotations

import re
from typing import Any

from maos.domain.cs import objects
from maos.domain.cs.ports import Binding

#: 全角 ASCII 区（``！`` U+FF01 到 ``～`` U+FF5E）与半角 ``!``..``~`` 的码位差。
_FULLWIDTH_FIRST = 0xFF01
_FULLWIDTH_LAST = 0xFF5E
_FULLWIDTH_OFFSET = 0xFEE0
#: 全角空格（U+3000）。
_IDEOGRAPHIC_SPACE = 0x3000


#: 单号前缀（p15 契约 §1 C7），在全角转半角之后的文本上从头匹配，可以叠几层（「订单号：#A1001」）：
#:
#: * 中文前缀词：订单编号 / 订单号码 / 订单号 / 订单 / 单号，后面可带冒号；
#: * ``order`` / ``order no.`` / ``order number`` / ``order id``：后面必须有分隔符（冒号、``#``、点或空白）；
#: * ``no`` 必须带 ``.`` / ``:`` / ``#``（``No.`` / ``NO:``）；``№``（U+2116）本身就是前缀；
#: * 单独的 ``#``。
_PREFIX_RE = re.compile(
    r"^(?:(?:订单编号|订单号码|订单号|订单|单号)\s*:?"
    r"|order(?:\s*(?:no\.?|number|num|id))?(?:\s*[:#.]|\s+)"
    r"|no\s*[.:#]"
    r"|№\.?"
    r"|#)\s*",
    re.IGNORECASE)


def _fold_width(text: str) -> str:
    """全角 ASCII 转半角，全角空格转半角空格。"""
    out: list[str] = []
    for ch in text or "":
        code = ord(ch)
        if _FULLWIDTH_FIRST <= code <= _FULLWIDTH_LAST:
            out.append(chr(code - _FULLWIDTH_OFFSET))
        elif code == _IDEOGRAPHIC_SPACE:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def normalize_display_no(text: str) -> str:
    """客户看得到的单号的规范形：全角 ASCII 转半角（全角空格转半角空格）、去首尾空白、去前缀。

    前缀去完只剩空串（整串就是「#」「单号：」）→ 返回空串：写入侧据此拒收，核验侧据此不认。
    """
    no = _fold_width(text).strip()
    while True:
        m = _PREFIX_RE.match(no)
        if m is None or not m.group(0):
            return no
        no = no[m.end():].strip()


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
