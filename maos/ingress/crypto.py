"""回调的签名校验与解密 —— 三个平台的密码学细节全在这里，adapter 只管调。

**为什么单独一个文件**：这些是唯一「写错了也照样跑，只是不安全」的代码。
签名比对用 ``==`` 而不是常量时间比较、时间戳不校验、padding 不检查 —— 每一条都
不会让任何测试变红，也不会让消息收不到。集中到一处，才有一个地方可以逐条盯。

## 依赖

飞书**零依赖可通**：把应用后台的 Encrypt Key 留空即明文回调，签名走 sha256，
标准库就够。企业微信 / 微信客服的回调**强制加密**（AES-256-CBC），需要
``cryptography`` 或 ``pycryptodome`` 之一，缺则显式抛 :class:`ChannelDepMissing`，
不静默回落（理由见该异常的 docstring）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from typing import Callable

from maos.ingress.contracts import ChannelDepMissing, VerifyError

#: 回调时间戳与本机时钟的最大偏差（秒）。超出即拒。
#:
#: 校验它是为了让**重放**有代价：签名是对 (timestamp, nonce, body) 算的，攻击者
#: 原样重发一份抓到的合法回调，签名当然对得上。没有这道闸，一条抓到的
#: ``/refund`` 可以被无限次重放 —— 而幂等键那道闸挡的是**平台自己的重推**
#: （同一个 msg_id），换个 msg_id 重放它就不认了。两道闸挡的不是同一件事。
#:
#: 300 秒是三家文档的公约数，也压过了正常的机器时钟漂移。
MAX_CLOCK_SKEW = 300

#: 企业微信的 AES 块大小。**是 32 不是 16** —— 它对 PKCS7 做了自己的规定，
#: 按标准的 16 去补/去 padding 会在长度恰好落在边界上的消息里偶发解密失败，
#: 而绝大多数短消息看不出区别。这种「大部分时候是对的」最难查。
WECOM_BLOCK_SIZE = 32


# --------------------------------------------------------------------------
# AES：两个后端择一，都没有则显式抛
# --------------------------------------------------------------------------
def _aes_backend() -> Callable[[bytes, bytes, bytes, bool], bytes]:
    """返回 ``(key, iv, data, encrypt) -> bytes``。惰性 import，模块导入不吃依赖。

    两个后端都收：装了 ``cryptography`` 用它，否则试 ``pycryptodome``。不是为了
    「兼容性好」，是因为这两个包在不同的机器上各自可能已经躺着 —— 让联调那天
    少一次 pip install，而不是多一层抽象。
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        def _run(key: bytes, iv: bytes, data: bytes, encrypt: bool) -> bytes:
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
            op = cipher.encryptor() if encrypt else cipher.decryptor()
            return op.update(data) + op.finalize()

        return _run
    except ImportError:
        pass

    try:
        from Crypto.Cipher import AES  # type: ignore[import-not-found]

        def _run(key: bytes, iv: bytes, data: bytes, encrypt: bool) -> bytes:
            cipher = AES.new(key, AES.MODE_CBC, iv)
            return cipher.encrypt(data) if encrypt else cipher.decrypt(data)

        return _run
    except ImportError as exc:
        raise ChannelDepMissing(
            "企业微信 / 微信客服的回调是强制加密的，需要 AES："
            "python3 -m pip install 'cryptography'（或 pycryptodome）。"
            "飞书不吃这条依赖 —— 把飞书应用后台的 Encrypt Key 留空即明文回调。"
        ) from exc


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    return _aes_backend()(key, iv, data, False)


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    return _aes_backend()(key, iv, data, True)


def pkcs7_pad(data: bytes, block: int) -> bytes:
    pad = block - (len(data) % block)
    return data + bytes([pad]) * pad


def pkcs7_unpad(data: bytes, block: int) -> bytes:
    """去 padding，**并校验它合法**。

    不校验也能跑（多切掉几个字节，后面按长度取正文时正好绕过去），但那等于
    接受任意构造的尾部 —— padding oracle 一类的问题正是从「解密后不校验」开始的。
    """
    if not data:
        raise VerifyError("解密结果为空")
    pad = data[-1]
    if not 1 <= pad <= block or pad > len(data):
        raise VerifyError(f"padding 非法（{pad}），密文或 EncodingAESKey 不对")
    return data[:-pad]


# --------------------------------------------------------------------------
# 通用判据
# --------------------------------------------------------------------------
def check_timestamp(raw: str, *, now: float | None = None,
                    skew: int = MAX_CLOCK_SKEW) -> None:
    """时间戳在允许偏差内，否则抛。空串或非数字一律判失败，**不放行**。"""
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        raise VerifyError(f"时间戳不是数字：{raw!r}") from None
    drift = abs((time.time() if now is None else now) - ts)
    if drift > skew:
        raise VerifyError(
            f"时间戳偏差 {drift:.0f}s 超过 {skew}s —— 要么是重放，"
            f"要么是本机时钟没对时（先查 `date`，别急着改代码）")


