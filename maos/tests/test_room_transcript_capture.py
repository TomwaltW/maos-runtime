"""Matrix 房间 transcript 必须从历史 API 响应自动追加，不能手抄。"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request

import pytest


def _event(event_id: str, sender: str, body: str, ts: int) -> dict:
    return {
        "event_id": event_id,
        "type": "m.room.message",
        "sender": sender,
        "origin_server_ts": ts,
        "content": {"msgtype": "m.text", "body": body},
    }


def test_append_uses_only_events_after_boundary_in_chronological_order(
        monkeypatch, tmp_path):
    capture = importlib.import_module("scripts.capture_room_transcript")
    boundary = "$boundary"
    newest_first = [
        _event("$reply", "@maos-bot:maos.local", "已批准 task-s7b-finance", 3000),
        _event("$command", "@boss:maos.local", "/approve task-s7b-finance", 2000),
        _event(boundary, "@maos-bot:maos.local", "旧窗口最后一条", 1000),
        _event("$old", "@maos-bot:maos.local", "不属于本轮", 500),
    ]
    transcript = tmp_path / "transcript.md"
    transcript.write_text(
        "# generated at old from deadbeef\n\n"
        "#### 41. `m.notice` — @old — 2026-08-29T00:00:00+00:00\n",
        encoding="utf-8",
    )
    token = "MATRIX_TOKEN_SENTINEL_TRANSCRIPT"
    monkeypatch.setenv("MATRIX_TOKEN", token)

    selected = capture.events_after_boundary(newest_first, boundary)
    section = capture.render_section(
        selected, start_number=42, boundary_event_id=boundary,
        captured_at="2026-09-01T02:03:04+00:00", git_sha="abc123-dirty",
    )
    capture.append_section_atomic(transcript, section, boundary_event_id=boundary)
    body = transcript.read_text(encoding="utf-8")

    assert body.index("/approve task-s7b-finance") < body.index("已批准 task-s7b-finance")
    assert "#### 42." in body and "#### 43." in body
    assert "不属于本轮" not in body and "旧窗口最后一条" not in body
    assert "边界 event_id：`$boundary`" in body
    assert "abc123-dirty" in body
    assert token not in body
    assert not list(tmp_path.glob("*.tmp"))


def test_missing_boundary_and_token_leak_never_modify_target(monkeypatch, tmp_path):
    capture = importlib.import_module("scripts.capture_room_transcript")
    with pytest.raises(capture.CaptureError):
        capture.events_after_boundary([_event("$new", "@boss:x", "hello", 2)], "$gone")

    transcript = tmp_path / "transcript.md"
    original = "# generated at old from deadbeef\n"
    transcript.write_text(original, encoding="utf-8")
    token = "MATRIX_TOKEN_MUST_NOT_LAND"
    monkeypatch.setenv("MATRIX_TOKEN", token)

    with pytest.raises(capture.CaptureError):
        capture.append_section_atomic(
            transcript, f"leak={token}\n", boundary_event_id="$token-test")
    assert transcript.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*.tmp"))


def test_matrix_token_uses_authorization_header_not_query(monkeypatch):
    capture = importlib.import_module("scripts.capture_room_transcript")
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def read(self) -> bytes:
            return b'{"chunk": [], "end": "e1"}'

    def fake_open_request(request, timeout):
        seen["url"] = request.full_url
        seen["authorization"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(capture, "_open_request", fake_open_request)
    config = {
        "MATRIX_HOMESERVER": "https://matrix.example.org",
        "MATRIX_TOKEN": "secret-token-value",
        "MATRIX_ROOM_ID": "!room:example.org",
    }
    capture._request_json(config, limit=3)

    assert "secret-token-value" not in seen["url"]
    assert "access_token" not in seen["url"]
    assert seen["authorization"] == "Bearer secret-token-value"


def test_redirects_are_rejected_before_authorization_can_be_forwarded():
    capture = importlib.import_module("scripts.capture_room_transcript")
    request = Request(
        "https://matrix.example.org/_matrix/client/v3/rooms/x/messages",
        headers={"Authorization": "Bearer secret-token-value"},
    )

    with pytest.raises(capture.CaptureError, match="重定向"):
        capture._RejectRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, "https://attacker.example/steal")


def test_room_id_is_encoded_as_one_opaque_path_segment(monkeypatch):
    capture = importlib.import_module("scripts.capture_room_transcript")
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def read(self) -> bytes:
            return b'{"chunk": []}'

    def fake_open_request(request, timeout):
        seen["url"] = request.full_url
        return Response()

    monkeypatch.setattr(capture, "_open_request", fake_open_request)
    room_id = "!room/with?query#fragment:☃"
    capture._request_json({
        "MATRIX_HOMESERVER": "https://matrix.example.org",
        "MATRIX_TOKEN": "secret-token-value",
        "MATRIX_ROOM_ID": room_id,
    }, limit=3)

    assert f"/rooms/{quote(room_id, safe='')}/messages?" in seen["url"]
    assert room_id not in seen["url"]


def test_empty_page_with_end_token_continues_until_boundary(monkeypatch):
    capture = importlib.import_module("scripts.capture_room_transcript")
    boundary = "$boundary"
    pages = iter([
        {"chunk": [], "end": "e1"},
        {"chunk": [_event("$new", "@boss:x", "/approve task-x", 2)], "end": "e2"},
        {"chunk": [_event(boundary, "@bot:x", "old", 1)]},
    ])
    seen_from = []

    def fake_request(config, *, limit, from_token=None):
        seen_from.append(from_token)
        return next(pages)

    monkeypatch.setattr(capture, "_request_json", fake_request)
    selected = capture._fetch_after_boundary({}, boundary, max_pages=3)

    assert [event["event_id"] for event in selected] == ["$new"]
    assert seen_from == [None, "e1", "e2"]


def test_overlapping_pages_are_deduplicated_by_event_id(monkeypatch):
    capture = importlib.import_module("scripts.capture_room_transcript")
    boundary = "$boundary"
    duplicate = _event("$new", "@boss:x", "/approve task-x", 2)
    pages = iter([
        {"chunk": [duplicate], "end": "e1"},
        {"chunk": [duplicate, _event(boundary, "@bot:x", "old", 1)]},
    ])
    monkeypatch.setattr(
        capture, "_request_json",
        lambda config, *, limit, from_token=None: next(pages),
    )

    selected = capture._fetch_after_boundary({}, boundary, max_pages=2)
    assert [event["event_id"] for event in selected] == ["$new"]


def test_repeated_boundary_is_rejected_without_modifying_transcript(tmp_path):
    capture = importlib.import_module("scripts.capture_room_transcript")
    transcript = tmp_path / "transcript.md"
    boundary = "$already-captured"
    original = f"# evidence\n\n边界 event_id：`{boundary}`  \n"
    transcript.write_text(original, encoding="utf-8")

    with pytest.raises(capture.CaptureError, match="已采集"):
        capture.append_section_atomic(
            transcript, "## duplicate\n", boundary_event_id=boundary)

    assert transcript.read_text(encoding="utf-8") == original


def test_boundary_marker_inside_message_body_does_not_trigger_idempotency(tmp_path):
    capture = importlib.import_module("scripts.capture_room_transcript")
    transcript = tmp_path / "transcript.md"
    boundary = "$body-only"
    body = f"message opens a fence\n`````\n边界 event_id：`{boundary}`  \n`````"
    previous = capture.render_section(
        [_event("$previous", "@user:x", body, 1)], start_number=1,
        boundary_event_id="$different", captured_at="2026-09-01T00:00:00+00:00",
        git_sha="abc",
    )
    transcript.write_text("# evidence\n\n" + previous, encoding="utf-8")
    section = capture.render_section(
        [], start_number=2, boundary_event_id=boundary,
        captured_at="2026-09-01T00:00:00+00:00", git_sha="abc",
    )

    capture.append_section_atomic(
        transcript, section, boundary_event_id=boundary)
    assert transcript.read_text(encoding="utf-8").count(
        f"边界 event_id：`{boundary}`  ") == 2


def test_stale_section_number_is_rejected_after_another_append(tmp_path):
    capture = importlib.import_module("scripts.capture_room_transcript")
    transcript = tmp_path / "transcript.md"
    transcript.write_text("# evidence\n", encoding="utf-8")
    event = _event("$one", "@boss:x", "first", 1)
    first = capture.render_section(
        [event], start_number=1, boundary_event_id="$b1",
        captured_at="2026-09-01T00:00:00+00:00", git_sha="abc",
    )
    stale = capture.render_section(
        [_event("$two", "@boss:x", "second", 2)], start_number=1,
        boundary_event_id="$b2", captured_at="2026-09-01T00:00:01+00:00",
        git_sha="abc",
    )
    capture.append_section_atomic(transcript, first, boundary_event_id="$b1")
    after_first = transcript.read_bytes()

    with pytest.raises(capture.CaptureError, match="并发更新"):
        capture.append_section_atomic(transcript, stale, boundary_event_id="$b2")
    assert transcript.read_bytes() == after_first
    assert not list(tmp_path.glob("*.capture.lock"))


def test_mark_continues_across_empty_page(monkeypatch):
    capture = importlib.import_module("scripts.capture_room_transcript")
    pages = iter([
        {"chunk": [], "end": "e1"},
        {"chunk": [_event("$latest", "@bot:x", "latest", 3)]},
    ])
    seen_from = []

    def fake_request(config, *, limit, from_token=None):
        seen_from.append(from_token)
        return next(pages)

    monkeypatch.setattr(capture, "_request_json", fake_request)
    assert capture._latest_event_id({}) == "$latest"
    assert seen_from == [None, "e1"]


def test_request_rejects_event_without_event_id(monkeypatch):
    capture = importlib.import_module("scripts.capture_room_transcript")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def read(self) -> bytes:
            return b'{"chunk": [{"type": "m.room.message"}]}'

    monkeypatch.setattr(capture, "_open_request", lambda request, timeout: Response())
    with pytest.raises(capture.CaptureError, match="缺 event_id"):
        capture._request_json({
            "MATRIX_HOMESERVER": "https://matrix.example.org",
            "MATRIX_TOKEN": "token",
            "MATRIX_ROOM_ID": "!room:x",
        }, limit=1)


def test_append_refuses_empty_window_without_consuming_boundary(monkeypatch, tmp_path):
    capture = importlib.import_module("scripts.capture_room_transcript")
    boundary_file = tmp_path / "boundary.json"
    boundary_file.write_text(
        '{"boundary_event_id": "$empty"}\n', encoding="utf-8")
    transcript = tmp_path / "transcript.md"
    original = "# evidence\n"
    transcript.write_text(original, encoding="utf-8")

    monkeypatch.setattr(capture, "_required_env", lambda: {})
    monkeypatch.setattr(capture, "_fetch_after_boundary", lambda *args, **kwargs: [])

    assert capture.main([
        "append", "--boundary-file", str(boundary_file),
        "--transcript", str(transcript),
    ]) == 2
    assert transcript.read_text(encoding="utf-8") == original


# ==========================================================================
# 真跑日采集演练 —— 离线夹具驱动 mark -> append 全程
# ==========================================================================
#
# 上面那些测试守的是**脚本自身**：边界对不对、追加原不原子、token 漏不漏。
# 它们全绿，却回答不了真跑日真正要问的那个问题：
#
#   **采下来的东西，够不够填 `docs/demo-script.md:420` 与
#   `docs/defense-brief.md:78 / :86` 那两处占位。**
#
# 这一节就是那个问题的守卫。用构造的房间历史（`fixtures/room_transcript_live_run.json`，
# 形状对齐 2026-09-12 的房间口径）把 `mark` -> `append` 全程跑一遍，
# 把产物形状钉死。**不连真房间**：跨轨契约 §E 明写采集演练走离线夹具。
#
# 这一节发现并促成了 `render_section` 的一处改动：段标题此前硬编码
# ``## P8 退款核心链（--case refund-s7b）``，而真跑日采的是 p10 的圆桌 + 真人审批，
# 落盘会写出一句错的出处。现在标题由 `--title` 当场指定。

FIXTURE_LIVE_RUN = (Path(__file__).parent / "fixtures"
                    / "room_transcript_live_run.json")
BOUNDARY_EVENT_ID = "$boundary-p10"

#: 真跑日那一轮要采的四类内容。**这四类就是两处占位的全部诉求** ——
#: 少任何一类，材料里对应那句话就只能继续写「等 9/18 真跑日采集」。
FOUR_KINDS = {
    "真人 /approve 回帖": "$reply-approve",
    "真人 /reject 回帖": "$reply-reject",
    "圆桌五岗卡片": "$seat-finance",
    "四条结果面命令回执": "$reply-resolve",
}


@pytest.fixture()
def live_run_page() -> dict:
    return json.loads(FIXTURE_LIVE_RUN.read_text(encoding="utf-8"))


def _page_at_mark_time(page: dict) -> dict:
    """`mark` 那一刻的房间历史：这一轮还没跑，最新一条就是边界。

    真跑日的时序是 mark -> 房间里跑一轮 -> append，两次看到的历史不是同一份。
    夹具切两刀来复现这个时序，比拿同一份历史跑两次更接近真实。
    """
    chunk = page["chunk"]
    index = next(i for i, event in enumerate(chunk)
                 if event["event_id"] == BOUNDARY_EVENT_ID)
    return {"chunk": chunk[index:], "end": page["end"]}


def _rehearse(capture, monkeypatch, tmp_path, page, *, title=None):
    """离线跑一遍 mark -> append，返回落盘后的 transcript 全文。"""
    current = {"page": _page_at_mark_time(page)}
    monkeypatch.setattr(capture, "_required_env", lambda: {})
    monkeypatch.setattr(capture, "_git_sha", lambda: "c0d8303")
    monkeypatch.setattr(
        capture, "_request_json",
        lambda config, *, limit, from_token=None: current["page"])

    boundary_file = tmp_path / "p10-boundary.json"
    assert capture.main(["mark", "--boundary-out", str(boundary_file)]) == 0
    recorded = json.loads(boundary_file.read_text(encoding="utf-8"))
    assert recorded["boundary_event_id"] == BOUNDARY_EVENT_ID

    # —— 房间里跑了一轮：圆桌五岗 + 真人 /approve 与 /reject + 四条结果面命令 ——
    current["page"] = page

    transcript = tmp_path / "transcript.md"
    transcript.write_text("# generated at old from deadbeef\n", encoding="utf-8")
    argv = ["append", "--boundary-file", str(boundary_file),
            "--transcript", str(transcript)]
    if title is not None:
        argv += ["--title", title]
    assert capture.main(argv) == 0
    return transcript.read_text(encoding="utf-8")


def test_rehearsal_captures_exactly_the_window_after_the_boundary(
        monkeypatch, tmp_path, live_run_page):
    """窗口两端都不许错：边界之前的一条都不进，`m.reaction` 也不进。

    `m.reaction` 单列一条断言：真房间里有人给卡片点个赞就会产生它，而它
    **没有 `content.body`** —— 采进来会渲染成一条空消息，在逐字副本里看着
    像「机器人发了条空白」。
    """
    capture = importlib.import_module("scripts.capture_room_transcript")
    body = _rehearse(capture, monkeypatch, tmp_path, live_run_page)

    assert "上一轮的最后一条" not in body
    assert "圆桌就绪：5 岗" not in body          # 边界那条自己也不进
    assert "m.reaction" not in body
    assert "👍" not in body
    # 夹具 22 条事件 - 边界前那条 - 边界自己 - m.reaction = 19 条落盘
    assert body.count("\n#### ") == 19


def test_rehearsal_keeps_chronological_order_of_the_whole_round(
        monkeypatch, tmp_path, live_run_page):
    """一轮处置的顺序就是它的论证力：命令在前、回帖在后，五岗在放行之前。

    顺序错了的逐字副本比没有更坏 —— 它会让人以为机器人先回帖、审批人后补签。
    """
    capture = importlib.import_module("scripts.capture_room_transcript")
    body = _rehearse(capture, monkeypatch, tmp_path, live_run_page)

    milestones = ["/refund ORD-2026-0007", "【申请受理岗", "【财务执行岗",
                  "/approve RC-ORD-2026-0007", "已放行 · 案子 RC-ORD-2026-0007",
                  "/assign MT-RC-ORD-2026-0007", "/resolve MT-RC-ORD-2026-0007",
                  "/confirm RC-ORD-2026-0007", "/complain RC-ORD-2026-0007",
                  "/reject RC-ORD-2026-0008", "已驳回 · 案子 RC-ORD-2026-0008"]
    positions = [body.index(text) for text in milestones]
    assert positions == sorted(positions), "采下来的顺序与房间里发生的顺序不一致"


def test_rehearsal_covers_all_four_kinds_the_placeholders_need(
        monkeypatch, tmp_path, live_run_page):
    """四类内容一样不少 —— 这是两处占位能不能填的总判据。"""
    capture = importlib.import_module("scripts.capture_room_transcript")
    body = _rehearse(capture, monkeypatch, tmp_path, live_run_page)
    ordered = live_run_page["chunk"]
    by_id = {event["event_id"]: event for event in ordered}

    for kind, event_id in FOUR_KINDS.items():
        first_line = by_id[event_id]["content"]["body"].splitlines()[0]
        assert first_line in body, f"{kind} 没采下来"


def test_rehearsal_carries_the_public_status_line_verbatim(
        monkeypatch, tmp_path, live_run_page):
    """「对客户口径」那一行必须逐字落盘。

    这一条单列，因为它是 `docs/defense-brief.md:86` 的**唯一**出处：那一行只在
    真 Matrix 房间那条路上打得出来（圆桌财务岗的执行段只由
    `maos/ingress/router.py` 调到），`make_case_bundle.py` 走的是预检段，
    所以五束 `roundtable.json` 里一个字都没有。真跑日这一次采不到它，
    材料里那句话就只能继续写「证据等真跑日采集」。
    """
    capture = importlib.import_module("scripts.capture_room_transcript")
    body = _rehearse(capture, monkeypatch, tmp_path, live_run_page)

    assert "对客户口径：已提出退款" in body        # /approve 回帖卡上
    assert "对客户口径：已补偿（未到账）" in body   # /resolve 回执上
    assert "对客户口径：已驳回" in body            # /reject 回帖卡上


def test_rehearsal_keeps_five_seats_distinguishable(
        monkeypatch, tmp_path, live_run_page):
    """五岗得能一岗一岗分得开，否则「五岗合议」在逐字副本里就是一团话。

    代言形态（`_ProxyVoice`）靠正文前缀 `【岗位名 · agent_id】` 区分 ——
    五个岗位账号没配齐时房间走的就是它，而那是真跑日更可能的形态。
    """
    capture = importlib.import_module("scripts.capture_room_transcript")
    body = _rehearse(capture, monkeypatch, tmp_path, live_run_page)

    for seat in ("【申请受理岗 · refund-intake】", "【规则审核岗 · refund-policy】",
                 "【证据核验岗 · refund-evidence】", "【风险反欺诈岗 · refund-risk】",
                 "【财务执行岗 · refund-finance】"):
        assert seat in body
    assert "收口 · approve （RC-ORD-2026-0007）" in body


def test_rehearsal_keeps_multiline_cards_intact_inside_the_fence(
        monkeypatch, tmp_path, live_run_page):
    """多行卡片要整张落盘，不许只留第一行。

    回帖卡是十来行的一张表，而 fence 宽度由正文里最长的一串反引号决定
    （`render_section`）。卡片里出现 ``` 之类的东西时 fence 要跟着变宽，
    否则后半张卡会从代码块里漏出去、在 Markdown 里被重新解析。
    """
    capture = importlib.import_module("scripts.capture_room_transcript")
    body = _rehearse(capture, monkeypatch, tmp_path, live_run_page)

    # /approve 回帖卡的首行与末行都在，中间那几行自然也在
    assert "已放行 · 案子 RC-ORD-2026-0007" in body
    assert "关单：/resolve MT-RC-ORD-2026-0007 <渠道流水号> <线下凭证摘要>" in body
    # 收口卡的框线字符原样保留
    assert "┌─ 收口 · approve" in body and "└ 下一步：" in body


def test_rehearsal_section_title_says_what_was_actually_captured(
        monkeypatch, tmp_path, live_run_page):
    """段标题得说真跑日那一轮，不许再写 P8 / refund-s7b。

    这是本节促成的那处改动的守卫：脚本 2026-09-01 之后一个字没改，
    标题却写死着 P8 的案号 —— 真跑日第一次用它落盘就会写出一句错的出处。
    """
    capture = importlib.import_module("scripts.capture_room_transcript")
    title = "## 2026-09-18 真跑日 · 退款圆桌五岗 + 真人 /approve 与 /reject"
    body = _rehearse(capture, monkeypatch, tmp_path, live_run_page, title=title)

    assert title in body
    assert "P8 退款核心链" not in body
    assert "refund-s7b" not in body


def test_default_section_title_no_longer_claims_to_be_p8(monkeypatch, tmp_path,
                                                         live_run_page):
    """不给 `--title` 时落的是中性标题，不是一句错的出处。"""
    capture = importlib.import_module("scripts.capture_room_transcript")
    body = _rehearse(capture, monkeypatch, tmp_path, live_run_page)

    assert capture.DEFAULT_SECTION_TITLE in body
    assert "P8 退款核心链" not in body and "refund-s7b" not in body


def test_rehearsal_records_provenance_head_for_every_section(
        monkeypatch, tmp_path, live_run_page):
    """每段都要带齐出处三件套：采集时间、git sha、边界 event_id（铁律 3）。"""
    capture = importlib.import_module("scripts.capture_room_transcript")
    body = _rehearse(capture, monkeypatch, tmp_path, live_run_page)

    assert "git sha：`c0d8303`" in body
    assert f"边界 event_id：`{BOUNDARY_EVENT_ID}`" in body
    assert "来源：Matrix client-server API" in body


def test_rehearsal_redacts_homeserver_and_room_id_from_bodies(
        monkeypatch, tmp_path, live_run_page):
    """房间正文里要是出现了 homeserver / room_id，落盘前得抹掉（铁律 6）。"""
    capture = importlib.import_module("scripts.capture_room_transcript")
    monkeypatch.setenv("MATRIX_HOMESERVER", "https://sentinel-hs.example.internal")
    monkeypatch.setenv("MATRIX_ROOM_ID", "!sentinel-room:example.internal")
    page = json.loads(json.dumps(live_run_page))
    for event in page["chunk"]:
        if event["event_id"] == "$reply-confirm":
            event["content"]["body"] += (
                "\n详情 https://sentinel-hs.example.internal/#/room/"
                "!sentinel-room:example.internal")
    body = _rehearse(capture, monkeypatch, tmp_path, page)

    assert "sentinel-hs.example.internal" not in body
    assert "!sentinel-room:example.internal" not in body
    assert "<redacted>" in body


def test_rehearsal_is_idempotent_on_the_same_boundary(
        monkeypatch, tmp_path, live_run_page):
    """真跑日手抖跑第二次 `append`，不许把同一轮采两遍。

    逐字副本里出现两份相同的 `/approve`，评委会问「到底批了几次」。
    """
    capture = importlib.import_module("scripts.capture_room_transcript")
    current = {"page": _page_at_mark_time(live_run_page)}
    monkeypatch.setattr(capture, "_required_env", lambda: {})
    monkeypatch.setattr(capture, "_git_sha", lambda: "c0d8303")
    monkeypatch.setattr(
        capture, "_request_json",
        lambda config, *, limit, from_token=None: current["page"])

    boundary_file = tmp_path / "p10-boundary.json"
    capture.main(["mark", "--boundary-out", str(boundary_file)])
    current["page"] = live_run_page
    transcript = tmp_path / "transcript.md"
    transcript.write_text("# generated at old from deadbeef\n", encoding="utf-8")
    argv = ["append", "--boundary-file", str(boundary_file),
            "--transcript", str(transcript)]

    assert capture.main(argv) == 0
    after_first = transcript.read_text(encoding="utf-8")
    assert capture.main(argv) == 2          # 第二次被边界幂等拦下
    assert transcript.read_text(encoding="utf-8") == after_first

    # 真人**发出**的那条命令只许有一条。整串子串会数到 3 次 ——
    # 财务岗事实卡与收口卡都在正文里引用了同一句 `/approve …`，那是**提示**、
    # 不是有人又批了一次。所以数的是「独占一行、行首无缩进」的那种。
    issued = [line for line in after_first.splitlines()
              if line == "/approve RC-ORD-2026-0007"]
    assert len(issued) == 1
