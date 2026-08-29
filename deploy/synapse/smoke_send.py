#!/usr/bin/env python3
"""房间收发自证：往 MATRIX_ROOM_ID 发一条 m.notice，再读回来确认服务端收下了。

刻意**不 import hiclaw** —— 本脚本证明的是「Synapse 房间真能收发」这一层地基，
hiclaw/matrix_bus.py 的三条假设（镜像 / 审批 / 越权）归 C-2 验，不在这里越界。

用法（四键来自 ~/.maos-matrix/room.env）：

    . ~/.maos-matrix/room.env && ~/.maos-matrix/venv/bin/python deploy/synapse/smoke_send.py

退出码：0 = 发送并读回成功；1 = 缺环境变量；2 = 发送或读回失败。
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

from nio import AsyncClient, RoomSendResponse

REQUIRED = ("MATRIX_HOMESERVER", "MATRIX_USER", "MATRIX_TOKEN", "MATRIX_ROOM_ID")


async def main() -> int:
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        print("缺环境变量 " + ", ".join(missing) + "；先 . ~/.maos-matrix/room.env",
              file=sys.stderr)
        return 1

    homeserver = os.environ["MATRIX_HOMESERVER"]
    user = os.environ["MATRIX_USER"]
    room_id = os.environ["MATRIX_ROOM_ID"]

    # token 鉴权，不走 login —— 口令只存在 ~/.maos-matrix/creds.txt，脚本里不该有它。
    client = AsyncClient(homeserver, user)
    client.access_token = os.environ["MATRIX_TOKEN"]
    client.user_id = user
    client.device_id = "MAOS_SMOKE"

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = "[smoke] MAOS 房间地基自证 " + stamp
    try:
        resp = await client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.notice", "body": body},
        )
        if not isinstance(resp, RoomSendResponse):
            print("发送失败：" + str(resp), file=sys.stderr)
            return 2
        print("sent event_id=" + resp.event_id)

        # 读回：只有服务端真收下了，这条才拿得到。
        got = await client.room_get_event(room_id, resp.event_id)
        got_body = getattr(getattr(got, "event", None), "body", None)
        if got_body != body:
            print("读回不一致：" + str(got), file=sys.stderr)
            return 2
        print("echo   body=" + got_body)
        print("SMOKE OK")
        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
