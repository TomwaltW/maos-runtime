#!/usr/bin/env bash
#
# make_release.sh —— 打「提交用」的压缩包。
#
# 复赛要求提交「可执行代码仓库（含源码/压缩包）」。这个脚本产出那个压缩包，
# 并且**打完当场解压验一遍**：没验过的包等于没打。
#
#   bash scripts/make_release.sh            # 打 HEAD，产出 dist/maos-runtime-<sha7>.zip
#   bash scripts/make_release.sh --no-verify  # 只打包不验（不推荐，仅用于调试打包本身）
#
# 三条不可协商的口径：
#
#   1. **只打版本库里的东西**，用 `git clone --depth 1`。绝不 `zip -r .` ——
#      那会把 .worktrees/、__pycache__/、evidence/**/maos.db、.env、
#      review/paste-*.md（.git/info/exclude 里排除的内部派单）一起打进去。
#      clone 只带版本库内容，未跟踪文件一个都进不来。
#
#      **为什么不是 `git archive`**：`git archive` 产出的目录**没有 `.git`**，
#      而 `scripts/make_evidence.py` 取不到 git sha 就按全局铁律 3
#      （「证据必须有出处」）拒绝生成 —— 实测 `exit=2`，连带
#      `maos/tests/test_repro_path.py` 的 5 条也红。也就是说 archive 出来的包
#      **跑不了 README 的 ①②**，等于交了一个不可复现的「可执行代码仓库」。
#      `clone --depth 1` 保留 git 上下文，解压即等同 clone 一份仓库，
#      同时照样只带版本库内容。这是「等价手段」里唯一两头都满足的那个。
#
#   2. **打完必须解压跑一遍**：pytest 全绿 + make_evidence.py + verify.py 到 7/7 PASS。
#      任一不过就非 0 退出，不产出「跑不起来的交付物」。
#
#   3. **密钥自查是打包的最后一步，不过就非 0 退出**（全局铁律 6）。
#      查的是真值不是字样：拿本机环境里的敏感变量值做哨兵反查
#      （与 scripts/make_evidence.py 的出口脱敏同一套思路），外加已知密钥形态正则。
#      文档里出现 `MAOS_LLM_API_KEY` 这种**变量名**是正常的，不算命中。
#
# 产物落在 dist/。dist/ 已被 .gitignore 挡着（第 7 行），压缩包不会误入版本库 ——
# 这是有意的：sha 一变就要重打，把二进制提进 git 只会让每轮整合多一坨没法 review 的 diff。
# **提交前现打一次**，把 dist/ 下的包单独上传。

set -u
set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

DO_VERIFY=1
[ "${1:-}" = "--no-verify" ] && DO_VERIFY=0

# ---------------------------------------------------------------- 0. 版本与落点
SHA7="$(git rev-parse --short=7 HEAD)"
SHA_FULL="$(git rev-parse HEAD)"
NAME="maos-runtime-${SHA7}"
DIST="${REPO_ROOT}/dist"
ZIP="${DIST}/${NAME}.zip"

DIRTY_LINES="$(git status --porcelain | wc -l | tr -d ' ')"

echo "==> 打包 ${NAME}"
echo "    HEAD      : ${SHA_FULL}"
echo "    工作区脏行: ${DIRTY_LINES}"
if [ "$DIRTY_LINES" != "0" ]; then
  echo ""
  echo "    ⚠️  工作区有 ${DIRTY_LINES} 行未提交改动。"
  echo "       本脚本 clone 的是**当前分支的 HEAD**，这些改动**不会**进包。"
  echo "       要把它们打进去，先 commit 再重跑本脚本。"
  echo ""
fi

mkdir -p "$DIST"
rm -f "$ZIP"

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/maos-release-XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "${STAGE}/${NAME}"

# ---------------------------------------------------------------- 1. clone 出干净副本
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "==> git clone --depth 1 (${BRANCH}) -> ${STAGE}/${NAME}"
rmdir "${STAGE}/${NAME}" 2>/dev/null
if ! git clone --quiet --depth 1 --no-hardlinks --single-branch \
       --branch "$BRANCH" "file://${REPO_ROOT}" "${STAGE}/${NAME}"; then
  echo "[FAIL] git clone 失败"
  exit 1
fi

CLONED_SHA="$(cd "${STAGE}/${NAME}" && git rev-parse HEAD)"
if [ "$CLONED_SHA" != "$SHA_FULL" ]; then
  echo "[FAIL] 包内 HEAD (${CLONED_SHA}) 与仓库 HEAD (${SHA_FULL}) 不一致"
  exit 1
fi

