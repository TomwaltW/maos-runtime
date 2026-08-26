# MAOS 复赛改造 · 全局约定（common.md）

每个 Phase 开一个 Claude Code 会话，读取仓库根目录的 CLAUDE.md、本文件和 docs/phases/phase-\<N\>.md 后执行。
遇到与文档冲突或文档没覆盖的决策，停下来问人类，不要自行发挥；若判断该决策太小不值得打断，仍必须记入 docs/DECISIONS.md 一行。

**Plan mode 使用约定**：Phase 2、Phase 4 涉及架构判断（Gate 改造、补偿回滚、Replan 触发条件），先用 Plan mode 出方案，人类看过再放行执行。Phase 0、5、6、7 是纯执行型，直接跑。

**本机环境注意**：这台 Mac 没有 `python` 命令，一律用 `python3`（3.11）；GNU 专属参数按 macOS/BSD 适配（如 `ls --time-style=full-iso` → `ls -l -T`）。各 Phase 文档里的命令均按此替换执行。

## 全局铁律（七条）

1. 冻结契约：maos/contracts/events.py 与 maos/contracts/states.py 禁止任何修改，除非手册某 Phase 明确列出的增量；maos/core/store.py 现有表结构禁改，只允许**新增**表。本条由三重机制强制：.claude/settings.json 的 deny 规则、PreToolUse 守卫 hook（scripts/guard_bash.py，封 Bash 侧路）、maos/tests/test_contracts_frozen.py 指纹校验。不是口头约定。
2. 每个 Phase 结束时，存量测试 + 该 Phase 新增测试必须全绿：`python -m pytest maos/tests -q`。
3. 证据必须真实：evidence/ 下所有文件必须来自真实命令输出，禁止手写或编造。每个 evidence 文件首行必须是 `# generated at <ISO8601> from <git sha>`，由生成脚本自动写入。
4. 不做手册范围外的"顺手优化"；发现问题记入 docs/BACKLOG.md，不当场改。
5. 提交规范：`feat(p<N>): <一句话>`，一个 Phase 至少一个 commit，验收全绿才许 commit。只许本地 commit，**禁止 push**，推送由人类手动做。
6. 任何需要真实密钥的配置只读环境变量，禁止把密钥写进任何文件。也禁止让密钥出现在 evidence/ 的任何输出里——凡是可能回显 env 或 URL 的命令，输出前必须过脱敏。
7. 凡是没有严格按手册执行、或手册没覆盖而自行做了判断的地方，必须在 docs/DECISIONS.md 追加一行：`<日期> | Phase N | 情境 | 选择 | 理由`。

## 附 A：每日收工检查单

- `python -m pytest maos/tests -q` 全绿
- `python run.py`（Scripted 模式）四个老场景没被改坏
- 今日 Phase 的验收命令逐条跑过
- commit 已按规范落盘，本地 commit 即可，推送由人类手动做
- **人类手动 git push 到 GitHub 私有 remote**（"禁止 push"约束的是 Claude Code，不是备份。八天的活不能只活在一台笔记本上）
- **D6 起追加**：完整跑一遍场景 3 --matrix 含 /approve，失败当天修——D8 录制日必须是第 N 次执行，不是第一次彩排
- 落后 ≥ 半天？→ 打开总体方案 §12 砍序表执行降级，不拖明天

## 附 B：每个 Phase 结束的 30 秒人肉检查

不要接受"测试全绿"的文字汇报，人类自己敲一遍：

```bash
git diff --stat HEAD~1                              # 有没有超出手册范围的文件被动
git diff HEAD~1 -- maos/contracts/ maos/core/store.py
                                                    # 前者必须为空；后者只许出现 CREATE TABLE 新增
git diff HEAD~1 -- .contracts.lock .claude/ scripts/relock_contracts.py scripts/guard_bash.py
                                                    # 必须为空。任何一个字节的变动都意味着守卫被动过
python -m pytest maos/tests -q                      # 自己跑，不看转述
git log --oneline -5                                # commit 数量与顺序对不对
cat docs/DECISIONS.md                               # 它自行做过哪些判断
cat docs/BACKLOG.md                                 # 发现的问题有没有老实记下来
ls -l -T evidence/                                  # 证据时间戳合不合理（macOS 用 -T 看完整时间）
git diff --cached | grep -iE 'sk-|api[_-]?key|token'  # commit 前最后一道密钥筛
```
