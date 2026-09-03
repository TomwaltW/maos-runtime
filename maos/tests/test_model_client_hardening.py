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

import logging

import pytest

from maos.model.client import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_MODEL,
    GatewayModelClient,
    HigressModelClient,
    ModelClient,
    ScriptedModelClient,
    select_model_client,
)

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


# ---------------------------------------------------------------------------
# T47 · 静默降级要留痕（真模型 -> Scripted）
#
# 上面几条守的是「key 不许漏出去」。这几条守的是另一件事：**降级不许安静**。
#
# ``select_model_client`` 三个环境变量缺任一个就回落 ScriptedModelClient，原先只有
# 一条 ``log.info``。降级本身是对的（缺 key 时不该去打网络），坏的是它没有声音 ——
# 「以为在跑真模型、其实在跑假模型」不会有任何显眼提示，而这一跑的成本读数、
# 延迟、模型行为结论全部作废。复赛现场 key 配错，「真模型跑通了」就是假结论。
#
# 所以两条判据分开钉：
#   ① 级别是 WARNING，且正文说得出**后果**（不是只说「降级了」）；
#   ② 正文里不许出现任何变量的**值**（铁律 6）—— 这条是机器判据，比人眼复核可靠。
# ---------------------------------------------------------------------------
ENV_TRIPLE = (ENV_BASE_URL, ENV_API_KEY, ENV_MODEL)

# 后果那半句必须点到的东西。措辞可以改，「读完知道哪些结论作废」这件事不许丢。
_CONSEQUENCE_WORDS = ("不成立", "成本", "脚本回放")


def _all_missing(monkeypatch):
    for name in ENV_TRIPLE:
        monkeypatch.delenv(name, raising=False)


def test_degradation_is_a_warning_not_an_info(monkeypatch, caplog):
    """三个变量都不配时，降级必须是 WARNING。

    INFO 在演示现场是看不见的 —— ``run.py`` 一屏几十行 INFO，多一条不多。真正要防的
    不是「没记录」，是「记录了但没人会注意到」。
    """
    _all_missing(monkeypatch)
    with caplog.at_level(logging.INFO, logger="maos.model"):
        client = select_model_client()

    assert isinstance(client, ScriptedModelClient), "缺变量还去构造真客户端就是打网络"
    degraded = [r for r in caplog.records if "ScriptedModelClient" in r.getMessage()]
    assert len(degraded) == 1, [r.getMessage() for r in caplog.records]
    assert degraded[0].levelno == logging.WARNING, \
        f"降级记在了 {degraded[0].levelname}，INFO 级的降级等于没说"


def test_degradation_warning_spells_out_the_consequences(monkeypatch, caplog):
    """正文得让人读完知道**这一跑的哪些结论作废**，不是只说「降级了」。

    只说「降级了」，读日志的人还要自己推后果；写明后果，他一眼知道接下来哪些
    结论不能信。这条判的就是那半句在不在。
    """
    _all_missing(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="maos.model"):
        select_model_client()

    text = caplog.text
    for word in _CONSEQUENCE_WORDS:
        assert word in text, f"降级告警没说到「{word}」这层后果：{text}"
    for name in ENV_TRIPLE:
        assert name in text, f"没点名缺的是哪个变量（{name}），现场没法照着补：{text}"


@pytest.mark.parametrize("absent", ENV_TRIPLE)
def test_degradation_warning_never_echoes_any_value(monkeypatch, caplog, absent):
    """铁律 6 的机器判据：告警里只许出现**变量名**，一个值都不许有。

    改的正是那条打日志的路径，而 ``env`` 这个 dict 就在手边 —— 把它顺手带进
    格式化参数是最容易的手滑，且手滑之后 base_url 与 key 会一起进日志、进证据。
    所以三个变量各设一个一眼能认的哨兵，逐个缺一个跑一遍，断言哨兵不在文本里。
    """
    sentinels = {name: f"{CANARY}-{name}" for name in ENV_TRIPLE}
    for name, value in sentinels.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(absent, raising=False)

    with caplog.at_level(logging.WARNING, logger="maos.model"):
        client = select_model_client()

    assert isinstance(client, ScriptedModelClient), "缺一个也得降级，不许拿两个去凑"
    text = caplog.text
    assert absent in text, "缺的那个变量名要点出来"
    for name, value in sentinels.items():
        if name == absent:
            continue
        assert value not in text, f"{name} 的**值**漏进了日志：{text}"
    assert CANARY not in text
