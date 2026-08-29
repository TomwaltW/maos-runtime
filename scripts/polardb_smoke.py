#!/usr/bin/env python3
"""PolarDB PostgreSQL 版 / 本机 pgvector 的地基冒烟脚本。

这个脚本回答一个问题，且只回答这一个问题：
**给定一条 PostgreSQL 连接串，MAOS 的 PG 通道要用到的四样能力在这个实例上到底能不能用。**

四样能力 = 连得上、装得上 pgvector、全文检索跑得通、向量检索跑得通。
第五步是把自己建的表删干净。

## 为什么是独立脚本

它**不 import maos**，是刻意的：

1. 任何人拿到一条 DSN 就能复跑，不需要先装 MAOS 本体、不需要仓库处于某个状态；
2. 它验的是**数据库这一侧**的地基，与 maos/store 那边的实现进度解耦 ——
   实现没写完的时候，这个脚本照样能回答「云上这台机器行不行」。

所以它跑绿**不等于** MAOS 的 PG 后端跑通了，只等于地基没问题。两件事别混。

## 依赖

需要 psycopg（v3，找不到时回退 psycopg2）。**刻意不写进 pyproject.toml 的
dependencies** —— MAOS 核心是零运行时依赖，这是一个验收工具，不是运行时的一部分：

    python3 -m pip install 'psycopg[binary]'

## 用法

    export MAOS_PG_DSN='postgresql://<user>:<pass>@<host>:<port>/<db>'
    python3 scripts/polardb_smoke.py

    python3 scripts/polardb_smoke.py --local              # 连本机 compose 起的 pgvector
    python3 scripts/polardb_smoke.py --dsn-env OTHER_VAR  # 从别的环境变量读

## 安全

DSN **只从环境变量读**，不接受命令行传入 —— 命令行会进 shell 历史。
脚本的任何一条输出都不回显连接串：

- 第 1 步（连接）失败时**只报驱动异常的类名**，不报 message ——
  多数驱动会把 host 拼进连接失败的 message 里。
- 第 2-5 步是 SQL 层错误，message 有诊断价值（比如「装不上 vector 是权限问题
  还是根本没这个扩展」正是本脚本最想知道的），所以报 message，但先过 _redact()。
- _redact() 是双保险：既替换从 DSN 解析出的具体片段，也用正则兜底任何
  形如 scheme://...@... 的串。

## 退出码

    0  五步全绿
    1  连上了，但有步骤失败
    2  没配 DSN（优雅退出，不抛栈）
    3  驱动没装
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import traceback
from urllib.parse import urlsplit

# 自建对象统一用这个前缀，方便万一残留时人工辨认和清理。
TABLE_FTS = "maos_smoke_fts"
TABLE_VEC = "maos_smoke_vec"

# 本机 compose 起 pgvector 时的缺省值。取自 deploy/docker-compose.yml 的
# ${POSTGRES_USER:-maos} 那几行 —— 是公开的本地开发缺省值，不是秘密。
# --local 存在的意义：本机自测时命令行里不必出现任何连接串。
LOCAL_DEFAULTS = {
    "user": "maos",
    "password": "maos-local-dev",
    "db": "maos",
    "host": "127.0.0.1",
    "port": "5432",
}

# 无条件子串替换：host / 口令 / 整条 DSN。铁律 7 的硬要求就是这几样
# ——「被人 grep 一遍都不该出现真实 host 或口令」。
_SECRETS_STRICT: list[str] = []
# 词边界替换：库名 / 用户名。这两样常是 maos 这种短词，无条件子串替换会误伤
# 正文（把 maos_smoke_fts 抹成 <redacted>_smoke_fts），\b 能避开下划线连写。
_SECRETS_WORD: list[str] = []


def _remember_secrets(dsn: str) -> None:
    """把 DSN 里的敏感片段登记下来，供 _redact() 逐个替换掉。"""
    _SECRETS_STRICT.append(dsn)
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return
    for piece in (parts.hostname, parts.password, parts.netloc):
        if piece:
            _SECRETS_STRICT.append(str(piece))
    for piece in (parts.username, parts.path.lstrip("/")):
        if piece:
            _SECRETS_WORD.append(str(piece))


def _redact(text: str) -> str:
    """抹掉任何可能泄漏连接串的片段（铁律 7）。"""
    out = str(text)
    # 长的先替换，免得短片段先把长片段切碎导致漏网。
    for secret in sorted(set(_SECRETS_STRICT), key=len, reverse=True):
        if secret:
            out = out.replace(secret, "<redacted>")
    for secret in sorted(set(_SECRETS_WORD), key=len, reverse=True):
        if secret:
            out = re.sub(rf"\b{re.escape(secret)}\b", "<redacted>", out)
    # 兜底：没登记到的连接串形态也一并抹掉。
    out = re.sub(r"\b[a-zA-Z][\w+.-]*://[^\s'\"]+", "<dsn-redacted>", out)
    return out


class Reporter:
    """逐步记录成败。本脚本的价值全在「哪一步过了、哪一步没过」，
    所以绝不能一个 try 包住全部 —— 一个笼统的失败等于没做。"""

    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def ok(self, step: str, detail: str = "") -> None:
        self.rows.append((step, True, detail))
        print(f"  [ OK ] {step}" + (f" -> {_redact(detail)}" if detail else ""))

    def fail(self, step: str, detail: str = "") -> None:
        self.rows.append((step, False, detail))
        print(f"  [FAIL] {step}" + (f" -> {_redact(detail)}" if detail else ""))

    def note(self, step: str, detail: str = "") -> None:
        """观察项：记录现象，不计入成败、不影响退出码。"""
        print(f"  [note] {step}" + (f" -> {_redact(detail)}" if detail else ""))

    @property
    def all_green(self) -> bool:
        return bool(self.rows) and all(ok for _, ok, _ in self.rows)

    def summary(self) -> str:
        passed = sum(1 for _, ok, _ in self.rows if ok)
        return f"{passed}/{len(self.rows)} 步通过"


def _err(exc: BaseException) -> str:
    """SQL 层异常：类名 + 脱敏后的 message。"""
    msg = _redact(str(exc)).strip().replace("\n", " ")
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def load_driver():
    """返回 (connect_callable, 驱动名)。找不到就退出码 3。"""
    try:
        import psycopg  # type: ignore

        return psycopg.connect, f"psycopg {psycopg.__version__}"
    except ImportError:
        pass
    try:
        import psycopg2  # type: ignore

        return psycopg2.connect, f"psycopg2 {psycopg2.__version__}"
    except ImportError:
        pass
    print("驱动没装：需要 psycopg（v3）或 psycopg2。")
    print("装一个再试：python3 -m pip install 'psycopg[binary]'")
    print("（刻意不写进 pyproject.toml 的 dependencies —— MAOS 核心零运行时依赖，")
    print("  这是验收工具，不是运行时的一部分。）")
    return None, None


def resolve_dsn(args: argparse.Namespace) -> str | None:
    if args.local:
        d = LOCAL_DEFAULTS
        user = os.environ.get("POSTGRES_USER", d["user"])
        password = os.environ.get("POSTGRES_PASSWORD", d["password"])
        db = os.environ.get("POSTGRES_DB", d["db"])
        port = os.environ.get("POSTGRES_PORT", d["port"])
        return f"postgresql://{user}:{password}@{d['host']}:{port}/{db}"
    return os.environ.get(args.dsn_env)


# --------------------------------------------------------------------------
# 五步
# --------------------------------------------------------------------------


def step1_connect(connect, dsn: str, rep: Reporter):
    """连得上 + SELECT version()。失败意味着网络 / 白名单 / 账号问题。"""
    try:
        conn = connect(dsn, connect_timeout=10)
        conn.autocommit = True
    except Exception as exc:
        # 刻意只报类名：连接失败的 message 里几乎一定带 host。
        rep.fail("1. 连接 + SELECT version()", f"连不上：{type(exc).__name__}")
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
        rep.ok("1. 连接 + SELECT version()", version)
        return conn
    except Exception as exc:
        rep.fail("1. 连接 + SELECT version()", _err(exc))
        conn.close()
        return None


def step2_extension(conn, rep: Reporter) -> bool:
    """CREATE EXTENSION vector —— 本脚本最想知道的那一条。

    云上 PG 常把扩展装载权限收在控制台的「插件管理」里，
    所以这一步失败先去控制台看一眼，不要在这里绕。
    """
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cur.fetchone()
        ver = row[0] if row else "?"
        rep.ok("2. CREATE EXTENSION vector", f"pgvector {ver}")
        return True
    except Exception as exc:
        rep.fail("2. CREATE EXTENSION vector", _err(exc))
        return False


def step3_fulltext(conn, rep: Reporter) -> bool:
    """全文通道：to_tsvector + ts_rank 跑通，并断言命中集合。"""
    docs = [
        ("d1", "the quick brown fox jumps over the lazy dog"),
        ("d2", "postgresql full text search with tsvector and tsquery"),
        ("d3", "brown bears eat fish in the river"),
    ]
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {TABLE_FTS}")
            cur.execute(f"CREATE TABLE {TABLE_FTS} (id text PRIMARY KEY, body text)")
            for doc_id, body in docs:
                cur.execute(f"INSERT INTO {TABLE_FTS} (id, body) VALUES (%s, %s)", (doc_id, body))
            # 'fox & brown' 只有 d1 同时含两个词 —— 命中集合是确定的，可以硬断言。
            cur.execute(
                f"""
                SELECT id, ts_rank(to_tsvector('english', body), q) AS score
                FROM {TABLE_FTS}, to_tsquery('english', 'fox & brown') q
                WHERE to_tsvector('english', body) @@ q
                ORDER BY score DESC, id ASC
                """
            )
            hits = cur.fetchall()
    except Exception as exc:
        rep.fail("3. 全文 to_tsvector + ts_rank", _err(exc))
        return False

    ids = [str(r[0]) for r in hits]
    if ids != ["d1"]:
        rep.fail("3. 全文 to_tsvector + ts_rank", f"命中集合应为 ['d1']，实得 {ids}")
        return False
    score = float(hits[0][1])
    if not score > 0:
        rep.fail("3. 全文 to_tsvector + ts_rank", f"ts_rank 应 > 0，实得 {score}")
        return False
    rep.ok("3. 全文 to_tsvector + ts_rank", f"命中 {ids}，ts_rank={score:.6f}（越大越相关）")

    # 观察项：PG 自带的分词器不认中文，这里只记录现象，口径见 deploy/polardb.md（T10）。
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_tsvector('simple', %s)::text", ("退款订单已经超时未处理",))
            zh = cur.fetchone()[0]
        rep.note("   （观察）中文 to_tsvector('simple')", zh)
    except Exception as exc:
        rep.note("   （观察）中文 to_tsvector('simple')", _err(exc))
    return True


def step4_vector(conn, rep: Reporter) -> bool:
    """向量通道：vector 列 + <=> 算子 + 排序。构造已知相似度，硬断言全序。

    查询向量是 [1,0,0]，<=> 是余弦距离（越小越相关）：
        a = [1, 0, 0]     距离 0
        c = [0.9, 0.1, 0] 距离 ≈ 0.0062
        b = [0, 1, 0]     距离 1
    所以顺序必须是 a, c, b。
    """
    rows = [("a", "[1,0,0]"), ("b", "[0,1,0]"), ("c", "[0.9,0.1,0]")]
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {TABLE_VEC}")
            cur.execute(f"CREATE TABLE {TABLE_VEC} (id text PRIMARY KEY, embedding vector(3))")
            for doc_id, vec in rows:
                cur.execute(
                    f"INSERT INTO {TABLE_VEC} (id, embedding) VALUES (%s, %s::vector)",
                    (doc_id, vec),
                )
            cur.execute(
                f"""
                SELECT id, embedding <=> %s::vector AS distance
                FROM {TABLE_VEC}
                ORDER BY distance ASC, id ASC
                """,
                ("[1,0,0]",),
            )
            hits = cur.fetchall()
    except Exception as exc:
        rep.fail("4. 向量 vector 列 + <=> 排序", _err(exc))
        return False

    ids = [str(r[0]) for r in hits]
    if ids != ["a", "c", "b"]:
        rep.fail("4. 向量 vector 列 + <=> 排序", f"顺序应为 ['a','c','b']，实得 {ids}")
        return False
    dists = ", ".join(f"{r[0]}={float(r[1]):.6f}" for r in hits)
    rep.ok("4. 向量 vector 列 + <=> 排序", f"top-1={ids[0]}，余弦距离 {dists}（越小越相关）")
    return True


def step5_cleanup(conn, rep: Reporter) -> bool:
    """清理自建表 —— 别在人家实例上留垃圾。"""
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {TABLE_FTS}")
            cur.execute(f"DROP TABLE IF EXISTS {TABLE_VEC}")
        rep.ok("5. 清理临时表", f"{TABLE_FTS} / {TABLE_VEC} 已删")
        return True
    except Exception as exc:
        rep.fail("5. 清理临时表", _err(exc))
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PolarDB PostgreSQL 版 / 本机 pgvector 地基冒烟（五步）",
    )
    parser.add_argument(
        "--dsn-env",
        default="MAOS_PG_DSN",
        metavar="VAR",
        help="从哪个环境变量读 DSN（缺省 MAOS_PG_DSN）。不接受直接传连接串。",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="连本机 compose 起的 pgvector，用其公开缺省值拼 DSN，命令行不出现连接串。",
    )
    args = parser.parse_args(argv)

    print("=" * 68)
    print("PolarDB / pgvector 地基冒烟 —— 五步")
    print("=" * 68)

    connect, driver = load_driver()
    if connect is None:
        return 3
    print(f"驱动：{driver}")

    dsn = resolve_dsn(args)
    if not dsn:
        print(f"没配 DSN：环境变量 {args.dsn_env} 是空的。")
        print(f"  export {args.dsn_env}='postgresql://<user>:<pass>@<host>:<port>/<db>'")
        print("  或者用 --local 连本机 compose 起的 pgvector。")
        return 2
    # 占位符没替换是最容易犯的一种错：文档里的样例串被原样 export 进来，
    # 于是「配了」和「配对了」被混成同一件事，最后表现为一个没头没脑的
    # OperationalError。这里当场说清，别让它伪装成「连不上」。
    if re.search(r"<(user|pass|password|host|port|db|dbname)>", dsn):
        print(f"DSN 是占位符模板，没有替换成真实连接串：环境变量 {args.dsn_env}")
        print("  里面还留着 <user> / <host> / <port> 这类尖括号占位符。")
        print("  把它换成实例的真实连接串再跑。")
        return 2
    _remember_secrets(dsn)

    target = "本机 pgvector（--local）" if args.local else f"环境变量 {args.dsn_env}"
    print(f"目标：{target}")
    print("-" * 68)

    conn = step1_connect(connect, dsn, rep := Reporter())
    if conn is None:
        print("-" * 68)
        print(f"结论：{rep.summary()} —— 连不上，后面四步没跑。")
        return 1

    try:
        has_vector = step2_extension(conn, rep)
        step3_fulltext(conn, rep)
        if has_vector:
            step4_vector(conn, rep)
        else:
            # 扩展没装上时第 4 步必然失败，如实记为未跑，不伪装成通过。
            rep.fail("4. 向量 vector 列 + <=> 排序", "跳过：第 2 步没拿到 vector 扩展")
        step5_cleanup(conn, rep)
    finally:
        conn.close()

    print("-" * 68)
    print(f"结论：{rep.summary()}")
    return 0 if rep.all_green else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中断。")
        sys.exit(130)
    except Exception:  # 兜底：任何漏网异常也不许把 DSN 带进栈回溯
        print("未预期的异常（栈回溯已脱敏）：")
        print(_redact(traceback.format_exc()))
        sys.exit(1)
