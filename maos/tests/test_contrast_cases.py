"""三组对照 case 的机器守卫 —— 租户 / 渠道 / 政策版本。

**判据只有一个来源**：`scenarios/refund/cases/*.json` 的 `_expected` 块。
本文件一个期望值都不自写 —— 两份期望值一定会漂，漂了之后测试绿而结论错，
那比红更坏。凡是出现在断言里的数字（30 / 7 / 20 / 1280.00 …）都从 `_expected`
或从库里读出来，没有一个是字面量。

不发网络请求，不依赖 `MAOS_LLM_API_KEY`：`contrast.run_case` 走
`select_model_client(..., force_scripted=True)`，配了 key 的机器上也一行网络不走。
"""

from __future__ import annotations

import json

import pytest

from maos import kb
from maos.contracts.states import PlanState, TaskState
from maos.core.store import SqliteStore
from maos.domain.refund import fixtures, objects
from maos.flows import contrast
from maos.kb import guardrails, retriever

#: 状态机的合法取值域。断言「没有新状态」靠它 —— 铁律 9 说的就是这条：
#: 换维度不许往 Task 状态机里加任何东西。
TASK_STATES = frozenset(getattr(TaskState, n) for n in dir(TaskState) if n.isupper())


# ---------------------------------------------------------------------------
# 三组各跑一次，本模块共用（每个 case 都要跑一整条 DAG，跑一次就够）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def r3() -> list[dict]:
    return contrast.run_group("R3", verbose=False)


@pytest.fixture(scope="module")
def r4() -> list[dict]:
    return contrast.run_group("R4", verbose=False)


@pytest.fixture(scope="module")
def r6() -> list[dict]:
    return contrast.run_group("R6", verbose=False)


@pytest.fixture
def seeded_r4b():
    """装好 case_r4b 靶场的库 —— 给「差异是不是 if 出来的」那两条负例用。"""
    store = SqliteStore()
    store.init_schema()
    payload = fixtures.load_case("case_r4b.json")
    fixtures.seed_case(store, payload)
    return store, payload


def _by_case(rows: list[dict]) -> dict[str, dict]:
    return {r["observed"]["case_id"]: r for r in rows}


# ---------------------------------------------------------------------------
# 0. 判据比对：六个 case 全部与 `_expected` 一致
# ---------------------------------------------------------------------------
def test_all_three_groups_match_their_expected_blocks(r3, r4, r6):
    """三组六个 case 逐条对上 case json 的 `_expected`，一处不符即红。"""
    bad = [f"{row['file']}: {m}" for rows in (r3, r4, r6) for row in rows
           for m in row["mismatch"]]
    assert not bad, "对照结果与 _expected 不符：\n  " + "\n  ".join(bad)


# ---------------------------------------------------------------------------
# 1. R3 租户维度：唯一变量是 tenant_id，结论相反
# ---------------------------------------------------------------------------
def test_r3_same_inputs_except_tenant_id_yield_opposite_decisions(r3):
    """A 通过、B 驳回；且两次运行**除 tenant_id 外输入完全相同**。

    第二句是这一组的立身之本：任何一个别的变量不同，「结论差异来自租户」
    就说不出口了。所以这里不只比结论，先把输入逐字段比一遍。
    """
    a, b = _by_case(r3)["RC-R3A"], _by_case(r3)["RC-R3B"]

    seed_a = fixtures.case_seed_of(fixtures.load_case("case_r3a.json"))
    seed_b = fixtures.case_seed_of(fixtures.load_case("case_r3b.json"))
    # case_id / order_id 是标识符不是变量（两个租户各有自己的案号与订单号），
    # tenant_id 是本组**刻意**要变的那一个。其余每一个字段都必须逐字相同。
    varying = {"tenant_id", "case_id", "order_id"}
    for key in set(seed_a) | set(seed_b):
        if key in varying:
            continue
        assert seed_a[key] == seed_b[key], f"R3 两侧的 case.{key} 不同，变量控制破了"
    assert seed_a["tenant_id"] != seed_b["tenant_id"]

    obs_a, obs_b = a["observed"], b["observed"]
    for key in ("paid_at", "elapsed_days", "pinned_policy_version", "reason_code",
                "channel_id", "matched_rules"):
        assert obs_a[key] == obs_b[key], f"R3 两侧观测到的 {key} 不同，变量控制破了"

    # 结论相反，且判据取自 `_expected`，不是本文件写死的 approve/reject。
    assert obs_a["decision"] == a["expected"]["decision"]
    assert obs_b["decision"] == b["expected"]["decision"]
    assert obs_a["decision"] != obs_b["decision"]
    # 差异只可能来自同一条规则在两个租户下的窗口参数。
    assert obs_a["deciding_rule"] == obs_b["deciding_rule"]
    assert obs_a["no_reason_days"] != obs_b["no_reason_days"]


