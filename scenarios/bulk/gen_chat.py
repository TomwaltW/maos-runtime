#!/usr/bin/env python3
"""客户聊天截图生成器 —— 渲染成真能看清的对话，不是噪点占位。

## 与 `scenarios/custom/evidence/` 那两张的分工

那两张是 96×96 的确定性噪点，理由写在它们的 README 里：证据核验岗核的是
「有没有、digest 空不空、类型对不对」，不是图里画了什么，用真照片只会给仓库添
一份要脱敏的东西。**那个理由今天依然成立**，所以这里也一张真照片都没有。

不同的是这些图**画的是文字**：客户与客服的对话。文字可以是编的，编的文字不需要
脱敏，而它比噪点多给两样东西：

1. **答辩/演示时能投出来给人看** —— 噪点图投出来只能说「这里有张图」。
2. **将来接视觉抽取时有考题** —— AP 那条通道已经在做「发票图片 → 抽取 → 待复核表」
   （`docs/ap-entry.md`）。聊天截图是退款域同形的那一道：图里那句话到底是哪种诉求，
   现在没人读，接上就有得读了。

## 文件名就是配单方式

`ORD-2026-1007-chat.png` 里 `ORD-2026-1007` 是前缀，后面**必须紧跟 `-` 或 `.`**
（判据在 `scenarios/custom/evidence/README.md`，真房间拖附件走的是同一段代码）。
所以这些图可以直接：

    python3 scripts/room_team_smoke.py --evidence scenarios/bulk/chat --recheck

## 确定性

对话逐条写死在 :data:`CONVERSATIONS` 里，渲染不含任何随机。同一台机器跑两次
逐字节一致；**跨机器不保证** —— 字体版本不同，字形就不同。这是与噪点图那条
「纯 stdlib 可复现」的真实差别，别把两者混着承诺。

    python3 scenarios/bulk/gen_chat.py            # 落到 scenarios/bulk/chat/
    python3 scenarios/bulk/gen_chat.py --one ORD-2026-1007   # 只出一张，调样式用
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent

#: macOS 自带。找不到就退到别的中文字体 —— **不退到默认字体**：
#: PIL 的默认位图字体画中文全是方框，出来的图看着像渲染坏了，比报错更难查。
FONT_CANDIDATES = [
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("/System/Library/Fonts/STHeiti Medium.ttc", 0),
    ("/System/Library/Fonts/Supplemental/Songti.ttc", 0),
]

W = 750                      # 手机截图宽度
PAD = 24                     # 页边距
AVATAR = 72
GAP = 20                     # 气泡之间
BUBBLE_PAD = 20
RADIUS = 12
MAX_BUBBLE = 440             # 气泡最大宽度，超了折行

BG = (237, 237, 237)
BAR_BG = (237, 237, 237)
BAR_LINE = (214, 214, 214)
THEM_BUBBLE = (255, 255, 255)
ME_BUBBLE = (149, 236, 105)
TEXT = (23, 23, 23)
META = (154, 154, 154)
AVATAR_THEM = (122, 146, 178)
AVATAR_ME = (94, 158, 108)
SYS_BG = (218, 218, 218)

#: `(订单号, 客户名, 时间戳, [(谁, 说什么), ...])`
#: `谁` 是 `them`（客户）/ `me`（客服）/ `sys`（居中的系统提示）。
#:
#: 每段对话都对着 `refund-requests-raw.csv` 里那一单的原话展开 —— 两份数据讲
#: 同一件事，对不上的话演示时会被人当场问住。
CONVERSATIONS: list[tuple[str, str, str, list[tuple[str, str]]]] = [
    ("ORD-2026-1000", "杭州锐驰机电 · 王工", "2026-09-05 09:12", [
        ("sys", "2026年9月5日 上午9:12"),
        ("them", "在吗？上周到的那批 6204 轴承有问题"),
        ("them", "轴承装上去转起来有异响，才用了半个月"),
        ("me", "王工您好，方便拍段视频或者照片我看看吗？"),
        ("them", "[图片]"),
        ("them", "拆下来看外圈已经有点发热变色了"),
        ("me", "收到，我这边给您登记质量问题工单，订单号 ORD-2026-1000 对吗"),
        ("them", "对，就这单。什么时候能处理"),
        ("me", "已提交售后，质保期内的质量问题走全额退，等财务复核后退款"),
    ]),
    ("ORD-2026-1003", "宁波恒信传动 · 李经理", "2026-09-02 14:40", [
        ("sys", "2026年9月2日 下午2:40"),
        ("them", "你好，上个月那单减速机想退掉"),
        ("me", "李经理您好，是有质量问题吗？"),
        ("them", "没有，货没拆封，financial 那边卡住了付不了款，先退单"),
        ("me", "明白，这属于无理由退货，我看下窗口期"),
        ("them", "会不会超时间了？我记得是三十天"),
        ("me", "您这单下单时锁定的是 v1 政策，30 天窗口，还在期内"),
        ("them", "那就好，麻烦你了"),
    ]),
    ("ORD-2026-1007", "苏州华工自动化 · 张采购", "2026-08-28 11:05", [
        ("sys", "2026年8月28日 上午11:05"),
        ("them", "货到了，但是我订的是 6204，你们发的是 6205"),
        ("me", "张工您好，麻烦拍一下箱子上的标签和实物"),
        ("them", "[图片]"),
        ("them", "标签贴的是我们的单号，里面的货对不上"),
        ("me", "确认是我们发货环节出的错，非常抱歉"),
        ("me", "这属于发错货，全额退并且免手续费，运费我们承担"),
        ("them", "那退款大概多久"),
        ("me", "金额较大需要主管复核，复核通过后当天发起"),
    ]),
    ("ORD-2026-1011", "台州百晟液压 · 陈师傅", "2026-09-08 16:22", [
        ("sys", "2026年9月8日 下午4:22"),
        ("them", "你们这个气缸参数跟你们说明书上写的对不上"),
        ("me", "陈师傅您好，具体是哪个参数？"),
        ("them", "气缸推力不够，标称 500N 实测只有 300 多"),
        ("me", "您是用什么方式测的？"),
        ("them", "拉力计测的，压力表读数 0.5MPa，跟你们标的工况一样"),
        ("me", "好的，我先按质量问题登记，会安排技术复核实测数据"),
        ("them", "复核要多久？我这边生产线等着用"),
        ("me", "这一单需要人工确认，我已经标记加急"),
    ]),
    ("ORD-2026-1016", "无锡中远流体 · 刘工", "2026-09-09 08:30", [
        ("sys", "2026年9月9日 上午8:30"),
        ("them", "这单不要了"),
        ("me", "刘工您好，方便说下原因吗？我们好走对应的流程"),
        ("them", "……"),
        ("them", "反正就是不要了，退了吧"),
        ("me", "好的，我这边先登记，具体原因等您补充后再确认类型"),
        ("sys", "该会话已转人工客服"),
    ]),
    ("ORD-2026-1022", "常州精锐工业 · 赵总", "2026-09-01 10:15", [
        ("sys", "2026年9月1日 上午10:15"),
        ("them", "上周到的那批法兰，收到货就发现外圈有一圈锈，明显是放久了"),
        ("them", "[图片]"),
        ("them", "整整一托盘，至少三分之一都这样"),
        ("me", "赵总您好，锈蚀情况我看到了，这批是 2026 年 3 月的库存"),
        ("them", "你们仓储条件是不是有问题"),
        ("me", "我们会内部核查。您这单我先按质量问题提交全额退"),
        ("them", "行，另外之前那两单退款到账了吗"),
        ("me", "我查一下……有一笔已到账，还有一笔在处理中"),
    ]),
    ("ORD-2026-1029", "杭州锐驰机电 · 王工", "2026-08-20 15:48", [
        ("sys", "2026年8月20日 下午3:48"),
        ("them", "电机通电就跳闸，师傅说是绕组短路"),
        ("me", "王工您好，这批电机是什么时候到的？"),
        ("them", "上个月到的，装了六台，有两台一通电就跳"),
        ("me", "麻烦提供一下电工的检测记录，我们走质量问题需要举证"),
        ("them", "[图片]"),
        ("them", "这是他们出的检测单，写了绕组对地绝缘不合格"),
        ("me", "收到，金额比较大，需要主管审批后才能发起退款"),
        ("them", "理解，走流程吧"),
    ]),
    ("ORD-2026-1034", "宁波恒信传动 · 李经理", "2026-09-03 13:20", [
        ("sys", "2026年9月3日 下午1:20"),
        ("them", "送来的是上一批的旧型号，已经停产那款"),
        ("me", "李经理，麻烦确认下铭牌上的型号"),
        ("them", "[图片]"),
        ("them", "铭牌写的 NMRV63-A，我订的是 NMRV63-B"),
        ("me", "确实发错了，A 版去年就停产了，是仓库串货"),
        ("them", "这批我急着用，能不能先换货"),
        ("me", "换货走另一条流程，退款这边我先按发错货全额退登记"),
    ]),
    ("ORD-2026-1041", "苏州华工自动化 · 张采购", "2026-09-07 09:55", [
        ("sys", "2026年9月7日 上午9:55"),
        ("them", "配件清单里说有两个接头，实际一个都没有"),
        ("me", "张工您好，我核对一下发货明细"),
        ("me", "查到了，配件是分开包装的，可能漏放了"),
        ("them", "那我这算什么？漏发了两个"),
        ("me", "我先登记，少发和发错是两种处理方式，需要确认后再定"),
        ("sys", "该会话已转人工客服"),
    ]),
    ("ORD-2026-1047", "台州百晟液压 · 陈师傅", "2026-07-28 17:30", [
        ("sys", "2026年7月28日 下午5:30"),
        ("them", "六月初买的那批密封件，现在想退"),
        ("me", "陈师傅您好，是产品有问题吗？"),
        ("them", "东西没问题，就是我们不要了"),
        ("them", "项目取消了，这批货用不上了，能退吗"),
        ("me", "我看一下您的下单时间……6 月 22 日付款"),
        ("me", "抱歉，您这单锁定的是 v2 政策，无理由窗口是 15 天，现在第 36 天"),
        ("them", "怎么改成十五天了"),
        ("me", "6 月 1 日起新单适用 v2，您这单下单时就锁定了这一版"),
    ]),
    ("ORD-2026-1053", "无锡中远流体 · 刘工", "2026-09-06 11:40", [
        ("sys", "2026年9月6日 上午11:40"),
        ("them", "能不能取消这个订单"),
        ("me", "刘工您好，货已经签收了，取消需要走退货流程"),
        ("them", "那就退货吧"),
        ("me", "方便说下退货原因吗？质量问题和无理由走的政策不一样"),
        ("them", "你们看着办吧"),
        ("me", "这个我们不能替您定，判错了套用的政策就不对"),
        ("sys", "该会话已转人工客服"),
    ]),
    ("ORD-2026-1058", "常州精锐工业 · 赵总", "2026-07-15 14:05", [
        ("sys", "2026年7月15日 下午2:05"),
        ("them", "五月那单传感器，放着一直没用，现在想退掉"),
        ("me", "赵总您好，我查一下订单……5 月 8 日付款签收"),
        ("me", "您这单是 v1 政策，无理由窗口 30 天，现在已经第 68 天"),
        ("them", "超了这么多啊"),
        ("me", "是的，无理由这条走不了。如果产品本身有质量问题是另一条路"),
        ("them", "东西没拆封，谈不上质量问题"),
        ("me", "那这一单只能驳回，很抱歉。我把政策条款发您留档"),
    ]),
]


def _assert_orders_match() -> None:
    """本文件的对话与 `generate.py::CHAT_ORDERS` 必须一一对上。

    对不上的症状全是哑的：图落了盘、`--evidence` 报一行「认不出订单号」就跳过、
    圆桌照跑，只是每一单的证据核验都判 missing —— 而那正是配了图要演的反面。
    所以在这里当场抛。
    """
    sys.path.insert(0, str(HERE.parents[1]))
    from scenarios.bulk.generate import CHAT_ORDERS

    mine = tuple(c[0] for c in CONVERSATIONS)
    if mine != CHAT_ORDERS:
        raise SystemExit(
            "gen_chat.CONVERSATIONS 与 generate.CHAT_ORDERS 对不上：\n"
            f"  只在对话里：{sorted(set(mine) - set(CHAT_ORDERS))}\n"
            f"  只在清单里：{sorted(set(CHAT_ORDERS) - set(mine))}\n"
            "  （顺序也要一致）两处改一处漏一处，图就配不上单，而且不会报错。")


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path, index in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size, index=index)
    raise SystemExit(
        "找不到任何中文字体，试过：\n  " + "\n  ".join(p for p, _ in FONT_CANDIDATES)
        + "\n退到 PIL 默认位图字体只会画出一版全是方框的图 —— 那比报错更难查，所以这里直接停。")


def wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """按像素宽折行。中文没有词边界，逐字量宽是唯一稳的办法。"""
    lines: list[str] = []
    current = ""
    for char in text:
        probe = current + char
        if font.getlength(probe) > max_width and current:
            lines.append(current)
            current = char
        else:
            current = probe
    if current:
        lines.append(current)
    return lines


def render(order_id: str, title: str, stamp: str,
           messages: list[tuple[str, str]], out_path: Path) -> None:
    font = load_font(26)
    font_bar = load_font(28)
    font_meta = load_font(20)
    line_h = 38

    # 先量高度，再开画布 —— 一次成图，不用先画大再裁。
    laid: list[tuple[str, list[str], int, int]] = []
    y = 96 + PAD                                     # 顶栏 + 上边距
    for who, text in messages:
        if who == "sys":
            laid.append((who, [text], y, line_h + 12))
            y += line_h + 12 + GAP
            continue
        lines = wrap(text, font, MAX_BUBBLE - 2 * BUBBLE_PAD)
        height = len(lines) * line_h + 2 * BUBBLE_PAD
        laid.append((who, lines, y, height))
        y += max(height, AVATAR) + GAP
    total_h = y + PAD

    img = Image.new("RGB", (W, total_h), BG)
    draw = ImageDraw.Draw(img)

    # 顶栏
    draw.rectangle([0, 0, W, 95], fill=BAR_BG)
    draw.line([0, 95, W, 95], fill=BAR_LINE, width=2)
    # 返回箭头手画。用 "‹" 之类的字符会撞上字体缺字形，画出一个方框 ——
    # 满屏中文里就那一个方框，看着像整张图渲染坏了。
    draw.line([(PAD + 20, 47), (PAD + 8, 35)], fill=(80, 80, 80), width=3)
    draw.line([(PAD + 20, 47), (PAD + 8, 59)], fill=(80, 80, 80), width=3)
    bar_w = font_bar.getlength(title)
    draw.text(((W - bar_w) / 2, 32), title, font=font_bar, fill=(23, 23, 23))

    for who, lines, top, height in laid:
        if who == "sys":
            text = lines[0]
            text_w = font_meta.getlength(text)
            box = [(W - text_w) / 2 - 14, top, (W + text_w) / 2 + 14, top + line_h + 6]
            draw.rounded_rectangle(box, radius=6, fill=SYS_BG)
            draw.text(((W - text_w) / 2, top + 6), text, font=font_meta, fill=(250, 250, 250))
            continue

        width = max(font.getlength(ln) for ln in lines) + 2 * BUBBLE_PAD
        mine = who == "me"
        if mine:
            ax = W - PAD - AVATAR
            bx1 = ax - 18
            bx0 = bx1 - width
            fill, avatar_fill, initial = ME_BUBBLE, AVATAR_ME, "服"
        else:
            ax = PAD
            bx0 = ax + AVATAR + 18
            bx1 = bx0 + width
            fill, avatar_fill, initial = THEM_BUBBLE, AVATAR_THEM, title[-1]

        draw.rounded_rectangle([ax, top, ax + AVATAR, top + AVATAR],
                               radius=8, fill=avatar_fill)
        iw = font.getlength(initial)
        draw.text((ax + (AVATAR - iw) / 2, top + 20), initial, font=font,
                  fill=(255, 255, 255))

        draw.rounded_rectangle([bx0, top, bx1, top + height], radius=RADIUS, fill=fill)
        # 气泡小尖角：一个贴着头像那侧的小方块，圆角盖不到，看着就连上了。
        if mine:
            draw.rectangle([bx1 - RADIUS, top + 14, bx1, top + 30], fill=fill)
        else:
            draw.rectangle([bx0, top + 14, bx0 + RADIUS, top + 30], fill=fill)

        for i, text in enumerate(lines):
            draw.text((bx0 + BUBBLE_PAD, top + BUBBLE_PAD + i * line_h - 4),
                      text, font=font, fill=TEXT)

    footer = f"{order_id} · 客服会话记录 · {stamp}"
    draw.text((PAD, total_h - 30), footer, font=font_meta, fill=META)
    img.save(out_path, "PNG", optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(HERE / "chat"))
    parser.add_argument("--one", metavar="ORDER_ID", default=None,
                        help="只出这一单的图，调样式用")
    args = parser.parse_args()

    _assert_orders_match()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    wanted = [c for c in CONVERSATIONS if not args.one or c[0] == args.one]
    if not wanted:
        print(f"没有 {args.one} 的对话", file=sys.stderr)
        return 1

    for order_id, title, stamp, messages in wanted:
        path = out_dir / f"{order_id}-chat.png"
        render(order_id, title, stamp, messages, path)
        size = path.stat().st_size
        with Image.open(path) as probe:
            w, h = probe.size
        print(f"  {path.name:28s} {w}x{h}  {size / 1024:.1f} KB  {len(messages)} 条")
    print(f"\n{len(wanted)} 张，落在 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
