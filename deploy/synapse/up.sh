#!/usr/bin/env bash
# 一键起 Synapse + Element，产出跨轨握手件 ~/.maos-matrix/{room.env,creds.txt,STATUS}。
#
# 幂等：重复跑不炸，也不会重复建房 —— 容器已在就跳过，账号已存在就复用 creds.txt 里的
# 口令，room.env 里的房间还在（bot 仍是成员且未加密）就沿用同一个 room_id。
#
# 口令与 access_token 只落 ~/.maos-matrix/（chmod 600），本脚本里没有任何硬编码凭证。
#
#   bash deploy/synapse/up.sh          # 起
#   bash deploy/synapse/down.sh        # 停（默认保留数据卷）
set -euo pipefail

# localhost 不该走代理：本机常挂 Clash，走了代理就连不上自己的 8008。
export NO_PROXY="localhost,127.0.0.1,::1,${NO_PROXY:-}"
export no_proxy="$NO_PROXY"

SERVER_NAME="${MAOS_SERVER_NAME:-maos.local}"
HS_PORT="${MAOS_HS_PORT:-8008}"
EL_PORT="${MAOS_EL_PORT:-8080}"
HS_URL="http://localhost:${HS_PORT}"
EL_URL="http://localhost:${EL_PORT}"

# 镜像：ghcr.io 与 docker.io 在本机 docker daemon 侧均不可达（见 docs/hiclaw-probe.md），
# 默认走南大镜像站代理的 element-hq 官方镜像（镜像 label 的 image.source 指向
# github.com/element-hq/synapse.git；tag 缓存比 ghcr.io 落后，两边 latest 的 digest 不等）。
# 网络正常的机器覆盖这两个变量换回 ghcr.io 即可，脚本其余部分不用改。
SYNAPSE_IMAGE="${MAOS_SYNAPSE_IMAGE:-ghcr.nju.edu.cn/element-hq/synapse:latest}"
ELEMENT_IMAGE="${MAOS_ELEMENT_IMAGE:-ghcr.nju.edu.cn/element-hq/element-web:latest}"

SYNAPSE_CT="maos-synapse"
ELEMENT_CT="maos-element"
VOLUME="maos_synapse"

SHARE="${HOME}/.maos-matrix"
ROOM_ENV="${SHARE}/room.env"
CREDS="${SHARE}/creds.txt"
STATUS="${SHARE}/STATUS"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BOT_USER="maos-bot"
BOSS_USER="boss"
INTERN_USER="intern"

say() { printf '[up] %s\n' "$*"; }
die() {
  printf '[up] 失败：%s\n' "$*" >&2
  mkdir -p "$SHARE" && chmod 700 "$SHARE"
  printf 'BLOCKED %s\n' "$*" > "$STATUS"
  chmod 600 "$STATUS"
  exit 1
}

# ---------------------------------------------------------------- 0. 前置
command -v docker >/dev/null 2>&1 || die "本机没有 docker"
docker info >/dev/null 2>&1 || die "docker daemon 没在跑"
command -v python3 >/dev/null 2>&1 || die "本机没有 python3"

mkdir -p "$SHARE"
chmod 700 "$SHARE"

# jq 不是本机必备，JSON 一律交给 python3 从 stdin 取字段。
jget() { python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get(sys.argv[1],""))' "$1"; }

# ---------------------------------------------------------------- 1. 镜像
for IMG in "$SYNAPSE_IMAGE" "$ELEMENT_IMAGE"; do
  if ! docker image inspect "$IMG" >/dev/null 2>&1; then
    say "拉镜像 $IMG"
    docker pull "$IMG" || die "拉不到镜像 $IMG（改 MAOS_SYNAPSE_IMAGE / MAOS_ELEMENT_IMAGE 换源）"
  fi
done