# 包内工作区必须干净：脏了的话 make_evidence.py 会给证据首行的 sha 加 `-dirty`，
# 提交自查单 A-2 那条「八个场景 sha 全干净」当场就红。
# 这也是**不剔除 review/ 的原因**：review/ 下那两个文件（派单模板、守卫探针）是入库的，
# 删掉它们等于让包内工作区自带两行 `D`。派单正文 review/paste-*.md 全部未跟踪，
# clone 本来就带不进来 —— 要挡的那个东西已经挡住了。
STAGE_DIRTY="$(cd "${STAGE}/${NAME}" && git status --porcelain | wc -l | tr -d ' ')"
if [ "$STAGE_DIRTY" != "0" ]; then
  echo "[FAIL] 包内工作区不干净（${STAGE_DIRTY} 行）—— 证据 sha 会带 -dirty"
  exit 1
fi

FILE_COUNT="$(find "${STAGE}/${NAME}" -type f -not -path '*/.git/*' | wc -l | tr -d ' ')"
GIT_SIZE="$(du -sh "${STAGE}/${NAME}/.git" | awk '{print $1}')"
echo "    版本库文件数: ${FILE_COUNT}   .git 体积: ${GIT_SIZE}   工作区: 干净"

# ---------------------------------------------------------------- 2. 打 zip
# zip 而不是 tar.gz：评委多半在 macOS / Windows 上双击解压，zip 两边都是原生支持。
echo "==> zip -> ${ZIP}"
if ! (cd "$STAGE" && zip -q -r "$ZIP" "$NAME"); then
  echo "[FAIL] zip 失败"
  exit 1
fi
ZIP_SIZE="$(ls -lh "$ZIP" | awk '{print $5}')"
ZIP_ENTRIES="$(unzip -l "$ZIP" | tail -1 | awk '{print $2}')"
echo "    大小: ${ZIP_SIZE}   条目数: ${ZIP_ENTRIES}"

# ---------------------------------------------------------------- 3. 排除项自查
echo "==> 排除项自查"
EXCL_FAIL=0
# 只看条目名（unzip -Z1），不看 unzip -l 的抬头 —— 抬头里印着 zip 自己的路径，
# 而这个路径就带着 `.worktrees`，拿它当判据会永远假警。
ENTRIES="$(unzip -Z1 "$ZIP")"
check_absent() {
  local pat="$1" label="$2" n
  n="$(printf '%s\n' "$ENTRIES" | grep -cE -- "$pat" || true)"
  if [ "$n" != "0" ]; then
    echo "    [FAIL] 包里出现 ${label}：${n} 条"
    printf '%s\n' "$ENTRIES" | grep -E -- "$pat" | head -5 | sed 's/^/      /'
    EXCL_FAIL=1
  else
    echo "    [ok]   ${label}: 0 命中"
  fi
}
check_absent '(^|/)\.worktrees/' ".worktrees/"
check_absent '(^|/)__pycache__/' "__pycache__/"
check_absent '\.db$'             "*.db（sqlite 库）"
check_absent '(^|/)review/paste' "review/paste-*.md（内部派单正文）"
check_absent '(^|/)\.env$'       ".env 实体文件（.env.example 模板允许存在）"

# 正向检查：.git 必须**在**包里，否则 make_evidence.py 取不到出处、拒绝生成。
if printf '%s\n' "$ENTRIES" | grep -qE '(^|/)\.git/'; then
  echo "    [ok]   .git/ 在包里（make_evidence.py 要靠它取出处 sha）"
else
  echo "    [FAIL] 包里没有 .git/ —— 解压后 make_evidence.py 会拒绝生成证据"
  EXCL_FAIL=1
fi

# ---------------------------------------------------------------- 4. 解压验证
VERIFY_DIR=""
if [ "$DO_VERIFY" = "1" ]; then
  echo "==> 解压验证（pytest + make_evidence.py + verify.py）"
  VERIFY_DIR="$(mktemp -d "${TMPDIR:-/tmp}/maos-relverify-XXXXXX")"
  if ! unzip -q "$ZIP" -d "$VERIFY_DIR"; then
    echo "[FAIL] 解压失败"
    exit 1
  fi
  RUN="${VERIFY_DIR}/${NAME}"

  # 解压出来的目录**没有 .git**。这正是评委拿到压缩包时的处境，
  # 所以验证必须在这个处境下做，不许 cd 回仓库取巧。
  (
    cd "$RUN" || exit 1
    echo "    --- pytest ---"
    python3 -m pytest maos/tests -q 2>&1 | tail -2
    exit "${PIPESTATUS[0]}"
  )
  PYTEST_RC=$?

  (
    cd "$RUN" || exit 1
    echo "    --- make_evidence.py ---"
    python3 scripts/make_evidence.py 2>&1 | tail -3
    exit "${PIPESTATUS[0]}"
  )
  EVID_RC=$?

  (
    cd "$RUN" || exit 1
    echo "    --- verify.py ---"
    python3 scripts/verify.py 2>&1 | grep -E '^\[(PASS|FAIL|SKIP)\]|^RESULT:'
    exit "${PIPESTATUS[0]}"
  )
  VERIFY_RC=$?

  # 解压出来的 git 上下文得是活的：证据首行 sha 必须是打包的那个 sha，且不带 -dirty。
  # 带了 -dirty 就说明解压后的工作区与 HEAD 对不上，提交自查单 A-2 会红。
  DIRTY_EVID="$(cd "$RUN" && grep --exclude-dir=.git -rl '^# generated at.*-dirty' evidence/ 2>/dev/null | wc -l | tr -d ' ')"
  EVID_SHA="$(cd "$RUN" && grep --exclude-dir=.git -rh '^# generated at' evidence/ 2>/dev/null | sed 's/.* from //' | sort -u | tr '\n' ' ')"
  echo "    --- 解压后的出处 sha ---"
  echo "      证据首行 sha : ${EVID_SHA}"
  echo "      带 -dirty 的 : ${DIRTY_EVID} 个"
  if [ "$DIRTY_EVID" != "0" ]; then
    echo "    [FAIL] 解压后生成的证据带 -dirty"
    EXCL_FAIL=1
  fi

  echo "    pytest exit=${PYTEST_RC}  make_evidence exit=${EVID_RC}  verify exit=${VERIFY_RC}"
