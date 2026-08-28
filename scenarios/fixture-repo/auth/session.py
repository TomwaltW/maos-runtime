"""会话有效期判定 —— 演示靶场里那个真 bug 就在这个文件。

留 bug 是有意的（见 README）：`is_session_valid` 把 UTC 时间戳先换算成
本地墙上时间，又把那个墙上时间当成 UTC 拿去做差，于是会话年龄凭空多出
一个时区偏移，没到期的会话被提前判成过期。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 业务方所在时区，配置里写死的「本地时区」。
#
# 写死而不是读机器的 TZ：沙箱容器里 TZ 就是 UTC，靠环境时区的 bug 一进容器
# 就自动消失，靶场会变成「宿主上红、沙箱里绿」—— 那样演示什么都证明不了。
LOCAL_TZ = timezone(timedelta(hours=8))

# 「记住我」会话的有效期。
SESSION_TTL = timedelta(days=7)


def is_session_valid(last_seen: datetime, now: datetime) -> bool:
    """会话在 last_seen 之后 SESSION_TTL 之内算有效。两个入参都是 UTC 感知时间。"""
    # BUG：astimezone 换出来的是本地墙上时间，replace 又给它贴了个 UTC 的标签，
    # 于是 now 凭空往后跳了一个时区偏移，做差得到的年龄比真实年龄大 8 小时。
    now_wall = now.astimezone(LOCAL_TZ).replace(tzinfo=timezone.utc)
    return now_wall - last_seen < SESSION_TTL
