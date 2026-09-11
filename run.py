"""薄入口 —— `python run.py` 顺跑场景 1-7 端到端，等价于 `python -m maos.main`。

多一个开关：`python run.py --contrast` 跑三组对照 case（租户 / 渠道 / 政策版本），
实现在 `maos/flows/contrast.py`。

**它为什么挂在这里而不是 `maos/main.py`**：`main.py` 在 Task-0 完工后冻结（附录 D），
且它的 `--scenario` 选项域是 `ALL_SCENARIOS`，而三组对照**刻意不进那个集合** ——
缺省证据束恒为 8 束是跨轨冻结口径（`scripts/demo_preflight.sh` 与复赛材料都写死了 8）。
对照是**另开一条路**，不是第 8、9、10 个场景。所以本文件先把 `--contrast` 摘掉，
其余参数原样透传给 `maos.main`，`python run.py` 不带参数时行为一个字节不变。

第二个开关 `--live-model`（T125，契约 §G）同样只能挂在这里，理由同上：`main.py`
冻结，而这件事必须在**进 `main()` 之前**做完 —— 场景模块在 `run()` 里就调
`select_model_client()`，那时再设已经晚了。

**缺省翻面了，这是本文件最该被读到的一句**：不带 `--live-model` 时本文件先
`os.environ.setdefault("MAOS_FORCE_SCRIPTED", "1")`，于是 `python3 run.py` 在
**配了 key 的机器上也走脚本回放**。改缺省不是为了省钱，是因为原先那条口径
（「人记得加 `env -u MAOS_LLM_API_KEY ... ` 前缀」）在演示机上按天失效：
`~/.bash_profile` 里 export 了 `MAOS_LLM_*`，漏加一次前缀，这一跑的成本读数、
延迟、以及证据束里的一切就都不是确定性的了，而**屏幕上没有任何提示**
（`docs/BACKLOG.md` 的 2263 / 2290 记着这笔账）。要真模型请显式说出来。

`setdefault` 不是 `environ[...] = "1"`：人在外面显式 `MAOS_FORCE_SCRIPTED=0` 时
本文件不该把它按回去 —— 那是第二个「说了不算」的开关。
"""

from __future__ import annotations

import os
import sys

from maos.main import main as scenarios_main

CONTRAST_FLAG = "--contrast"
LIVE_MODEL_FLAG = "--live-model"

#: 与 `maos/model/client.py::ENV_FORCE_SCRIPTED` 同一个名字，硬编码而不 import ——
#: 本文件是「薄入口」，为一个字符串再拉一层 import 与那个定位相悖。代价是要人工
#: 同步，`test_code_repo_patch_selfrepair.py` 有一条断言钉着两边逐字相等。
#:
#: **写入时机与 import 顺序无关**，别照着「早于 import」去理解：`forced_scripted()`
#: 是在 `select_model_client()` 被调用那一刻现读 `os.environ` 的，而那发生在
#: 各 `flows/scenario_*.py` 的 `run()` 里 —— 比这里晚得多。真正要守的时机只有
#: 一条：早于 `scenarios_main(args)`。
FORCE_SCRIPTED_ENV = "MAOS_FORCE_SCRIPTED"


def _apply_model_mode(args: list[str]) -> list[str]:
    """摘掉 `--live-model`，并按它决定要不要替人钉死 Scripted。返回剩下的参数。

    两条路（场景与 `--contrast`）都要经过这里，所以它排在分岔**之前**。
    """
    if LIVE_MODEL_FLAG in args:
        args = [a for a in args if a != LIVE_MODEL_FLAG]
        # **赋值，不是 delenv**：这个旗标要压得过**继承来的** `MAOS_FORCE_SCRIPTED=1`。
        # 摘旗标却不清环境的话，在 export 过该变量的 shell 里（演示机的
        # `.bash_profile` 就 export 着 `MAOS_LLM_*` 那一串）`--live-model` 静默失效、
        # 仍走 Scripted，而日志还在提示「要真模型请用 --live-model」—— 人照做了，
        # 什么也没变，且没有任何红灯。显式开关必须压过环境，这是它之所以叫显式。
        # 写 "0" 而不是删掉：它在 `_FORCE_OFF_VALUES` 里，且 `env | grep` 看得见
        # 「这一跑刻意关掉了强制」，删掉则与「从来没设过」不可区分。
        os.environ[FORCE_SCRIPTED_ENV] = "0"
    else:
        os.environ.setdefault(FORCE_SCRIPTED_ENV, "1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _apply_model_mode(list(sys.argv[1:] if argv is None else argv))
    if CONTRAST_FLAG not in args:
        return scenarios_main(args)

    args.remove(CONTRAST_FLAG)
    matrix = "--matrix" in args
    if matrix:
        args.remove("--matrix")
    if args:
        print(f"{CONTRAST_FLAG} 不接受其它参数（多余的：{args}）", file=sys.stderr)
        return 2

    from maos.flows.contrast import run as run_contrast
    return run_contrast(matrix=matrix)


if __name__ == "__main__":
    sys.exit(main())
