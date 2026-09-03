## 改了什么

<!-- 一句话说清行为上的变化。不是文件清单，是「之前会 X，现在会 Y」。 -->

## 为什么

<!-- 触发这次改动的问题。有 issue 就链过来。 -->

## 机器验收（贴原始输出，不要写「已通过」）

```bash
python3 -m pytest maos/tests -q       #
python3 scripts/gen_docs.py --check   #
git diff --stat maos/contracts/       #
bash scripts/demo_preflight.sh        #
```

- [ ] `pytest` 全绿，条数：`____ passed`（存量 + 本次新增，一条都不许少）
- [ ] `gen_docs.py --check` exit=0（改了代码就要重新生成那三份文档）
- [ ] `git diff --stat maos/contracts/` **空输出** —— 冻结契约未被动过
- [ ] `bash scripts/demo_preflight.sh` exit=0

## 规矩自查

- [ ] 提交格式是 `<type>(p<N>): <一句话>`
- [ ] `git add` 逐文件点名，没有 `git add -A` / `git commit -a`
- [ ] `evidence/` 下没有手写或改写过的内容（每个文件首行的时间与 sha 由生成脚本写入）
- [ ] 没有把任何密钥写进文件 —— 密钥只读环境变量
- [ ] 没有做本次范围外的「顺手优化」；顺手发现的问题记进了 [`docs/BACKLOG.md`](../docs/BACKLOG.md)
- [ ] 做了既定做法之外的判断的话，记进了 [`docs/DECISIONS.md`](../docs/DECISIONS.md)

## 新增了测试吗

- [ ] 有，覆盖的是：
- [ ] 没有，因为：<!-- 纯文档改动可以没有；改了行为却没有测试要说明理由 -->

## 影响面

- [ ] 只动了本次范围内的文件
- [ ] 动了 `maos/core/store.py` → 是**新增表**，没有改现有表结构
- [ ] 动了业务状态 → 是业务对象自己的字段，**没有**往 `maos/contracts/states.py` 加状态或迁移
