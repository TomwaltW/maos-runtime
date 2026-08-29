"""``maos/core/store.py`` 三处审计缺陷的回归（T3 轨，出处 P2-6 / P2-7 / P2-8）。

三条的共性是**失败时不报错**，所以事后极难发现，只能用测试钉住：

1. **P2-6 ``update_task`` 的列名直接拼进 SQL**。占位符只管值、管不了标识符，
   所以列名必须先过白名单。当前调用点的字段名都是硬编码的，尚不可从外部触发 ——
   但 ``update_task(**fields)`` 的签名是开放的，只要有一处把外部键透传成 kwargs
   就成灾。白名单外的键**直接抛**而不是静默丢弃：丢弃会把「字段名写错了」
   伪装成「更新成功但没生效」。
2. **P2-7 ``claim_idempotency`` 把所有 ``IntegrityError`` 都当重复投递**。
   冲突若不是这个 key 引起的，回查得到 ``None``，``dict(None)`` 抛 ``TypeError``。
   幂等闸门以一个**误导性的类型错误**炸掉，排查会一头扎进幂等逻辑，而 bug 在别处。
3. **P2-8 ``list_knowledge`` 不转义 LIKE 通配符**。``keyword="%"`` 静默退化成
   全表扫描 —— 「看起来检索命中了」比「返回空」更坏，这些结果是 Manager 的规划输入。
   要的是**转义**不是过滤：含字面 ``%`` / ``_`` / ``\\`` 的知识仍然要能被搜到，
   把这两个字符从关键词里删掉只是把一个 bug 换成另一个。
"""

from __future__ import annotations

import sqlite3

import pytest

from maos.contracts.events import new_id
from maos.core.store import SqliteStore

# task 表的全部列名。与 init_schema 的建表语句是两份独立来源，故意的：
# 这里写死，白名单那边走 PRAGMA，两边对不上就说明 schema 动过了（铁律 2 禁改）。
_TASK_COLUMNS = frozenset({
    "task_id", "plan_id", "trace_id", "role", "title", "state", "attempt",
    "max_attempts", "risk_level", "effect_risk", "depends_on", "inputs",
    "acceptance", "findings", "worker_id", "last_error", "created_at", "updated_at",
})

# 每个列各来一个合法样例值，用来证明白名单没有把正常路径堵死。
# 不含 task_id：它是 update_task 的位置参数，签名就挡住了，永远到不了 **fields。
_LEGAL_VALUES = {
    "plan_id": "plan-x",
    "trace_id": "tr-2",
    "role": "review",
    "title": "新标题",
    "state": "RUNNING",
    "attempt": 1,
    "max_attempts": 5,
    "risk_level": "H",
    "effect_risk": "M",
    "depends_on": ["t0"],
    "inputs": {"a": 1},
    "acceptance": ["ok"],
    "findings": ["f"],
    "worker_id": "w-1",
    "last_error": "boom",
    "created_at": "2026-01-01T00:00:00+00:00",
    "updated_at": "2026-01-01T00:00:00+00:00",
}


def _store() -> tuple[SqliteStore, str]:
    s = SqliteStore(":memory:")
    s.init_schema()
    pid = new_id("plan")
    s.insert_plan({"plan_id": pid, "trace_id": new_id("trace"), "goal": "g", "state": "PENDING"})
    return s, pid


def _with_task() -> tuple[SqliteStore, str]:
    s, pid = _store()
    s.insert_task({
        "task_id": "t1", "plan_id": pid, "trace_id": "tr", "role": "coding",
        "title": "原标题", "state": "PENDING", "depends_on": [], "inputs": {},
        "acceptance": [], "findings": [],
    })
    return s, pid


def _knowledge(s: SqliteStore, pid: str, kid: str, title: str, body: str = "正文") -> None:
    s.insert_knowledge({
        "id": kid, "plan_id": pid, "kind": "note", "title": title, "body": body, "tags": [],
    })


# --- P2-6 update_task 字段白名单 ---------------------------------------