# ---------------------------------------------------------------- 2. generate
# 只做一次：homeserver.yaml 里带 registration_shared_secret 与签名密钥，重跑会毁掉已有房间。
if docker run --rm -v "${VOLUME}:/data" --entrypoint test "$SYNAPSE_IMAGE" -f /data/homeserver.yaml; then
  say "homeserver.yaml 已存在，跳过 generate"
else
  say "generate 配置（server_name=${SERVER_NAME}）"
  # 库内注释写的是 docker run -it，非 tty 会话下 -it 直接失败 —— 去掉。
  docker run --rm -v "${VOLUME}:/data" \
    -e SYNAPSE_SERVER_NAME="$SERVER_NAME" -e SYNAPSE_REPORT_STATS=no \
    "$SYNAPSE_IMAGE" generate || die "generate 失败"
fi

# ---------------------------------------------------------------- 3. 起 Synapse
if [ "$(docker inspect -f '{{.State.Running}}' "$SYNAPSE_CT" 2>/dev/null || echo false)" = "true" ]; then
  say "容器 ${SYNAPSE_CT} 已在跑，跳过"
else
  docker rm -f "$SYNAPSE_CT" >/dev/null 2>&1 || true
  say "起 ${SYNAPSE_CT}（:${HS_PORT}）"
  docker run -d --name "$SYNAPSE_CT" -v "${VOLUME}:/data" -p "${HS_PORT}:8008" \
    "$SYNAPSE_IMAGE" >/dev/null || die "起 ${SYNAPSE_CT} 失败（端口 ${HS_PORT} 被占？）"
fi

say "等 ${HS_URL} 就绪"
ready=""
for _ in $(seq 1 60); do
  if curl -sf "${HS_URL}/_matrix/client/versions" >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
[ -n "$ready" ] || die "Synapse 起来了但 ${HS_URL}/_matrix/client/versions 不通（docker logs ${SYNAPSE_CT} 看原因）"

# ---------------------------------------------------------------- 4. 三个账号
# 口令：已有 creds.txt 就沿用（不然重跑会把人类记下的口令冲掉），否则现生成。
if [ -f "$CREDS" ]; then
  # shellcheck disable=SC1090
  . "$CREDS"
fi
BOT_PASSWORD="${BOT_PASSWORD:-$(openssl rand -hex 16)}"
BOSS_PASSWORD="${BOSS_PASSWORD:-$(openssl rand -hex 16)}"
INTERN_PASSWORD="${INTERN_PASSWORD:-$(openssl rand -hex 16)}"

reg() {  # reg <user> <password>；已存在视作成功
  local u="$1" p="$2" out
  out="$(docker exec "$SYNAPSE_CT" register_new_matrix_user \
        -u "$u" -p "$p" --no-admin -c /data/homeserver.yaml "$HS_URL" 2>&1)" || true
  case "$out" in
    *"User ID already taken"*) say "账号 ${u} 已存在" ;;
    *Success*|"")              say "账号 ${u} 已注册" ;;
    *)                         die "注册 ${u} 失败：${out}" ;;
  esac
}
reg "$BOT_USER"    "$BOT_PASSWORD"
reg "$BOSS_USER"   "$BOSS_PASSWORD"
reg "$INTERN_USER" "$INTERN_PASSWORD"

umask 077
cat > "$CREDS" <<CREDS_EOF
# MAOS Matrix 账号口令 —— 仓库外，永不入库。供 C-4 登 Element 截图用。
# 生成于 $(date -u +%Y-%m-%dT%H:%M:%SZ)
BOT_USER=${BOT_USER}
BOT_PASSWORD=${BOT_PASSWORD}
BOSS_USER=${BOSS_USER}
BOSS_PASSWORD=${BOSS_PASSWORD}
INTERN_USER=${INTERN_USER}
INTERN_PASSWORD=${INTERN_PASSWORD}
ELEMENT_URL=${EL_URL}
HOMESERVER=${HS_URL}
CREDS_EOF
chmod 600 "$CREDS"

