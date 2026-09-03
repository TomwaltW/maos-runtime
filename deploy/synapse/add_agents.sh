#!/usr/bin/env bash
# 给退款圆桌的五个岗位各建一个 Matrix 账号、拉进演示房，产出 ~/.maos-matrix/agents.env。
#
# 房间里原本只有 maos-bot 一个身份在说话，五岗靠 `【岗位 · 工号】` 前缀区分。跑完这个
# 脚本，Element 里就是五个头像、五个显示名 —— 谁说的一眼可见。
#
# 幂等：重复跑不炸，也**不重复登录**。口令沿用 agents.env 里的，token 先用
# /account/whoami 验一次、能用就不重登，已经在房里的号整段跳过。这不是省事，是必须：
# Synapse 的 rc_login.address 默认 burst_count=5 / 0.003 per_second（实测于容器内
# synapse/config/ratelimiting.py），五个号首跑正好顶满，无条件重登第二次就卡死 ——
# 而症状是进程**静止**不是报错（docs/BACKLOG.md 的 `## task-C1` 最后一条写着完整现象）。
# 放宽 homeserver.yaml 的限流不是选项（docs/DECISIONS.md 2026-08-29 那条已定）。
#
# 与 up.sh 的分工：up.sh 起容器、建房、管 maos-bot/boss/intern 三个号，并且**整份重写**
# room.env（:247-260）。所以本脚本的产物是**独立**的 agents.env，一个字也不碰 room.env
# 与 creds.txt —— 不然下次 up.sh 一跑，五岗的 token 就没了。
#
# 口令与 access_token 只落 ~/.maos-matrix/（chmod 600），本脚本里没有任何硬编码凭证，
# 任何一行输出都不打 token 与口令（铁律 6）。
#
#   bash deploy/synapse/add_agents.sh                 # 建号 + 进房 + 写 agents.env
#   bash deploy/synapse/add_agents.sh --dry-run       # 只打计划，一个请求都不发
#   bash deploy/synapse/add_agents.sh --room '!x:maos.local'   # 覆盖房间
set -euo pipefail

# localhost 不该走代理：本机常挂 Clash，走了代理就连不上自己的 8008。
export NO_PROXY="localhost,127.0.0.1,::1,${NO_PROXY:-}"
export no_proxy="$NO_PROXY"

SERVER_NAME="${MAOS_SERVER_NAME:-maos.local}"
HS_PORT="${MAOS_HS_PORT:-8008}"
HS_URL="http://localhost:${HS_PORT}"

SYNAPSE_CT="maos-synapse"
SHARE="${HOME}/.maos-matrix"
ROOM_ENV="${SHARE}/room.env"
AGENTS_ENV="${SHARE}/agents.env"

#: 五个岗位：agent_id | Matrix localpart | 岗位名（= Matrix 显示名）。
#: 逐字对齐跨轨契约 §1.1。岗位名的**权威定义**是 maos/roundtable/team.py 的 TITLES
#: （T87），这里是第二份字面量 —— shell 没法 import Python，只能重复一次；两处不一致
#: 的症状是「房间里显示名与回帖里的名牌对不上」，已记 docs/DECISIONS.md 的 `## task-T84`。
AGENTS=(
  "refund-intake|maos-intake|申请受理岗"
  "refund-policy|maos-policy|规则审核岗"
  "refund-evidence|maos-evidence|证据核验岗"
  "refund-risk|maos-risk|风险反欺诈岗"
  "refund-finance|maos-finance|财务执行岗"
)

DRY_RUN=""
ROOM_OVERRIDE=""

say() { printf '[agents] %s\n' "$*"; }
die() { printf '[agents] 失败：%s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --room)    ROOM_OVERRIDE="${2:-}"; [ -n "$ROOM_OVERRIDE" ] || die "--room 后面要给房间 id"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *)         die "认不出的参数：$1（只认 --dry-run / --room <id>）" ;;
  esac
done

# jq 不是本机必备，JSON 一律交给 python3 从 stdin 取字段（同 up.sh:63）。
jget() { python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get(sys.argv[1],""))' "$1"; }

# agent_id -> env 键名里的那一段：refund-intake -> REFUND_INTAKE。
# 与 hiclaw/room_voices.py::env_keys_of 同源，改一处要改两处。
agent_key() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]' | tr '-' '_'; }

