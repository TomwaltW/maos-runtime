"""Artifact 种类、形状校验与引用解析 —— 跨轨共用的唯一一份口径。

为什么集中在这里：补偿 artifact 的 patch_ref 由 D 产、由 C 的干跑闸读，
两边各写一份解析就一定会在字段名或嵌套层级上分叉，联调期才爆。
所以解析**只走** resolve_patch_ref()，校验**只走** validate_artifact()。

validate_artifact 返回**错误列表**（空 = 通过），与 maos/contracts/events.py 的
validate() 同构 —— 上层拿到的是可以直接写进 findings 的东西，不是一个异常。
"""

from __future__ import annotations

from typing import Any

KIND_PATCH_SET = "patch_set"
KIND_TEST_REPORT = "test_report"
KIND_ARCH_CONTRACT = "architecture_contract"
KIND_REVIEW_NOTE = "review_note"
KIND_COMPENSATION = "compensation"

ALL_KINDS = (
    KIND_PATCH_SET, KIND_TEST_REPORT, KIND_ARCH_CONTRACT,
    KIND_REVIEW_NOTE, KIND_COMPENSATION,
)

_PASS_FAIL = ("pass", "fail")

# 补偿模式（C-5 冻结）：本阶段只有反向应用一种，不定义第二种。
# 这是「零模型补偿」的落点 —— 逆补丁不由模型生成，只把正向补丁反着打一遍。
MODE_REVERSE = "reverse"


def _missing(content: dict, keys: tuple[str, ...]) -> list[str]:
    return [f"缺必填键 {k}" for k in keys if k not in content]


def _check_patch_set(c: dict) -> list[str]:
    errs = _missing(c, ("files", "summary", "self_check"))
    files = c.get("files")
    if not isinstance(files, list) or not files:
        errs.append("files 必须是非空 list")
    else:
        for i, f in enumerate(files):
            if not isinstance(f, dict) or "path" not in f or "diff" not in f:
                errs.append(f"files[{i}] 必须含 path 与 diff")
    check = c.get("self_check")
    if not isinstance(check, dict):
        errs.append("self_check 必须是 dict")
    else:
        for k in ("build", "lint"):
            if check.get(k) not in _PASS_FAIL:
                errs.append(f"self_check.{k} 必须是 pass|fail")
    return errs


def _check_test_report(c: dict) -> list[str]:
    """形状 = C-7 的 sandbox_pytest_run 返回值。tool_error 与 failed 不是一回事。"""
    errs = _missing(c, ("passed", "failed", "errors", "cases", "duration", "tool_error"))
    for k in ("passed", "failed", "errors"):
        if k in c and not isinstance(c[k], int):
            errs.append(f"{k} 必须是 int")
    if "duration" in c and not isinstance(c["duration"], (int, float)):
        errs.append("duration 必须是数字")
    if "tool_error" in c and not (c["tool_error"] is None or isinstance(c["tool_error"], str)):
        errs.append("tool_error 必须是 str 或 null")
    cases = c.get("cases")
    if not isinstance(cases, list):
        errs.append("cases 必须是 list")
    else:
        for i, case in enumerate(cases):
            if not isinstance(case, dict) or not {"id", "status", "msg"} <= set(case):
                errs.append(f"cases[{i}] 必须含 id/status/msg")
    return errs


def _check_compensation(c: dict) -> list[str]:
    """形状 = C-5 golden fixture，mode 取值域一并锁死。

    mode 恒为 "reverse"：放行别的值不会当场报错，而是让补偿走不到反向应用分支，
    症状是「补偿静默不执行、日志一片正常」，要到演示现场才发现文件没还原。
    """
    errs = _missing(c, ("mode", "patch_ref"))
    if "mode" in c and c["mode"] != MODE_REVERSE:
        errs.append(f"mode 必须恒为 {MODE_REVERSE!r}，本阶段不定义第二种补偿模式"
                    f"（实际: {c['mode']!r}）")
    ref = c.get("patch_ref")
    if not isinstance(ref, dict):
        errs.append("patch_ref 必须是 dict")
    else:
        errs += [f"patch_ref.{e}" for e in _missing(ref, ("task_id", "kind", "attempt"))]
        if "task_id" in ref and not isinstance(ref["task_id"], str):
            errs.append("patch_ref.task_id 必须是 str")
        if "attempt" in ref and not isinstance(ref["attempt"], int):
            errs.append("patch_ref.attempt 必须是 int")
        if ref.get("kind") not in (None, KIND_PATCH_SET):
            errs.append(f"patch_ref.kind 必须是 {KIND_PATCH_SET}")
    return errs


_CHECKERS = {
    KIND_PATCH_SET: _check_patch_set,
    KIND_TEST_REPORT: _check_test_report,
    KIND_COMPENSATION: _check_compensation,
    KIND_ARCH_CONTRACT: lambda c: _missing(
        c, ("api", "idempotency", "audit", "reversibility")),
    KIND_REVIEW_NOTE: lambda c: [],     # 形状未冻结，留给 C 轨细化
}


def validate_artifact(kind: str, content: Any) -> list[str]:
    """返回错误列表，空列表 = 通过。"""
    if kind not in _CHECKERS:
        return [f"未知 artifact kind: {kind}（合法值: {list(ALL_KINDS)}）"]
    if not isinstance(content, dict):
        return [f"{kind} 的 content 必须是 dict，实际是 {type(content).__name__}"]
    return _CHECKERS[kind](content)


def resolve_patch_ref(store: Any, ref: dict | None) -> dict | None:
    """按补偿里的 patch_ref 取回原补丁集 artifact；取不到返回 None。

    口径固定：``ref["task_id"]`` 下 kind==patch_set 且 version==``ref["attempt"]``。
    artifact 的 version 就是产出它的那一次 attempt —— 返工产生的多版补丁靠它区分。
    """
    if not isinstance(ref, dict):
        return None
    task_id = ref.get("task_id")
    if not task_id:
        return None
    attempt = ref.get("attempt")
    for art in store.list_artifacts(task_id):
        if art.get("kind") == KIND_PATCH_SET and art.get("version") == attempt:
            return art
    return None