def equal(a: str, b: str) -> bool:
    """常量时间比较。签名比对**必须**走这里，不许用 ``==``。

    ``==`` 在第一个不同的字节就返回，比对耗时因此泄漏「猜对了几位」。这是可测量的：
    远程攻击者用足够多的样本能把签名一位一位试出来。而两种写法功能完全一致，
    没有任何测试能把它们区分开 —— 所以只能靠盯。
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# --------------------------------------------------------------------------
# 飞书
# --------------------------------------------------------------------------
def feishu_signature(timestamp: str, nonce: str, encrypt_key: str, body: bytes) -> str:
    """飞书事件订阅 v2 的签名：``sha256(timestamp + nonce + encrypt_key + body)``。

    ``body`` 用**原始字节**，不是重新序列化的 JSON —— 重序列化会改键序和空格，
    算出来的摘要必然对不上，而错误表现是「签名一直不通过」，很容易被误判成密钥配错。
    """
    h = hashlib.sha256()
    h.update(timestamp.encode("utf-8"))
    h.update(nonce.encode("utf-8"))
    h.update(encrypt_key.encode("utf-8"))
    h.update(body)
    return h.hexdigest()


def feishu_decrypt(encrypt_key: str, payload: str) -> bytes:
    """解飞书的 ``{"encrypt": "..."}``。key = ``sha256(encrypt_key)``，IV = 密文前 16 字节。"""
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    blob = base64.b64decode(payload)
    if len(blob) <= 16:
        raise VerifyError("飞书密文过短，取不出 IV")
    return pkcs7_unpad(aes_cbc_decrypt(key, blob[:16], blob[16:]), 16)


# --------------------------------------------------------------------------
# 企业微信 / 微信客服（同一套 WXBizMsgCrypt）
# --------------------------------------------------------------------------
def wecom_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """``sha1`` of 四项**字典序排序后**拼接。排序是规范要求，不是实现细节。"""
    parts = sorted([token, timestamp, nonce, encrypt])
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


def wecom_aes_key(encoding_aes_key: str) -> bytes:
    """``EncodingAESKey`` 是 43 位 base64，补一个 ``=`` 才是合法 base64 -> 32 字节。"""
    if len(encoding_aes_key) != 43:
        raise VerifyError(
            f"EncodingAESKey 应为 43 位，实际 {len(encoding_aes_key)} 位 —— "
            "多半是复制时带了空格或漏了一位")
    key = base64.b64decode(encoding_aes_key + "=")
    if len(key) != 32:
        raise VerifyError("EncodingAESKey 解出来不是 32 字节")
    return key


def wecom_decrypt(encoding_aes_key: str, encrypt: str, *,
                  expect_receiveid: str = "") -> tuple[bytes, str]:
    """解一条企微密文，返回 ``(明文, receiveid)``。

    ``expect_receiveid`` 非空时**必须相等**，否则抛。这道校验容易被省掉（省了也能
    收到消息），但它挡的是「别家企业的合法回调被打到我这个地址」—— 那条回调签名
    是对的（用的是它自己的 token），只有 receiveid 能把它认出来。
    """
    key = wecom_aes_key(encoding_aes_key)
    blob = base64.b64decode(encrypt)
    plain = pkcs7_unpad(aes_cbc_decrypt(key, key[:16], blob), WECOM_BLOCK_SIZE)
    if len(plain) < 20:
        raise VerifyError("企微明文过短，取不出长度位")
    # 结构：16 字节随机数 + 4 字节网络序长度 + 正文 + receiveid
    (size,) = struct.unpack(">I", plain[16:20])
    if size < 0 or 20 + size > len(plain):
        raise VerifyError(f"企微明文长度位 {size} 与实际不符，密钥或密文不对")
    return plain[20:20 + size], plain[20 + size:].decode("utf-8", "replace")


def wecom_encrypt(encoding_aes_key: str, plain: bytes, receiveid: str, *,
                  nonce16: bytes | None = None) -> str:
    """按同一结构加密。仅被测试与「被动回复」路径用到。

    ``nonce16`` 只为测试可复现留口，生产恒走 ``os.urandom``。默认参数不写成
    ``os.urandom(16)`` —— 默认值在函数定义时求值一次，那会让所有消息共用同一个
    随机头，而且看起来完全正常。
    """
    import os

    head = os.urandom(16) if nonce16 is None else nonce16
    body = head + struct.pack(">I", len(plain)) + plain + receiveid.encode("utf-8")
    key = wecom_aes_key(encoding_aes_key)
    return base64.b64encode(
        aes_cbc_encrypt(key, key[:16], pkcs7_pad(body, WECOM_BLOCK_SIZE))
    ).decode("ascii")


def parse_xml_fields(body: bytes, *names: str) -> dict[str, str]:
    """从企微回调的 XML 里取指定标签。**用 stdlib 的 XML 解析器，且禁外部实体。**

    ``xml.etree`` 默认不展开外部实体，所以这里没有 XXE 面；但仍不用正则去抠 ——
    正则在 CDATA 与转义上会悄悄取错值，而取错的症状是签名对不上，查向密钥。
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(body.decode("utf-8"))
    except (ET.ParseError, UnicodeDecodeError) as exc:
        raise VerifyError(f"企微回调不是合法 XML：{exc}") from exc
    out: dict[str, str] = {}
    for name in names:
        node = root.find(name)
        if node is not None and node.text:
            out[name] = node.text.strip()
    return out