# ---------------------------------------------------------------- 0. 前置
[ -f "$ROOM_ENV" ] || die "没有 ${ROOM_ENV}，先跑 bash deploy/synapse/up.sh"
# shellcheck disable=SC1090
. "$ROOM_ENV"

ROOM_ID="${ROOM_OVERRIDE:-${MATRIX_ROOM_ID:-}}"
[ -n "$ROOM_ID" ] || die "取不到房间 id（room.env 里没有 MATRIX_ROOM_ID，也没给 --room）"
BOT_MXID="${MATRIX_USER:-@maos-bot:${SERVER_NAME}}"
BOT_TOKEN="${MATRIX_TOKEN:-}"

if [ -n "$DRY_RUN" ]; then
  say "== dry-run：只打计划，不发任何请求 =="
  say "homeserver ${HS_URL}    房间 ${ROOM_ID}    邀请人 ${BOT_MXID}"
  for row in "${AGENTS[@]}"; do
    aid="${row%%|*}"; rest="${row#*|}"; lp="${rest%%|*}"; title="${rest#*|}"
    key="$(agent_key "$aid")"
    say "岗位 ${aid}（${title}） -> @${lp}:${SERVER_NAME}"
    say "     键 MAOS_AGENT_${key}_USER / _PASSWORD / _TOKEN"
  done
  say "产物 ${AGENTS_ENV}（chmod 600，末尾追加 MAOS_ROOM_BOTS）"
  say "不碰 ${ROOM_ENV} 与 ${SHARE}/creds.txt"
  exit 0
fi

command -v docker >/dev/null 2>&1 || die "本机没有 docker"
command -v python3 >/dev/null 2>&1 || die "本机没有 python3"
command -v openssl >/dev/null 2>&1 || die "本机没有 openssl（要它生成口令）"
curl -sf "${HS_URL}/_matrix/client/versions" >/dev/null 2>&1 \
  || die "${HS_URL} 不通，先跑 bash deploy/synapse/up.sh"
[ "$(docker inspect -f '{{.State.Running}}' "$SYNAPSE_CT" 2>/dev/null || echo false)" = "true" ] \
  || die "容器 ${SYNAPSE_CT} 没在跑（注册要 docker exec 进去）"

mkdir -p "$SHARE"
chmod 700 "$SHARE"

# ---------------------------------------------------------------- 1. 复用已有产物
# 口令与 token 都在这里面。**先读**是幂等的全部要点：重跑时口令不变（人类记下的那份
# 仍然有效）、token 能用就不重登（避开 rc_login）。
if [ -f "$AGENTS_ENV" ]; then
  # shellcheck disable=SC1090
  . "$AGENTS_ENV"
  say "沿用已有的 ${AGENTS_ENV}（口令与仍有效的 token 都不重新生成）"
fi

# ---------------------------------------------------------------- 2. 工具函数
reg() {  # reg <localpart> <password>；已存在视作成功（同 up.sh:113-122）
  local u="$1" p="$2" out
  out="$(docker exec "$SYNAPSE_CT" register_new_matrix_user \
        -u "$u" -p "$p" --no-admin -c /data/homeserver.yaml "$HS_URL" 2>&1)" || true
  case "$out" in
    *"User ID already taken"*) say "  账号 ${u} 已存在" ;;
    *Success*|"")              say "  账号 ${u} 已注册" ;;
    *)                         die "注册 ${u} 失败：${out}" ;;
  esac
}

whoami_ok() {  # whoami_ok <token> <expected_mxid>（同 up.sh:163-167）
  [ -n "$1" ] || return 1
  [ "$(curl -s -H "Authorization: Bearer $1" "${HS_URL}/_matrix/client/v3/account/whoami" \
       | jget user_id)" = "$2" ]
}

#: 本次真正发生过几次登录。计数与「第 2 次起先歇 3 秒」的判据都必须留在**主循环**里：
#: `login` 是在 `$(...)` 里被调的，命令替换跑在子 shell，函数内部对这个变量的自增
#: 传不回来 —— 判据于是恒为假，五个号连着登、一次都不歇。这次没撞 429 纯粹因为
#: rc_login.address 的 burst 正好是 5、五个号刚好用完，加第六个岗就卡死。
#: （限流参数实测于容器内 synapse/config/ratelimiting.py：per_second 0.003 / burst 5。）
LOGIN_COUNT=0

