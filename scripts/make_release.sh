#!/usr/bin/env bash
#
# make_release.sh —— 打「提交用」的压缩包。
#
# 复赛要求提交「可执行代码仓库（含源码/压缩包）」。这个脚本产出那个压缩包，
# 并且**打完当场解压验一遍**：没验过的包等于没打。
#
#   bash scripts/make_release.sh            # 打 HEAD，产出 dist/maos-runtime-<sha7>.zip
#   bash scripts/make_release.sh --client     # 打**客户包**，产出 dist/maos-client-<sha7>.zip
#   bash scripts/make_release.sh --no-verify  # 只打包不验（不推荐，仅用于调试打包本身）
#
# 四条不可协商的口径（第 4 条只管 `--client`）：
#
#   1. **只打版本库里的东西**，用 `git clone`（浅克隆，深度见下面 CLONE_DEPTH）。绝不 `zip -r .` ——
#      那会把 .worktrees/、__pycache__/、evidence/**/maos.db、.env、
#      review/paste-*.md（.git/info/exclude 里排除的内部派单）一起打进去。
#      clone 只带版本库内容，未跟踪文件一个都进不来。
#
#      **为什么不是 `git archive`**：`git archive` 产出的目录**没有 `.git`**，
#      而 `scripts/make_evidence.py` 取不到 git sha 就按全局铁律 3
#      （「证据必须有出处」）拒绝生成 —— 实测 `exit=2`，连带
#      `maos/tests/test_repro_path.py` 的 5 条也红。也就是说 archive 出来的包
#      **跑不了 README 的 ①②**，等于交了一个不可复现的「可执行代码仓库」。
#      `clone` 保留 git 上下文，解压即等同 clone 一份仓库，
#      同时照样只带版本库内容。这是「等价手段」里唯一两头都满足的那个。
#      **深度必须够 `verify.py` 的第 9 项读到祖先 tree**，理由见下面 CLONE_DEPTH 处。
#
#   2. **打完必须解压跑一遍**：pytest 全绿 + make_evidence.py + verify.py 到 10/10 PASS。
#      任一不过就非 0 退出，不产出「跑不起来的交付物」。
#
#   3. **密钥自查是打包的最后一步，不过就非 0 退出**（全局铁律 6）。
#      查的是真值不是字样：拿本机环境里的敏感变量值做哨兵反查
#      （与 scripts/make_evidence.py 的出口脱敏同一套思路），外加已知密钥形态正则。
#      文档里出现 `MAOS_LLM_API_KEY` 这种**变量名**是正常的，不算命中。
#
#   4. **`--client` 是「裁剪 + 重产」，不是「少打几个文件」**。
#      客户包发给业务方与决策者：解压双击就看效果，不开终端，macOS + Windows 两边跑。
#      包里不许有内部开发物料（产品决策）。裁什么写在 `docs/client-manifest.txt`，
#      裁法与三条配套动作都不可省，缺一条包就是坏的：
#
#        a. **orphan commit**。`git rm` + 普通 commit 是假裁：clone 带进来的**每个**
#           commit 的 tree 都含这些文件，裁完再 commit 一次，
#           `git show HEAD^:docs/BACKLOG.md` 照样吐全文。
#           判据是 `git rev-list --all --objects` 里还剩几个对象，两种克隆深度都实测过：
#             --depth 1  —— 包里 1 个 commit，裁前 BACKLOG 可达对象 1 个
#             --depth 50 —— 包里 151 个 commit，裁前 BACKLOG 可达对象 106 个
#           两种情况下，普通 commit 之后都仍然捞得到；而 orphan + `branch -D` +
#           `remote remove` + `reflog expire` + `gc --prune=now` 之后都是 **0**
#           （旧历史整个不可达，一律被 prune 掉）。所以这一步与克隆深度无关，
#           改 `--depth` 不影响这条保证 —— 但改完要重跑一次 `--client` 才算验过。
#        b. **登记进包内 `.git/info/exclude`**。裁掉之后，留在包里的文档仍在引用它们；
#           `scripts/check_docs.py` 的射程按 git 划（见其模块头「射程 = git 管得到的
#           东西」），被忽略的路径整个不在射程内。不登记的实测后果是包内 152 条判负
#           （D-link 9 + E-missing 143，散在 27 份文档里），把守测试跟着红。
#        c. **按包内新 HEAD 重产全部证据束**。`verify.py` 第 9 项 provenance 的判据是
#           「每束自称的出处 sha == 当前 HEAD 且不带 -dirty」。orphan commit 换了 HEAD，
#           不重产就是全束对不上 —— 客户一跑核验就是 FAIL。重产**必须停在这里，
#           不许再 commit**：再 commit 一次 HEAD 又变了，证据又全部对不上（自指悖论）。
#           所以客户包的正确状态是「工作区在 evidence/ 里脏着」，这不是遗漏。
#           它不会让客户跑出 `-dirty`：`make_evidence.py::git_sha()` 的脏判定刻意用
#           `--untracked-files=no` 且排除 `evidence/` 自身（见该函数注释）。
#
# 产物落在 dist/。dist/ 已被 .gitignore 挡着（第 7 行），压缩包不会误入版本库 ——
# 这是有意的：sha 一变就要重打，把二进制提进 git 只会让每轮整合多一坨没法 review 的 diff。
# **提交前现打一次**，把 dist/ 下的包单独上传。

