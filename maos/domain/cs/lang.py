"""语种判定（p13 契约 §1.4 T173、§2 第 0 步）：一句客户原文 → ``zh`` 或 ``en``。

契约口径一句话：**有 CJK 即 zh；否则 ASCII 字母占多数即 en；否则 zh**。本模块只把这句话里
两个没说死的词定下来（见 docs/DECISIONS.md task-t173）：

* **先逐字 NFKC**：全角字母（「Ｈｉ」）按半角字母算，全角标点（「，」「？」）变成半角 ——
  用中文输入法打的一句英文（「Hi，where is my order？」）不因为一个全角逗号被判成中文。
* **「CJK」**：汉字（基本区、扩展 A–F、兼容区）、日文假名、韩文、以及「CJK 符号和标点」区
  （。、「」等 NFKC 不改写的中文标点）。出现一个就是 zh —— 只有中文的人才打得出来。
* **「占多数」的分母**：只数字母与数字（Unicode 类别 L* / N*），标点、空白、表情不计。
  ASCII 字母**严格**多于一半才算 en：「A1001」（一个字母四个数字）、纯数字、纯符号、空串都是 zh
  —— 缺省语种是中文（首发渠道是微信客服），判不出就回缺省。

纯函数、确定性、零模型：同一句话恒得同一个结果。只依赖标准库与冻结的 ``ports``。
"""

from __future__ import annotations

import unicodedata

from maos.domain.cs.ports import LANG_EN, LANG_ZH

#: 算作 CJK 的码位区间（闭区间）。
CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x11FF),    # 韩文字母
    (0x3000, 0x303F),    # CJK 符号和标点（。、「」『』〔〕……）
    (0x3040, 0x30FF),    # 平假名、片假名
    (0x3130, 0x318F),    # 韩文兼容字母
    (0x31F0, 0x31FF),    # 片假名扩展
    (0x3400, 0x4DBF),    # 汉字扩展 A
    (0x4E00, 0x9FFF),    # 汉字基本区
    (0xAC00, 0xD7AF),    # 韩文音节
    (0xF900, 0xFAFF),    # 汉字兼容区
    (0x20000, 0x2FA1F),  # 汉字扩展 B–F 与兼容补充
)


def is_cjk(ch: str) -> bool:
    """单个字符是不是 CJK（见 :data:`CJK_RANGES`）。"""
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in CJK_RANGES)


def detect_lang(text: str) -> str:
    """``zh`` / ``en``。有 CJK 即 zh；否则字母数字里 ASCII 字母严格过半即 en；否则 zh。"""
    norm = unicodedata.normalize("NFKC", text or "")
    ascii_letters = 0
    alnum = 0
    for ch in norm:
        if is_cjk(ch):
            return LANG_ZH
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "N"):
            alnum += 1
            if ch.isascii() and ch.isalpha():
                ascii_letters += 1
    if alnum and ascii_letters * 2 > alnum:
        return LANG_EN
    return LANG_ZH
