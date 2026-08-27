"""模型调用抽象 —— 后续接 Higress AI Gateway 时只改这一个文件。

刻意做成注入式：Agent 不知道背后是通义、DeepSeek 还是 OpenAI，只知道 tier。
tier 到具体模型的映射是治理决策，属于网关的职责，不属于 Agent。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class Tier:
    STRONG = "strong"   # 强推理：Manager / Requirement / Architecture
    MEDIUM = "medium"   # 中等：Coding / Testing
    LIGHT = "light"     # 轻量：格式化、分类


@dataclass
class ModelResponse:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    meta: dict = field(default_factory=dict)


class ModelClient(ABC):
    @abstractmethod
    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse: ...


class ScriptedModelClient(ModelClient):
    """MVP 阶段用的假模型：按 (tier, 关键字) 返回预设答案。

    这样第一步验证的是「事件契约和状态机对不对」，不会被模型输出的随机性干扰。
    换真模型只需要把 main.py 里注入的实例换掉。
    """

    def __init__(self, script: dict[str, str] | None = None) -> None:
        self.script = script or {}
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        self.calls.append({"tier": tier, "system": system[:60], "user": user[:120]})
        for kw, answer in self.script.items():
            if kw in user:
                return ModelResponse(text=answer, tokens_in=len(user) // 4,
                                     tokens_out=len(answer) // 4, model=f"scripted-{tier}")
        return ModelResponse(text="{}", model=f"scripted-{tier}")


class HigressModelClient(ModelClient):
    """占位。Track B 接网关时实现：走 Higress 统一入口，tier 作为路由 header。"""

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url
        self.api_key = api_key

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        raise NotImplementedError("Track B：接入 Higress 时实现")


def select_model_client(script: dict[str, str] | None = None, *,
                        force_scripted: bool = False) -> ModelClient:
    """选择模型客户端 —— 上层唯一的构造入口，签名与语义冻结（A-12）。

    **Task-0 版恒返 ScriptedModelClient**：真模型分支由 Task-A 填，
    读这四个环境变量（只读 env，禁止写进任何文件）：
    ``MAOS_LLM_BASE_URL`` / ``MAOS_LLM_API_KEY`` / ``MAOS_LLM_MODEL`` /
    ``MAOS_LLM_TIMEOUT``（默认 120s）。异常与日志里禁止回显 api key。

    force_scripted=True 表示「无论环境如何都要确定性输出」——场景 5 与全部测试
    必须显式传它。现在两条分支还没分叉，所以它暂时不改变行为；等 Task-A 填完
    真模型分支，这些调用点一行都不用改就仍然走脚本模型。
    """
    return ScriptedModelClient(script)