set -u
set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

DO_VERIFY=1
CLIENT=0
# 逐个认参数、认不出就非 0 退出：`--clientt` 这种手滑以前会被静默忽略，
# 于是人以为拿到的是客户包、实际拿到的是提交包 —— 而两者长得一模一样。
for arg in "$@"; do
  case "$arg" in
    --no-verify) DO_VERIFY=0 ;;
    --client)    CLIENT=1 ;;
    *)
      echo "未知参数: ${arg}"
      echo "用法: bash scripts/make_release.sh [--client] [--no-verify]"
      exit 2
      ;;
  esac
done

# ---------------------------------------------------------------- 0. 版本与落点
SHA7="$(git rev-parse --short=7 HEAD)"
SHA_FULL="$(git rev-parse HEAD)"
# 两种包必须是两个名字、两个文件：它们的内容差着一整套内部物料，
# 同名就意味着后打的那个会悄悄盖掉前一个，而从文件名上看不出盖的是哪种。
if [ "$CLIENT" = "1" ]; then
  NAME="maos-client-${SHA7}"
  MODE_LABEL="客户包（已裁剪）"
else
  NAME="maos-runtime-${SHA7}"
  MODE_LABEL="提交包（完整）"
fi
MANIFEST_REL="docs/client-manifest.txt"
MANIFEST="${REPO_ROOT}/${MANIFEST_REL}"
DIST="${REPO_ROOT}/dist"
ZIP="${DIST}/${NAME}.zip"

DIRTY_LINES="$(git status --porcelain | wc -l | tr -d ' ')"

echo "==> 打包 ${NAME}   模式: ${MODE_LABEL}"
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
#
# **深度不是 1**（T153）：`verify.py` 第 9 项 provenance 要回答「这批证据是不是当前
# 这份代码跑出来的」。证据入库本身会让 HEAD 前进一格，于是证据恒自称上一个 sha，
# 判据靠 `touched_outside_evidence()` 去问「那一格之后动过 evidence/ 以外的东西吗」。
# 那一问要读**祖先 commit 的 tree** —— `--depth 1` 一个字节都没带过来，于是包里
# 12 束证据全部核不出出处（实测 `provenance 4/16`、`RESULT 9/10`、verify exit=1），
# 连带这个脚本自己第 2 条口径不过、产不出可提交的包。
#
# 深度取 50 而不是"完整历史"：够跨连续若干个纯证据 commit（实测当前形态 2 就够），
# 又不必把整部历史塞进交付包。真跨过 50 格还核不出来的话，`verify.py` 会说
# 「这个克隆带不到那段历史」并按 SKIP 计，不会冒充判负（见 `_SHALLOW_HINT`）。
CLONE_DEPTH=50
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "==> git clone --depth ${CLONE_DEPTH} (${BRANCH}) -> ${STAGE}/${NAME}"
rmdir "${STAGE}/${NAME}" 2>/dev/null
if ! git clone --quiet --depth "$CLONE_DEPTH" --no-hardlinks --single-branch \
       --branch "$BRANCH" "file://${REPO_ROOT}" "${STAGE}/${NAME}"; then
  echo "[FAIL] git clone 失败"
  exit 1