else
  PYTEST_RC=0; EVID_RC=0; VERIFY_RC=0
  echo "==> 跳过解压验证（--no-verify）"
fi

# ---------------------------------------------------------------- 5. 密钥自查
# 全局铁律 6：禁止把密钥写进任何文件。这一步是打包的最后一道闸。
echo "==> 密钥自查"
SECRET_FAIL=0
SCAN_DIR="${STAGE}/${NAME}"

# A 层（硬判据）：哨兵反查 —— 拿本机环境里的敏感变量**值**去包里找。
# 只取长度 ≥ 8 的值，短值（"1"、"true"）会误伤。值本身一个字都不打印。
SENTINEL_HITS=0
SENTINEL_NAMES=""
while IFS='=' read -r vname vval; do
  case "$vname" in
    *TOKEN*|*KEY*|*SECRET*|*PASSWORD*|*CREDS*|*DSN*)
      [ "${#vval}" -lt 8 ] && continue
      if grep --exclude-dir=.git -rqF -- "$vval" "$SCAN_DIR" 2>/dev/null; then
        SENTINEL_HITS=$((SENTINEL_HITS + 1))
        SENTINEL_NAMES="${SENTINEL_NAMES} ${vname}"
      fi
      ;;
  esac
done < <(env)
if [ "$SENTINEL_HITS" != "0" ]; then
  echo "    [FAIL] 哨兵反查命中：环境变量${SENTINEL_NAMES} 的值出现在包里（值不打印）"
  SECRET_FAIL=1
else
  echo "    [ok]   哨兵反查: 0 命中（本机敏感变量的值都没进包）"
fi

# B 层（硬判据）：已知密钥形态。命中即失败。
# 逐条都是「长度够、前缀确定」的真密钥形态，不会被文档里的变量名触发。
PATTERNS='sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[bpsa]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|syt_[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
SHAPE_HITS="$(grep --exclude-dir=.git -rEl "$PATTERNS" "$SCAN_DIR" 2>/dev/null | wc -l | tr -d ' ')"
if [ "$SHAPE_HITS" != "0" ]; then
  echo "    [FAIL] 密钥形态命中 ${SHAPE_HITS} 个文件："
  grep --exclude-dir=.git -rEl "$PATTERNS" "$SCAN_DIR" 2>/dev/null | sed "s|${SCAN_DIR}/|      |"
  SECRET_FAIL=1
else
  echo "    [ok]   密钥形态正则: 0 命中"
fi

# C 层（只打印，不判负）：字样计数。文档里提变量名是正常的，人扫一眼即可。
echo "    --- 字样计数（供人扫一眼，不作判据）---"
for w in api_key API_KEY token TOKEN homeserver creds password; do
  c="$(grep --exclude-dir=.git -rIl -- "$w" "$SCAN_DIR" 2>/dev/null | wc -l | tr -d ' ')"
  printf '      %-12s 出现在 %s 个文件\n' "$w" "$c"
done

# ---------------------------------------------------------------- 6. 收尾判定
echo ""
echo "================ 结果 ================"
echo "包            : ${ZIP}"
echo "大小 / 条目数 : ${ZIP_SIZE} / ${ZIP_ENTRIES}"
echo "基线          : ${SHA_FULL}"
RC=0
[ "$EXCL_FAIL"   != "0" ] && RC=1
[ "$SECRET_FAIL" != "0" ] && RC=1
[ "$PYTEST_RC"   != "0" ] && RC=1
[ "$EVID_RC"     != "0" ] && RC=1
[ "$VERIFY_RC"   != "0" ] && RC=1

if [ -n "$VERIFY_DIR" ]; then
  rm -rf "$VERIFY_DIR"
fi

if [ "$RC" = "0" ]; then
  echo "结论          : ✅ 打包 + 解压验证 + 密钥自查 全过"
else
  echo "结论          : ❌ 有未通过项，见上（包已产出但**不要提交**）"
fi
exit "$RC"
