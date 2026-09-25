"""客服前台评测批量化（p14 跨轨契约 §2「T177」）。

::

    python3 scripts/cs_eval.py [--set dev12|dev13|holdout12|holdout14|all] [--db PATH]
                               [--out FILE] [--json]

各集的跑法（门槛一律取各文件的 ``_thresholds``）：

* ``dev12``     —— ``evaluate.run_eval`` 跑 p12_cases.json（p12 路径，不注入端口）；
* ``dev13``     —— ``evaluate.run_eval_p13`` 跑 p13_cases.json（evaluate 的夹具端口）；
* ``holdout12`` —— p12 留出集两条路径都跑：p12 路径不注入端口、p13 路径注入**空夹具端口**
  （没写 fixtures 的 case 换成空的 ``EvalFixtures()``）；两条都达标才算 meets；
* ``holdout14`` —— ``run_eval_p13`` 跑 p14_holdout_cases.json；文件不存在 → 该集 SKIP 并说明，不报错；
* ``all``       —— 以上四集依次跑。

**只出聚合数与 id**（契约 §0「留出集是盲的」，dev 集同口径）：各指标、``meets``、没达标的
门槛说明（只含指标名与数字）、没对上的轮只列 ``<case id>#<轮次>``、按问题种类的计数。
客户原文、回复原文、期望 / 实得明细、前台异常原文一律不出 —— stdout、``--json``、``--out`` 同口径。
本脚本的作者没有打开过任何留出集文件：它们只按下面的路径常量加载。

``--db PATH``：本次运行的所有前台共享一个 ``SqliteStore(PATH)``（话术库在开跑前 seed 一次，
``seed_cs_kb`` 是 upsert、重复 seed 不重复落行）。同一个库可以连跑多次：每次运行给每个 case 的
跑批客户加一个本次运行独有的后缀（``eval-<case id>~<运行标记>``），于是每次都是**新会话**，
不会撞上一次留下的 handed_off 会话（那样后面的轮全成 silent）。没给 ``--db`` 时每个 case
一个新的 ``:memory:`` 库，case id 原样（与 maos/tests 里跑开发集的口径逐字一致）。

``--out FILE``：写 JSON 报告，首行 ``# generated at <ISO8601> from <git sha>``
（``scripts/make_evidence.header_line`` / ``git_sha``，同一口径）。

stderr 同口径：跑批期间接管 ``maos`` 这一支 logger（不再向上传播、也不落到 lastResort），
每条 WARNING 及以上只出「级别 logger 名 异常类名」一行 —— 日志消息正文、参数、异常原文与堆栈
一律不出（前台异常消息里可能带客户原文；出错轮数已计在 ``miss_by_problem`` 里）。跑完原样还原。
报告里的 ``thresholds`` 只留数值型门槛项（非数值键如 ``_note`` 不出）。

退出码：所选集（SKIP 的除外）全部 meets → 0；否则 1；用法错 → 2。
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import logging
import os
import pathlib
import sys
import uuid
from typing import Any, Callable, Mapping, Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from maos.core.store import SqliteStore               # noqa: E402
from maos.domain.cs import evaluate                   # noqa: E402
from maos.domain.cs.corpus import seed_cs_kb          # noqa: E402
from maos.domain.cs.desk import CsConfig, FrontDesk   # noqa: E402

# ---------------------------------------------------------------------------
# 路径常量（运行时按名字取，测试可以替换）
# ---------------------------------------------------------------------------
EVAL_DIR: pathlib.Path = evaluate.EVAL_PATH.parent
DEV12_PATH: pathlib.Path = evaluate.EVAL_PATH
DEV13_PATH: pathlib.Path = evaluate.P13_EVAL_PATH
HOLDOUT12_PATH: pathlib.Path = EVAL_DIR / "p12_holdout_cases.json"
HOLDOUT14_PATH: pathlib.Path = EVAL_DIR / "p14_holdout_cases.json"

SETS = ("dev12", "dev13", "holdout12", "holdout14")
SET_CHOICES = SETS + ("all",)

#: 跑批前台的配置：与 maos/tests 里跑开发集的口径一致（wk_eval → tnt-demo、不投递卡片）。
EVAL_TENANTS: Mapping[str, str] = dict(evaluate.DEFAULT_TENANT_MAP)

#: 集的状态。
STATUS_PASS, STATUS_FAIL, STATUS_SKIP = "PASS", "FAIL", "SKIP"

#: 跑批客户后缀的分隔符（只在 --db 时加）。
_TAG_SEP = "~"


def set_path(name: str) -> pathlib.Path:
    """某个集的文件路径（每次调用现取模块常量）。"""
    return {"dev12": DEV12_PATH, "dev13": DEV13_PATH,
            "holdout12": HOLDOUT12_PATH, "holdout14": HOLDOUT14_PATH}[name]


# ---------------------------------------------------------------------------
# 前台工厂
# ---------------------------------------------------------------------------
class _Stores:
    """给前台工厂发库：共享库（--db）或每个 case 一个 ``:memory:``。"""

    def __init__(self, db_path: str | None):
        self.shared = None
        if db_path:
            self.shared = SqliteStore(db_path)
            self.shared.init_schema()
            seed_cs_kb(self.shared)               # upsert：同一个库重复 seed 不重复落行

    def get(self) -> Any:
        if self.shared is not None:
            return self.shared
        store = SqliteStore(":memory:")
        seed_cs_kb(store)
        return store


def _p12_factory(stores: _Stores) -> Callable[[], FrontDesk]:
    def factory() -> FrontDesk:
        return FrontDesk(stores.get(), CsConfig(tenants=dict(EVAL_TENANTS), handoff_target=None))
    return factory


def _p13_factory(stores: _Stores) -> Callable[[Mapping[str, Any]], FrontDesk]:
    def factory(ports: Mapping[str, Any]) -> FrontDesk:
        return FrontDesk(stores.get(), CsConfig(tenants=dict(EVAL_TENANTS), handoff_target=None),
                         **dict(ports))
    return factory


# ---------------------------------------------------------------------------
# case 改写：共享库的运行标记、p13 路径的空夹具
# ---------------------------------------------------------------------------
def _tag_cases(cases: Sequence[evaluate.EvalCase], tag: str
               ) -> tuple[tuple[evaluate.EvalCase, ...], dict[str, str]]:
    """共享库时给每个 case id 加本次运行的后缀（跑批客户、msg_id 都由 id 派生，于是是新会话）。
    返回改写后的 case 与「改写后 id → 原 id」。tag 为空不改。"""
    if not tag:
        return tuple(cases), {c.id: c.id for c in cases}
    out, back = [], {}
    for case in cases:
        new_id = f"{case.id}{_TAG_SEP}{tag}"
        out.append(dataclasses.replace(case, id=new_id))
        back[new_id] = case.id
    return tuple(out), back


def _with_empty_fixtures(cases: Sequence[evaluate.EvalCase]) -> tuple[evaluate.EvalCase, ...]:
    """p13 路径跑 p12 式 case：没写 fixtures 的换成空夹具（注入三个端口，但谁的单都查不到）。"""
    return tuple(c if c.fixtures is not None
                 else dataclasses.replace(c, fixtures=evaluate.EvalFixtures()) for c in cases)


# ---------------------------------------------------------------------------
# 报告（只聚合数与 id）
# ---------------------------------------------------------------------------
def _run_summary(path_name: str, report: evaluate.EvalReport, thresholds: Mapping[str, Any],
                 back: Mapping[str, str]) -> dict[str, Any]:
    misses: list[str] = []
    by_problem: dict[str, int] = {}
    for miss in report.failures:
        misses.append(f"{back.get(miss.case_id, miss.case_id)}#{int(miss.turn)}")
        for problem in miss.problems:
            by_problem[str(problem)] = by_problem.get(str(problem), 0) + 1
    metrics = {k: (round(v, 4) if isinstance(v, float) else int(v))
               for k, v in report.metrics().items()}
    shortfalls = report.shortfalls(thresholds)
    return {"path": path_name, "cases": int(report.cases), "turns": int(report.turns),
            "metrics": metrics, "meets": not shortfalls, "shortfalls": list(shortfalls),
            "misses": misses, "miss_by_problem": dict(sorted(by_problem.items()))}


def run_set(name: str, stores: _Stores, *, tag: str = "") -> dict[str, Any]:
    """跑一个集，返回该集的聚合结果（``status`` ∈ PASS / FAIL / SKIP）。"""
    path = set_path(name)
    result: dict[str, Any] = {"set": name, "file": _display_path(path)}
    if not path.exists():
        if name == "holdout14":
            result.update(status=STATUS_SKIP, meets=None, runs=[],
                          note="文件不存在（T181 盲写留出集尚未合入），该集跳过")
            return result
        raise FileNotFoundError(f"{name} 的评测文件不存在：{_display_path(path)}")
    thresholds = evaluate.load_thresholds(path)
    cases = evaluate.load_cases(path)
    runs: list[dict[str, Any]] = []
    if name in ("dev12", "holdout12"):
        tagged, back = _tag_cases(cases, f"{tag}-{name}-p12" if tag else "")
        report = evaluate.run_eval(_p12_factory(stores), tagged)
        runs.append(_run_summary("p12", report, thresholds, back))
    if name in ("dev13", "holdout12", "holdout14"):
        base = _with_empty_fixtures(cases) if name == "holdout12" else cases
        tagged, back = _tag_cases(base, f"{tag}-{name}-p13" if tag else "")
        report = evaluate.run_eval_p13(_p13_factory(stores), tagged)
        runs.append(_run_summary("p13", report, thresholds, back))
    meets = all(r["meets"] for r in runs)
    result.update(status=STATUS_PASS if meets else STATUS_FAIL, meets=meets,
                  thresholds=_numeric_thresholds(thresholds), runs=runs)
    return result


def _numeric_thresholds(thresholds: Mapping[str, Any]) -> dict[str, Any]:
    """报告里只留数值型门槛项：非数值键（说明文字等）不出。"""
    return {str(k): v for k, v in thresholds.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


# ---------------------------------------------------------------------------
# stderr 打码：前台日志只出级别、logger 名与异常类名
# ---------------------------------------------------------------------------
_REDACT_LOGGER = "maos"


class _RedactingHandler(logging.Handler):
    """只出「[cs_eval] 级别 logger 名 异常类名」一行；消息正文、参数、异常原文、堆栈都不出。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            exc_name = ""
            if record.exc_info and record.exc_info[0] is not None:
                exc_name = f" exc={record.exc_info[0].__name__}"
            sys.stderr.write(f"[cs_eval] {record.levelname} {record.name}{exc_name}"
                             "（日志正文与异常原文已打码）\n")
        except Exception:                          # noqa: BLE001 —— 打码失败也不回落原文
            pass