fi

CLONED_SHA="$(cd "${STAGE}/${NAME}" && git rev-parse HEAD)"
if [ "$CLONED_SHA" != "$SHA_FULL" ]; then
  echo "[FAIL] 包内 HEAD (${CLONED_SHA}) 与仓库 HEAD (${SHA_FULL}) 不一致"
  exit 1
fi

# ---------------------------------------------------------------- 1b. 客户模式：裁剪 + 重产
# 只在 `--client` 下发生。默认模式一个字节都不动，它还要用于比赛提交。
CLIENT_HEAD=""
TRIM_N=0
if [ "$CLIENT" = "1" ]; then
  echo "==> 客户模式：按 ${MANIFEST_REL} 裁剪内部物料"
  if [ ! -f "$MANIFEST" ]; then
    echo "[FAIL] 找不到裁剪清单 ${MANIFEST_REL}"
    exit 1
  fi

  TRIM_PATHS=()
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"                       # 行尾的 `# 说明` 是给人看的
    line="$(printf '%s' "$line" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    [ -z "$line" ] && continue
    TRIM_PATHS+=("$line")
  done < "$MANIFEST"
  TRIM_N="${#TRIM_PATHS[@]}"
  if [ "$TRIM_N" = "0" ]; then
    echo "[FAIL] ${MANIFEST_REL} 里一条路径都没有 —— 空清单会打出一个没裁过的「客户包」"
    exit 1
  fi

  # 逐条确认清单里的路径在干净副本里**真的存在**。清单会过期（改名、目录合并），
  # 而过期清单最坏的结局是「以为裁了，其实没裁」——「裁掉了一个不存在的东西」
  # 和「裁掉了内部账本」在屏幕上长得一模一样。宁可整包打不出来。
  MISSING=""
  for p in "${TRIM_PATHS[@]}"; do
    [ -e "${STAGE}/${NAME}/${p%/}" ] || MISSING="${MISSING} ${p}"
  done
  if [ -n "$MISSING" ]; then
    echo "[FAIL] 清单里这些路径在干净副本里不存在（${MANIFEST_REL} 过期了）：${MISSING}"
    exit 1
  fi
  echo "    清单 ${TRIM_N} 条，逐条在副本里确认存在"

  if ! (
    set -e
    cd "${STAGE}/${NAME}"
    git rm -r -q -- "${TRIM_PATHS[@]%/}"
    # 登记进 .git/info/exclude：让这些路径退出 scripts/check_docs.py 的射程。
    # 见抬头口径 4b —— 不登记的实测后果是包内 152 条判负、把守测试 3 红。
    {
      echo ""
      echo "# ---- 客户发行版：以下路径按设计不随包发布（见 ${MANIFEST_REL}）----"
      echo "# 登记在这里，是为了让 scripts/check_docs.py 的射程与「包里有什么」一致："
      echo "# 被 git 忽略的路径不在射程内，留在包里的文档引用它们不算断链。"
      for p in "${TRIM_PATHS[@]}"; do echo "/${p}"; done
    } >> .git/info/exclude
    # orphan：把「裁掉」落成新基线的**第一个**提交。少了这一步裁剪就是假的，
    # 判据见抬头口径 4a。
    git checkout -q --orphan client-release
    git add -A
    git -c user.name="MAOS Release" -c user.email="release@maos.invalid" \
        commit -q -m "MAOS 客户发行版 —— 自 ${SHA7} 裁剪，内部开发物料不随包发布"
    git branch -D "$BRANCH" >/dev/null 2>&1 || true
    git remote remove origin >/dev/null 2>&1 || true
    git reflog expire --expire=now --all
    git gc --prune=now --quiet
  ); then
    echo "[FAIL] 裁剪失败"
    exit 1
  fi
  CLIENT_HEAD="$(cd "${STAGE}/${NAME}" && git rev-parse HEAD)"
  CLIENT_COMMITS="$(cd "${STAGE}/${NAME}" && git rev-list --count HEAD)"
  echo "    裁剪完成：包内成为单根仓库 HEAD=${CLIENT_HEAD} commit 数=${CLIENT_COMMITS}"

  # -- 按包内新 HEAD 重产全部证据束（抬头口径 4c）--------------------------
  # orphan commit 换了 HEAD，不重产就是全束出处对不上，客户一跑 verify.py 就 FAIL。
  # 跑完**停在这里不再 commit**：再 commit 一次 HEAD 又变，证据又全部对不上。
  regen() {
    local label="$1"; shift
    local out
    printf '    --- %-32s ' "$label"
    if out="$("$@" 2>&1)"; then
      printf '%s\n' "$(printf '%s' "$out" | tail -1)"
    else
      printf 'FAILED\n'
      printf '%s\n' "$out" | tail -20 | sed 's/^/        /'
      return 1
    fi
  }
  echo "==> 客户模式：按包内新 HEAD 重产全部证据束"
  if ! (
    cd "${STAGE}/${NAME}" || exit 1
    MAOS_EVIDENCE_PINNED_SHA="$(git rev-parse HEAD)"
    export MAOS_EVIDENCE_PINNED_SHA
    regen "make_evidence.py"             python3 scripts/make_evidence.py               || exit 1
    regen "make_evidence.py --domains"   python3 scripts/make_evidence.py --domains     || exit 1
    regen "make_case_bundle --all-paths" python3 scripts/make_case_bundle.py --all-paths || exit 1
    regen "maos.kb.experiment --r8"      python3 -m maos.kb.experiment --r8             || exit 1
    regen "gen_capability_matrix.py"     python3 scripts/gen_capability_matrix.py       || exit 1
    regen "render_trace.py"              python3 scripts/render_trace.py                || exit 1
  ); then
    echo "[FAIL] 证据重产失败 —— 客户拿到的包会在 verify.py 第 9 项判负"
    exit 1
  fi

  # 在副本里跑过 Python，就会留下 __pycache__ / .pytest_cache。默认模式从不在副本里
  # 跑东西，所以从来没有这个问题；客户模式非跑不可，于是得自己收拾。
  # 只清编译缓存，**不清 evidence/ 下的 .db** —— 那些库是这次打包自己跑出来的，
  # 且决定了客户跑 verify.py 时的分母，理由写在下面第 3 节的 .db 判据里。
  find "${STAGE}/${NAME}" -type d -name '__pycache__'   -prune -exec rm -rf {} + 2>/dev/null
  find "${STAGE}/${NAME}" -type d -name '.pytest_cache' -prune -exec rm -rf {} + 2>/dev/null
  find "${STAGE}/${NAME}" -type f -name '*.pyc' -delete 2>/dev/null
  echo "    编译缓存已清（__pycache__ / .pytest_cache / *.pyc）"