# ---------------------------------------------------------------- 5. 取 bot 的 token
BOT_MXID="@${BOT_USER}:${SERVER_NAME}"
BOSS_MXID="@${BOSS_USER}:${SERVER_NAME}"
INTERN_MXID="@${INTERN_USER}:${SERVER_NAME}"

# Synapse 的 rc_login 默认 burst_count=3：每次重跑都无条件登三个账号，第二次就会
# 撞 M_LIMIT_EXCEEDED。所以 token 能复用就复用，登录只在真拿不到 token 时才做。
login() {  # login <user> <password> -> access_token；撞限流按 retry_after_ms 退避重试
  local u="$1" p="$2" body resp tok wait_ms
  body="$(python3 -c 'import json,sys;print(json.dumps({"type":"m.login.password","identifier":{"type":"m.id.user","user":sys.argv[1]},"password":sys.argv[2],"initial_device_display_name":"maos-provision"}))' "$u" "$p")"
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

whoami_ok() {  # whoami_ok <token> <expected_mxid>
  [ -n "$1" ] || return 1
  [ "$(curl -s -H "Authorization: Bearer $1" "${HS_URL}/_matrix/client/v3/account/whoami" \
       | jget user_id)" = "$2" ]
}

OLD_TOKEN=""
[ -f "$ROOM_ENV" ] && OLD_TOKEN="$(sed -n 's/^export MATRIX_TOKEN=//p' "$ROOM_ENV" | tail -1)"
if whoami_ok "$OLD_TOKEN" "$BOT_MXID"; then
  BOT_TOKEN="$OLD_TOKEN"
  say "沿用 room.env 里仍有效的 bot token（不重登，避开 rc_login 限流）"
else
  BOT_TOKEN="$(login "$BOT_USER" "$BOT_PASSWORD")"
  say "bot access_token 已取到"
fi

# ---------------------------------------------------------------- 6. 房间（非加密）
# 复用条件：room.env 里有 room_id，且 bot 现在仍是该房成员。否则新建。
OLD_ROOM=""
if [ -f "$ROOM_ENV" ]; then
  OLD_ROOM="$(sed -n 's/^export MATRIX_ROOM_ID=//p' "$ROOM_ENV" | tail -1)"
fi
ROOM_ID=""
if [ -n "$OLD_ROOM" ]; then
  if curl -s -H "Authorization: Bearer ${BOT_TOKEN}" "${HS_URL}/_matrix/client/v3/joined_rooms" \
     | python3 -c 'import sys,json;print("\n".join(json.load(sys.stdin).get("joined_rooms",[])))' \
     | grep -qxF "$OLD_ROOM"; then
    ROOM_ID="$OLD_ROOM"
    say "沿用已有房间 ${ROOM_ID}"
  fi
fi

if [ -z "$ROOM_ID" ]; then
  say "建房（非加密，preset=private_chat）"
  # 绝不带 initial_state 的 m.room.encryption —— 加密房会让 matrix_bus 当场降级 log-only。
  body="$(python3 -c 'import json,sys;print(json.dumps({"name":"MAOS 审批","topic":"MAOS 多智能体审批室（非加密）","preset":"private_chat","invite":[sys.argv[1],sys.argv[2]]}))' "$BOSS_MXID" "$INTERN_MXID")"
  resp="$(curl -s -XPOST "${HS_URL}/_matrix/client/v3/createRoom" \
          -H "Authorization: Bearer ${BOT_TOKEN}" -d "$body")"
  ROOM_ID="$(printf '%s' "$resp" | jget room_id)"
  [ -n "$ROOM_ID" ] || die "建房失败：$(printf '%s' "$resp" | jget error)"
  say "房间 ${ROOM_ID}"
fi

# 自查非加密：期望 M_NOT_FOUND。查到 m.room.encryption 就是加密房，直接判失败。
enc="$(curl -s -H "Authorization: Bearer ${BOT_TOKEN}" \
       "${HS_URL}/_matrix/client/v3/rooms/${ROOM_ID}/state/m.room.encryption")"
case "$(printf '%s' "$enc" | jget errcode)" in
  M_NOT_FOUND) say "非加密自查通过（M_NOT_FOUND）" ;;
  *) die "房间 ${ROOM_ID} 疑似已加密，matrix_bus 会降级 log-only：${enc}" ;;
