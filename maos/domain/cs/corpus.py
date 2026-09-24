"""客服话术库的装载（p12 契约 §1.4 T168 / §1.5）。

话术库是生成物：``scripts/gen_cs_kb.py`` 确定性地产出 ``scenarios/cs/kb/cs_scripts.json``，
本模块只管「读进来、校验列清单、灌进 kb_doc」。检索在 ``maos.domain.cs.scripts``。

**列清单逐键校验，不靠位置对齐**：``kb_doc`` 的列清单是 ``kb.DOC_COLUMNS``，语料行的键
多一个少一个都当场抛 —— 少键时 ``upsert_doc`` 会把缺的列静默写成 NULL，多键时多出来的
那一列静默丢掉，两种都不报错（口径同 ``maos/tests/test_kb_corpus.py`` 的列清单守卫）。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from maos import kb
from maos.kb import retriever

#: 话术库产物。生成器是 scripts/gen_cs_kb.py，改内容改生成器，不手改这份 JSON。
CORPUS_PATH = (pathlib.Path(__file__).resolve().parents[3]
               / "scenarios" / "cs" / "kb" / "cs_scripts.json")


def load_corpus(path: Any = CORPUS_PATH) -> list[dict]:
    """读话术库，返回 kb_doc 行（每行的键 == ``kb.DOC_COLUMNS``）。

    文件形状是 ``{_note, _provenance, kb_doc: [...]}``。``kb_doc`` 不是数组、某一行不是
    对象、某一行的键与 ``kb.DOC_COLUMNS`` 不相等（缺键或多键），一律抛 ``ValueError``。
    """
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    rows = payload.get("kb_doc") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"{path}: 缺 kb_doc 数组")
    expected = set(kb.DOC_COLUMNS)
    out: list[dict] = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{path}: kb_doc[{idx}] 不是对象")
        keys = set(row)
        if keys != expected:
            raise ValueError(
                f"{path}: kb_doc[{idx}]（{row.get('doc_id')!r}）的列与 kb.DOC_COLUMNS 不一致："
                f"多 {sorted(keys - expected)}，缺 {sorted(expected - keys)}")
        out.append(dict(row))
    return out


def seed_cs_kb(store: Any, *, tenant_id: str | None = None) -> int:
    """把话术库 upsert 进 ``kb_doc``，返回写了几条。幂等：重跑是同样的行。

    ``tenant_id`` 给了就只写该租户的行（语料里没有这个租户就写 0 条 —— 不把 tnt-demo
    的话术改个租户号灌给别人，那是替别的租户做了内容决定）；不给就整份写。

    向量在装载时现算（语料里 ``embedding`` 恒为 null），口径同
    ``maos.kb.experiment.seed_process_kb``：落库那一刻按当时的嵌入实现算，
    PG 后端的向量加速列靠这一列派生。
    """
    kb.ensure_schema(store)
    written = 0
    for row in load_corpus():
        if tenant_id is not None and row["tenant_id"] != tenant_id:
            continue
        kb.upsert_doc(store, {
            **row,
            "embedding": retriever.embed(f"{row['title']} {row['body']}"),
        })
        written += 1
    return written
