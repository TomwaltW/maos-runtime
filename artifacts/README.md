# artifacts/ —— 复赛提交件

| 文件 | 是什么 | 关系 |
| :-- | :-- | :-- |
| `maos-复赛方案.html` | **正本**。自包含单文件演示稿，15 页，16:9 | 改这一份 |
| `maos-复赛方案.pdf` | **提交件**。从正本导出，15 页，页面 960×540 pt（= 1280×720 px，16:9） | 由正本重导，不要手改 |

正本是 HTML 不是 PPTX：可版本控制、可 diff、可一条命令重导。PPTX 改一个字就是一坨没法 review 的二进制 diff。

---

## 1. 重新导出 PDF

### 路子 A：命令行（本轨实际用的这条，30 秒）

在仓库根目录执行：

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --disable-gpu --no-pdf-header-footer \
  --print-to-pdf="$PWD/artifacts/maos-复赛方案.pdf" \
  "file://$PWD/artifacts/maos-复赛方案.html"
```

成功时最后一行是 `<N> bytes written to file …`。**不要**加 `--print-to-pdf-no-header`（旧拼法，已失效），
也不要用 `timeout` 包住 —— macOS 没有 `timeout` 命令，包了会整条失败且不报错在点子上。

### 路子 B：浏览器手动（不装 Chrome 命令行也能做）

1. 浏览器打开 `artifacts/maos-复赛方案.html`
2. 打印（`⌘P`）
3. 目标选「另存为 PDF」
4. 纸张 → **自定义 1280 × 720 px**（或 13.33 × 7.5 英寸）
5. 边距 **无**，缩放 **100%**，**关闭**页眉和页脚
6. 存为 `artifacts/maos-复赛方案.pdf`

页面里按 `P` 键可直接唤起打印对话框。

### 导出后必须验两条

```bash
# ① 页数必须是 15，不多不少
grep -a -o "/Count [0-9]*" artifacts/maos-复赛方案.pdf | sort -u -t' ' -k2 -n | tail -1
# → /Count 15

# ② 页面尺寸必须是 16:9
grep -a -o "/MediaBox\s*\[[^]]*\]" artifacts/maos-复赛方案.pdf | sort -u
# → /MediaBox [0 0 960 540]
```

页数 > 15 说明某页内容溢出被分页了 —— 见下面第 3 节。

---

## 2. 截图 slot：房间实拍**已回填**（2026-08-31）

**位置**：`maos-复赛方案.html` 的 **P6**（AgentTeams 事件链）页，右栏下部。
在源码里搜这一行注释即可定位：

```html
<!-- SLOT: room-screenshot —— 已回填 evidence/room/02-transitions.png … -->
```

紧跟它的是一个 `<figure>`，内嵌 base64 图 + 一行 `figcaption`。

**回填时一并做的三件事**（换图时同样要做）：

1. 图先缩放再内嵌 —— `sips -Z 900 <src>.png --out /tmp/room-900.png`，
   原图 2880×1882 的 Retina 截图直接内嵌会让 HTML 涨到 500KB 以上；
   缩到 900px 宽后 base64 约 150KB，PDF 从 3.84MB 涨到 3.98MB。
2. `<img>` 上必须留 `max-height:248px` —— **这是溢出实测收敛出来的值**。
   不限高时 P6 溢出 160px，内容会被静默裁掉（见第 3 节 ②）。
3. P6 右栏原有的第二个 card（「为什么第 5 项要单列」）**已删**，其论点并入
   「当前真实状态（不吹）」那张 card —— 与表格第 5 行、card 正文三处重复，
   删掉才腾得出图的位置。

选 `02-transitions.png` 的理由：它证明状态迁移是**逐条**镜像的
（`PENDING → DISPATCHED → RUNNING → AWAITING_REVIEW → BLOCKED` 五条各一条消息），
最贴 P6「事件链」这个页名，也是五张里最难伪造的一张。

---

### 换一张图的步骤

**回填步骤**（整合轮做，T4 的真房间证据到位之后）：

1. 把截图转成 base64 data URI —— **不许外链图片文件**，正本必须保持自包含：

   ```bash
   python3 -c "import base64,sys;print('data:image/png;base64,'+base64.b64encode(open(sys.argv[1],'rb').read()).decode())" \
     evidence/room/<截图>.png > /tmp/room-uri.txt
   ```

2. 把上面整个 `<div class="slot">…</div>` 换成：

   ```html
   <img src="data:image/png;base64,…" alt="Matrix 房间实拍：审批命令与事件镜像"
        style="width:100%; border:1px solid var(--line2); border-radius:6px;">
   ```

3. **口径已随回填一起改到位**（2026-08-31）：P6 的 card 已由 `bad` 改 `ok`、
   P13 口径表 Matrix 那一行、P14 缺口清单里的「真房间未接通」三处均已改口，
   与 `docs/agentteams-mapping.md` 的「真房间已接通」一致 ——
   **那份文档是口径上位法，不是这里**。

4. 重导 PDF（第 1 节），重验页数 15。加图后 P6 可能溢出，若页数变成 16，按第 3 节调。

---

## 3. 改稿时的三条硬约束

### ① 自包含 —— 不许外链任何东西

CSS、JS、图片全部内联；中文走系统字体栈，不 `@import` 网络字体；架构图是手写内联 SVG，
**不引 mermaid 的 CDN 脚本**。评委离线打开也必须是完整的。改完自查：

```bash
grep -c "cdn\|https://unpkg\|https://cdnjs\|@import url(" artifacts/maos-复赛方案.html   # 必须是 0
grep -oE 'https?://[^"'"'"' )]+' artifacts/maos-复赛方案.html                              # 必须无输出
```

### ② 一页就是一页 —— 不许溢出

每个 `<section class="slide">` 是 1280×720，`.body` 是 `overflow:hidden`：
**内容超高不会分页，会被静默裁掉**，PDF 上看不出来。所以加内容之后必须实测。

三页已经带了精确缩放（内容最满的三页，值是实测收敛出来的，别随手改）：

| 页 | zoom | 备注 |
| :-- | :-- | :-- |
| P7 | `.94` | Skill / ToolPort 两张九要素表 |
| P12 | `.93` | 两域对照 9 行表 + 区间 A 实测数字 |
| P13 | `.96` | A-4 口径表 + 安全边界表 |

**溢出实测脚本**（改完必跑，输出应全为 `bodyOverflow=0`）：把下面这段临时插到 `</body>` 前，
用无头 Chrome `--dump-dom` 取回结果，看完删掉：

```html
<pre id="probe-out"></pre>
<script>
var out=[];document.querySelectorAll('.slide').forEach(function(s){
  var b=s.querySelector('.body')||s.querySelector('.cover');
  out.push(s.id+' bodyOverflow='+(b?b.scrollHeight-b.clientHeight:0));});