fi

# 包内工作区必须干净：脏了的话 make_evidence.py 会给证据首行的 sha 加 `-dirty`，
# 提交自查单 A-2 那条「八个场景 sha 全干净」当场就红。
# 这也是**默认模式不剔除 review/ 的原因**：review/ 下那两个文件（派单模板、守卫探针）
# 是入库的，删掉它们等于让包内工作区自带两行 `D`。派单正文 review/paste-*.md 全部未跟踪，
# clone 本来就带不进来 —— 要挡的那个东西已经挡住了。
#
# `--client` 下这条口径必须改写：重产证据必然把 evidence/ 弄脏，而那是**正确状态**
# （抬头口径 4c）。客户模式因此查两条：evidence/ **之外**必须干净，
# 外加 evidence/ 自己的硬判据 —— 每份证据自称的出处必须就是包内 HEAD。
if [ "$CLIENT" = "1" ]; then
  STAGE_DIRTY="$(cd "${STAGE}/${NAME}" && git status --porcelain -- . ':(exclude)evidence' | wc -l | tr -d ' ')"
  DIRTY_SCOPE="evidence/ 之外"
else
  STAGE_DIRTY="$(cd "${STAGE}/${NAME}" && git status --porcelain | wc -l | tr -d ' ')"
  DIRTY_SCOPE="全部"
fi
if [ "$STAGE_DIRTY" != "0" ]; then
  echo "[FAIL] 包内工作区不干净（${DIRTY_SCOPE} ${STAGE_DIRTY} 行）—— 证据 sha 会带 -dirty"
  (cd "${STAGE}/${NAME}" && git status --porcelain | head -10 | sed 's/^/      /')
  exit 1
fi

