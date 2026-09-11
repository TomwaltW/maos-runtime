"""全仓测试的起跑线：让这台机器 export 了什么与测试结果无关（H-6 / T15 / T125）。

**要买的东西**：测试结果只由代码决定，不由「跑测试这台机器上恰好 export 了什么」
决定。没有这一层，同一个 commit 在干净机器上全绿，在演示机上变红 —— 而演示机
正是最需要测试可信的那台。

现在有三条 autouse fixture，前两条**只删**、第三条**要设**：

===========================  ==============================================
``_no_ambient_matrix_env``   删 5 个 ``MATRIX_*`` / ``MAOS_APPROVERS``（H-6）
``_no_ambient_store_env``    删 ``MAOS_STORE_BACKEND`` / ``MAOS_PG_DSN``（T15）
``_force_scripted_model``    设 ``MAOS_FORCE_SCRIPTED=1``（T125）
===========================  ==============================================

第三条为什么反着来，理由写在它自己的 docstring 里 —— 一句话：它与生产口径是
**同一个开关**，删 env 才是造一种只有测试里才有的环境。

**洞在哪**（出处 ``docs/BACKLOG.md`` 的 ``## task-C2`` 第 1 条）::

    maos/tests/test_registry_autodiscovery.py:286   build({}, matrix=True)
      -> maos/flows/common.py::_wrap_matrix
        -> hiclaw.matrix_bus.MatrixBusConfig.from_env()      # 读 os.environ

``from_env()`` 缺必填项才降级 ``log_only=True``。四个键齐全时它不降级，
``MatrixEventBus`` 于是真去 ``open_channel()`` 连房间 —— 于是那条
「无 env 必须自动降级」的断言当场变红，而且**红之前已经把消息发出去了**。

H-6 在 1131795 上实测（假 nio 模拟一台连得通的 homeserver，四键齐全）：
一次 ``pytest maos/tests`` 连 homeserver 4 次、往房间发 22 条，其中 6 条带真实
task_id 的 TaskAssignment / TaskResult / ReviewVerdict。四次连接来自两处
``build(matrix=True)``：本文件同级的 ``test_registry_autodiscovery.py`` 一次，
``hiclaw/room_demo.py:209`` 经 ``test_room_wiring.py`` 三次。

本机系统 python3 未装 matrix-nio，``open_channel`` 恒 ImportError 后降级，所以这个
洞在这里是**静默**的 —— 它只在装了 matrix-nio 的那台机器上炸，而那正是采集演示
证据的机器。别因为「本机跑着是绿的」就以为它不存在。

**只删不设**是刻意的：设值等于给全仓测试造一个假环境，那是把一种环境依赖换成
另一种。要买的是「起跑线自己划」，不是「起跑线由 conftest 划」。真要验有 env 的
分支，用例自己 ``monkeypatch.setenv`` —— autouse fixture 先于同 scope 的普通
fixture 实例化，用例的 setenv 一定盖得住这里的 delenv（``test_sandbox_isolation.py``
的哨兵 token 就依赖这个顺序，H-6 已实测其 3 条断言不受影响）。
"""
import pytest

#: 逐字对齐 ``hiclaw/matrix_bus.py`` 的 ENV_* 常量（前四个即 ``REQUIRED_ENV``）。
#: 这里硬编码而不 import，是因为 conftest 在 collection 阶段就执行，从它去 import
#: hiclaw 等于给全仓测试绑一个可选依赖层 —— 而那一层缺席时不该让 collection 集体
#: 失败，这条取向在 test_registry_autodiscovery.py 与 test_matrix_bus.py 里都写着。
#: 代价是要人工同步；hiclaw 那边加键时，本文件跟着加一行。
MATRIX_ENV_VARS = (
    "MATRIX_HOMESERVER",
    "MATRIX_USER",
    "MATRIX_TOKEN",
    "MATRIX_ROOM_ID",
    "MAOS_APPROVERS",
)


@pytest.fixture(autouse=True)
def _no_ambient_matrix_env(monkeypatch):
    """每条用例开跑前删掉 Matrix 环境变量，跑完由 monkeypatch 自动还原。"""
    for name in MATRIX_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


#: 逐字对齐 ``maos/store/__init__.py`` 的 ``BACKEND_ENV`` 与 ``maos/store/pg_store.py``
#: 的 ``DSN_ENV``。同上不 import：``maos.store`` 本身没有可选依赖，但从 conftest
#: import 生产模块会让 collection 依赖它的导入链，取向与上面那条一致。
#:
#: **这一组比 Matrix 那五个更危险**。Matrix 那组漏网时最坏是往房间多发几条消息；
#: 这一组漏网时 ``create_store()`` 会按 ambient 的 ``MAOS_STORE_BACKEND`` 去连
#: ``MAOS_PG_DSN`` 指的那个库，于是一次 ``pytest`` 就**往真库写表**。P5 的工厂放行
#: PG 之后这条路才通，所以它是「放行带出来的新欠账」，不是老问题的复述
#: （出处 ``docs/BACKLOG.md`` 的 ``## task-T15`` 第 1 条）。
STORE_ENV_VARS = (
    "MAOS_STORE_BACKEND",
    "MAOS_PG_DSN",
)


