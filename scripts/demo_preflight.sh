#!/usr/bin/env bash
#
# Demo 录制前置 —— 一条命令跑完全部前置检查并逐条断言期望值。
#
#   bash scripts/demo_preflight.sh
#
# 设计口径（写在这里，改脚本前先读）：
#
# 1. **逐条断言，不是跑完就算。** 每一步都从命令输出里**解析**真实读数再比对，
#    任一条不符立即非 0 退出，退出码 = 出错的步号（1..5），并打印实际值 vs 期望值。
#    只会打印不会失败的前置脚本等于没写。
#
# 2. **期望值可用环境变量覆盖**，不必改文件：
#      MAOS_EXPECT_TESTS=1069        第 1 步的测试条数
#      MAOS_EXPECT_BUNDLES=8         第 4 步落盘的证据束个数
#      MAOS_EXPECT_VERIFY='RESULT: 8/8 PASS'   第 5 步的核验结论行
#    负例自证就靠它：MAOS_EXPECT_TESTS=999 bash scripts/demo_preflight.sh
#    → 非 0 退出并指出是第 1 步。
#    第 1 步的条数**自己认环境分两档**（见下面 EXPECT_TESTS_NOPG / _PG），
#    显式传 MAOS_EXPECT_TESTS 仍然盖过自动判断 —— 覆盖能力是负例自证的地基。
#
# 3. **零出网、不依赖任何 API key。** 全程走 ScriptedModelClient 路径，
#    评委在没有任何密钥的机器上照样跑得出同一份结果 —— 这是卖点，第 0 步会显式打出来。
#    唯一一次网络动作是第 1 步的 PG 探测，且**只在人自己配了 MAOS_PG_DSN 时发生**：
#    没配就一个包都不发，与 API key 无关。
#
# 4. **本脚本不替人做决定。** 第 4 步必然把 evidence/ 弄脏（出处头每跑一次都变），
#    脚本只统计脏行数并给出二选一提示，**绝不自动还原** ——
#    万一人类正好有未提交的在制品，一条自动还原就把它冲掉了。
#
# 5. 本机没有 `python` 命令，全脚本一律 python3。

set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# 第 1 步的测试条数按环境分两档。差的 29 条 = 22（maos/tests/test_pg_store_live.py）
# + 7（maos/tests/test_pg_rank_parity.py）：没库时它们整个 skip，有库时全部真跑。
#
# 有库那档**由无库那档加 29 算出来，不写死**。写死的代价整合轮 13 当场吃到了：
# 契约 B 把有库钉成 932，而同轮 T24/T25/T26 各自加了 7/13/12 条测试，两个数一起作废——
# 症状是「配了 DSN 的机器上第 1 步报回归」，恰恰是本档要治的那个病。改成算式之后，
# 以后谁加测试都只需改 EXPECT_TESTS_NOPG 一个数，29 这个差值才是真正要守的不变量。
# 整合轮 14 实测（合并态 T27–T30）：1069 passed / 39 skipped（无库）。39 = PG 门控 29
#（22 live + 7 parity）+ 非 PG 门控 10（RocketMQ 8 条 + Nacos 2 条，有库时同样 skip）。
# 有库档 1069+29=1098 由算式得出。**2026-08-31（T34 轨）已实测**，不再是纸面推算：
# 起本机 pgvector 容器（本仓库 deploy/docker-compose.yml 的 pg profile，initdb 自动装
# vector 扩展）、配上 MAOS_PG_DSN 后整条 5 步跑通，exit=0；第 1 步实得
# **1098 passed / 10 skipped**，与算式逐位吻合，新判据「SKIPPED 行里不许出现 test_pg_」
# 也随之首次真正启用并通过。剩的 10 条是非 PG 门控（RocketMQ 8 + Nacos 2）。
#
# 🔴 **这个数是并轨的下游产物，每次并入新轨都要重取，不许照抄上一行。**
# 2026-09-01（T51 整合轮）实测合并态 T37–T39 后：**1370 passed / 39 skipped（无库）**。
# 三个业务域纵向切片共加 301 条。skipped 仍是 39 —— 新增的三个域一条门控测试都没加，
# 所以 PG_GATED_TESTS=29 这个不变量没被动，有库档由算式得 1370+29=1399（未实测，
# 上一次实测的 1098 对应 1069 那一档）。
#
# 这条被漏过一次，代价是**门禁在该拦的时候拦错了人**：T46 并完三轨后本脚本第 1 步
# 报「实际 1370 / 期望 1069」并打出「前置未通过，不要开始录制」，exit=1 ——
# 而当时代码是全绿的。录制前唯一的机器判据自己变成了假警报，是最坏的一种失效：
# 下一次它真的红了，人会先怀疑是这个数又没刷。**加测试的那一轨改这个数，
# 不要留给录制那天的人。**
PG_GATED_TESTS=29
EXPECT_TESTS_NOPG=1370
EXPECT_TESTS_PG=$((EXPECT_TESTS_NOPG + PG_GATED_TESTS))
EXPECT_BUNDLES="${MAOS_EXPECT_BUNDLES:-8}"
EXPECT_VERIFY="${MAOS_EXPECT_VERIFY:-RESULT: 8/8 PASS}"