esac

# boss / intern：先看谁已经在房里，只给不在的人登录并 join。
# 无条件重登会在第二次重跑时撞 rc_login 限流（burst_count 默认 3）。
JOINED="$(curl -s -H "Authorization: Bearer ${BOT_TOKEN}" \
          "${HS_URL}/_matrix/client/v3/rooms/${ROOM_ID}/joined_members" \
          | python3 -c 'import sys,json;print("\n".join(json.load(sys.stdin).get("joined",{})))')"

ensure_joined() {  # ensure_joined <user> <mxid> <password>
  local u="$1" mx="$2" p="$3" tok r
  if printf '%s\n' "$JOINED" | grep -qxF "$mx"; then
    say "${u} 已在房间内"
    return 0
  fi
  tok="$(login "$u" "$p")"
  r="$(curl -s -XPOST -H "Authorization: Bearer ${tok}" -d '{}' \
       "${HS_URL}/_matrix/client/v3/rooms/${ROOM_ID}/join")"
  [ -n "$(printf '%s' "$r" | jget room_id)" ] || die "${u} 加入房间失败：${r}"
  say "${u} 已加入房间"
}
ensure_joined "$BOSS_USER"   "$BOSS_MXID"   "$BOSS_PASSWORD"
ensure_joined "$INTERN_USER" "$INTERN_MXID" "$INTERN_PASSWORD"

# ---------------------------------------------------------------- 7. Element web
if [ "$(docker inspect -f '{{.State.Running}}' "$ELEMENT_CT" 2>/dev/null || echo false)" = "true" ]; then
  say "容器 ${ELEMENT_CT} 已在跑，跳过"
else
  docker rm -f "$ELEMENT_CT" >/dev/null 2>&1 || true
  say "起 ${ELEMENT_CT}（:${EL_PORT}）"
  docker run -d --name "$ELEMENT_CT" -p "${EL_PORT}:80" \
    -v "${HERE}/element-config.json:/app/config.json:ro" \
    "$ELEMENT_IMAGE" >/dev/null || die "起 ${ELEMENT_CT} 失败（端口 ${EL_PORT} 被占？）"
fi

# ---------------------------------------------------------------- 8. 握手件
cat > "$ROOM_ENV" <<ENV_EOF
# MAOS · C 轮 Matrix 房间握手件 —— 仓库外，永不入库。由 deploy/synapse/up.sh 生成。
# 生成于 $(date -u +%Y-%m-%dT%H:%M:%SZ)
#
# MATRIX_HOMESERVER 是**宿主机**口径。容器内跑要换成 http://host.docker.internal:${HS_PORT}。
export MATRIX_HOMESERVER=${HS_URL}
export MATRIX_USER=${BOT_MXID}
export MATRIX_TOKEN=${BOT_TOKEN}
export MATRIX_ROOM_ID=${ROOM_ID}
export MAOS_APPROVERS=${BOSS_MXID}
export MAOS_MATRIX_OUTSIDER=${INTERN_MXID}
export MAOS_ELEMENT_URL=${EL_URL}
ENV_EOF
chmod 600 "$ROOM_ENV"

printf 'READY %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$STATUS"
chmod 600 "$STATUS"

say "完成。room.env / creds.txt / STATUS 已写入 ${SHARE}"
say "Element: ${EL_URL}    Homeserver: ${HS_URL}    Room: ${ROOM_ID}"
