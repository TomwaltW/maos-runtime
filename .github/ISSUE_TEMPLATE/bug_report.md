---
name: Bug 报告
about: 代码行为与预期不符
title: ''
labels: bug
assignees: ''
---

## 现象

<!-- 一句话说清「预期什么 / 实际什么」。贴原始输出，不要转述。 -->

**预期**：
**实际**：

## 复现

```bash
# 从干净工作区开始的完整命令序列，一条不要省
git status --porcelain    # 应为空
```

## 环境

- 提交 sha：`git rev-parse HEAD` →
- `python3 --version` →
- 操作系统：
- `docker info` 是否 exit=0（沙箱走容器还是降级路径）：

## 相关读数

<!-- 有哪条跑红了就贴哪条的原始输出 -->

```
python3 -m pytest maos/tests -q     →
python3 scripts/verify.py           →
bash scripts/demo_preflight.sh      →
```

## 排查过

- [ ] 工作区是干净的（`git status --porcelain` 为空）—— 脏工作区会让证据首行的 sha 带 `-dirty`
- [ ] 跑过 `python3 scripts/make_evidence.py` 之后才跑的 `verify.py`（顺序不能换）
- [ ] 没有用 `git checkout -- evidence/` 单独还原过证据（会让新库配旧快照，核验掉到 3/7）

> ⚠️ 如果是**安全**问题，不要开 issue，按 [`SECURITY.md`](../../SECURITY.md) 私下联系。