LOG_DIR="$(mktemp -d)"
trap 'rm -rf "$LOG_DIR"' EXIT

step_no=0

banner() {
    step_no=$((step_no + 1))
    printf '\n\033[1m[%d/5] %s\033[0m\n' "$step_no" "$1"
}

ok() {
    printf '      \033[32mOK\033[0m   %s\n' "$1"
}

die() {
    trap - EXIT                     # 失败时保留日志目录，否则下面这个路径指向一个刚被删掉的文件
    printf '\n\033[31m[FAIL] 第 %d 步不符期望：%s\033[0m\n' "$step_no" "$1" >&2
    printf '       实际：%s\n' "$2" >&2
    printf '       期望：%s\n' "$3" >&2
    printf '       完整输出（已保留）：%s\n' "$4" >&2
    printf '       ── 末 5 行 ──\n' >&2
    sed -e 's/^/       /' "$4" | tail -5 >&2
    printf '\n前置未通过，\033[1m不要开始录制\033[0m。\n' >&2
    exit "$step_no"
}

# 有没有一台**连得上**的 PG？—— 判据与 test_pg_store_live.py / test_pg_rank_parity.py
# 自己的 skipif **同源**：那两个模块的 `_live_dsn()` 也是拿 PgStorePort 当场 connect
# 一次，连不上就当没库。这里照走同一条码路，两边判据不会漂。
#
# 为什么不是「DSN 非空」就算有库：DSN 配了而库没起（容器没起、白名单没放行）时，
# 那 29 条照样 skip，条数仍是无库那档；按「非空」判会让第 1 步在**配了 DSN 的机器上**
# 误红，而那正是本档要治的病（docs/BACKLOG.md 的 ## task-T18 第 4 条）。
# 代价（实测）：本机拒连 0.2 秒就回来；最坏是防火墙静默丢包，吃满
# maos/store/pg_store.py 的 DEFAULT_CONNECT_TIMEOUT = 5 秒。第 1 步本身要跑 20 秒
# 测试，这点开销买的是「配了 DSN 的机器上不误红」，值。且只在 DSN 已配时发生。
pg_reachable() {
    python3 - <<'PY' 2>/dev/null
import os, sys

dsn = os.environ.get("MAOS_PG_DSN", "")
if not dsn:
    sys.exit(1)
try:
    from maos.store.pg_store import PgStorePort
    port = PgStorePort(dsn)
    try:
        port.connect()
    finally:
        port.close()
except Exception:          # 驱动缺失 / 连不上 / DSN 形状不对，一律按「没库」
    sys.exit(1)
sys.exit(0)
PY
}

# 选档。显式传的 MAOS_EXPECT_TESTS 永远盖过自动判断（负例自证靠它）。
pick_expect_tests() {
    if [ -n "${MAOS_EXPECT_TESTS:-}" ]; then
        EXPECT_TESTS="$MAOS_EXPECT_TESTS"
        EXPECT_TESTS_WHY='人为指定 MAOS_EXPECT_TESTS，已盖过自动判断'
        REQUIRE_PG_TESTS_RAN=0         # 人自己指定条数时不替他多判一层
    elif pg_reachable; then
        EXPECT_TESTS="$EXPECT_TESTS_PG"
        EXPECT_TESTS_WHY='有可连的 PG —— live 22 条 + parity 7 条会真跑'
        REQUIRE_PG_TESTS_RAN=1
    else
        EXPECT_TESTS="$EXPECT_TESTS_NOPG"
        EXPECT_TESTS_WHY='无可连的 PG —— live 22 条 + parity 7 条 skip'
        REQUIRE_PG_TESTS_RAN=0
    fi
}

