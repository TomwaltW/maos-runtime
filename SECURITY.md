# 安全政策

## 支持的版本

**工作分支是 `goai-restructure`，它才是活的那一支。**
只有它的最新提交在 MVP 阶段受支持。

> ⚠️ 仓库的 GitHub **默认分支目前是 `main`，而 `main` 上是已封存的早期
> TypeScript 实现**（`src/` + `package.json`），不是当前运行时。
> 裸 `git clone` 会落在 `main` 上，请显式指定分支：
>
> ```bash
> git clone -b goai-restructure <本仓库地址> maos && cd maos
> ```
>
> 对着 `main` 上的代码报安全问题，报的是一份没在跑的历史实现。

## 报告漏洞

**不要开公开 issue。** 也不要在公开位置贴 PoC 细节、凭证、客户数据或未脱敏日志。

请私下联系仓库所有者，附上：影响面的简述、受影响的提交 sha、复现步骤、**脱敏后**的证据。
维护者会确认收到、评估影响，并在公开披露前完成修复协调。

## 当前的安全边界

这是复赛演示实现，不是生产系统：**不含生产凭证、客户数据，也不做任何不可逆的对外写入**
（支付网关走的是对齐官方公开规范的模拟实现）。密钥一律只读环境变量，
禁止写进任何文件，证据束落盘时还会做出口脱敏 + 哨兵反查，命中即销毁目录并失败。

仓库刻意去证明的是「agent **干不了**什么」，逐条在 [README §7](README.md#7-安全边界)。
其中最要紧的一条是**权威事实边界**：

- 全系统只有 `payment.observe` 写得进 `settled`，且必须在同一事务里附上回执。
- 越权写入**不静默失败** —— 抛异常并落一条 `AuthoritativeFactViolation` 事件。
  也就是说，绕过边界这件事本身会在 `event_log` 里留下痕迹。
- 这条边界是**可核验的**，不是文档承诺：`python3 scripts/verify.py` 的第 3 项
  `authoritative-fact` 就在重放校验它，失败即意味着边界被绕过。
  设计与那次被核验器抓到的绕过，见 [`docs/authoritative-facts.md`](docs/authoritative-facts.md)。

同样可核验的还有：模型生成的代码只在沙箱里落盘与执行（容器 `--network none --read-only`，
Docker 不可用时降级为按白名单重建 env 的裸 subprocess）；补丁落盘走三重路径校验；
Agent 只能调 Identity `allowed_tools` 白名单内的工具，越权抛 `PermissionDenied` 并落审计行。

**最有价值的报告**是那些指出边界逃逸、非预期写入、授权绕过或敏感数据泄露的 ——
尤其是能让上面某一条「可核验」的断言变成假的。
