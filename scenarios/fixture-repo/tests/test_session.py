"""靶场用例 —— 打补丁前 1 挂 1 过，打对补丁后全过（契约附录 C 冻结）。

两条用例都自带 now，不读系统时钟：靶场要在宿主和容器里给出同一个结果，
凡是依赖「现在几点」「机器在哪个时区」的断言都做不到这件事。
"""

from datetime import datetime, timedelta, timezone

from auth.session import SESSION_TTL, is_session_valid

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def test_valid_session():
    """刚活跃过的会话必须有效 —— 打补丁前后都过。

    一小时前活跃，离 7 天的 TTL 还差得远；就算带上 8 小时的时区偏移也还在期内，
    所以这条用例挡不住那个 bug。它守的是另一件事：补丁别把好的路径一起改坏。
    """
    assert is_session_valid(NOW - timedelta(hours=1), NOW)


def test_expired_session():
    """会话必须**恰好**在 TTL 之后才失效，不许提前。

    第一条断言是打补丁前挂的那条：还差一小时到期的会话，被时区偏移推过了
    TTL，于是提前判成过期。第二条断言打补丁前后都过 —— 留着它是为了防补丁
    「把 TTL 调大」这种糊弄式修法：那样第一条会绿，第二条会红。
    """
    almost_expired = NOW - SESSION_TTL + timedelta(hours=1)
    assert is_session_valid(almost_expired, NOW), "没到 TTL 就被判过期了"

    long_gone = NOW - SESSION_TTL - timedelta(hours=1)
    assert not is_session_valid(long_gone, NOW), "超过 TTL 还判有效"
