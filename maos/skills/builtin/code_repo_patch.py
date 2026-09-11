"""code.repo-patch —— Coding 角色唯一的补丁产出入口。

投放即注册（C-1）：本文件放进 builtin/ 就会被 discover() 扫到，不改 __init__.py。

IO 契约（附录 B，逐字段）：
  入：{"title": str, "inputs": dict, "acceptance": list[str], "rework_findings": list[dict]}
  出：{"files": [{"path": str, "diff": str}], "summary": str,
       "self_check": {"build": "pass|fail", "lint": "pass|fail"}}
出参形状与 maos/flows/common.py 的 GOOD_PATCH 一致 —— 直接落成 patch_set artifact。

security_boundary 就在本文件的 ``_reject_protected_paths``，判据本身住在
``maos/tools/paths.py``（全仓唯一一处，sandbox 层从同一处取）。
本文件**不转出**这些名字：留一个「为了兼容」的别名就是第二个入口，
两个入口一定会漂，而漂的那次没人会发现 —— 直到有人靠改测试让测试通过。

self_check 只收敛类型、不校验取值：判「build/lint 是不是 pass」是 ReviewerGate 的活，
skill 抢着判会让 Gate 永远见不到失败样本（场景 2 的返工链就断了）。
但类型必须收敛 —— 非 dict 的 self_check 传下去会让 Gate 崩在 .get 上，
那不叫「留给 Gate 判」，那叫让 Gate 没机会判。

## 补丁预检与自修复（T125）

返回之前先 ``sandbox_git_apply(..., check_only=True)`` 干跑一遍；打不上就把 git
的原话递回模型再问，同一次 invoke 之内最多 ``ctx.identity.max_self_repair`` 次
（Coding 声明的是 2）。

**为什么这一层是 skill 而不是 Gate 或 Agent**，四条，缺一条它就该挪走：

1. **判据同源。** 真正打补丁的是 ``flows/common.py`` 调的同一个 ``sandbox_git_apply``。
   预检换成自己写一个 diff 解析器，两份判据一定会漂，而漂的那次表现成
   「预检说行、真打说不行」，没人会怀疑到预检头上。这里只调不改（C-7）。
2. **``check_only=True`` 是现成的**（``sandbox.py:494-495``），本来就是给补偿干跑
   用的那道闸，一行新代码都不用加就能问「这份补丁现在打得上吗」。
3. **``max_self_repair`` 是个空槽。** ``AgentIdentity`` 从 T-0 就有这个字段，
   全仓没有任何一处读它 —— 「一个 attempt 之内自己修几次」这件事，语义早就定好了
   位置，只是一直没人站进去。
4. **错误原文只在这一层拿得到并且还能用。** 再往上一层（Gate）拿到的时候，这次
   模型调用已经结束、attempt 已经计数、返工链已经起步；而模型要的只是
   「你这个 hunk 的行号对不上」这一句。

**它不是重试。** ``failure_policy="escalate"`` / ``max_retries=0`` 一个字没动（见
``SkillContract`` 那段注释）：重试是「同样的输入再跑一遍」，自修复是「把失败原因
加进输入再问一次」，前者会让 attempt 计数失真，后者在一次 invoke 里收口。

**它也不是一道新闸。** 次数用尽仍不合法时补丁**照旧交出去**，由 Gate 真打一次再
判 —— 预检只增加机会，不增加判定权（详见 ``run()`` 里那段）。预检自己判死的只有
一种情形：撞上受保护路径，那是安全事件。``max_self_repair == 0`` 的岗位连预检都
不做，整条路径逐字节回到 T125 之前。

**Scripted 路径逐字节不变**：``GOOD_PATCH`` / ``BAD_PATCH`` 是 ``flows/common.py``
导入时用真 ``git diff`` 现造的，预检必过，一次都不重问，输出连
``self_repair_rounds`` 这个键都不会多出来。
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from typing import Any

from maos.core.store import record_model_failure, record_model_usage
from maos.model.client import Tier
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill
from maos.tools.mcp.git_tool import FIXTURE_ROOT, GIT_MCP_PORT
from maos.tools.paths import PROTECTED_SEGMENTS, _path_segments
from maos.tools.port import invoke_tool
from maos.tools.sandbox import prepare_sandbox_workdir, sandbox_git_apply

log = logging.getLogger("maos.skills.code_repo_patch")

#: 落 ``model_usage`` 时写进 ``call_site`` 列的值。
#: **自修复的每一轮都记在这一个 call_site 上**，不新登记：重问不是一个新的调用点，
#: 是同一个调用点问了几次。于是「这次补丁一共烧了多少 token」仍是把这个 call_site
#: 的行加起来，成本视图一行不用改；轮数则由行数 - 1 数出来
#: （``test_cost_metrics.py`` 按 ``_REGISTERED_CALL_SITES`` 白名单校验，加一个
#: 新 call_site 会当场让它变红，而那是本轨没资格改的判据）。
CALL_SITE = "maos/skills/builtin/code_repo_patch.py::CodeRepoPatchSkill.run"

SYSTEM = """你是 Coding Agent。严格按架构契约产出补丁集。
只输出 JSON，不要任何解释文字，格式：
{"files":[{"path":"...","diff":"..."}],"summary":"...","self_check":{"build":"pass|fail","lint":"pass|fail"}}
禁止触碰任意层级下名为 infra、.github、secrets、tests 的目录 —— 尤其不许改测试让测试通过。
每个文件的 diff 必须是 `git diff` 风格的 unified diff：要有 `--- a/<path>` 与 `+++ b/<path>` 两行文件头，每段变更前要有 `@@ -<起始行>,<行数> +<起始行>,<行数> @@` 形式的 hunk 头，且 hunk 头里的行号与紧随其后的上下文行必须与文件的真实内容逐字对得上，不许估算。
每个 diff 的最后一行以换行符结尾。
不许省略上下文行：hunk 头声明了几行就要写出几行，变更处前后各留 3 行上下文，任何地方都不许用 `...`、`（略）` 或「此处省略」代替真实行。"""

#: 预检失败后值得**再问一次模型**的 stage。这两个说的都是「这份补丁本身不合法 /
#: 打不上去」—— 那恰好是模型改得动的东西，把 git 的原话递回去它就有了第二次机会。
_REPAIRABLE_STAGES = frozenset({"validate", "apply"})

#: 安全 stage。走到这里说明补丁绕过了上面的 ``_reject_protected_paths``
#: （它只看**声明**的 ``path``，而 sandbox 还看 diff 正文抠出来的路径、以及 git
#: 解码后的真实落盘清单 —— C-quoted 八进制那类绕过正是这么被逮住的）。
#: 绕过口一旦被预检看见，要走的是**安全事件**那条路：不重试、不降级，与
#: ``ProtectedPathViolation`` 完全同一个口径。重问等于给绕过三次机会。
_SECURITY_STAGES = frozenset({"path_check", "path_escape", "conftest_guard"})


class ProtectedPathViolation(Exception):
    """补丁触碰受保护路径。安全事件：不重试、不降级，直接终止本次产出。

    invoker 只把异常转成 ``"<类名>: <消息>"`` 字符串，所以类名本身就是跨模块协议 ——
    改名要同步改 ``maos/agents/coding.py`` 的 SECURITY_ERROR_PREFIX。
    """


def _reject_protected_paths(files: list[dict]) -> None:
    violations = [
        f["path"] for f in files
        if PROTECTED_SEGMENTS.intersection(_path_segments(f["path"]))
    ]
    if violations:
        raise ProtectedPathViolation(f"触碰受保护路径，已中止: {violations}")


@register_skill
class CodeRepoPatchSkill(Skill):
    contract = SkillContract(
        name="code.repo-patch",
        version="1.0.0",
        purpose="按任务契约产出补丁集，返回前完成受保护路径校验",
        input_schema={
            "title": "str",
            "inputs": "dict",
            "acceptance": "list[str]",
            "rework_findings": "list[dict]",
        },
        output_schema={
            "files": "list[{path:str,diff:str}]",
            "summary": "str",
            "self_check": "{build:'pass|fail', lint:'pass|fail'}",
        },
        preconditions=["title", "inputs", "acceptance"],
        depends_tools=["git-mcp", "sandbox"],
        # 刻意不 retry：重试归 worker 的 attempt 层（max_attempts），
        # skill 层再叠一层会让 attempt 计数失真；安全违规更不该被重试。
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "受保护路径判定：补丁路径规范化后按 / 分段，任一段命中 PROTECTED_SEGMENTS"
            "（infra / .github / secrets / tests，任意层级、大小写不敏感）"
            "立即抛 ProtectedPathViolation，不重试、不降级；skill 自身不落盘、不执行补丁"
        ),
        reuse_note="Coding 角色唯一的补丁产出入口；返工走同一入口，findings 从 payload 进",
        owner_roles=["coding"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        if ctx.model is None:
            # 与 req.normalize 不同：补丁没有规则兜底可言，无模型就是接线错了。
            raise RuntimeError("code.repo-patch 需要 ctx.model，调用方必须传 extras={'model': ...}")

        # 接住整个 ModelResponse 再取 .text（口径同 req_normalize）：补丁产出是本仓
        # 最贵的一类调用，用量在这一行丢掉，成本视图里最大的那一块就是空的。
        # 补丁基线经 git-mcp 取（``depends_tools`` 里声明的那个 git-mcp，现在真调了）。
        # 拿的是「这份补丁是针对哪个 HEAD 产出的」——补丁本身不带基线，事后就只能靠
        # 时间戳去猜它对应哪一版代码，而时间戳在返工链上恰好是最不可信的那个字段。
        # 失败一律抛：连不上就是连不上，不许悄悄回落到本地 git，否则「这一步走没走
        # MCP」在证据里查不出来。
        baseline = invoke_tool(
            GIT_MCP_PORT,
            {"op": "baseline", "root": FIXTURE_ROOT},
            store=ctx.store, extras=ctx.extras,
        )

        tier = ctx.extras.get("tier") or Tier.MEDIUM
        user = self._build_prompt(payload, ctx, baseline)

        # 一次 invoke 之内最多再问几遍。**这是 `AgentIdentity.max_self_repair` 全仓
        # 的第一个读取点** —— 那个字段 T-0 就声明了（`agents/base.py:69`「Agent 内部
        # 自修复上限」），但在此之前没有任何代码读它，Coding 声明的 2 一直是死字。
        # 从 `ctx.identity` 读而不是从 payload/extras：它是**岗位属性**，不是这次
        # 任务的参数 —— 改的人应该去改那个 Agent 的 identity，而不是某个调用点。
        budget = max(0, int(getattr(ctx.identity, "max_self_repair", 0) or 0))

        # 预检用的工作目录**自己现造，不复用 `payload["inputs"]["workdir"]`**。
        # 两个理由，第二个才是硬的：
        #   ① coding 任务的 inputs 里压根没有 workdir —— `flows/scenario_1.py` 的
        #      `_with_workdir()` 只注入 `role == "testing"` 的节点。
        #   ② 就算有，它也**不是真打补丁时的那个状态**：`flows/common.py:252-253`
        #      在每次真打之前先 `rmtree` + `prepare_sandbox_workdir()` 还原成靶场
        #      基线。返工链上那个目录里躺着上一轮的补丁，拿它预检，「过」与「不过」
        #      都可能与真打相反 —— 而假阴性会把一份好补丁判死，假阳性等于没预检。
        # 现造一份干净基线，与真打那一刻逐字节同源。代价 ~80ms/次（实测，靶场 24KB）。
        workdir: str | None = None
        rounds = 0
        try:
            while True:
                patch = self._parse(self._complete(ctx, user, tier).text)
                if budget == 0:
                    # **不自修复的岗位一次预检都不做**，路径逐字节回到 T125 之前。
                    # 预检存在的全部理由是驱动自修复；不修的话它就只剩两样东西：
                    # 80ms 的开销，和一道抢在 Gate 前面的闸 —— 而「补丁打不上」
                    # 归 Gate 判是本文件反复写着的取向（见 self_check 那段）。
                    break
                # 空补丁集不需要工作目录：`sandbox_git_apply` 对空 files 在碰
                # `os.path.realpath(workdir)` **之前**就早返回（sandbox.py:450-457），
                # 「查过了，没有要改的」那条合法结论因此一分钱不花、也不会被判死。
                # 顺序反了（先造目录再判空）只是白付 80ms，不影响结论 —— 但那 80ms
                # 乘上全量测试里的每一次调用就不是零了。
                if patch["files"] and workdir is None:
                    workdir = prepare_sandbox_workdir()

                checked = sandbox_git_apply(patch, workdir or "", check_only=True)
                if checked.get("ok"):
                    break

                err = checked.get("error") or {}
                stage = str(err.get("stage") or "")
                if stage in _SECURITY_STAGES:
                    # 预检逮住了 `_reject_protected_paths` 漏掉的绕过口。这是安全
                    # 事件，走与它同一个出口：不重问、不降级（见 _SECURITY_STAGES）。
                    # **唯一一条预检自己判死的路** —— 安全事件不交给下游再判一次，
                    # 因为「交给下游」意味着这份补丁还要在沙箱里被真打一次。
                    raise ProtectedPathViolation(
                        f"补丁触碰受保护面，已中止（预检 stage={stage} "
                        f"path={err.get('path')}）: {err.get('message')}")
                if stage not in _REPAIRABLE_STAGES or rounds >= budget:
                    # **次数用尽不判死，照旧把补丁交出去。** 这一条最容易被改错，
                    # 所以理由写全：
                    #
                    # ① 判「补丁打不上」是 Gate 的活。`flows/common.py:255-264`
                    #    真打一次、拿 `tool_error` 包成 test_report、Gate 据此出
                    #    blocker finding、`AWAITING_REVIEW -> REWORK -> PENDING`。
                    #    skill 在这里抛，这条链一步都走不到 —— 证据里从此没有
                    #    「补丁没落进沙箱」那份带 `sandbox_mode=not-run` 的报告，
                    #    只剩一个没有上下文的 skill failed。这与 self_check 那段
                    #    「skill 抢着判会让 Gate 永远见不到失败样本」是同一条取向。
                    # ② 自修复要买的是「多两次机会」，不是「多一道闸」。原先没有
                    #    预检时这份补丁本来就会被交出去，现在只是交出去之前先问过
                    #    两遍 —— 行为只增不减，返工链一个字节没动。
                    # ③ 于是「用尽后怎么办」的答案与 T125 之前完全一致，
                    #    `test_contracts.py` / `test_mcp_git_tool.py` 那 11 条拿占位
                    #    diff 判 self_check 收敛与 git-mcp 接线的用例照旧全绿。
                    log.warning("补丁预检仍未过（stage=%s，已重问 %d/%d 次），"
                                "照旧交给 Gate 真打并判定：%s",
                                stage, rounds, budget, err.get("message"))
                    break

                rounds += 1
                # 留痕：重问过几次、为什么重问，现场看日志就要看得见。这不是噪音 ——
                # rounds > 0 本身就说明模型第一次没产出合法 diff，那是要记进账的事实。
                log.warning("补丁预检未过（stage=%s），第 %d/%d 次自修复重问：%s",
                            stage, rounds, budget, err.get("message"))
                user = self._repair_prompt(user, err, rounds, budget)
        finally:
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)

        if rounds:
            # **只有真重问过才加这个键**，于是 Scripted 路径的输出逐字节不变
            # （GOOD_PATCH / BAD_PATCH 预检必过，rounds 恒 0）—— `SkillInvoked.detail`
            # 里的 `output_hash` 因此也一个字节没动。加了键时它进 patch_set artifact，
            # 证据束里直接看得见「这份补丁重问了几次」。另一条同样能数出来的路是
            # `model_usage` 里本 CALL_SITE 的行数（= 1 + rounds），两条互为对账。
            patch["self_repair_rounds"] = rounds
        return patch

    # ------------------------------------------------------------------
    def _complete(self, ctx: SkillContext, user: str, tier: str) -> Any:
        """问一次模型，成败两路都记账。自修复的每一轮都走这里。"""
        started = time.perf_counter()
        try:
            resp = ctx.model.complete(system=SYSTEM, user=user, tier=tier)
        except Exception as exc:
            # 失败也要留账（T54），口径同 req_normalize：补丁是本仓最贵的一类调用，
            # 一次超时烧掉的输入侧 token 原先在成本视图里完全不存在。
            record_model_failure(
                ctx.store, exc,
                agent_role=getattr(ctx.identity, "role", "") or "unknown",
                call_site=CALL_SITE, tier=tier,
                latency_ms=int((time.perf_counter() - started) * 1000),
                model=getattr(ctx.model, "model", "") or "",
                trace_id=ctx.extras.get("trace_id") or "",
                plan_id=ctx.extras.get("plan_id") or "",
                task_id=ctx.extras.get("task_id"),
            )
            raise
        record_model_usage(
            ctx.store, resp, client=ctx.model,
            agent_role=getattr(ctx.identity, "role", "") or "unknown",
            call_site=CALL_SITE, tier=tier,
            latency_ms=int((time.perf_counter() - started) * 1000),
            trace_id=ctx.extras.get("trace_id") or "",
            plan_id=ctx.extras.get("plan_id") or "",
            task_id=ctx.extras.get("task_id"),
        )
        return resp

    # ------------------------------------------------------------------
    @staticmethod
    def _parse(raw: str) -> dict:
        """把模型这一轮的文本收敛成合法补丁集。不合法的一律抛，**不重问**。

        重问只治「diff 打不上」那一类（见 `_REPAIRABLE_STAGES`）。这里抛的三种是
        另一回事：JSON 都不合法、字段类型不对、碰了受保护路径 —— 前两种说明这次
        产出连形状都不成立，后一种是安全事件。把它们也塞进重问循环会让
        `max_self_repair` 变成一个笼统的「模型不听话就再来一遍」，而那正是
        `max_retries=0` 那条注释要挡的事。
        """
        try:
            patch = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"模型输出非合法 JSON: {exc}") from None
        if not isinstance(patch, dict):
            raise ValueError(f"补丁集应为 JSON 对象，实际 {type(patch).__name__}")

        files = patch.get("files") or []
        summary = patch.get("summary")
        has_summary = isinstance(summary, str) and summary.strip()
        if not files and not has_summary:
            # **空补丁集 + 空 summary** 才是错误（模型什么都没产出）。空补丁集本身
            # 是一个**合法结论**：定位/调查类任务的正确答案完全可能是「查过了，
            # 没有要改的」，而验收标准往往就是这么写的（docs/BACKLOG.md:2293 实测：
            # 模型逐字照做，反被这一句判成失败，配了 key 的机器上恒红）。口径与
            # `agents/refund/policy_agent.py` 的模块 docstring 一致：「裁定为 reject
            # 时不产出空产物、也不 failed —— 那是一个有效的业务结论，不是一次执行失败」。
            raise ValueError("补丁集为空，且没有 summary 说明查了什么 —— "
                             "分不清「查过了，没有要改的」与「模型没产出」")
        patch["files"] = files      # 显式落成空 list：Gate 的 schema 闸查的是这个键在不在
        # diff 与 path 同等必校：output_schema 声明的是 {path:str,diff:str}，
        # 而代价落在零模型补偿链 —— artifacts.py 反向打补丁时拿不到 diff，
        # 补偿会「成功」地什么都没还原，是静默失败，不是报错。
        bad = [
            f for f in files
            if not isinstance(f, dict)
            or not isinstance(f.get("path"), str)
            or not isinstance(f.get("diff"), str)
        ]
        if bad:
            raise ValueError(f"补丁集里有 {len(bad)} 项缺少合法 path/diff 字段")

        _reject_protected_paths(files)          # security_boundary 执行处

        # 显式类型收敛，不是 setdefault。setdefault 只在键缺失时填缺省，键在则
        # 原样保留 —— 于是 self_check: null 和 self_check: "pass" 会照原样穿透到
        # Gate，在 gate.py 的 check.get() 上抛 AttributeError。那里是裸调用，
        # 异常逃出后整个 plan 驱动循环当场崩，连一次返工都退化不出来。
        # 收敛的是**类型**不是取值：build/lint 判 pass 还是 fail 是 ReviewerGate
        # 的活，skill 抢着判会让 Gate 永远见不到失败样本（场景 2 的返工链就断了）。
        if not isinstance(patch.get("self_check"), dict):
            patch["self_check"] = {}
        if not isinstance(patch.get("summary"), str):
            patch["summary"] = ""
        return patch

    # ------------------------------------------------------------------
    @staticmethod
    def _repair_prompt(user: str, error: dict, round_no: int, budget: int) -> str:
        """把 git 的原话接在上一轮提示词后面，要一份**完整重产**的 JSON。

        递回去的是 `git apply --check` 自己说的那句（stage / path / hunk / message），
        不是我们转译的一句「格式不对」：转译会丢掉行号，而行号恰好是模型修得动
        这个错的关键信息。铁律 6 在这里不需要额外脱敏 —— git 的报错里只有路径与
        行号，`sandbox_git_apply` 也从不把环境变量带进 message。

        要「完整重产」而不是「给个增量」：增量要模型自己记住上一版长什么样，而
        它上一版恰恰是错的；重产一份的成本是一次 medium 调用，比修补一份坏 diff 稳。
        """
        return "\n\n".join((
            user,
            f"【自修复 {round_no}/{budget}】上一版补丁没能通过 `git apply --check`，"
            f"整份补丁都没有落盘。git 的原话是：\n"
            f"  stage={error.get('stage')} path={error.get('path') or '未报'} "
            f"hunk={error.get('hunk') or '未报'}\n"
            f"  {error.get('message')}\n"
            f"请**重新输出完整的 JSON**（不是增量、不要解释文字），"
            f"把 diff 修成 `git apply` 能接受的 unified diff："
            f"文件头、hunk 头的行号、以及上下文行都要与文件真实内容对得上。",
        ))

    # ------------------------------------------------------------------
    @staticmethod
    def _build_prompt(payload: dict, ctx: SkillContext, baseline: dict | None = None) -> str:
        """attempt 从 extras 取，不进 payload —— 入参字段以附录 B 为准，不许扩。

        基线行只放 sha / 分支 / 干净与否三个短标量，**不放文件内容**：
        ``ScriptedModelClient`` 按 ``kw in user`` 子串匹配取第一个命中的键，
        往提示词里灌仓库正文会凭空多出误命中，而那种失配表现成「模型答非所问」，
        查起来会绕很远。
        """
        parts = [
            f"任务：{payload.get('title', '')}",
            f"任务输入：{json.dumps(payload.get('inputs') or {}, ensure_ascii=False)}",
            f"验收标准：{json.dumps(payload.get('acceptance') or [], ensure_ascii=False)}",
        ]
        if baseline:
            parts.append(
                "补丁基线：%s@%s（%s）" % (
                    baseline.get("repo_name") or "?",
                    baseline.get("head_short") or "?",
                    "工作树有未提交改动" if baseline.get("dirty") else "工作树干净",
                )
            )
        findings = payload.get("rework_findings") or []
        attempt = int(ctx.extras.get("attempt") or 1)
        if attempt > 1 and findings:
            # 返工时把结构化 findings 喂回去，而不是让模型重头猜
            parts.append(
                "这是第 %d 次返工，必须逐条解决以下问题：\n%s"
                % (attempt, json.dumps(findings, ensure_ascii=False, indent=2))
            )
        return "\n\n".join(parts)