# --- 第 0 步：说清楚这一跑不需要任何密钥 ------------------------------------
printf '\033[1mMAOS Demo 录制前置\033[0m　仓库根 %s\n' "$REPO_ROOT"
printf '提交 %s\n' "$(git rev-parse --short HEAD)"
printf '\n本脚本\033[1m零出网、不读任何 API key\033[0m：全程走 ScriptedModelClient 路径，\n'
printf '拿到仓库的人在没有任何密钥的机器上跑，得到的是同一份确定性结果。\n'

# --- 第 1 步：全量测试 --------------------------------------------------------
banner '全量测试　python3 -m pytest maos/tests -q -rs'
pick_expect_tests
printf '      期望 %s passed（%s）\n' "$EXPECT_TESTS" "$EXPECT_TESTS_WHY"
if ! python3 -m pytest maos/tests -q -rs > "$LOG_DIR/pytest.log" 2>&1; then
    die '存量测试没有全绿' "$(tail -3 "$LOG_DIR/pytest.log" | tr '\n' ' ')" \
        "${EXPECT_TESTS} passed，0 failed" "$LOG_DIR/pytest.log"
fi
passed="$(grep -Eo '[0-9]+ passed' "$LOG_DIR/pytest.log" | tail -1 | grep -Eo '^[0-9]+' || true)"
[ -n "$passed" ] || die '解析不出测试条数' '（输出里没有 "N passed"）' \
    "${EXPECT_TESTS} passed" "$LOG_DIR/pytest.log"
if [ "$passed" != "$EXPECT_TESTS" ]; then
    die '测试条数与期望不符' "${passed} passed" "${EXPECT_TESTS} passed" "$LOG_DIR/pytest.log"
fi
# 有库那档还要守另半条不变量：29 条 live/parity 一条都不许被饿死。
# 只查条数守不住它 —— conftest 的 delenv 若把 DSN 清早了，29 条会从 passed 掉回 skipped，
# 那时 passed 正好等于无库那档的数，「测试变干净了」而不是红灯（docs/BACKLOG.md 的 ## task-T26 第 2 条）。
#
# 判据**不再是「0 skipped」**：整合轮 14 起 RocketMQ（8 条）与 Nacos（2 条）两组门控
# 在有库时同样 skip，那个写法会把它们误报成回归 —— 与写死 935 是同一个病，投影变了
# 判据就作废。要守的一直是「PG 那两个文件一条都没被饿死」，所以直接判 SKIPPED 行里
# 有没有 test_pg_，这个判据不随别的门控组增减而过期。-rs 就是为它加的。
pg_skipped="$(grep -c '^SKIPPED.*test_pg_' "$LOG_DIR/pytest.log" || true)"
if [ "$REQUIRE_PG_TESTS_RAN" = 1 ] && [ "${pg_skipped:-0}" != 0 ]; then
    die '有可连的 PG，PG 用例却仍被 skip' "${pg_skipped} 处 test_pg_* 仍是 SKIPPED" \
        'test_pg_store_live.py 22 条 + test_pg_rank_parity.py 7 条必须真跑' \
        "$LOG_DIR/pytest.log"
fi
ok "${passed} passed"

# --- 第 2 步：场景 1-7 端到端 -------------------------------------------------
banner '场景 1-7 端到端　python3 run.py'
if ! python3 run.py > "$LOG_DIR/run-all.log" 2>&1; then
    die 'run.py 非 0 退出' "exit=$?" 'exit=0' "$LOG_DIR/run-all.log"
fi
ok "exit=0（$(wc -l < "$LOG_DIR/run-all.log" | tr -d ' ') 行输出）"

# --- 第 3 步：单跑失败路径 ----------------------------------------------------
banner '单跑失败路径　python3 run.py --scenario 7'
if ! python3 run.py --scenario 7 > "$LOG_DIR/run-s7.log" 2>&1; then
    die 'run.py --scenario 7 非 0 退出' "exit=$?" 'exit=0' "$LOG_DIR/run-s7.log"
fi
grep -q 'disposition=replan_channel' "$LOG_DIR/run-s7.log" \
    || die '镜 5 的换渠道段不在屏幕上了' '输出里没有 disposition=replan_channel' \
           '第七道闸判 replan_channel' "$LOG_DIR/run-s7.log"
grep -q '业务状态  : compensated' "$LOG_DIR/run-s7.log" \
    || die '镜 6 的收口行不在屏幕上了' '输出里没有「业务状态  : compensated」' \
           '业务状态收在 compensated' "$LOG_DIR/run-s7.log"
ok 'exit=0，镜 5 换渠道段与镜 6 收口行都在'