if [ "$CLIENT" = "1" ]; then
  # evidence/ 脏是对的，但脏成什么样有判据：每一份证据都必须自称出自包内 HEAD。
  # 差一份就说明有束没重产到 —— 而那一份会在客户手上变成 verify.py 的第 9 项判负。
  EVID_SHAS="$(cd "${STAGE}/${NAME}" && grep --exclude-dir=.git -rh '^# generated at' evidence/ 2>/dev/null | sed 's/.* from //' | sort -u)"
  EVID_TOTAL="$(cd "${STAGE}/${NAME}" && grep --exclude-dir=.git -rl '^# generated at' evidence/ 2>/dev/null | wc -l | tr -d ' ')"
  if [ "$EVID_SHAS" != "$CLIENT_HEAD" ]; then
    echo "[FAIL] 包内证据的出处不是「全部 == 包内 HEAD」"
    echo "       包内 HEAD : ${CLIENT_HEAD}"
    echo "       实际出处   : $(printf '%s' "$EVID_SHAS" | tr '\n' ' ')"
    exit 1
  fi
  echo "    证据出处自查: ${EVID_TOTAL} 份全部 == 包内 HEAD，无 -dirty"
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
if [ "$CLIENT" = "1" ]; then
  # 客户模式**换判据，不是放宽**。evidence/ 下的 .db 是这次打包自己跑出来的，
  # 而且它们决定了客户跑 verify.py 时的**分母**：带上是 hash-integrity 179 /
  # business-ref 136 / trace-tree 77 / case-outcome 51；删掉之后仍然 10/10 PASS，
  # 但只剩 114 / 61 / 29 / 6（实测 2026-09-14）—— 因为解压后那一步只跑
  # `make_evidence.py`，它只重建 scenario-* 那 8 束的库，case-real-01 / domains /
  # contrast-R8 三组没有库就只能少核。一个分母掉一半的 10/10 不是同一句话。
  # 两种模式要挡的是同一个东西：开发机上跑出来的、散落在 evidence/ 之外的库文件。
  DB_ALL="$(printf '%s\n' "$ENTRIES" | grep -cE '\.db$' || true)"
  DB_EVID="$(printf '%s\n' "$ENTRIES" | grep -cE '(^|/)evidence/.*\.db$' || true)"
  DB_STRAY=$((DB_ALL - DB_EVID))
  if [ "$DB_STRAY" != "0" ]; then
    echo "    [FAIL] evidence/ 之外出现 ${DB_STRAY} 个 .db"
    printf '%s\n' "$ENTRIES" | grep -E '\.db$' | grep -vE '(^|/)evidence/.*\.db$' | head -5 | sed 's/^/      /'
    EXCL_FAIL=1
  else
    echo "    [ok]   *.db（sqlite 库）: evidence/ 下 ${DB_EVID} 个（本次打包跑出来的），别处 0 个"
  fi
else
  check_absent '\.db$'             "*.db（sqlite 库）"
fi
check_absent '(^|/)review/paste' "review/paste-*.md（内部派单正文）"
check_absent '(^|/)\.env$'       ".env 实体文件（.env.example 模板允许存在）"

# 客户模式追加：清单里的每一条都要在**两个地方**都查不到 ——
# zip 条目里（文件没进包）与 git 历史里（内容也捞不出来）。
# 后一条才是 orphan commit 那一步的机器判据：只 `git rm` + 普通 commit 的话，
# 文件确实不在条目里，但 `git show HEAD^:docs/BACKLOG.md` 照样吐全文。
if [ "$CLIENT" = "1" ]; then
  echo "    --- 裁剪项逐条自查（zip 条目 + git 历史）---"
  HIST="$(cd "${STAGE}/${NAME}" && git rev-list --all --objects)"
  for p in "${TRIM_PATHS[@]}"; do
    bare="${p%/}"
    # 清单里的路径只含字母数字与 / - . ，正则里要转义的只有点
    esc="$(printf '%s' "$bare" | sed 's/\./\\./g')"
    case "$p" in
      */) pat="(^|/)${esc}/" ;;
      *)  pat="(^|/)${esc}\$" ;;
    esac
    zn="$(printf '%s\n' "$ENTRIES" | grep -cE -- "$pat" || true)"
    # `git rev-list --objects` 每行是 `<sha> <路径>`，只比对路径字段、且要求在分段
    # 边界上结束。拿整行做子串匹配会假警：`review` 能在 `maos/agents/reviewer.py`、
    # `scripts/review_invoices.py` 里各命中一次，报出 4 条其实不存在的残留。
    hn="$(printf '%s\n' "$HIST" | awk '{print $2}' | grep -cE "^${esc}(/|\$)" || true)"
    if [ "$zn" != "0" ] || [ "$hn" != "0" ]; then
      echo "    [FAIL] ${p}  zip ${zn} 命中 / git 历史 ${hn} 命中"
      EXCL_FAIL=1
    else
      printf '    [ok]   %-34s zip 0 命中 / git 历史 0 命中\n' "$p"
    fi
  done
