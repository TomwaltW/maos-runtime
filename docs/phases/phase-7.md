# Phase 7（D8 · 9.2）提交材料

这天大部分是人类和网页版 Claude 的活，Claude Code 只做两件：

1. docs/demo-script.md：Demo 视频分镜脚本（3–5 分钟，场景 3 主线）：00:00 多源信号 → 00:30 房间里看聚合与拆解 → 01:30 沙箱真补丁真测试 → 02:30 Gate 过但 BLOCKED，Element 里 /approve → 03:30 DONE + trace.json/知识沉淀特写 → 04:00 一屏总结五项映射。每个镜头标注要执行的确切命令。🆙 若时间允许，02:00 处插 10 秒隔离特写：跑一次 escape-attempt，镜头给到"网络调用失败、密钥不存在"的输出——别人证明 agent 能干什么，你证明你的 agent 干不了什么。

2. docs/submission-checklist.md：复赛三件套自查清单（方案 PPT 逐页 ↔ 评审四维对照表、仓库链接自查项、视频规格），全部打勾才提交。

🆕 录制前自检：终端里 env | grep -iE 'key|token' 的结果不能出现在任何一个镜头里；Element 侧栏的 token、浏览器地址栏的 access_token 参数同理。录完回看一遍再剪。

PPT 更新和视频剪辑回到聊天界面找人类，把总体方案 §11 的对照表逐页铺进去即可。**9.3 只做提交，不写代码。**
