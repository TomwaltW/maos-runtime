#!/usr/bin/env bash
# 停 Synapse + Element。默认只停容器，**保留数据卷** —— 账号、房间、签名密钥都在卷里，
# 删了就要重注册、重建房，下游三轨手上的 room.env 会全部失效。
#
#   bash deploy/synapse/down.sh            # 停容器，数据留着，下次 up.sh 秒起
#   bash deploy/synapse/down.sh --purge    # 连数据卷一起删（不可逆，慎用）
set -euo pipefail

SYNAPSE_CT="maos-synapse"
ELEMENT_CT="maos-element"
VOLUME="maos_synapse"
SHARE="${HOME}/.maos-matrix"

PURGE=""
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,9p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) printf '未知参数：%s\n' "$arg" >&2; exit 2 ;;
  esac
done

say() { printf '[down] %s\n' "$*"; }

for ct in "$SYNAPSE_CT" "$ELEMENT_CT"; do
  if docker inspect "$ct" >/dev/null 2>&1; then
    docker rm -f "$ct" >/dev/null
    say "已停并删除容器 ${ct}"
  else
    say "容器 ${ct} 不存在，跳过"
  fi
done

if [ -n "$PURGE" ]; then
  if docker volume inspect "$VOLUME" >/dev/null 2>&1; then
    docker volume rm "$VOLUME" >/dev/null
    say "已删除数据卷 ${VOLUME}（账号 / 房间 / 签名密钥全没了）"
  else
    say "数据卷 ${VOLUME} 不存在，跳过"
  fi
  # 卷没了，room.env 里的 token 与 room_id 全是死的。不改写 STATUS 的话，
  # 下游三轨会拿着过期的 READY 一直连不上还找不到原因。
  if [ -d "$SHARE" ]; then
    printf 'BLOCKED 数据卷已 --purge 删除，room.env 失效，需重跑 deploy/synapse/up.sh\n' > "${SHARE}/STATUS"
    chmod 600 "${SHARE}/STATUS"
    say "已把 ${SHARE}/STATUS 置为 BLOCKED"
  fi
else
  say "数据卷 ${VOLUME} 保留（要连卷一起删：--purge）"
fi
