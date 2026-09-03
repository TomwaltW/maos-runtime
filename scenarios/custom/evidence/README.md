# 演示用随案证据

这两张图是给 `scripts/room_team_smoke.py --evidence` 用的**演示语料**，
补的是 `docs/BACKLOG.md ## task-T89` 记的那条：申请表只有四列，没有地方放附件，
所以「证据齐」这条剧情在 CSV 那条路上只演得出一半 —— 每一单的证据核验都判 `missing`。

## 配给哪一单，由文件名前缀决定

| 文件 | 配给 | 演的是 |
| :-- | :-- | :-- |
| `ORD-2026-0004-rust.png` | ORD-2026-0004 | 剧情①证据齐 —— 质检判 defect、物流已签收，再补一张锈蚀照片就凑齐 |
| `ORD-2026-0006-damage.png` | ORD-2026-0006 | 剧情③大额 / ④重复退款 —— 证据齐但风险高，收口卡该给「升级审批」 |

前缀取申请表里出现过的订单号，按长度从长到短匹配，**前缀之后必须紧跟 `-` 或 `.`**。
所以 `ORD-2026-00041-x.png` 不会被当成 `ORD-2026-0004` 的证据。
认不出订单号的文件跳过并报一行，不静默丢掉。

## 🔴 ORD-2026-0005 故意一张都不配

不是漏了。四种结局要在同一张表上各演一单，而 `need_more` 只有靠「证据缺」演得出来：

| 结局 | 哪一单 | 靠什么 |
| :-- | :-- | :-- |
| `approve` | ORD-2026-0004 | 证据齐 + 风险 low |
| `escalate` | ORD-2026-0006 | 证据齐但风险 high（底账里已有 settled + pending 两条） |
| `need_more` | ORD-2026-0005（质量问题那行） | **不配证据** -> 证据核验判 missing |
| `reject` | ORD-2026-0005（七天无理由那行） | 第 58 天申请，超出 AS-001 的 30 天窗口 |

给 0005 配上图，`need_more` 这一格当场就空了 —— 它那两行里，质量问题那行会变成
`approve`，七天无理由那行仍是 `reject`（政策驳回优先于任何证据）。

## 图是怎么来的

96×96 的确定性噪点 PNG，纯 stdlib 生成，各约 20–25 KB。**没有用真实照片**：
证据核验岗核的是「有没有、digest 空不空、类型对不对」，不是图里画了什么
（见 `maos/skills/builtin/refund/evidence_check.py::_items`）。用真照片只会给仓库
添一份需要脱敏的东西。要重新生成或再加一张：

```python
import random, struct, zlib
W = H = 96

def png_bytes(seed: int, base: tuple) -> bytes:
    rng = random.Random(seed)
    raw = bytearray()
    for _y in range(H):
        raw.append(0)                                   # filter type 0
        for _x in range(W):
            for channel in base:
                raw.append(max(0, min(255, channel + rng.randint(-28, 28))))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)  # 8bit truecolor RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))

open("ORD-2026-0004-rust.png", "wb").write(png_bytes(20260904, (150, 92, 48)))
open("ORD-2026-0006-damage.png", "wb").write(png_bytes(20260906, (96, 104, 112)))
```

种子不同 -> 字节不同 -> digest 不同。同一颗种子跑两次逐字节一致，所以这两个文件
是可复现的，不是随手截来的。

## 它们不进底账

随案证据走的是**房间里拖附件**那条路（`Ticket.evidence` -> 圆桌的 `evidence` 入参
-> `payload["customer_evidence"]`），不写进 `ledger.json`。
底账里加一个顶层 `customer_evidence` 会挂到每一单头上、包括老三单，而且
`run_requests.py` 的汇总表**不会变色** —— 那是一条指纹测不出来的脏数据，
`maos/tests/test_room_team_fixture.py::test_ledger_does_not_gain_a_customer_evidence_block`
专门守着它。

冒烟脚本落盘走 `AttachmentStore`（内容寻址，落在 `var/attachments/`，已 .gitignore），
与真房间逐字同一条路 —— 白名单校验、digest、同图去重全部免费获得。