login() {  # login <localpart> <password> -> access_token；撞限流按 retry_after_ms 退避
  local u="$1" p="$2" body resp tok wait_ms
  body="$(python3 -c 'import json,sys;print(json.dumps({"type":"m.login.password","identifier":{"type":"m.id.user","user":sys.argv[1]},"password":sys.argv[2],"initial_device_display_name":"maos-agent"}))' "$u" "$p")"
  for _ in 1 2 3 4 5; do
    resp="$(curl -s -XPOST "${HS_URL}/_matrix/client/v3/login" -d "$body")"
    tok="$(printf '%s' "$resp" | jget access_token)"
    if [ -n "$tok" ]; then printf '%s' "$tok"; return 0; fi
    [ "$(printf '%s' "$resp" | jget errcode)" = "M_LIMIT_EXCEEDED" ] || break
    wait_ms="$(printf '%s' "$resp" | jget retry_after_ms)"
    sleep "$(python3 -c 'import sys;print(max(1,int((sys.argv[1] or 1000))//1000)+1)' "$wait_ms")"
  done
  die "登录 ${u} 失败：$(printf '%s' "$resp" | jget error)"
}

# ---------------------------------------------------------------- 3. bot 与房间
# 🔴 **绝不重登 maos-bot**：它的 token 由 up.sh 管，重登既浪费一次 rc_login 配额，
# 又会让 room.env 里那份变成第二个设备的 token（两份都有效，但排查时分不清谁是谁）。
whoami_ok "$BOT_TOKEN" "$BOT_MXID" \
  || die "room.env 里的 MATRIX_TOKEN 用不了（whoami 认不出 ${BOT_MXID}）。重跑 up.sh 换一份"
say "bot ${BOT_MXID} 的 token 有效（不重登）"

# 房间非加密自查：期望 M_NOT_FOUND（判据同 up.sh:206-212）。加密房里岗位账号发的话
# 谁也读不到，而 matrix_bus 只会降级 log-only —— 早一步判死比晚一步降级好。
enc="$(curl -s -H "Authorization: Bearer ${BOT_TOKEN}" \
       "${HS_URL}/_matrix/client/v3/rooms/${ROOM_ID}/state/m.room.encryption")"
case "$(printf '%s' "$enc" | jget errcode)" in
  M_NOT_FOUND) say "房间 ${ROOM_ID} 非加密自查通过（M_NOT_FOUND）" ;;
  *)           die "房间 ${ROOM_ID} 疑似已加密：${enc}" ;;
esac

JOINED="$(curl -s -H "Authorization: Bearer ${BOT_TOKEN}" \
          "${HS_URL}/_matrix/client/v3/rooms/${ROOM_ID}/joined_members" \
          | python3 -c 'import sys,json;print("\n".join(json.load(sys.stdin).get("joined",{})))')"
say "房间现有成员 $(printf '%s\n' "$JOINED" | grep -c . || true) 人"

# ---------------------------------------------------------------- 4. 逐岗
ROOM_BOTS=""

