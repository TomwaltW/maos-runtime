#!/usr/bin/env python3
"""起 IM 接入面 —— 飞书 / 企业微信 / 微信客服打进来的回调都收在这里。

    python3 scripts/run_ingress.py --status              # 只看哪些渠道配好了
    python3 scripts/run_ingress.py --simulate "/help"    # 本机自测，零凭证
    python3 scripts/run_ingress.py                       # 起 webhook（127.0.0.1:8737）

## 先用 --simulate

它绕开 HTTP 与签名，直接把一句话喂给 router，跑的是**同一条**处置链路。
没有任何凭证也能验：命令解析、底账查订单、政策裁定、待办、放行、回帖措辞。
联调之前先用它把业务侧跑通，能省掉一大半「到底是我的代码错了还是回调没配对」。

## 再接真平台

三个平台都要求回调地址是**公网 HTTPS**，而本进程只讲 HTTP、默认只听
127.0.0.1。正确的部署是前面放一层 nginx / frp 做 TLS，把
``https://<你的域名>/ingress/feishu`` 反代到本进程。地址与所需环境变量见
`docs/ingress-setup.md`。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maos.core.store import SqliteStore                      # noqa: E402
from maos.ingress.contracts import (                              # noqa: E402
    CHANNEL_FEISHU, Attachment, InboundMessage,
)
from maos.ingress.router import (                                  # noqa: E402
    IngressRouter, describe_config, ensure_room_schema,
)
from maos.ingress.server import ROUTES, IngressServer, build_adapters  # noqa: E402

BAR = "=" * 68


class _ConsoleAdapter:
    """`--simulate` 用的回声通道：把回帖打到终端而不是发给平台。"""

    configured = True

    def __init__(self, name: str = CHANNEL_FEISHU) -> None:
        self.name = name

    def send(self, msg) -> None:
        print(f"\n[回帖 -> {self.name}:{msg.chat_id}]\n{msg.text}")

    def fetch(self, att) -> bytes:
        """`--photo` 的取件：字节来自本机文件，走的是与真渠道**同一条**落盘链路。

        白名单、体积闸、内容嗅探、内容寻址、暂存与认领全都照跑 —— 所以
        「本机能跑通」这句话在附件这条链路上是有分量的，不是只测了参数解析。
        """
        return Path(att.msg_ref["path"]).read_bytes()


#: serve 用的库文件（p13）：给了就落盘（会话、槽位、观察、转人工卡片跨重启还在，重推也认得出），
#: 不给照旧 `:memory:`。口径同 `hiclaw/room_ingress.py` 的同名变量。
ENV_INGRESS_DB = "MAOS_INGRESS_DB"
#: 订单绑定种子文件（p13）：``{"bindings": [...]}``，启动时经 ``records.load_bindings_file`` 灌进库。
#: 绑定只从这里（内部路径）写，外部渠道永远写不进来。
ENV_CS_BINDINGS = "MAOS_CS_BINDINGS"
#: 退款预检认的台账租户（p13）。不配就不装预检（退款 / 退货查单成功后转人工写「预检未装」）。
ENV_CS_LEDGER_TENANT = "MAOS_CS_LEDGER_TENANT"


def _store(path: str = ":memory:") -> SqliteStore:
    """幂等用的库。`:memory:` 意味着重启后重推会被当成新消息 —— 见 ``MAOS_INGRESS_DB``。

    建表口径与 `hiclaw/room_ingress.py::wire()` **共用一个** `ensure_room_schema()`
    （T129）：从前那边有退款域的三句 ensure、这边只有 `init_schema()`，同样是「起
    房间」，两个入口的库形状不一样。今天靠 Skill 层的懒建表撑住不崩，但读代码的人
    会在「`/assign` 在这个入口能用吗」这一问上卡住，而两处各写一份迟早还要再分叉一次。
    """
    store = SqliteStore(path)
    ensure_room_schema(store)
    return store


def _front_desk(store: SqliteStore, env=None, *, ledger=None):
    """客服前台（p12 / p13）：``MAOS_CS_TENANTS`` 非空才装，装之前把话术库灌进这个库。

    不配就返回 None —— router 与今天逐字节一致。配了但格式不对，``CsConfig.from_env``
    抛 ``ValueError``（由 `cmd_serve` 报出来、不起服务：带着一份读错的租户映射起来，
    客户会被算到别的租户名下）。只在 serve 装配；``--simulate`` 不动。

    p13 的端口与模型（review/p13-cs-contracts.md §1.2）：

    * ``MAOS_CS_BINDINGS`` 给了就把绑定种子文件灌进库（坏文件抛 ``ValueError``、一条都不写）；
    * 查单端口 = ``cs_ports.build_order_lookup_from_env(env, ledger_path=台账)``（不配
      ``MAOS_CS_ORDER_SYSTEMS`` 就是 None，前台照 p12 行为）；装了查单才装身份核验
      ``BindingVerifier`` 与退款预检 ``LedgerRefundPrecheck``（后者要 ``MAOS_CS_LEDGER_TENANT``，
      没配就不装）；
    * 理解层的模型 = ``select_model_client()``（没配真模型或 ``MAOS_FORCE_SCRIPTED=1`` 时是
      Scripted：理解层一次模型都不调）。

    打印只报装没装、几个，不打任何取值（铁律 6）。
    """
    env = os.environ if env is None else env
    if not str(env.get("MAOS_CS_TENANTS") or "").strip():
        return None
    from maos.domain.cs import records
    from maos.domain.cs.corpus import seed_cs_kb
    from maos.domain.cs.desk import CsConfig, FrontDesk
    from maos.domain.cs.identity import BindingVerifier
    from maos.ingress.cs_ports import LedgerRefundPrecheck, build_order_lookup_from_env
    from maos.model.client import select_model_client

    config = CsConfig.from_env(env)
    seeded = seed_cs_kb(store)
    target = config.handoff_target[0] if config.handoff_target else "未配置（卡片只落库）"
    print(f"客服前台：已装 · 客服账号 {len(config.tenants)} 个 · 话术 {seeded} 篇 · "
          f"转人工卡片投到 {target}")

    bindings_path = str(env.get(ENV_CS_BINDINGS) or "").strip()
    if bindings_path:
        n = records.load_bindings_file(store, bindings_path)
        print(f"客服前台：订单绑定种子 {n} 条")
    lookup = build_order_lookup_from_env(env, ledger_path=ledger)
    verifier = precheck = None
    if lookup is not None:
        verifier = BindingVerifier()
        tenant = str(env.get(ENV_CS_LEDGER_TENANT) or "").strip()
        if tenant:
            precheck = LedgerRefundPrecheck(ledger, ledger_tenant=tenant)
        print(f"客服前台：只读查单已装 · 订单系统 {len(lookup.system_names)} 个 · 退款预检 "
              + ("已装" if precheck is not None else f"未装（没配 {ENV_CS_LEDGER_TENANT}）"))
    model = select_model_client()
    return FrontDesk(store, config, verifier=verifier, lookup=lookup, precheck=precheck,
                     model=model)


def cmd_status(args) -> int:
    adapters = build_adapters()
    print(f"{BAR}\n渠道配置（只报配没配，不打值）\n{BAR}")
    print(describe_config(adapters))
    print("\n回调路径：")
    for path, channel in ROUTES.items():
        state = "已配置" if adapters[channel].configured else "未配置 -> 回 503"
        print(f"  {path:24} {channel:12} {state}")
    print("\n所需环境变量见 docs/ingress-setup.md")
    return 0


def cmd_simulate(args) -> int:
    """不走 HTTP、不校签名，直接喂一句话给 router。"""
    adapter = _ConsoleAdapter()
    router = IngressRouter({adapter.name: adapter}, store=_store(),
                           ledger_path=args.ledger)

    # 照片先进，命令后到 —— 这是人在群里的真实动作顺序，也是暂存存在的理由。
    # 单独发一条只有附件的消息，而不是把图挂在 /refund 那条上：后者测不到
    # 「先发图、隔一会儿再打命令」这条主路径。
    if args.photo:
        atts = tuple(
            Attachment(channel=adapter.name, file_key=str(i), kind="image",
                       filename=Path(p).name, msg_ref={"path": str(Path(p).resolve())})
            for i, p in enumerate(args.photo, 1)
        )
        print(f"\n[{args.sender} 发了 {len(atts)} 张图] "
              f"{'、'.join(a.filename for a in atts)}")
        router.handle(InboundMessage(
            channel=adapter.name, chat_id="sim", sender=args.sender,
            text="", msg_id="sim-photo", attachments=atts))

    for i, text in enumerate(args.simulate):
        print(f"\n[{args.sender} 说] {text}")
        router.handle(InboundMessage(
            channel=adapter.name, chat_id="sim", sender=args.sender,
            text=text, msg_id=f"sim-{i}"))
    return 0


def cmd_serve(args) -> int:
    adapters = build_adapters()
    if not any(a.configured for a in adapters.values()):
        # 起一个一个渠道都没配的 webhook 没有意义：它对每条回调都回 503，
        # 而平台后台只会显示「回调失败」。早说比晚说好。
        print("一个渠道都没配好，不起服务。先看：\n"
              "  python3 scripts/run_ingress.py --status\n"
              "本机自测不需要凭证：\n"
              '  python3 scripts/run_ingress.py --simulate "/help"', file=sys.stderr)
        return 2

    store = _store(os.environ.get(ENV_INGRESS_DB) or ":memory:")
    try:
        desk = _front_desk(store, ledger=args.ledger)
    except ValueError as exc:
        print(f"客服前台配置有误，不起服务：{exc}", file=sys.stderr)
        return 2
    router = IngressRouter(adapters, store=store, ledger_path=args.ledger, cs=desk)
    server = IngressServer(adapters, router, host=args.host, port=args.port)
    print(f"{BAR}\n渠道：{describe_config(adapters)}")
    print(f"监听 http://{args.host}:{args.port}，路径 {', '.join(ROUTES)}")
    print(f"公网入口请用 nginx/frp 反代并加 TLS —— 三个平台都只接受 https\n{BAR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="run_ingress", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--status", action="store_true", help="只打印渠道配置状态")
    p.add_argument("--simulate", nargs="+", metavar="消息",
                   help="本机自测：把这些消息依次喂给 router（零凭证、不走 HTTP）")
    p.add_argument("--sender", default="ou_demo", help="--simulate 时的发送者标识")
    p.add_argument("--photo", nargs="+", metavar="路径", default=[],
                   help="--simulate 时先发这些本机图片当证据（走真的落盘与认领链路）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8737)
    p.add_argument("--ledger", default=None, help="底账路径，缺省 scenarios/custom/ledger.json")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(name)-22s %(message)s")
    if args.ledger is None:
        from maos.ingress.router import DEFAULT_LEDGER
        args.ledger = DEFAULT_LEDGER

    if args.status:
        return cmd_status(args)
    if args.photo and not args.simulate:
        # 单独给 --photo 会静默落到 cmd_serve 上，症状是「图没发出去，却起了个服务」。
        print("--photo 只在 --simulate 下有意义。真渠道的图从平台回调进来，不从命令行。\n"
              '  例：--photo 破损.jpg --simulate "/refund ORD-2026-0001 质量问题"',
              file=sys.stderr)
        return 2
    if args.simulate:
        return cmd_simulate(args)
    return cmd_serve(args)


if __name__ == "__main__":
    sys.exit(main())