def test_update_task_rejects_injected_column_name() -> None:
    """注入形状的键必须抛，且一个字段都不许被改。

    修复前：一次调用改了两个字段 —— 注入的 ``title='PWNED'`` 生效，
    传入的值 ``IGNORED`` 落到了 ``state`` 上。
    """
    s, _ = _with_task()
    with pytest.raises(ValueError, match="task 表以外的字段名"):
        s.update_task("t1", **{"title='PWNED', state": "IGNORED"})
    t = s.get_task("t1")
    assert t["title"] == "原标题"
    assert t["state"] == "PENDING"


def test_update_task_rejects_typo_field_loudly() -> None:
    """拼错的字段名要当场抛，不许静默丢弃 —— 否则「没生效」会伪装成「成功了」。"""
    s, _ = _with_task()
    with pytest.raises(ValueError, match="stat"):
        s.update_task("t1", stat="DONE")
    assert s.get_task("t1")["state"] == "PENDING"


def test_update_task_rejects_mixed_batch_atomically() -> None:
    """合法键与非法键混在一起时整批拒绝，不许「合法的那半边写进去了」。"""
    s, _ = _with_task()
    with pytest.raises(ValueError):
        s.update_task("t1", state="RUNNING", bogus="x")
    assert s.get_task("t1")["state"] == "PENDING"


def test_whitelist_covers_every_task_column() -> None:
    """白名单必须和 task 表的列一一对上 —— 漏一列就会让某次合法更新突然开始抛异常。"""
    s, _ = _with_task()
    assert s._task_columns() == _TASK_COLUMNS


def test_update_task_accepts_every_legal_column() -> None:
    """逐列做一次合法更新，证明白名单没把正常路径堵死。"""
    s, _ = _with_task()
    assert set(_LEGAL_VALUES) == _TASK_COLUMNS - {"task_id"}
    for col, val in _LEGAL_VALUES.items():
        s.update_task("t1", **{col: val})


def test_update_task_normal_path_unchanged() -> None:
    """最常走的那条路径（状态迁移 + JSON 列）行为不变。"""
    s, _ = _with_task()
    s.update_task("t1", state="RUNNING", findings=["f1"], attempt=2)
    t = s.get_task("t1")
    assert (t["state"], t["findings"], t["attempt"]) == ("RUNNING", ["f1"], 2)


def test_update_task_empty_fields_is_noop() -> None:
    s, _ = _with_task()
    s.update_task("t1")
    assert s.get_task("t1")["state"] == "PENDING"


def test_update_task_without_schema_reports_missing_table() -> None:
    """没建表时要报 SQLite 的 no such table，不许被「合法列为 []」盖掉。

    白名单是从 ``PRAGMA table_info`` 取的，表不存在时它返回空集。若照常校验，
    报错就成了误导性的「字段名不合法」—— 那正是 P2-7 那类把排查引到错地方的
    错误，修它的同时不能自己再犯一次。
    """
    s = SqliteStore(":memory:")  # 故意不 init_schema
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        s.update_task("t1", state="RUNNING")


def test_task_columns_cache_not_poisoned_by_empty_pragma() -> None:
    """建表前查过一次白名单，建表后必须能拿到真列名（空集不许进缓存）。"""
    s = SqliteStore(":memory:")
    assert s._task_columns() == frozenset()
    s.init_schema()
    assert s._task_columns() == _TASK_COLUMNS


# --- P2-7 claim_idempotency 不吞非目标 key 的冲突 -----------------------

def test_claim_idempotency_reraises_unrelated_integrity_error() -> None:
    """冲突不是这个 key 引起的时候，抛出的必须是原始的完整性错误。

    修复前：``TypeError: 'NoneType' object is not iterable`` —— 真正的
    ``NOT NULL constraint failed`` 被吞掉了。
    """
    s, _ = _with_task()
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        s.claim_idempotency("brand-new-key", "claim", None)  # type: ignore[arg-type]


def test_claim_idempotency_not_typeerror() -> None:
    """显式钉住「不是 TypeError」这一点 —— 它才是当年把排查带偏的那个症状。"""
    s, _ = _with_task()
    with pytest.raises(sqlite3.IntegrityError):
        s.claim_idempotency("another-new-key", "claim", None)  # type: ignore[arg-type]