# ---------------------------------------------------------------------------
# 2/3. R4 渠道维度：多一个核销任务、审批人不同
# ---------------------------------------------------------------------------
def test_r4_dealer_plan_has_exactly_one_more_task_and_it_is_nameable(r4):
    """经销组的任务数比自营组多 1，且多出来的那个 role/title 指得出来。"""
    a, b = _by_case(r4)["RC-R4A"], _by_case(r4)["RC-R4B"]
    obs_a, obs_b = a["observed"], b["observed"]

    assert obs_b["task_count"] == obs_a["task_count"] + 1, (
        f"经销组应比自营组多 1 个任务，实际 {obs_a['task_count']} vs {obs_b['task_count']}")

    only_b = ({t["task_id"] for t in obs_b["tasks"]}
              - {t["task_id"].replace("r4a", "r4b") for t in obs_a["tasks"]})
    assert len(only_b) == 1, f"多出来的任务应恰好一个，实际 {sorted(only_b)}"
    extra = next(t for t in obs_b["tasks"] if t["task_id"] in only_b)

    # 指认：role 与 title 都说得出，且都逐字来自政策规则，不是本文件编的。
    rule = _as004_params(fixtures.load_case("case_r4b.json"))["extra_tasks"][0]
    assert extra["role"] == rule["owner_role"]
    assert extra["title"] == rule["title"]
    assert extra["state"] == TaskState.DONE
    # `_expected.extra_tasks` 记的是 task_key，两侧都对上。
    assert obs_b["extra_tasks"] == list(b["expected"]["extra_tasks"])
    assert obs_a["extra_tasks"] == list(a["expected"]["extra_tasks"]) == []


def test_r4_approver_differs_between_channels(r4):
    """两组审批人不同；经销那一侧的审批人逐字来自 `AS-004` 的 approver_role。"""
    a, b = _by_case(r4)["RC-R4A"], _by_case(r4)["RC-R4B"]
    obs_a, obs_b = a["observed"], b["observed"]

    assert obs_a["approver_role"] != obs_b["approver_role"]
    assert obs_b["approver_role"] == _as004_params(
        fixtures.load_case("case_r4b.json"))["approver_role"]
    assert obs_b["approver_role"] == b["expected"]["approver_role"]
    assert obs_a["approver_role"] == contrast.DEFAULT_APPROVER_ROLE
    # 审批**真的发生过**：approval_record 的签名里带着那个角色。
    assert obs_b["approvals"] and obs_b["approver_role"] in obs_b["approvals"][0]


def test_r4_amount_is_identical_so_the_difference_is_isolated_on_channel(r4):
    """金额两侧相同 —— 差异被隔离在渠道这一个变量上（`_expected` 的原话）。"""
    a, b = _by_case(r4)["RC-R4A"], _by_case(r4)["RC-R4B"]
    assert a["observed"]["amount_approved"] == a["expected"]["amount_approved"]
    assert b["observed"]["amount_approved"] == b["expected"]["amount_approved"]
    assert a["observed"]["amount_approved"] == b["observed"]["amount_approved"]


# ---------------------------------------------------------------------------
# 4/5. R6 政策版本维度
# ---------------------------------------------------------------------------
def test_r6_uses_the_version_pinned_by_the_order_snapshot(r6):
    """命中的 `policy_rule.version` 是订单快照锁定的 v1，不是库里最新的 v2。"""
    row = r6[0]
    obs, exp = row["observed"], row["expected"]
    correct = exp["correct"]

    assert obs["pinned_policy_version"] == exp["pinned_policy_version"]
    assert obs["deciding_rule"] == correct["matched_rule"]
    assert obs["no_reason_days"] == correct["no_reason_days"]
    assert obs["decision"] == correct["decision"]
    assert obs["amount_approved"] == correct["amount_approved"]
    # 命中的每一条都是锁定版本，一条 v2 都不许混进来。
    assert all(ref.endswith(f"@v{exp['pinned_policy_version']}")
               for ref in obs["matched_rules"]), obs["matched_rules"]


