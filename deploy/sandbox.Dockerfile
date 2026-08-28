# 沙箱执行镜像 —— 模型生成的代码只在这里面跑（phase-2.md 第 2 步）。
#
# 刻意保持四行：镜像里除了 pytest 什么都不装。装得越多，逃逸面越大，
# 而靶场只需要跑 pytest。
#
# 这里的 python:3.11-slim 与镜像内的 `python` 命令**不受**「本机没有 python」
# 那条约束 —— 那条只管宿主机上直接执行的命令，容器内 python 是有的。
#
# USER runner（uid 1000）配合 docker run 的 --user 1000:1000：
# 容器内不给 root，挂进来的 workdir 写入也就落在同一个非特权 uid 上。
FROM python:3.11-slim
RUN pip install --no-cache-dir pytest && useradd -m -u 1000 runner
USER runner
WORKDIR /w
