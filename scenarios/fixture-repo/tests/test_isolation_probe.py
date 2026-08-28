"""隔离探针 —— 靶场自带，**不由模型生成**。隔离成立时三条全绿。

这三条不是冒烟，是沙箱那几个 docker 参数与 env 白名单的直接验证：
断网对应 `--network none`，拿不到密钥对应「容器不继承宿主 env」/降级路径的
env 白名单，读不到 home 对应容器里的 `--user 1000:1000` 与降级路径的临时 HOME。
谁把这些参数删了，红的是这里。

放在靶场里而不是 maos/tests/ 里，是因为它们必须在**沙箱内部**跑才有意义 ——
在宿主上跑一遍全绿，什么也没证明。
"""

import os
import pathlib
import socket

import pytest

# Docker 给每个容器都放这个文件，是判「我在不在容器里」最省事的标记。
# 刻意不用环境变量做标记：env 正是这一组探针要验的东西，拿它当判据是自证。
IN_CONTAINER = pathlib.Path("/.dockerenv").exists()

SECRET_NAMES = ("MAOS_LLM_API_KEY", "MATRIX_TOKEN")


def test_no_network():
    """容器主路径 `--network none`：连外网必须抛异常。

    降级路径（裸 subprocess）断不了网，这条在那里 skip —— 派单明写允许。
    判据用容器标记而不是「连上了就 skip」：后者会把容器里真的没断网
    （隔离失效）也一起 skip 掉，那正是这条最该报红的时候。
    """
    if not IN_CONTAINER:
        pytest.skip("降级路径：裸 subprocess 断不了网，这条只在容器主路径有意义")
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 443), 3).close()


def test_no_host_secrets():
    """宿主密钥一个都不许进来。容器不继承 env；降级路径靠 env 白名单。"""
    leaked = [name for name in SECRET_NAMES if name in os.environ]
    assert leaked == [], f"宿主密钥漏进沙箱: {leaked}"

    # 再按前缀扫一遍，比点名两个更严：将来多一个 MAOS_LLM_TOKEN 之类的变量，
    # 点名清单不会跟着变，这条会。
    #
    # 刻意**不**扫「名字里含 KEY / TOKEN」：实测容器里会命中 GPG_KEY —— 那是
    # python:3.11-slim 基础镜像自带的（用来校验 Python 源码包签名），不是宿主
    # 漏下来的。白名单管的是「宿主往下传什么」，镜像自带的变量不在它的管辖内，
    # 拿泛化词扫只会把这条探针变成一条恒红的假警报。
    leaked_by_prefix = [k for k in os.environ if k.startswith(("MAOS_", "MATRIX_"))]
    assert leaked_by_prefix == [], f"宿主的 MAOS_/MATRIX_ 变量漏进沙箱: {leaked_by_prefix}"


def test_no_home_access():
    """读不到宿主的 ~/.ssh —— 目录不存在、读不动、或为空，都算通过。"""
    ssh_dir = pathlib.Path.home() / ".ssh"
    try:
        entries = list(ssh_dir.iterdir())
    except OSError:
        return                      # 不存在或读不动，正是要的结果
    assert entries == [], f"沙箱里能列出宿主的 ~/.ssh: {[p.name for p in entries]}"