def test_r6_the_newer_version_really_exists_and_really_flips_the_verdict():
    """库里确实有 v2，且按最新版判会得出**相反**结论。

    没有这一条，上一条测的就是空气：库里没有 v2，「锁定 v1」无所谓锁不锁。
    这里把错误路径真的走一遍 —— 陷阱得真踩得进去才叫证据。
    """
    store = SqliteStore()
    store.init_schema()
    payload = fixtures.load_case("case_r6.json")
    fixtures.seed_case(store, payload)
    seed, expected = fixtures.case_seed_of(payload), fixtures.expected_of(payload)

    pinned = expected["pinned_policy_version"]
    newer = objects.query(
        store, "SELECT rule_no, version FROM policy_rule WHERE tenant_id=? AND version>?"
               " ORDER BY rule_no", (seed["tenant_id"], pinned))
    assert newer, f"库里没有比 v{pinned} 更新的政策版本，R6 这一组证明不了任何东西"

    observed, bad = contrast.check_r6_wrong_path(store, seed, expected)
    assert not bad, bad
    assert observed["decision"] != expected["correct"]["decision"], (
        "两条路径应给出相反结论，否则「按哪一版判」这件事看不出来")
    assert observed["latest_policy_version"] > pinned


# ---------------------------------------------------------------------------
# 6. 三组跑完，状态迁移全部落在既有状态机内
# ---------------------------------------------------------------------------
def test_no_new_task_state_was_introduced_by_any_dimension(r3, r4, r6):
    """换三个维度，`states.py` 一个新状态都没加（铁律 9）。

    业务上的 approve / reject / 渠道核销全都表达成**别的东西** ——
    DAG 的形状、政策规则的参数、业务对象自己的字段 —— 而不是 Task 状态。
    """
    for rows in (r3, r4, r6):
        for row in rows:
            obs = row["observed"]
            unknown = set(obs["state_transitions"]) - TASK_STATES
            assert not unknown, (
                f"{obs['case_id']} 出现了状态机之外的状态 {sorted(unknown)} —— "
                f"业务差异不该变成新的 Task 状态")
            assert obs["plan_state"] == PlanState.DONE
            # 业务状态也没被这三个维度改动过：对照测的是**裁定**，
            # 收款收口是场景 6/7 的事，这里不许有第二条 biz_status 写入路径。
            assert obs["biz_status"] == "submitted", obs["biz_status"]


# ---------------------------------------------------------------------------
# 7. failure_hint 不作为规划正例
# ---------------------------------------------------------------------------
def test_failure_hints_are_stored_but_never_used_as_planning_positives():
    """8 条失败案例进库、检索得到，但**不进规划正例**。

    分两截验，缺一不可：
      · 库里确实有 `failure_hint`（不然这条测的是「库里本来就没有」）；
      · `guardrails.apply_suggestions` 把它们滤掉了（`kb.POSITIVE_KINDS` 不含它）。
    """
    store = SqliteStore()
    store.init_schema()
    counted = fixtures.seed_history_kb(store)
    assert counted[kb.KIND_FAILURE_HINT] > 0
    assert kb.KIND_FAILURE_HINT not in kb.POSITIVE_KINDS

    hints = kb.list_docs(store, kind=kb.KIND_FAILURE_HINT)
    assert len(hints) == counted[kb.KIND_FAILURE_HINT]
    # 每一条都是 outcome=failed —— 分流口径与 `guardrails.classify_case` 同一份。
    assert {h["outcome"] for h in hints} == {kb.OUTCOME_FAILED}

    # 把失败案例硬塞进检索结果，护栏仍然一个任务都不许它补。
    baseline = [{"role": "refund_intake", "title": "受理",
                 "inputs": {"step": "intake", "tenant_id": hints[0]["tenant_id"]},
                 "depends_on": [], "risk_level": "L"}]
    docs = [{**h, "score": 1.0} for h in hints]
    merged, added = guardrails.apply_suggestions(baseline, docs)
    assert added == [], f"failure_hint 被当成规划正例补进了 DAG：{added}"
    assert merged == baseline


