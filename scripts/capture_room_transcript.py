#!/usr/bin/env python3
"""从 Matrix client-server API 真实房间历史原子追加 transcript。

两步使用，边界文件应放在仓库外或 ``work/``：

    python3 scripts/capture_room_transcript.py mark --boundary-out work/p8-boundary.json
    python3 scripts/capture_room_transcript.py append \
      --boundary-file work/p8-boundary.json --transcript evidence/room/transcript.md

token 只走 Authorization 请求头；脚本从不打印配置值，写前再用真 token 做哨兵反查。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class CaptureError(RuntimeError):
    pass


class _RejectRedirectHandler(HTTPRedirectHandler):
    """拒绝全部 30x，避免 Authorization 被 urllib 复制到重定向目标。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise CaptureError(f"Matrix /messages 返回 HTTP {code} 重定向；为保护 token 已拒绝")


_NO_REDIRECT_OPENER = build_opener(_RejectRedirectHandler())


def _open_request(request: Request, timeout: float):
    """单独包一层便于测试首跳请求，同时强制使用拒绝重定向的 opener。"""
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_env() -> dict[str, str]:
    names = ("MATRIX_HOMESERVER", "MATRIX_TOKEN", "MATRIX_ROOM_ID")
    values = {name: (os.environ.get(name) or "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise CaptureError(f"缺 Matrix 环境变量：{', '.join(missing)}")
    return values


def _request_json(config: dict[str, str], *, limit: int,
                  from_token: str | None = None) -> dict:
    query = {"dir": "b", "limit": str(limit)}
    if from_token:
        query["from"] = from_token
    base = config["MATRIX_HOMESERVER"].rstrip("/")
    room_segment = quote(config["MATRIX_ROOM_ID"], safe="")
    url = (f"{base}/_matrix/client/v3/rooms/{room_segment}/messages?"
           f"{urlencode(query)}")
    request = Request(
        url, headers={"Authorization": f"Bearer {config['MATRIX_TOKEN']}"})
    with _open_request(request, timeout=30) as response:      # URL 来自显式 env
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("chunk"), list):
        raise CaptureError("Matrix /messages 响应缺少 chunk 数组")
    for event in payload["chunk"]:
        if (not isinstance(event, dict)
                or not isinstance(event.get("event_id"), str)
                or not event["event_id"]):
            raise CaptureError("Matrix /messages 返回缺 event_id 的事件；拒绝采集不完整历史")
    return payload


def events_after_boundary(newest_first: list[dict], boundary_event_id: str) -> list[dict]:
    """截取边界之后的消息事件，并转换为时间正序。"""
    newer: list[dict] = []
    found = False
    for event in newest_first:
        if event.get("event_id") == boundary_event_id:
            found = True
            break
        newer.append(event)
    if not found:
        raise CaptureError(f"房间历史中找不到边界 event_id {boundary_event_id}")
    return [
        event for event in reversed(newer)
        if event.get("type") == "m.room.message"
        and isinstance((event.get("content") or {}).get("body"), str)
    ]


def _fetch_after_boundary(config: dict[str, str], boundary_event_id: str, *,
                          limit: int = 1000, max_pages: int = 20) -> list[dict]:
    newest_first: list[dict] = []
    seen_event_ids: set[str] = set()
    seen_cursors: set[str] = set()
    from_token: str | None = None
    for _ in range(max_pages):
        page = _request_json(config, limit=limit, from_token=from_token)
        chunk = page["chunk"]
        for event in chunk:
            event_id = str(event.get("event_id") or "")
            if event_id and event_id in seen_event_ids:
                continue
            if event_id:
                seen_event_ids.add(event_id)
            newest_first.append(event)
        if boundary_event_id in seen_event_ids:
            return events_after_boundary(newest_first, boundary_event_id)
        next_token = page.get("end")
        if not next_token:
            break
        next_cursor = str(next_token)
        if next_cursor in seen_cursors:
            raise CaptureError("Matrix /messages 分页游标出现循环；拒绝继续采集")
        seen_cursors.add(next_cursor)
        from_token = next_cursor
    raise CaptureError(
        f"翻页 {max_pages} 次仍找不到边界 event_id {boundary_event_id}；拒绝猜采集窗口")


def _redact_body(body: str) -> str:
    out = body
    for name in ("MATRIX_HOMESERVER", "MATRIX_ROOM_ID"):
        value = (os.environ.get(name) or "").strip()
        if value:
            out = out.replace(value, "<redacted>")
    return out


def render_section(events: list[dict], *, start_number: int, boundary_event_id: str,
                   captured_at: str, git_sha: str) -> str:
    lines = [
        "## P8 退款核心链（`--case refund-s7b`）",
        "",
        f"采集时间：`{captured_at}`  ",
        f"git sha：`{git_sha}`  ",
        f"边界 event_id：`{boundary_event_id}`  ",
        "来源：Matrix client-server API `/rooms/<redacted>/messages?dir=b`，非 stdout。",
        "",
    ]
    for number, event in enumerate(events, start=start_number):
        content = event.get("content") or {}
        msgtype = content.get("msgtype") or "m.text"
        sender = event.get("sender") or "<unknown>"
        millis = int(event.get("origin_server_ts") or 0)
        timestamp = datetime.fromtimestamp(millis / 1000, timezone.utc).isoformat()
        body = _redact_body(str(content.get("body") or ""))
        longest_run = max((len(run) for run in re.findall(r"`+", body)), default=0)
        fence = "`" * max(5, longest_run + 1)
        lines.extend([
            f"#### {number}. `{msgtype}` — {sender} — {timestamp}",
            "",
            fence,
            body,
            fence,
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def _next_number(transcript: str) -> int:
    numbers = [int(value) for value in re.findall(r"^#### (\d+)\.", transcript, re.M)]
    return max(numbers, default=0) + 1


def _captured_boundaries(transcript: str) -> set[str]:
    """只认 fenced block 之外的生成元数据，避免消息正文伪装成边界。"""
    found: set[str] = set()
    fence = ""
    pattern = re.compile(r"^边界 event_id：`([^`]+)`  $")
    for line in transcript.splitlines():
        stripped = line.strip()
        if fence:
            if stripped == fence:
                fence = ""
            continue
        fence_match = re.fullmatch(r"`{3,}", stripped)
        if fence_match:
            fence = fence_match.group(0)
            continue
        match = pattern.match(line)
        if match:
            found.add(match.group(1))
    return found


def append_section_atomic(path: Path, section: str, *, boundary_event_id: str) -> None:
    lock_key = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:24]
    lock = Path(tempfile.gettempdir()) / f"maos-p8-transcript-{lock_key}.lock"
    lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(lock_fd)
        raise CaptureError(f"transcript 正被另一采集进程写入：{lock}") from exc

    temporary: Path | None = None
    try:
        old = path.read_text(encoding="utf-8")
        if boundary_event_id in _captured_boundaries(old):
            raise CaptureError(f"边界 event_id {boundary_event_id} 已采集，拒绝重复追加")

        section_numbers = [
            int(value) for value in re.findall(r"^#### (\d+)\.", section, re.M)]
        expected_number = _next_number(old)
        if section_numbers and section_numbers[0] != expected_number:
            raise CaptureError(
                f"transcript 已被并发更新：本段从 {section_numbers[0]} 编号，"
                f"当前应从 {expected_number} 编号；拒绝覆盖")

        combined = old.rstrip() + "\n\n---\n\n" + section
        token = (os.environ.get("MATRIX_TOKEN") or "").strip()
        if token and token in combined:
            raise CaptureError("transcript 命中 MATRIX_TOKEN 哨兵，拒绝落盘")

        fd, name = tempfile.mkstemp(
            prefix=path.name + ".tmp.", dir=path.parent)
        os.close(fd)
        temporary = Path(name)
        temporary.write_text(combined, encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _latest_event_id(config: dict[str, str], *, limit: int = 1,
                     max_pages: int = 20) -> str:
    """取最新事件；空页只要 cursor 继续推进就继续翻。"""
    from_token: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(max_pages):
        page = _request_json(config, limit=limit, from_token=from_token)
        if page["chunk"]:
            return str(page["chunk"][0]["event_id"])
        next_token = page.get("end")
        if not next_token:
            break
        next_cursor = str(next_token)
        if next_cursor in seen_cursors:
            raise CaptureError("Matrix /messages 分页游标出现循环；无法建立采集边界")
        seen_cursors.add(next_cursor)
        from_token = next_cursor
    raise CaptureError("房间历史为空，无法建立采集边界")


def _git_sha() -> str:
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    return f"{sha}-dirty" if dirty else sha


def _write_boundary(path: Path, event_id: str) -> None:
    payload = json.dumps(
        {"boundary_event_id": event_id, "captured_at": _now()},
        ensure_ascii=False, indent=2,
    ) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从真 Matrix 房间历史标边界并原子追加 evidence/room/transcript.md")
    sub = parser.add_subparsers(dest="action", required=True)
    mark = sub.add_parser("mark", help="记录新一轮运行之前的最新 event_id")
    mark.add_argument("--boundary-out", required=True)
    append = sub.add_parser("append", help="追加边界之后的真实房间消息")
    append.add_argument("--boundary-file", required=True)
    append.add_argument("--transcript", required=True)
    append.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args(argv)

    try:
        config = _required_env()
        if args.action == "mark":
            event_id = _latest_event_id(config)
            _write_boundary(Path(args.boundary_out), event_id)
            print(f"boundary recorded: {event_id}")
            return 0

        boundary_data = json.loads(Path(args.boundary_file).read_text(encoding="utf-8"))
        boundary = str(boundary_data.get("boundary_event_id") or "")
        if not boundary:
            raise CaptureError("边界文件缺 boundary_event_id")
        events = _fetch_after_boundary(config, boundary, limit=args.limit)
        if not events:
            raise CaptureError("边界之后尚无房间消息；拒绝用空窗口消费 boundary")
        transcript = Path(args.transcript)
        old = transcript.read_text(encoding="utf-8")
        section = render_section(
            events, start_number=_next_number(old), boundary_event_id=boundary,
            captured_at=_now(), git_sha=_git_sha(),
        )
        append_section_atomic(transcript, section, boundary_event_id=boundary)
        print(f"appended {len(events)} room messages to {transcript}")
        return 0
    except Exception as exc:                              # noqa: BLE001
        print(f"capture failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