# --- 第 4 步：证据束落盘 ------------------------------------------------------
banner '证据束落盘　python3 scripts/make_evidence.py'
if ! python3 scripts/make_evidence.py > "$LOG_DIR/evidence.log" 2>&1; then
    die 'make_evidence.py 非 0 退出' "exit=$?" 'exit=0' "$LOG_DIR/evidence.log"
fi
bundles="$(grep -c '^  \[OK\] evidence/scenario-' "$LOG_DIR/evidence.log" || true)"
if [ "$bundles" != "$EXPECT_BUNDLES" ]; then
    die '落盘的证据束个数与期望不符' "${bundles} 束" "${EXPECT_BUNDLES} 束" "$LOG_DIR/evidence.log"
fi
ok "${bundles} 束（$(grep -o 'scenario-[0-9R]*' "$LOG_DIR/evidence.log" | sort -u | tr '\n' ' ')）"

# --- 第 5 步：一条命令核验 ----------------------------------------------------
banner '核验　python3 scripts/verify.py'
if ! python3 scripts/verify.py > "$LOG_DIR/verify.log" 2>&1; then
    die 'verify.py 非 0 退出' "$(grep -E '^\[FAIL\]|^RESULT' "$LOG_DIR/verify.log" | tail -2 | tr '\n' ' ')" \
        "${EXPECT_VERIFY}，exit=0" "$LOG_DIR/verify.log"
fi
if ! grep -qF "$EXPECT_VERIFY" "$LOG_DIR/verify.log"; then
    die '核验结论行与期望不符' "$(grep -E '^RESULT' "$LOG_DIR/verify.log" | tail -1)" \
        "$EXPECT_VERIFY" "$LOG_DIR/verify.log"
fi
warns="$(grep -c '· warn:' "$LOG_DIR/verify.log" || true)"
ok "${EXPECT_VERIFY}，exit=0（夹着 ${warns} 条 warn，不影响判定）"

# --- 收尾：工作区脏了，让人自己选怎么处理 -------------------------------------
dirty_evidence="$(git status --porcelain -- evidence/ | wc -l | tr -d ' ')"
dirty_all="$(git status --porcelain | wc -l | tr -d ' ')"

printf '\n\033[1m前置 5 步全部通过。\033[0m\n'
printf '\n\033[33m工作区现在是脏的：evidence/ %s 行，全仓共 %s 行。\033[0m\n' \
    "$dirty_evidence" "$dirty_all"
printf '第 4 步重写了每个证据文件的出处头（# generated at ... from <sha>），所以\033[1m必然\033[0m变 M。\n'
printf '\n最后一镜打的是 git diff --stat，\033[1m必须带着干净工作区去录\033[0m。二选一，\033[1m自己决定\033[0m：\n'
printf '  A) 把这次重跑提交掉：        git add evidence/ && git commit -m "chore: 录制前证据束重跑"\n'
printf '  B) 现在就还原，然后开录：    git checkout -- evidence/\n'
printf '\n\033[1m本脚本不替你做这个决定\033[0m —— 你可能正有未提交的在制品，一条自动还原就冲掉了。\n'
printf '\n\033[1m选 B 的话现在就还原，不必拖到最后一镜前\033[0m：evidence/*/maos.db 是 gitignore 的，\n'
printf '`git checkout -- evidence/` 只还原被跟踪的证据文件，\033[1m不会删掉那些 .db\033[0m ——\n'
printf '所以还原之后镜 7 的 sqlite3 查询照样跑得出来（本条已实测）。全程干净工作区最省心。\n'

# --- 录制检查清单 -------------------------------------------------------------
printf '\n\033[1m录制检查清单（对着过一遍再按录制键）\033[0m\n'
printf '  [ ] 终端字号调大到后排能看清（投影/压缩后仍可辨认）\n'
printf '  [ ] 终端窗口 ≥ 100 列 —— 状态迁移轨迹与 skill 调用链两屏都会换行\n'
printf '  [ ] 画面里不许出现 key / token / homeserver 地址：关掉会回显 env 的窗口，\n'
printf '      清掉 shell 历史里的密钥行，提示符不要带主机名或路径以外的敏感信息\n'
printf '  [ ] 最后一镜要求干净工作区：上面的 A/B 已经选完了吗\n'
printf '  [ ] 每一镜当天现跑一次对屏 —— 对不上以当天输出为准重贴，不许照分镜念\n'
printf '\n分镜见 docs/demo-script.md。\n'