# ---------------------------------------------------------------------------
# 8. 跨租户不召回（评委原话里的硬约束）
# ---------------------------------------------------------------------------
def test_tenant_a_never_retrieves_any_document_of_tenant_b():
    """租户 A 的 case 召不到租户 B 的任何一条 `kb_doc`。

    库里**必须**同时躺着两个租户的知识，否则这条测的是「库里本来就只有 A」。
    所以先断言 B 的条数非零，再断言 A 的候选集里一条 B 都没有。
    """
    store = SqliteStore()
    store.init_schema()
    fixtures.seed_policy_kb(store)
    fixtures.seed_history_kb(store)

    payload = fixtures.load_case("case_r3a.json")
    seed = fixtures.case_seed_of(payload)
    peer = fixtures.case_seed_of(fixtures.load_case("case_r3b.json"))
    assert seed["tenant_id"] != peer["tenant_id"]

    all_docs = kb.list_docs(store)
    theirs = [d for d in all_docs if d["tenant_id"] == peer["tenant_id"]]
    assert theirs, "库里没有对照租户的知识，这条约束测的是空气"

    query = {"tenant_id": seed["tenant_id"], "biz_type": "refund",
             "channel_id": seed["channel_id"], "sku": seed["sku"]}
    # 阶段一：候选集里一条别人家的都没有。
    candidates = retriever.prefilter(store, query)
    assert candidates, "本租户的候选集为空，这条测的也是空气"
    assert {c["tenant_id"] for c in candidates} == {seed["tenant_id"]}
    # 阶段二：四通道打完分，命中的仍然全是本租户 —— 且没有一条是别人家的 doc_id。
    theirs_ids = {d["doc_id"] for d in theirs}
    hits = retriever.retrieve(store, {**query, "keyword": "无理由退货 窗口 全额退"},
                              limit=20)
    assert hits, "本租户一条都召不回，这条测的还是空气"
    assert not ({h["doc_id"] for h in hits} & theirs_ids)


# ---------------------------------------------------------------------------
# 9/10. 差异是**规划**出来的，不是 if 出来的
# ---------------------------------------------------------------------------
def test_removing_as004_removes_the_writeoff_task_from_the_plan(seeded_r4b):
    """把 `AS-004` 从政策里拿掉，核销任务当场消失 —— 它不是硬塞的分支。"""
    store, payload = seeded_r4b
    seed = fixtures.case_seed_of(payload)
    args = {"tenant_id": seed["tenant_id"], "order_id": seed["order_id"],
            "order_version": int(seed["order_version"])}

    before = contrast.policy_directives(contrast.policy_view(store, **args)["rules"])
    assert [s["task_key"] for s in before["extra_tasks"]] == \
        list(fixtures.expected_of(payload)["extra_tasks"])
    assert before["approver_role"] != contrast.DEFAULT_APPROVER_ROLE

    objects.execute(store, "DELETE FROM policy_rule WHERE tenant_id=? AND rule_no=?",
                    (seed["tenant_id"], "AS-004"))
    after = contrast.policy_directives(contrast.policy_view(store, **args)["rules"])
    assert after["extra_tasks"] == []
    assert after["approver_role"] == contrast.DEFAULT_APPROVER_ROLE