document.getElementById('probe-out').textContent='MEASURE::'+out.join(' | ');
</script>
```

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu \
  --virtual-time-budget=4000 --window-size=1400,900 --dump-dom "file://$PWD/<临时文件>.html" \
  2>/dev/null | grep -o "MEASURE::.*"
```

更省事的等价判据：把 `overflow:hidden` 临时改成 `overflow:visible` 导一次 PDF，
**页数仍是 15 就说明没有任何一页被裁**；变成 16 就是有一页溢出。

### ③ 每一句断言都要指得出证据

页脚那一行「可核验证据」不是装饰。**每一页的每一个断言，都要能指出文件+行号，或一条能当场跑的命令。**
指不出来的话删掉比留着强 —— 评委会拿 PPT 逐条对仓库，一页吹过头，整份材料的可信度全塌。

行号会随主干漂移。**改稿前先复核一遍页脚里的行号**，别把过期锚点印进 PDF
（本轨渲染时就发现大纲里 12 处行号已过期，台账记在 `docs/ppt-outline.md` 末尾的「T 轮渲染台账」）。

---

## 4. 页锚清单

与 `docs/ppt-outline.md` 一一对应，编号由编排侧钉死，**不许改编号与页名**。

| 页锚 | 页名 | 承接的评审维度 |
| :-- | :-- | :-- |
| P1 | 封面 · 一句话主张 | — |
| P2 | 评委三段反馈，正面接住 | 三段反馈诊断 |
| P3 | 从一条退款说起 | 场景价值与复用性 20% |
| P4 | 架构一眼 | 地基页 |
| P5 | 状态机与七道闸 | 多 Agent 协同 25% ／ 工程落地与安全审计 30% |
| P6 | AgentTeams 事件链 | 多 Agent 协同 25% |
| P7 | Skill / ToolPort 九要素契约 | Skill 工程体系 20% |
| P8a | RAG（一）两阶段检索 | Skill 工程体系 20% |
| P8b | RAG（二）改变了计划，且被护栏挡住 | Skill 工程体系 20% |
| P9 | 权威事实边界 | 工程落地与安全审计 30% |
| P10 | 失败路径纵切（场景 7） | 多 Agent 协同 25% ／ 场景价值 20% |
| P11 | 证据核验：七项逐条重放 | 工程落地与安全审计 30% |
| P12 | 同一个内核，两个域 | 场景价值与复用性 20% |
| P13 | 数据口径与风险边界 | 工程落地与安全审计 30% |
| P14 | 复现指引：从零到 7/7 | 工程落地与安全审计 30% ／ 开源贡献 5% |

演示时的键位：`←` `→` 翻页，`Home` / `End` 首尾页，`P` 打印。
