"""Matrix 房间 transcript 必须从历史 API 响应自动追加，不能手抄。"""

from __future__ import annotations

import importlib
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
