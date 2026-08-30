#!/usr/bin/env bash
# MAOS T27 · 建 topic。起完 broker 必须跑这一步，否则第一条 publish 就抛。
#
# 🔴 三条实测出来的坑，都写在这里（详见 deploy/rocketmq-live.md §3）：
#
# 1. broker.conf 的 autoCreateTopicEnable = true 对 5.x 的 gRPC 客户端**不生效**。
#    客户端发消息前先拉路由，路由不存在就直接抛
#      Exception: failed to fetch topic:<name> route.
#    而不是触发自动建 topic。所以 topic 必须显式建。
#
# 2. **RocketMQ 的 topic 名不允许点号。** 客户端侧正则是 ^[%a-zA-Z0-9_-]+$，
#    broker 侧 mqadmin 同样拒绝。而 MAOS 契约里的 Topic 常量全是 maos.task.assignment
#    这种带点的形式，且那个文件冻结不许改 —— 所以点号到下划线的映射做在
#    RocketMQEventBus 里（见 maos/core/eventbus.py 的 rmq_topic_name）。
#    本脚本建的是**映射之后**的名字。
#
# 3. **mqadmin 建 topic 失败仍然 exit 0。** 拿退出码当判据会全绿而一个都没建上
#    （第一版脚本就这么骗了自己一次）。所以下面逐条回查 topicList，不信退出码。
#
# 🔴 -r 1 -w 1 是等价性证明的前提：RocketMQ 普通消息只保证「单队列内有序」。
# 多队列下同一串 publish 会被散列到不同队列，消费顺序与投递顺序不一致 ——
# 那样比对的就不是「后端语义」而是「队列数」。这条写在这里，不藏在测试里。
#
# 用法：  bash deploy/rocketmq/create-topics.sh [额外的 topic ...]
set -uo pipefail

NS="${MAOS_ROCKETMQ_NAMESRV:-namesrv:9876}"
CONTAINER="${MAOS_ROCKETMQ_BROKER_CONTAINER:-rmqbroker}"
CLUSTER="${MAOS_ROCKETMQ_CLUSTER:-DefaultCluster}"

# 契约里 Topic 的五个常量，按上面第 2 条映射后的名字（那个文件冻结，本脚本只读不改）。
#   maos.task.assignment -> maos_task_assignment
#   maos.task.result     -> maos_task_result
#   maos.review.verdict  -> maos_review_verdict
#   maos.task.rework     -> maos_task_rework
#   maos.dlq             -> maos_dlq
TOPICS=(
  maos_task_assignment
  maos_task_result
  maos_review_verdict
  maos_task_rework
  maos_dlq
)
# 等价性测试专用的靶 topic：测试跑在真 topic 上会污染演示数据。
TOPICS+=(t27_bus_a t27_bus_b)
TOPICS+=("$@")

for t in "${TOPICS[@]}"; do
  docker exec "$CONTAINER" sh mqadmin updateTopic \
    -n "$NS" -c "$CLUSTER" -t "$t" -r 1 -w 1 >/dev/null 2>&1
done

# 回查：唯一可信的判据是路由真的拉得到。
rc=0
for t in "${TOPICS[@]}"; do
  if docker exec "$CONTAINER" sh mqadmin topicRoute \
       -n "$NS" -t "$t" >/dev/null 2>&1; then
    echo "  ok    $t"
  else
    echo "  FAIL  $t   （路由拉不到 —— 名字里有点号或别的非法字符？）"
    rc=1
  fi
done

exit "$rc"
