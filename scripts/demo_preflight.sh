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
#      MAOS_EXPECT_TESTS=749         第 1 步的测试条数
#      MAOS_EXPECT_BUNDLES=8         第 4 步落盘的证据束个数
#      MAOS_EXPECT_VERIFY='RESULT: 7/7 PASS'   第 5 步的核验结论行
#    负例自证就靠它：MAOS_EXPECT_TESTS=999 bash scripts/demo_preflight.sh
#    → 非 0 退出并指出是第 1 步。
#
# 3. **零出网、不依赖任何 API key。** 全程走 ScriptedModelClient 路径，
#    评委在没有任何密钥的机器上照样跑得出同一份结果 —— 这是卖点，第 0 步会显式打出来。
#
# 4. **本脚本不替人做决定。** 第 4 步必然把 evidence/ 弄脏（出处头每跑一次都变），
#    脚本只统计脏行数并给出二选一提示，**绝不自动还原** ——
#    万一人类正好有未提交的在制品，一条自动还原就把它冲掉了。
#
# 5. 本机没有 `python` 命令，全脚本一律 python3。

set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
cd "$REPO_ROOT"

EXPECT_TESTS="${MAOS_EXPECT_TESTS:-749}"
EXPECT_BUNDLES="${MAOS_EXPECT_BUNDLES:-8}"
EXPECT_VERIFY="${MAOS_EXPECT_VERIFY:-RESULT: 7/7 PASS}"

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

# --- 第 0 步：说清楚这一跑不需要任何密钥 ------------------------------------
printf '\033[1mMAOS Demo 录制前置\033[0m　仓库根 %s\n' "$REPO_ROOT"
printf '提交 %s\n' "$(git rev-parse --short HEAD)"
printf '\n本脚本\033[1m零出网、不读任何 API key\033[0m：全程走 ScriptedModelClient 路径，\n'
printf '拿到仓库的人在没有任何密钥的机器上跑，得到的是同一份确定性结果。\n'

# --- 第 1 步：全量测试 --------------------------------------------------------
banner '全量测试　python3 -m pytest maos/tests -q'
if ! python3 -m pytest maos/tests -q > "$LOG_DIR/pytest.log" 2>&1; then
    die '存量测试没有全绿' "$(tail -3 "$LOG_DIR/pytest.log" | tr '\n' ' ')" \
        "${EXPECT_TESTS} passed，0 failed" "$LOG_DIR/pytest.log"
fi
passed="$(grep -Eo '[0-9]+ passed' "$LOG_DIR/pytest.log" | tail -1 | grep -Eo '^[0-9]+' || true)"
[ -n "$passed" ] || die '解析不出测试条数' '（输出里没有 "N passed"）' \
    "${EXPECT_TESTS} passed" "$LOG_DIR/pytest.log"
if [ "$passed" != "$EXPECT_TESTS" ]; then
    die '测试条数与期望不符' "${passed} passed" "${EXPECT_TESTS} passed" "$LOG_DIR/pytest.log"
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
