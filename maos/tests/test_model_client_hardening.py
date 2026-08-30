"""T30 回归 —— ``HigressModelClient`` 的 key 两道防线，与 ``GatewayModelClient`` 同口径。

`docs/BACKLOG.md` 2026-08-28 那条记的是：占位类把 key 放在**公开**属性
``self.api_key``、且没有 ``__repr__`` 兜底，与同文件 ``GatewayModelClient`` 的
``_api_key`` + 不含 key 的 ``__repr__`` 不一致。

一句话说清这些断言在买什么：**改之前 repr 里也查不到 key** —— 默认的
``object.__repr__`` 只打类名和内存地址，属性一个都不打。真正的泄漏面是两条：

  · 公开属性本身：``vars(c)`` / ``c.__dict__`` / 任何遍历属性的序列化都带出值；
  · 「将来」：谁给这个类加一个 ``@dataclass`` 或自己的 ``__repr__``，key 当场进
    repr、进 pytest 的对象打印、进 traceback。

所以下面钉的是**不变量**而不是止血。测试写成两个类**并排**跑同一组断言
（``CLIENTS``），是因为这条纪律的价值就在「同文件两个客户端不许有两套口径」——
只测一个类，下一个新客户端照样会漏掉。

范式抄 ``test_gateway.py::test_sandbox_adapter_never_leaks_private_key_in_repr``
（`assert not hasattr(..., "private_key")`），仓库里已有同口径的一条。
"""

from __future__ import annotations

import pytest

from maos.model.client import GatewayModelClient, HigressModelClient, ModelClient

# 一眼能认出来的哨兵。真出现在输出里，grep 得到。
CANARY = "sk-LEAK-CANARY-0123456789"

CLIENTS = [
    pytest.param(
        lambda: HigressModelClient(base_url="https://gw.example/v1", api_key=CANARY),
        id="higress",
    ),
    pytest.param(
        lambda: GatewayModelClient(base_url="https://gw.example/v1", api_key=CANARY,
                                   model="qwen-max"),
        id="gateway",
    ),
]


@pytest.mark.parametrize("make", CLIENTS)
def test_api_key_not_a_public_attribute(make):
    """key 挂在私有属性上 —— 公开的 ``api_key`` 一旦存在，``vars()`` 就把值带出去。"""
    client = make()
    assert not hasattr(client, "api_key"), "key 应挂在私有属性 _api_key 上"
    assert client._api_key == CANARY, "私有属性得真的存着 key，不然只是把它弄丢了"
    public = [name for name in vars(client) if not name.startswith("_")]
    assert CANARY not in str([getattr(client, n) for n in public]), \
        f"key 从公开属性 {public} 里漏出来了"


@pytest.mark.parametrize("make", CLIENTS)
def test_repr_never_contains_key(make):
    """repr 里查不到 key。改前就成立，这里是把它钉住，防将来漂移。"""
    client = make()
    assert CANARY not in repr(client)
    assert "sk-" not in repr(client)


@pytest.mark.parametrize("make", CLIENTS)
def test_repr_is_explicit_not_default_object_repr(make):
    """repr 必须是自己写的那一个。

    这条才是上一条的牙齿：默认 ``object.__repr__`` 天然不含 key，所以「repr 里
    没有 key」在**没写 ``__repr__``** 时也恒真 —— 单靠上一条，防线被删掉了也照样绿。
    这里断言 repr 是显式格式（类名 + base_url、无内存地址），删掉 ``__repr__``
    会当场变红。
    """
    client = make()
    text = repr(client)
    assert text.startswith(type(client).__name__ + "("), \
        f"repr 退化成默认 object repr：{text}"
    assert "object at 0x" not in text
    assert "https://gw.example/v1" in text, "base_url 该留在 repr 里 —— 它是排错要的"


@pytest.mark.parametrize("make", CLIENTS)
def test_str_and_format_also_clean(make):
    """``str()`` 与 f-string 走的也是同一个 ``__repr__``（两个类都没定义 ``__str__``）。

    单测 ``repr()`` 不够：日志里最常见的写法是 ``f"{client}"`` 和 ``"%s" % client``，
    它们走 ``__str__``；``__str__`` 缺省回落到 ``__repr__``，但那是**缺省**，
    哪天有人补一个 ``__str__`` 就绕开了上面两条。
    """
    client = make()
    assert CANARY not in str(client)
    assert CANARY not in f"{client}"
    assert CANARY not in "{}".format(client)      # noqa: UP032 —— 显式覆盖第三条路径


def test_higress_is_still_a_placeholder():
    """加固不许顺手把占位类实现掉 —— 本轮明确不接 Higress（派单 §0.2）。"""
    client = HigressModelClient(base_url="https://gw.example/v1", api_key=CANARY)
    assert isinstance(client, ModelClient)
    with pytest.raises(NotImplementedError):
        client.complete(system="s", user="u", tier="strong")


def test_higress_error_text_has_no_key():
    """``NotImplementedError`` 的文本里也不许夹带 key —— 它会进 traceback。"""
    client = HigressModelClient(base_url="https://gw.example/v1", api_key=CANARY)
    with pytest.raises(NotImplementedError) as exc:
        client.complete(system="s", user="u", tier="strong")
    assert CANARY not in str(exc.value)
