#!/usr/bin/env python3
"""_NioChannel 三条假设的真房间探针（task-C2）。

**独立脚本，不进 pytest、不进 CI。**它要连真 Synapse，而测试必须能在没有 Synapse
的机器上跑绿 —— 两件事混在一起，测试就成了「有房间才绿」的摆设。

跑法（本机没有 `python` 命令，且要用装了 matrix-nio 的那个解释器）::

    . ~/.maos-matrix/room.env && ~/.maos-matrix/venv/bin/python scripts/matrix_probe.py

对每条假设固定打三行：**判据原文 / 实际请求 / 实际响应**。三条假设错了的症状都是
「降级」而不是「崩」—— 它们不会自己暴露，所以这个脚本存在的意义就是主动去撞。

退出码：0 = 三条都验了；2 = 缺 env（一条都没验）；3 = 有条目没验成。
**缺 env 时绝不返回 0** —— 探针不许在没验的时候装成验过了。

可选 env：
  MATRIX_ROOM_ID_ENCRYPTED  另一个**已加密**的房间 id。只验未加密那一侧等于没验：
                            判据写反的话两侧都会「通过」，然后演示当天 send 全静默失败。
  MAOS_PROBE_LISTEN_SECONDS 假设 ② 等人在 Element 里发言的秒数，默认 20，0 = 跳过。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from hiclaw.matrix_bus import (ENC_CLEAR, ENC_ENCRYPTED, ENC_ERROR,  # noqa: E402
                               NO_HISTORY_FILTER, MatrixBusConfig, encryption_verdict,
                               should_deliver)

REQUIRED = ("MATRIX_HOMESERVER", "MATRIX_USER", "MATRIX_TOKEN", "MATRIX_ROOM_ID")

#: 打印用。任何一行输出都可能被贴进回执或 evidence/，token 一次都不许露（铁律 6）。
REDACTED = "***"


def mask(text: str, token: str) -> str:
    return text.replace(token, REDACTED) if token else text


class Report:
    """收集每条假设的结论，最后统一决定退出码。"""

    def __init__(self) -> None:
        self.verified: list[str] = []
        self.unverified: list[tuple[str, str]] = []

    def ok(self, name: str) -> None:
        self.verified.append(name)

    def skip(self, name: str, why: str) -> None:
        self.unverified.append((name, why))
        print(f"  ✗ 本条未验证：{why}")


def head(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def triple(criterion: str, request: str, response: str) -> None:
    """固定三行回执格式。"""
    print(f"  判据原文 : {criterion}")
    print(f"  实际请求 : {request}")
    print(f"  实际响应 : {response}")


#: 单次房间请求的上限。连不通时 matrix-nio 会自己重试到天荒地老（实测：指向一个
#: 没人监听的端口，whoami 挂了 3 分钟还没回）——探针不许跟着挂住，它是要被人盯着看的。
PROBE_TIMEOUT = float(os.environ.get("MAOS_PROBE_TIMEOUT", "15"))


async def call(coro, timeout: float = PROBE_TIMEOUT):
    """任何一次房间请求都套超时。超时按「本条未验证」处理，不按成功。"""
    return await asyncio.wait_for(coro, timeout)


def describe(resp: object) -> str:
    """把 nio 响应对象压成一行，只挑判据用得上的字段。"""
    name = type(resp).__name__
    bits = []
    for attr in ("status_code", "message", "content", "event_id", "user_id",
                 "device_id", "next_batch"):
        if hasattr(resp, attr):
            bits.append(f"{attr}={getattr(resp, attr)!r}")
    return f"{name}({', '.join(bits)})" if bits else name


# --------------------------------------------------------------------------
async def probe_encryption(client, room_id: str, label: str, expect: str,
                           token: str, report: Report) -> None:
    """假设 ①：加密房判定。"""
    criterion = ("encryption_verdict：status_code==M_NOT_FOUND -> clear；"
                 "content 有 errcode -> error；content 有 algorithm -> encrypted")
    request = f"room_get_state_event({room_id!r}, 'm.room.encryption')"
    try:
        resp = await call(client.room_get_state_event(room_id, "m.room.encryption"))
    except Exception as exc:                               # noqa: BLE001
        triple(criterion, request, f"<抛异常> {type(exc).__name__}: {mask(str(exc), token)}")
        report.skip(f"① 加密房判定（{label}）", f"请求本身失败：{type(exc).__name__}")
        return

    http = getattr(getattr(resp, "transport_response", None), "status", "?")
    triple(criterion, request, mask(f"HTTP {http} -> {describe(resp)}", token))
    verdict, detail = encryption_verdict(resp)
    print(f"  判定结果 : {verdict}（{detail}）  期望 {expect}")
    if verdict == expect:
        report.ok(f"① 加密房判定（{label}）")
    else:
        report.skip(f"① 加密房判定（{label}）", f"判定为 {verdict}，与期望 {expect} 不符")


# --------------------------------------------------------------------------
async def probe_sync(client, room_id: str, self_mxid: str, token: str,
                     report: Report, listen_seconds: int) -> None:
    """假设 ②：sync_forever、私有事件循环、首次 sync 会不会灌历史。"""
    from nio import RoomMessageText

    history: list[tuple[str, str]] = []

    async def _collect(room, event) -> None:
        if getattr(room, "room_id", None) == room_id:
            history.append((event.sender, event.body))

    client.add_event_callback(_collect, RoomMessageText)

    criterion = "首次 /sync（不带 since）会把房间 timeline 的历史消息派发给 add_event_callback"
    request = "sync(timeout=0)  ← 不带 since、不带过滤器，即 bot 冷启动那一次"
    try:
        resp = await call(client.sync(timeout=0))
    except Exception as exc:                               # noqa: BLE001
        triple(criterion, request, f"<抛异常> {type(exc).__name__}: {mask(str(exc), token)}")
        report.skip("② sync 全条", f"sync 失败：{type(exc).__name__}")
        return
    triple(criterion, request, mask(f"{describe(resp)}；回调收到 {len(history)} 条", token))
    for sender, body in history[:5]:
        print(f"             历史消息 sender={sender!r} body={mask(body, token)!r}")
    if history:
        print("  → 确认会灌历史。这就是 listen() 必须「先同步、后挂回调」的理由。")
        report.ok("②a 首次 sync 灌历史")
    else:
        print("  → 本次没收到历史（房间可能是空的）。先在房间里发几句再重跑，"
              "否则这条只是「没观察到」，不是「不会发生」。")
        report.skip("②a 首次 sync 灌历史", "房间无历史消息，观察不到")

    # 第二次：带过滤器，看能不能压到 0 条
    history.clear()
    client.next_batch = ""                     # 强行退回冷启动状态再试一次
    criterion2 = f"NO_HISTORY_FILTER = {NO_HISTORY_FILTER} 能把首次 sync 的 timeline 压到 0 条"
    request2 = f"sync(timeout=0, sync_filter={NO_HISTORY_FILTER})"
    try:
        resp2 = await call(client.sync(timeout=0, sync_filter=NO_HISTORY_FILTER))
    except Exception as exc:                               # noqa: BLE001
        triple(criterion2, request2, f"<抛异常> {type(exc).__name__}: {mask(str(exc), token)}")
        report.skip("②b 零 timeline 过滤器", f"sync 失败：{type(exc).__name__}")
        return
    triple(criterion2, request2, mask(f"{describe(resp2)}；回调收到 {len(history)} 条", token))
    if history:
        report.skip("②b 零 timeline 过滤器", f"过滤器没挡住，仍收到 {len(history)} 条")
    else:
        print("  → 过滤器生效。next_batch 已推到「现在」，此后 sync_forever 只看得见新消息。")
        report.ok("②b 零 timeline 过滤器")

    if listen_seconds <= 0:
        report.skip("②c 实时监听 + 回声过滤", "MAOS_PROBE_LISTEN_SECONDS=0，主动跳过")
        return

    # 第三次：真起 sync_forever，等人在 Element 里发言
    live: list[tuple[str, str]] = []
    dropped: list[tuple[str, str]] = []

    async def _live(room, event) -> None:
        pair = (event.sender, event.body)
        if should_deliver(room_id, self_mxid, room, event):
            live.append(pair)
        else:
            dropped.append(pair)

    client.add_event_callback(_live, RoomMessageText)
    task = asyncio.ensure_future(client.sync_forever(timeout=30_000))
    print(f"\n  ▸ sync_forever 已起。请在 Element 里往房间发一句（含 bot 自己发的），"
          f"等 {listen_seconds}s ……")
    await asyncio.sleep(listen_seconds)
    client.stop_sync_forever()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):            # noqa: BLE001
        pass

    criterion3 = (f"should_deliver：room_id 不符 或 sender=={self_mxid!r} 的消息不进 on_message")
    triple(criterion3, f"sync_forever(timeout=30000) 持续 {listen_seconds}s",
           mask(f"收下 {len(live)} 条、按回声/异房丢弃 {len(dropped)} 条", token))
    for sender, body in live:
        print(f"             收下 sender={sender!r} body={mask(body, token)!r}")
    for sender, body in dropped:
        print(f"             丢弃 sender={sender!r} body={mask(body, token)!r}")
    if live or dropped:
        report.ok("②c 实时监听 + 回声过滤")
    else:
        report.skip("②c 实时监听 + 回声过滤", f"{listen_seconds}s 内房间没有任何消息")


# --------------------------------------------------------------------------
async def probe_auth(client, cfg: MatrixBusConfig, report: Report) -> None:
    """假设 ③：直接赋 access_token 够不够鉴权 + user 到底该传什么。"""
    from nio import WhoamiError

    token = cfg.token
    criterion = ("直接赋 client.access_token（不调 login）即可鉴权；"
                 "nio 的 logged_in 只是 bool(access_token)，服务器认不认要看第一次真请求")
    request = "whoami()  ← 用 access_token 走的第一个真请求"
    try:
        resp = await call(client.whoami())
    except Exception as exc:                               # noqa: BLE001
        triple(criterion, request, f"<抛异常> {type(exc).__name__}: {mask(str(exc), token)}")
        report.skip("③ access_token 鉴权", f"连不上 homeserver：{type(exc).__name__}")
        return
    http = getattr(getattr(resp, "transport_response", None), "status", "?")
    triple(criterion, request, mask(f"HTTP {http} -> {describe(resp)}", token))

    if isinstance(resp, WhoamiError):
        report.skip("③ access_token 鉴权", f"{resp.status_code} {resp.message}")
        return
    print(f"  → access_token 单独可用，服务器认。client.logged_in={client.logged_in}")
    report.ok("③a access_token 鉴权")

    print()
    print(f"  MATRIX_USER 原文        : {cfg.user!r}")
    print(f"  AsyncClient.user        : {client.user!r}   ← 只是原样存下来")
    print(f"  whoami 回填的 user_id   : {client.user_id!r} ← 服务器给的权威 mxid")
    same = cfg.user == client.user_id
    print(f"  两者相等？{same}  —— 不等时，拿 MATRIX_USER 做回声过滤永远不会命中")
    report.ok("③b user 传 mxid 还是 localpart")

    # 真发一条，验 send 这条路也通
    criterion2 = "room_send 在只赋 access_token 的情况下应返回 RoomSendResponse 而非 M_MISSING_TOKEN"
    request2 = f"room_send({cfg.room_id!r}, 'm.room.message', msgtype=m.notice)"
    from nio import RoomSendError
    body = f"[matrix_probe] 探针连通性自检 {time.strftime('%H:%M:%S')}"
    try:
        resp2 = await call(client.room_send(
            room_id=cfg.room_id, message_type="m.room.message",
            content={"msgtype": "m.notice", "body": body}))
    except Exception as exc:                               # noqa: BLE001
        triple(criterion2, request2, f"<抛异常> {type(exc).__name__}: {mask(str(exc), token)}")
        report.skip("③c room_send", f"请求失败：{type(exc).__name__}")
        return
    http2 = getattr(getattr(resp2, "transport_response", None), "status", "?")
    triple(criterion2, request2, mask(f"HTTP {http2} -> {describe(resp2)}", token))
    if isinstance(resp2, RoomSendError):
        report.skip("③c room_send", f"{resp2.status_code} {resp2.message}")
    else:
        report.ok("③c room_send")


# --------------------------------------------------------------------------
async def main() -> int:
    missing = [k for k in REQUIRED if not (os.environ.get(k) or "").strip()]
    if missing:
        print("缺 " + "、".join(missing) + " 键，三条假设本轮全部未验证。")
        print("先等 C-1 交付 ~/.maos-matrix/room.env，再 `. ~/.maos-matrix/room.env` 后重跑。")
        return 2

    try:
        import nio                                          # noqa: F401
    except ImportError:
        print("缺 matrix-nio，本条未验证。用 ~/.maos-matrix/venv/bin/python 跑。")
        return 2

    from nio import AsyncClient

    cfg = MatrixBusConfig.from_env()
    report = Report()
    listen_seconds = int(os.environ.get("MAOS_PROBE_LISTEN_SECONDS", "20"))

    print(f"homeserver = {cfg.homeserver}")
    print(f"user       = {cfg.user}")
    print(f"room_id    = {cfg.room_id}")
    print(f"token      = {REDACTED}（长度 {len(cfg.token)}）")

    client = AsyncClient(cfg.homeserver, cfg.user)
    client.access_token = cfg.token

    try:
        head("假设 ③ · 直接赋 access_token 够不够鉴权（先跑：后两条都要它先成立）")
        await probe_auth(client, cfg, report)

        head("假设 ① · 加密房判定")
        print("-- 未加密房（C-1 建的那个）--")
        await probe_encryption(client, cfg.room_id, "未加密房", ENC_CLEAR,
                               cfg.token, report)
        enc_room = (os.environ.get("MATRIX_ROOM_ID_ENCRYPTED") or "").strip()
        print()
        print("-- 加密房（另一侧）--")
        if enc_room:
            await probe_encryption(client, enc_room, "加密房", ENC_ENCRYPTED,
                                   cfg.token, report)
        else:
            print("  判据原文 : 同上")
            print("  实际请求 : <未发出>")
            print("  实际响应 : <无>")
            report.skip("① 加密房判定（加密房一侧）",
                        "缺 MATRIX_ROOM_ID_ENCRYPTED 键。只验未加密那一侧等于没验")
        print()
        print("-- 错误一侧：不存在的房间（验非 404 错误体不会被念成加密房）--")
        await probe_encryption(client, "!definitely-not-a-real-room:invalid",
                               "不存在的房间", ENC_ERROR, cfg.token, report)

        head("假设 ② · sync_forever、私有事件循环、首次 sync 灌不灌历史")
        await probe_sync(client, cfg.room_id, client.user_id or cfg.user,
                         cfg.token, report, listen_seconds)
    finally:
        with contextlib.suppress(Exception):
            await call(client.close(), timeout=5)

    head("小结")
    for name in report.verified:
        print(f"  ✓ {name}")
    for name, why in report.unverified:
        print(f"  ✗ {name} —— {why}")
    print(f"\n已验 {len(report.verified)} 条，未验 {len(report.unverified)} 条。")
    return 0 if not report.unverified else 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
