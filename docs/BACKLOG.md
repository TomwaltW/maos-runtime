# BACKLOG —— 发现但不当场处理的问题（铁律 4）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-26 | P0 | docs/PLAN.md 暂缺：本地未找到《MAOS-GOAI-复赛总体方案.md》源文件 | 各 Phase 文档引用的总体方案 §2/§3/§5.1/§9/§11/§12 暂无仓库内对应 | 人类提供文件后随下一个 commit 入库，最迟 Phase 6 前 |
| 2026-08-26 | P0 | 根 README.md 仍描述旧目录/双轨结构 | 新克隆者可能按旧说明操作 | Phase 6 第 6 步整体重写（手册已排期，不提前动） |
| 2026-08-26 | P0 | ~~.claude/settings.json 仍缺 deny 规则与 PreToolUse hook 挂载~~ | ~~铁律 1 宣称的三重机制当前实际只有指纹测试一重生效~~ | **resolved 2026-08-27**：deny 三条 + PreToolUse hook 均已挂载并实测生效（本会话 Read/Bash 一触碰 settings.json 即被拦）。顺带复核两条旧判断：`Edit(/maos/contracts/**)` 单斜杠是作用域根相对，规则一直生效（原判"匹配不上"有误）；allow 已含 `Bash(python3 -m:*)`，与本机无 `python` 不冲突 |
| 2026-08-27 | P0 | 项目级 hook 仅在从仓库根启动时加载 | 从 `~` 或其它目录启动的会话守卫完全不生效（8-27 实测：家目录会话可自由改写 `guard_bash.py` 本身） | 候选解：`~/.claude/settings.json` 用户级 hook + 绝对路径；代价为全局每次工具调用多一次进程启动。复赛后评估，赛前不动 |
| 2026-08-27 | P0 | MAOS_RELOCK 授权只有"整晚敞着"一种用法 | hook 是独立进程，内联 `MAOS_RELOCK=1 cmd` 和 Bash 内 export 都传不到；唯一生效方式是启动 claude 前 export，此后整个会话对**全部**受保护文件放行 | 改为从命令文本识别前缀做单条授权；风险高于常规改动，复赛后单独处理 |
| 2026-08-27 | P0 | 守卫按命令文本匹配路径，只读与 git 操作一律拦 | ①无法 Read/grep `.claude/settings.json` 核对 deny 与 hook（复核旧判断时即被拦，只能靠 hook 报错反推它在生效）；②`git add .contracts.lock` 同样被拦，而 phase-5.md:23 要求该文件**必须提交** —— Claude 侧无法完成入库，只能由人类在自己终端做 | 与上面两条一并在复赛后收敛：读与 git 索引操作放行、写照拦。赛前不动，`.contracts.lock` 由人类手工提交 |
| 2026-08-28 | P1 | 多轨判断记录分叉：track-a 新建 `docs/decisions/task-a.md`，主干是单文件 `docs/DECISIONS.md` | 分文件是 track-a 为避开多轨同改冲突的**有意选择**（文件抬头写明理由），本身合理；缺的是回收规则 —— 六轨各写一份，谁在何时折回 `DECISIONS.md`、评审时以哪份为准，都没定。另：该目录当前 `??` 未跟踪，若 track-a 只暂存白名单内的代码文件，这份判断记录会随 worktree 一起丢 | 合并 track-a 前定回收规则（谁折、何时折）；`docs/decisions/` 是否入库当场决定。**不是大小写撞名** —— `DECISIONS.md` 与 `decisions` 去掉大小写仍不同名，本机 `ls docs/` 两者已并存，验证通过 |
| 2026-08-28 | P1 | 守卫逐行分词，跨行或落单的 ASCII 引号必被拦 | `check_bash` 把命令 `.split("\n")` 后**逐行** `shlex` 分词，任一行内 `'` 或 `"` 不成对即 `No closing quotation` → fail-closed 整条拦掉。**与中文、heredoc、反斜杠续行都无关** —— 续行已被上一句 `raw.replace` 提前折平，heredoc 正文引号成对即放行（8-28 用 10 个用例直跑守卫本体实测）。实际会咬到的只有两种：①单个 `-m` 的引号内含真换行；②heredoc 正文某行有孤立撇号（`it's`）。②对 `cat > msg.txt <<EOF` 同样成立 —— **改成写文件并不能绕开** | 免疫通道只有一条：message 用 **Write 工具**落临时文件（守卫对 Edit/Write 只查路径，`content 一律不看`）再 `git commit -F <file>`。已写进 common.md 铁律 5 附注。守卫侧修法（按 shell 语法而非换行切分、识别 heredoc 边界）与上面四条一并复赛后收敛，赛前不动 |

## fix-4

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P1 | 守卫对「`cd <仓库外目录> && cat > <相对路径> <<EOF … EOF`」误报。原命令是往 `~/.claude/jobs/<id>/tmp/` 写一个一次性脚本再 `python3` 跑它，**全程不碰仓库任何文件**，却被拦下并报 `blocked: 该操作触碰受保护面 maos/contracts/events.py（写入/执行位置）` —— 而该路径在命令文本里根本没出现过 | ①合法的仓库外临时脚本被 fail-closed 拦掉；②更麻烦的是**报错路径具有误导性** —— 它指名一个命令压根没提到的文件，照着这条报错查会直接查错方向（本轨即先怀疑是自己命令有问题，才转去做隔离实验）。与上一条「逐行分词」不是同一个成因：那条报的是 `No closing quotation`，这条报的是受保护面命中 | **成因未隔离，勿照抄推测**。已排除的：单纯 heredoc（放行）、heredoc 正文含 `Documents-MAOS` 字样（放行）、heredoc + `python3` 执行且全用绝对路径（放行）。未能复现的那一档是带 `cd` 前缀 + 相对写入路径的组合 —— 本轨进 worktree 后该形式被另一重「worktree 隔离」守卫先行拦下，无法继续二分。留给 fix-1（`fix/protected-paths`）从这三条已排除项接着做，与守卫其它五条一并复赛后收敛，赛前不动 |
