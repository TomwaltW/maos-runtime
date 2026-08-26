# Phase 2（D3 · 8.28）沙箱真实工具链 + 补全四个 Agent

🆕 本 Phase 建议先走 Plan mode：Gate 改造（第 7 步）会影响既有验收逻辑，方案确认后再执行。

## 目标

补丁是真补丁、测试是真测试；六角色到齐。🆙 模型生成的代码在**容器隔离**中执行，不是裸 subprocess。

## 步骤

1. maos/tools/port.py：@dataclass ToolPort：tool_name / entrypoint / param_schema / return_schema / scope / retry / idempotency / audit(bool) / degrade(str)。每次工具调用写 event_log（event_type: "ToolInvoked"）。

2. 🆙 沙箱镜像（本步骤先做，唯一需要网络的时刻）：创建 deploy/sandbox.Dockerfile 并 build：

```dockerfile
FROM python:3.11-slim
RUN pip install --no-cache-dir pytest && useradd -m -u 1000 runner
USER runner
WORKDIR /w
```

```bash
docker build -t maos-sandbox -f deploy/sandbox.Dockerfile .
```

3. maos/tools/sandbox.py 两个 ToolPort：

   - sandbox.git_apply(patch_set, workdir, reverse=False)：把 scenarios/fixture-repo/ 复制到临时目录 → 逐文件校验路径白名单（沿用 PROTECTED_PATHS，tests/ 禁改；🆙 **conftest.py（任意层级）显式列入禁改**——该文件在 pytest collection 阶段先于一切用例执行，是绕过"tests/ 禁改"的标准路径，新增或修改一律拒绝并返回结构化错误）→ git apply（🆙 reverse=True 时执行 git apply -R，Phase 4 补偿用）；失败返回结构化错误（哪个 hunk、什么原因）；
   - 🆙 sandbox.pytest_run(workdir)：**主路径为容器执行**——

```bash
docker run --rm --name maos-sb-<uuid> \
  --network none --read-only \
  -v "<workdir>":/w --tmpfs /tmp -w /w \
  --user 1000:1000 --memory 512m --cpus 1 --pids-limit 128 \
  maos-sandbox python -m pytest -q
```

     宿主侧 subprocess.run(..., timeout=MAOS_SANDBOX_TIMEOUT 默认 300)，超时后 docker rm -f 清场；容器天然不继承宿主环境变量，密钥隔离由此自动成立。**降级路径**（Docker 不可用）：照抄 MatrixEventBus 的降级 idiom，退回 subprocess 并打印醒目告警，但**必须** env= 传入白名单构造的干净字典（只放行 PATH / HOME / LANG，MAOS_* / MATRIX_* / 一切含 KEY、TOKEN 的变量一律不传——裸 subprocess 默认继承 os.environ，模型生成的代码能直接读走密钥，铁律 6 的出口脱敏管不到入口）。测试与 CI 永远走降级路径，保证无 Docker 环境可跑。产出结构化报告 {passed, failed, errors, cases:[{id,status,msg}], duration}；工具执行失败（环境错）与用例失败（业务错）**分开**上报。

4. 造演示靶场 scenarios/fixture-repo/：一个小 Python 项目——auth/session.py 里 is_session_valid() 有真实的登录超时 bug（比如用了本地时区导致会话提前过期），tests/test_session.py 两条用例：打补丁前 1 挂 1 过，打对补丁后全过。README 一段话说明这是演示靶场。🆙 另加隔离探针 tests/test_isolation_probe.py（fixture 自带，不由模型生成），三条用例，**隔离成立时它们全绿**：

```python
def test_no_network():        # socket.create_connection(("1.1.1.1", 443), 3) 必须抛异常
def test_no_host_secrets():   # os.environ 中不得存在 MAOS_LLM_API_KEY / MATRIX_TOKEN
def test_no_home_access():    # 读取 ~/.ssh/ 必须失败或为空
```

5. Skill 落地：test.verify v1.0.0（调 sandbox.pytest_run）。

6. 补全 4 个 Agent（照 coding.py 的模式：@register + Identity + 经 SkillInvoker）：

   - requirement.py：经 req.normalize，产出 acceptance + open_questions，open_questions 非空 → status=blocked（状态机已有 worker_blocked 路径）；
   - architecture.py：产出 architecture_contract artifact（API/幂等/审计必填；🆙 回滚字段改为**可逆性声明**——声明哪些产物类型可逆，git 补丁类可逆；不可逆产物禁止标 effect_risk=H 自动执行，contract 校验一行断言）；
   - testing.py：经 test.verify，产出 test_report artifact；
   - reviewer.py：模型语义审查全部产物，产出 review_note artifact（缺陷清单+结论）；超时 → needs_human。

7. 改 maos/runtime/gate.py::_gate_acceptance，🆙 判据说死：**代码类任务**（产出含 patch_set / test_report 的）从读 self_check 改为读同 attempt 的 test_report artifact——无报告即 blocker，无降级；有 failed 用例 = major，逐条转成结构化 findings（Coding 可直接消费）。**非代码类任务**（requirement / architecture / review 产物）继续用 self_check。（v2"self_check 保留为报告缺失时的降级判据"仅适用于后者，歧义表述删除。）

8. 更新 maos/main.py 场景 1/2：Plan DAG 变为 requirement → architecture → coding → testing（reviewer 语义审查挂在 Gate 后、审批前），coding 产出真补丁，testing 真跑。场景 2 的返工改为：第一轮故意给不完整契约导致真实用例挂，findings 喂回后第二轮修好。

9. 🆙 新增测试：maos/tests/test_sandbox_isolation.py——调 sandbox.pytest_run 跑 fixture，断言三条探针全过、宿主文件系统未被触碰；降级路径下 test_no_network 允许 skip（subprocess 断不了网），其余两条必须仍绿（env 白名单的直接验证）。另加 conftest.py 拒改负例。

## 验收

```bash
python -m pytest maos/tests -q                      # 含隔离测试与 conftest 负例
MAOS_LLM_API_KEY=... python run.py --scenario 1     # 真补丁 + 真 pytest 全过 → DONE
MAOS_LLM_API_KEY=... python run.py --scenario 2     # 第一轮真实用例挂 → rework → 第二轮 DONE
git -C /tmp/<sandbox-dir> log --oneline             # 能看到真实的 apply 记录
docker image ls maos-sandbox                        # 🆙 镜像存在
```

## 提交

`feat(p2): containerized sandbox (netless/ro/limits/env-stripped), conftest guard, fixture+probes, 4 agents, strict gate`
