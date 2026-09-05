"""按 digest 挑着取暂存 —— 一轮复检只认领**这条消息带来的**那几张图。

`claim`（全取并清空）对 ``/refund`` 是对的：一句命令认领这个会话攒下的所有图。
但「拖一张图 -> 立刻复检」那条路不是：全取会把三分钟前拖进来、还没决定挂给哪一单的
另外五张一起卷走，症状是「A 单的图挂到了 B 单的案子上」—— 与 `AttachmentBuffer`
类抬头写的那条同源，而这种错**不报错**，只有这里的用例挡得住。

`claim` 现在是 ``take(digests=None)`` 的一层薄壳，所以第一条钉的是**两条路径逐字等价**：
它红了就说明薄壳没薄，`handle_refund` 的语义已经被这一轨改动了。
"""

from __future__ import annotations

import threading
import time

from maos.ingress.attachments import AttachmentBuffer, AttachmentStore
from maos.ingress.contracts import CHANNEL_FEISHU, Attachment
from maos.tests.test_ingress_attachments import JPEG_HEAD, PNG_1PX

CHAT = "oc_A"


def _att() -> Attachment:
    return Attachment(channel=CHANNEL_FEISHU, file_key="img_v3_x", kind="image")


def _stored(store: AttachmentStore, data: bytes, chat_id: str = CHAT):
    return store.put(data, _att(), chat_id=chat_id, sender="ou_a")


def _pngs(store: AttachmentStore, n: int) -> list:
    """n 张 digest 互不相同的图。改末字节而不改魔数：落盘那步只嗅探头部类型。"""
    return [_stored(store, PNG_1PX[:-1] + bytes([i])) for i in range(n)]


def _filled(store: AttachmentStore, items) -> AttachmentBuffer:
    buf = AttachmentBuffer()
    for item in items:
        buf.add(item)
    return buf


def test_take_all_is_word_for_word_the_same_as_claim(tmp_path):
    """``digests=None`` 与 `claim` 逐字等价：同样的返回，之后同样空。

    两个 buffer 喂**同一批** StoredAttachment 对象 —— 各存各的话 ``received_at``
    会差出几微秒，比的就不再是行为而是时钟。
    """
    store = AttachmentStore(tmp_path)
    items = [_stored(store, PNG_1PX), _stored(store, JPEG_HEAD)]
    by_take, by_claim = _filled(store, items), _filled(store, items)

    assert by_take.take(CHANNEL_FEISHU, CHAT) == by_claim.claim(CHANNEL_FEISHU, CHAT) == items
    assert by_take.peek(CHANNEL_FEISHU, CHAT) == by_claim.peek(CHANNEL_FEISHU, CHAT) == []


def test_take_picks_the_named_one_and_leaves_the_rest(tmp_path):
    """只取点名的那张，另一张还在暂存里等它自己那一单。"""
    store = AttachmentStore(tmp_path)
    a, b = _stored(store, PNG_1PX), _stored(store, JPEG_HEAD)
    buf = _filled(store, [a, b])

    assert buf.take(CHANNEL_FEISHU, CHAT, digests={a.digest}) == [a]
    assert buf.peek(CHANNEL_FEISHU, CHAT) == [b]


def test_empty_set_takes_nothing_and_only_none_takes_all(tmp_path):
    """``set()`` 取 0 份，只有 ``None`` 是全取。

    ``if not digests:`` 会把两者混成一个，而那时最坏的一轮是「定位到单了、但一张
    证据都没挂上」—— 它反手把整个会话的暂存卷走，挂到别人的单子上。
    """
    store = AttachmentStore(tmp_path)
    items = [_stored(store, PNG_1PX), _stored(store, JPEG_HEAD)]
    buf = _filled(store, items)

    assert buf.take(CHANNEL_FEISHU, CHAT, digests=set()) == []
    assert buf.peek(CHANNEL_FEISHU, CHAT) == items          # 一份不少
    assert buf.take(CHANNEL_FEISHU, CHAT) == items          # None 才全取


def test_a_digest_not_in_the_buffer_is_skipped_not_raised(tmp_path):
    """暂存有 TTL 也有 cap：调用方手上那份 digest 与暂存对不上是常态，不是故障。"""
    store = AttachmentStore(tmp_path)
    a, b = _stored(store, PNG_1PX), _stored(store, JPEG_HEAD)
    buf = _filled(store, [a, b])

    assert buf.take(CHANNEL_FEISHU, CHAT, digests={a.digest, "f" * 64}) == [a]
    assert buf.peek(CHANNEL_FEISHU, CHAT) == [b]


def test_expired_items_are_not_taken_and_get_swept(tmp_path):
    """过期的取不到，且**顺手清掉** —— 与 peek / claim 里 `_expired` 那两处同一口径。

    看内部 `_by_chat` 是刻意的：从外面只看得见「没取到」，看不出那张过期的图是被
    清掉了还是留着下次再判一遍 —— 留着的话它会一直占着 cap 的一格。
    """
    store = AttachmentStore(tmp_path)
    old = _stored(store, PNG_1PX)
    buf = AttachmentBuffer(ttl=0)
    buf.add(old)
    time.sleep(0.01)

    assert buf.take(CHANNEL_FEISHU, CHAT, digests={old.digest}) == []
    assert buf._by_chat[(CHANNEL_FEISHU, CHAT)] == []


def test_returned_in_buffer_order_not_in_set_order(tmp_path):
    """返回顺序是先进先出，不是 ``digests`` 的迭代顺序。

    集合无序，而 PYTHONHASHSEED 随机化让它每次运行还不一样 —— 按它排的话，
    同一条消息里的三张图每回在案子里换一个次序，事后没人能解释是按什么排的。
    """
    store = AttachmentStore(tmp_path)
    items = _pngs(store, 8)
    buf = _filled(store, items)
    order = [it.digest for it in items]

    taken = buf.take(CHANNEL_FEISHU, CHAT, digests=set(order))
    assert [a.digest for a in taken] == order


def test_two_threads_hand_out_each_attachment_exactly_once(tmp_path):
    """两条消息并发认领同一批图，每份只许被取走一次。

    栅栏是必需的：不同步的话两个线程往往被调度成串行，竞争窗口根本没打开，
    这条就成了一个伪装成并发的顺序用例（范式同 `test_worker_claim_race`）。
    取走与回写分两步做的实现会在这里给出两份同样的图 —— 而那时两个案子引用
    同一张照片，事后没有任何一条记录能解释清楚。
    """
    store = AttachmentStore(tmp_path)
    items = _pngs(store, 8)
    buf = _filled(store, items)
    digests = {it.digest for it in items}

    barrier = threading.Barrier(2)
    results: list[list] = [[], []]
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        try:
            barrier.wait()
            results[idx] = buf.take(CHANNEL_FEISHU, CHAT, digests=digests)
        except BaseException as exc:        # noqa: BLE001 —— 线程里的异常要带回主线程
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "有线程没在 10 秒内退出，疑似死锁"
    assert not errors, f"取件过程抛异常：{errors!r}"

    handed = [a.digest for a in results[0] + results[1]]
    assert sorted(handed) == sorted(digests), "合起来应恰好是那 8 份，一份不多不少"
    assert buf.peek(CHANNEL_FEISHU, CHAT) == []
