# fixture-repo —— 演示靶场（不是生产代码）

这个目录是 MAOS 场景 1/2 的**演示靶场**：一个刻意留了 bug 的小 Python 项目，
用来让「模型产出补丁 → 沙箱应用补丁 → 沙箱跑测试 → Gate 判定」这条链路
有真东西可跑。**任何时候都不要把它当成可用的会话库。**

口径由 `docs/parallel/contracts.md` 附录 C 冻结，B 造靶场、C 写流程，两侧不得另立口径。

## 留在里面的 bug

`auth/session.py::is_session_valid` 把 UTC 时间戳换算成本地墙上时间之后，
又把那个墙上时间当成 UTC 拿去做差 —— 于是会话的「年龄」凭空多出一个时区偏移，
**没到期的会话被提前判成过期**。

时区是在模块里写死的 `LOCAL_TZ = UTC+8`，不读机器的 `TZ`。这不是图省事：
沙箱容器里 `TZ` 就是 UTC，靠环境时区的 bug 一进容器就自动消失，
靶场会变成「宿主上红、沙箱里绿」，那样这个演示什么都证明不了。

## 两条用例（`tests/test_session.py`）

| 用例 | 打补丁前 | 打补丁后 |
|---|---|---|
| `test_valid_session` | 过 | 过 |
| `test_expired_session` | **挂** | 过 |

`test_expired_session` 守的是过期**边界**：差一小时到期的会话必须仍然有效，
超过 TTL 的必须失效。上面那个 bug 只打第一条断言 —— 这正是「提前判过期」的形态。

## 三条隔离探针（`tests/test_isolation_probe.py`）

靶场自带，**不由模型生成**。隔离成立时全绿：断网、拿不到宿主密钥、读不到宿主 home。
降级路径（裸 subprocess）断不了网，`test_no_network` 在那里自行 skip，
另外两条**仍然必须绿** —— 它们是 env 白名单的直接验证。