for row in "${AGENTS[@]}"; do
  aid="${row%%|*}"; rest="${row#*|}"; lp="${rest%%|*}"; title="${rest#*|}"
  key="$(agent_key "$aid")"
  mxid="@${lp}:${SERVER_NAME}"
  user_var="MAOS_AGENT_${key}_USER"
  pass_var="MAOS_AGENT_${key}_PASSWORD"
  tok_var="MAOS_AGENT_${key}_TOKEN"

  say "${aid}（${title}） ${mxid}"

  # -- 口令：有就沿用，没有才现生成（同 up.sh:105-111 的取向）
  password="${!pass_var:-}"
  if [ -z "$password" ]; then password="$(openssl rand -hex 16)"; fi
  reg "$lp" "$password"

  # -- token：先验旧的，能用就完全不登录
  token="${!tok_var:-}"
  if whoami_ok "$token" "$mxid"; then
    say "  沿用仍有效的 token（不重登）"
  else
    if [ "$LOGIN_COUNT" -gt 0 ]; then
      say "  歇 3 秒再登（rc_login.address burst 5 / per_second 0.003）"
      sleep 3
    fi
    LOGIN_COUNT=$((LOGIN_COUNT + 1))
    token="$(login "$lp" "$password")"
    say "  已登录取到 access_token（本次第 ${LOGIN_COUNT} 次）"
  fi

  # -- 显示名：房间里看到的就是这四个字。先查后写，一致就不发 PUT。
  cur_name="$(curl -s "${HS_URL}/_matrix/client/v3/profile/${mxid}/displayname" | jget displayname)"
  if [ "$cur_name" = "$title" ]; then
    say "  显示名已是「${title}」"
  else
    body="$(python3 -c 'import json,sys;print(json.dumps({"displayname":sys.argv[1]}))' "$title")"
    curl -s -XPUT -H "Authorization: Bearer ${token}" -d "$body" \
      "${HS_URL}/_matrix/client/v3/profile/${mxid}/displayname" >/dev/null \
      || die "设 ${mxid} 显示名失败"
    say "  显示名已设为「${title}」"
  fi

  # -- 进房：bot（PL100、invite 阈值 0）先邀请，该号再用自己的 token join
  if printf '%s\n' "$JOINED" | grep -qxF "$mxid"; then
    say "  已在房间内"
  else
    inv="$(python3 -c 'import json,sys;print(json.dumps({"user_id":sys.argv[1]}))' "$mxid")"
    # 邀请失败不 die：已被邀请过就会回 M_FORBIDDEN，而决定性的一步是下面的 join。
    curl -s -XPOST -H "Authorization: Bearer ${BOT_TOKEN}" -d "$inv" \
      "${HS_URL}/_matrix/client/v3/rooms/${ROOM_ID}/invite" >/dev/null || true
    r="$(curl -s -XPOST -H "Authorization: Bearer ${token}" -d '{}' \
         "${HS_URL}/_matrix/client/v3/rooms/${ROOM_ID}/join")"
    [ -n "$(printf '%s' "$r" | jget room_id)" ] || die "${mxid} 加入房间失败：${r}"
    say "  已加入房间"

    # 🔴 **join 之后**才查加密。未 join 的号查这个状态事件拿到的是 403，而 nio 会把
    # 非 404 的错误体原样包成「成功」响应 —— 于是「号还没进房」被念成「房间开了加密」，
    # 降级日志里留一个假原因（hiclaw/matrix_bus.py::encryption_verdict 写着完整原委）。
    case "$(curl -s -H "Authorization: Bearer ${token}" \
            "${HS_URL}/_matrix/client/v3/rooms/${ROOM_ID}/state/m.room.encryption" \
            | jget errcode)" in
      M_NOT_FOUND) say "  该号视角下房间非加密（M_NOT_FOUND）" ;;
      *)           die "${mxid} 视角下房间状态查询异常，它大概没真进房" ;;
    esac
  fi

  eval "${user_var}=\$mxid"
  eval "${pass_var}=\$password"
  eval "${tok_var}=\$token"
  ROOM_BOTS="${ROOM_BOTS:+${ROOM_BOTS},}${mxid}"
done

# ---------------------------------------------------------------- 5. 产物
# umask 在 cat 之前：文件创建那一刻就是 600，不留一个「先 644 后 chmod」的窗口。
umask 077
{
  printf '# MAOS · 退款圆桌岗位账号 —— 仓库外，永不入库。由 deploy/synapse/add_agents.sh 生成。\n'
  printf '# 生成于 %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '#\n'
  printf '# 与 room.env 分工：room.env 归 up.sh（它会整份重写），本文件归本脚本。\n'
  printf '# 用法： set -a; . ~/.maos-matrix/room.env; . ~/.maos-matrix/agents.env; set +a\n'
  for row in "${AGENTS[@]}"; do
    aid="${row%%|*}"
    key="$(agent_key "$aid")"
    printf '\n# %s\n' "$aid"
    for suffix in USER PASSWORD TOKEN; do
      var="MAOS_AGENT_${key}_${suffix}"
      printf 'export %s=%s\n' "$var" "${!var}"
    done
  done
  printf '\n# 监听侧（maos-bot）对这些 sender 一律不投递 —— 一个房间只有一个监听者。\n'
  printf '# hiclaw/matrix_bus.py::open_channel 现读它，进 should_deliver 的忽略名单。\n'
  printf 'export MAOS_ROOM_BOTS=%s\n' "$ROOM_BOTS"
} > "$AGENTS_ENV"
chmod 600 "$AGENTS_ENV"

say "完成。${AGENTS_ENV} 已写入（chmod 600），本次登录 ${LOGIN_COUNT} 次"
say "五个岗位账号：${ROOM_BOTS}"
say "接下来： set -a; . ~/.maos-matrix/room.env; . ~/.maos-matrix/agents.env; set +a"