fi

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

  # 解压出来的目录里**有** `.git` —— 上面第 1 节的正向检查要的就是它，
  # `make_evidence.py` 要靠它取出处 sha。但它不是完整仓库，两种模式还不一样：
  #   默认模式   浅克隆，带 `.git/shallow`，只有 clone 深度那几个 commit
  #   `--client` orphan 之后是**单根仓库**：1 个 commit、没有 `.git/shallow`
  #              （`git rev-parse --is-shallow-repository` 回 false），旧历史已 prune
  # 两种情况下都一样：仓库里 `git cat-file` 得到的历史对象，包里查不到。
  # 这正是评委 / 客户拿到压缩包时的处境，所以验证必须在这个处境下做，不许 cd 回仓库取巧。
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
# 已登记的**假**密钥字面量。每条都要写出处与理由 —— 口径同 scripts/check_docs.py
# 的 ALLOW_MISSING：写不出理由的不该登记，否则这一层判据形同虚设。
# 登记的是**字面量本身**而不是文件名：同一个假值换个文件照样放行，而那个文件里
# 冒出别的密钥形态仍然判负。剔掉了几处照样打印出来 —— 一层会静默缩小的判据比没有更坏。
FAKE_LITERALS='sk-FAKE0000000000000000000000000000000000'
# ^ maos/tests/test_model_routing.py:37 的密钥闸测试桩。源码里紧挨着的那行注释写的是
#   「故意构造的**假** key，只在第 4 组用例里出现。它不是任何真实凭证」；字面量自己
#   拼着 FAKE、尾巴 36 个 0。没有这一条，B 层会恒定判负、这个包永远打不出 ✅。
SHAPE_FILES=""
while IFS= read -r f; do
  [ -z "$f" ] && continue
  # -a：evidence/ 下有 .db，二进制也要能把命中的字面量抠出来比对
  if grep -aEoh -- "$PATTERNS" "$f" 2>/dev/null | grep -qvxF -- "$FAKE_LITERALS"; then
    SHAPE_FILES="${SHAPE_FILES}${f}
"
  fi
done < <(grep --exclude-dir=.git -rEl -- "$PATTERNS" "$SCAN_DIR" 2>/dev/null)
SHAPE_HITS="$(printf '%s' "$SHAPE_FILES" | grep -c . || true)"
FAKE_SEEN="$(grep --exclude-dir=.git -raEoh -- "$PATTERNS" "$SCAN_DIR" 2>/dev/null | grep -cxF -- "$FAKE_LITERALS" || true)"
if [ "$SHAPE_HITS" != "0" ]; then
  echo "    [FAIL] 密钥形态命中 ${SHAPE_HITS} 个文件："
  printf '%s' "$SHAPE_FILES" | grep . | sed "s|${SCAN_DIR}/|      |"
  SECRET_FAIL=1
else
  echo "    [ok]   密钥形态正则: 0 命中（另有 ${FAKE_SEEN} 处命中的是已登记的假字面量）"
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
echo "模式          : ${MODE_LABEL}"
echo "大小 / 条目数 : ${ZIP_SIZE} / ${ZIP_ENTRIES}"
echo "基线          : ${SHA_FULL}"
if [ "$CLIENT" = "1" ]; then
  echo "裁剪          : ${TRIM_N} 项（${MANIFEST_REL}）"
  echo "包内 HEAD     : ${CLIENT_HEAD}"
  echo "                单根 orphan commit；全部证据的出处都指向它，客户重跑即自洽"
fi
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