@contextlib.contextmanager
def _redacted_logs():
    """跑批期间接管 maos 这一支 logger，跑完原样还原。"""
    logger = logging.getLogger(_REDACT_LOGGER)
    saved = (list(logger.handlers), logger.propagate, logger.level)
    handler = _RedactingHandler(level=logging.WARNING)
    logger.handlers = [handler]
    logger.propagate = False
    try:
        yield
    finally:
        logger.handlers, logger.propagate = saved[0], saved[1]
        logger.setLevel(saved[2])


def _display_path(path: pathlib.Path) -> str:
    try:
        return str(pathlib.Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def exit_code_of(results: Sequence[Mapping[str, Any]]) -> int:
    """所选集（SKIP 的除外）全部 meets → 0，否则 1。"""
    return 0 if all(r["status"] != STATUS_FAIL for r in results) else 1


def render_text(results: Sequence[Mapping[str, Any]], code: int) -> str:
    lines: list[str] = []
    for r in results:
        if r["status"] == STATUS_SKIP:
            lines.append(f"[{r['set']}] SKIP  {r['file']}：{r['note']}")
            continue
        lines.append(f"[{r['set']}] {r['status']}  {r['file']}")
        for run in r["runs"]:
            metrics = " ".join(f"{k}={v}" for k, v in run["metrics"].items())
            lines.append(f"  {run['path']}: cases={run['cases']} turns={run['turns']} {metrics}"
                         f" meets={'yes' if run['meets'] else 'no'}")
            for s in run["shortfalls"]:
                lines.append(f"    shortfall: {s}")
            if run["misses"]:
                counts = " ".join(f"{k}={v}" for k, v in run["miss_by_problem"].items())
                lines.append(f"    misses ({len(run['misses'])}; {counts}): "
                             + " ".join(run["misses"]))
    counted = [r for r in results if r["status"] != STATUS_SKIP]
    lines.append(f"RESULT: {sum(r['status'] == STATUS_PASS for r in counted)}/{len(counted)} sets meet"
                 f" ({sum(r['status'] == STATUS_SKIP for r in results)} skipped) exit={code}")
    return "\n".join(lines)


def _header() -> str:
    from scripts.make_evidence import git_sha, header_line
    return header_line(git_sha())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cs_eval.py", description="客服前台评测批量化（只出聚合数与 id）")
    p.add_argument("--set", dest="set_name", choices=SET_CHOICES, default="all",
                   help="跑哪个集（缺省 all）")
    p.add_argument("--db", default=None, help="所有前台共享的 SQLite 库路径（缺省每个 case 一个内存库）")
    p.add_argument("--out", default=None, help="JSON 报告写到这里（首行 generated-at 头）")
    p.add_argument("--json", action="store_true", help="stdout 出 JSON 而不是文本")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:                 # argparse 的用法错是 2；--help 是 0
        return int(exc.code or 0)
    names = SETS if args.set_name == "all" else (args.set_name,)
    with _redacted_logs():
        stores = _Stores(args.db)
        tag = uuid.uuid4().hex[:10] if args.db else ""
        results = [run_set(name, stores, tag=tag) for name in names]
    code = exit_code_of(results)
    doc = {"set": args.set_name, "db": bool(args.db), "exit_code": code, "sets": results}
    if args.out:
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_header() + "\n" + json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        print(render_text(results, code))
    return code


if __name__ == "__main__":
    sys.exit(main())