@pytest.fixture(autouse=True)
def _no_ambient_store_env(monkeypatch):
    """每条用例开跑前删掉存储后端的环境变量，跑完由 monkeypatch 自动还原。

    **这一条必须给 live 测试留活路，否则它买到的是假干净**。
    ``test_pg_store_live.py`` 与 ``test_pg_rank_parity.py`` 那 22 + 7 条靠
    ``MAOS_PG_DSN`` 决定跑不跑；无条件 delenv 若把它们一并饿死，有库环境的全量会从
    932 悄悄掉回 903 —— 症状是「测试变干净了」而不是红灯，没有人会去查。

    **它们没被饿死，靠的是时序而不是运气**：两个模块都在**模块级**写
    ``pytestmark = pytest.mark.skipif(_live_dsn() is None, ...)``，而 ``_live_dsn()``
    带 ``functools.lru_cache`` —— 这一句在 **collection 期（import 那一刻）**就求了值
    并把 DSN 缓存住了。本 fixture 是 function scope，最早也要等到第一条用例 setup
    才跑，比那一刻晚得多；用例执行期再调 ``_live_dsn()`` 命中的是缓存，读不到
    ``os.environ``。**delenv 够不着一个已经做完的决定。**

    所以这里刻意**不**给 live 测试开后门（不加 opt-in 参数、不改那两个文件）：
    冻结契约 B 把「有库 932」钉成判据，判据本体不该为了适配起跑线而松动。

    **代价（照实记，别当它不存在）**：这条活路依赖上面那个时序，而时序**没有任何
    断言钉着**。把 live 那两个模块的 ``pytestmark`` 换成用例内的 ``pytest.skip()``、
    或者摘掉 ``_live_dsn()`` 的 ``lru_cache``，判定就挪进了用例执行期 —— 那时
    ``os.environ`` 已被本 fixture 清空，29 条当场全 skip，而且**无库环境跑不出这个
    差别**（无库时它们本来就 skip，读数一模一样）。钉住它要新起一个测试文件，
    那超出 T26 白名单，已记 ``docs/BACKLOG.md`` 的 ``## task-T26`` 第 1 条。
    在那之前，唯一的哨兵是有库环境的全量必须是 932。
    """
    for name in STORE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


#: 与 ``maos/model/client.py::ENV_FORCE_SCRIPTED`` 同一个名字（契约 §G）。
#: 同上不 import：collection 期执行的代码不该把 ``maos.model`` 整条链先拉起来。
#: ``test_code_repo_patch_selfrepair.py`` 有一条断言钉着两边逐字相等。
FORCE_SCRIPTED_ENV = "MAOS_FORCE_SCRIPTED"


@pytest.fixture(autouse=True)
def _force_scripted_model(monkeypatch):
    """每条用例都跑在脚本回放上 —— 配了 key 的机器上也是（T125）。

    **这一条是「设」不是「删」，与上面两条刻意相反**，所以要把理由写在这里，
    免得后来人照着「只删不设」那条取向把它改掉。

    删掉三个 ``MAOS_LLM_*`` 同样能不打网络，但买到的是**另一条路**：
    ``select_model_client()`` 会走「缺配置 -> 降级」那一支，每次调用打一条
    WARNING「本次运行不发起任何网络请求，所有「模型」输出都是脚本回放：成本读数、
    延迟、以及任何与模型行为相关的结论，这一跑一律不成立」。那句话在全量里要刷
    几百遍，而它陈述的后果在测试语境下**是假的**（测试本来就不看成本读数）。
    把一句真告警刷成噪音，等于提前花掉它下次真该响时的信用
    （``test_model_client_hardening.py`` 那三条钉的正是这句告警的分量）。

    更要紧的是第二条：``MAOS_FORCE_SCRIPTED`` 与生产口径**是同一个开关**。
    ``run.py`` / ``scripts/demo_preflight.sh`` / ``scripts/make_evidence.py`` 的子进程
    都设它，所以测试这么设 = 测试跑在与它们逐字相同的模式下。删 env 才是造一种
    「只有测试里才存在」的环境 —— 生产上没有任何一处靠「把 key 删掉」保证确定性。

    **要验真模型选路的用例自己 ``monkeypatch.delenv(FORCE_SCRIPTED_ENV)``**，
    顺序上盖得住（autouse fixture 先于同 scope 的普通 fixture 实例化，见本文件
    开头那段）。现在这么做的有 ``test_model_client_hardening.py`` 的三条降级告警
    用例：它们判的是「缺 key 时那句 WARNING 的级别与措辞」，而强制开关会让
    ``select_model_client()`` 在走到那句话之前就返回，于是告警一条都不落。
    """
    monkeypatch.setenv(FORCE_SCRIPTED_ENV, "1")