def test_giving_as004_to_the_self_operated_channel_moves_the_task_there(seeded_r4b):
    """把 `AS-004` 的 `channel_scope` 改成自营渠道，核销任务就跟着跑到自营那边。

    这是比上一条更硬的一条：它证明代码里**没有任何地方认得「经销」这个词**。
    差异完全由 `channel_scope` 与 `extra_tasks` 两个数据字段决定，
    改数据就换结论，一行代码都不用动。
    """
    store, payload = seeded_r4b
    seed = fixtures.case_seed_of(payload)
    online = "ch-online"
    assert seed["channel_id"] != online

    # 把同一笔订单改挂到自营渠道：此时 AS-004（scope=ch-dealer）应当命中不上。
    objects.execute(store, "UPDATE order_snapshot SET channel_id=? WHERE tenant_id=?"
                           " AND order_id=? AND version=?",
                    (online, seed["tenant_id"], seed["order_id"],
                     int(seed["order_version"])))
    args = {"tenant_id": seed["tenant_id"], "order_id": seed["order_id"],
            "order_version": int(seed["order_version"])}
    assert contrast.policy_directives(
        contrast.policy_view(store, **args)["rules"])["extra_tasks"] == []

    # 再把 AS-004 的 scope 改成自营 —— 任务原封不动地出现在自营这一侧。
    objects.execute(store, "UPDATE policy_rule SET channel_scope=? WHERE tenant_id=?"
                           " AND rule_no=?", (online, seed["tenant_id"], "AS-004"))
    moved = contrast.policy_directives(contrast.policy_view(store, **args)["rules"])
    assert [s["task_key"] for s in moved["extra_tasks"]] == \
        list(fixtures.expected_of(payload)["extra_tasks"])
    assert moved["approver_role"] == _as004_params(payload)["approver_role"]


def test_every_extra_task_carries_the_rule_it_came_from(r4):
    """每个多出来的任务都说得出「是哪条政策规则要求的」。

    说不出出处的核销任务不该被规划出来 —— `RefundChannelAgent` 拿不到
    `rule_ref` 就直接 failed，这里守的是规划侧那一半。
    """
    obs = _by_case(r4)["RC-R4B"]["observed"]
    assert obs["extra_task_rules"]
    for key, ref in obs["extra_task_rules"].items():
        assert key in obs["extra_tasks"]
        assert ref in obs["matched_rules"], (
            f"核销任务 {key} 的出处 {ref} 不在本次命中的规则里")
        assert ref.startswith("AS-004@")


# ---------------------------------------------------------------------------
# 11. 契约 1：对照束不进缺省的 8 束
# ---------------------------------------------------------------------------
def test_contrast_never_enters_the_default_eight_bundle_set():
    """三组对照**不进** `ALL_SCENARIOS`，目录名也不叫 `scenario-*`。

    缺省证据束恒为 8 束是跨轨冻结口径（`scripts/demo_preflight.sh` 与复赛材料
    都写死了 8），而 `verify.py` 按 `scenario-` 前缀挑核验对象。两条都由这里守着：
    往 `ALL_SCENARIOS` 里加一个数、或者把对照目录改名成 `scenario-*`，本条即红。
    """
    from maos.main import ALL_SCENARIOS, DEFAULT_SCENARIOS

    assert len(ALL_SCENARIOS) == 7 and len(DEFAULT_SCENARIOS) == 7
    for group, _dim, _title in contrast.GROUPS:
        assert group not in {str(n) for n in ALL_SCENARIOS}
        assert not f"contrast-{group}".startswith("scenario-")


# ---------------------------------------------------------------------------
# 12. 灌数据：晋升规则分流
# ---------------------------------------------------------------------------
def test_history_corpus_is_split_by_promotion_rule_not_copied_verbatim():
    """24 条历史案例按 `outcome` 分流，不是照抄语料里的 kind。

    语料里 24 条的 `kind` 全是 `history_case`（数据侧只记「这是一条历史案例」）。
    落库时按「外部结果明不明确」分流，与 `guardrails.classify_case` 同一份口径。
    """
    from maos.kb import experiment

    raw = experiment._checked_rows(
        experiment.load_corpus("history/history_cases.json"), "kb_doc", kb.DOC_COLUMNS)
    assert {r["kind"] for r in raw} == {kb.KIND_HISTORY_CASE}, "语料侧的 kind 变了"
    want_failed = sum(1 for r in raw if r["outcome"] == kb.OUTCOME_FAILED)

    store = SqliteStore()
    store.init_schema()
    counted = fixtures.seed_history_kb(store)
    assert counted[kb.KIND_FAILURE_HINT] == want_failed
    assert counted[kb.KIND_HISTORY_CASE] == len(raw) - want_failed
    assert sum(counted.values()) == len(raw)


# ---------------------------------------------------------------------------
def _as004_params(payload: dict) -> dict:
    """从 case json 里取 `AS-004@v1` 的 body 参数 —— 断言里的角色名与标题都从这里来。"""
    row = next(r for r in payload["policy_rule"]
               if r["rule_no"] == "AS-004" and r["version"] == 1)
    return json.loads(row["body"])