def test_claim_idempotency_first_call_passes() -> None:
    s, _ = _with_task()
    assert s.claim_idempotency("k1", "claim", "t1") is None


def test_claim_idempotency_duplicate_returns_previous_record() -> None:
    s, _ = _with_task()
    assert s.claim_idempotency("k1", "claim", "t1") is None
    s.finish_idempotency("k1", {"ok": True})
    again = s.claim_idempotency("k1", "claim", "t1")
    assert again is not None
    assert again["op"] == "claim"
    assert again["task_id"] == "t1"
    assert again["outcome"] == {"ok": True}


# --- P2-8 list_knowledge 的 LIKE 通配符转义 -----------------------------

def test_list_knowledge_percent_is_literal_not_wildcard() -> None:
    """修复前：库里 2 条，``keyword="%"`` 命中 2 条（静默全表）。"""
    s, pid = _store()
    _knowledge(s, pid, "k1", "标题1", "正文1")
    _knowledge(s, pid, "k2", "标题2", "正文2")
    assert len(s.list_knowledge()) == 2
    assert s.list_knowledge(keyword="%") == []


def test_list_knowledge_underscore_is_literal_not_wildcard() -> None:
    """``_`` 匹配任意单字符，中文关键词里少见，英文规则编号里常见（AS_001）。"""
    s, pid = _store()
    _knowledge(s, pid, "k1", "标题1", "正文1")
    _knowledge(s, pid, "k2", "标题2", "正文2")
    assert s.list_knowledge(keyword="_") == []


def test_list_knowledge_finds_literal_percent() -> None:
    """转义的是通配语义，不是把 ``%`` 整个禁掉：含字面 ``%`` 的知识仍要能查到。"""
    s, pid = _store()
    _knowledge(s, pid, "k1", "折扣 50% 规则")
    _knowledge(s, pid, "k2", "折扣 5099 规则")  # 不转义的话这条会被 50%~ 一起捞出来
    hits = s.list_knowledge(keyword="50%")
    assert [h["id"] for h in hits] == ["k1"]


def test_list_knowledge_finds_literal_underscore() -> None:
    s, pid = _store()
    _knowledge(s, pid, "k1", "规则 AS_001")
    _knowledge(s, pid, "k2", "规则 AS1001")  # 不转义的话 _ 会匹配上这里的 1
    hits = s.list_knowledge(keyword="AS_001")
    assert [h["id"] for h in hits] == ["k1"]


def test_list_knowledge_finds_literal_backslash() -> None:
    """转义符自身也得转，否则关键词里的反斜杠会吃掉它后面那个字符。"""
    s, pid = _store()
    _knowledge(s, pid, "k1", "路径 a\\b")
    _knowledge(s, pid, "k2", "路径 ab")
    hits = s.list_knowledge(keyword="a\\b")
    assert [h["id"] for h in hits] == ["k1"]


def test_list_knowledge_matches_body_too() -> None:
    s, pid = _store()
    _knowledge(s, pid, "k1", "标题", "正文里有 100% 覆盖率")
    _knowledge(s, pid, "k2", "标题", "正文里有 100 分")
    hits = s.list_knowledge(keyword="100%")
    assert [h["id"] for h in hits] == ["k1"]


def test_list_knowledge_plain_keyword_unchanged() -> None:
    """普通关键词的检索行为一个字不变。"""
    s, pid = _store()
    _knowledge(s, pid, "k1", "标题1", "正文1")
    _knowledge(s, pid, "k2", "标题2", "正文2")
    assert [h["id"] for h in s.list_knowledge(keyword="标题1")] == ["k1"]
    assert len(s.list_knowledge(keyword="标题")) == 2


def test_list_knowledge_keyword_with_tags_still_intersects() -> None:
    """keyword 与 tags 组合的老行为不变。"""
    s, pid = _store()
    s.insert_knowledge({"id": "k1", "plan_id": pid, "kind": "note",
                        "title": "标题1", "body": "b", "tags": ["a"]})
    s.insert_knowledge({"id": "k2", "plan_id": pid, "kind": "note",
                        "title": "标题2", "body": "b", "tags": ["b"]})
    assert [h["id"] for h in s.list_knowledge(keyword="标题", tags=["a"])] == ["k1"]
