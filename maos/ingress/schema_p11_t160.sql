-- T160 的 schema 片段 —— 电商平台 webhook 的原文落库表。
--
-- 跨轨契约 §B.1：**不改 `maos/core/store.py` 的既有表、不改任何 schema.sql 主文件**。
-- 每轨把自己的 DDL 放进自己的片段文件，由自己的模块在首次使用时应用。
-- 本文件由 `maos/ingress/shop_callback.py::ensure_schema()` 读入并逐条执行。
--
-- 这里是 CREATE 而不是 ALTER：T160 加的是**一张新表**（铁律 1 明许「只允许新增表」），
-- 不是往既有表上加列。`IF NOT EXISTS` 给幂等性。
--
-- ## 为什么原文存 base64 而不是 TEXT
--
-- 验签是对**原始字节**算的。回调体不保证是 UTF-8（平台会发压缩块、二进制签名段），
-- 按 TEXT 存要先 decode，而 decode 的那一刻证据就毁了 —— 日后想复算「这条回调的
-- 签名当时到底对不对」就再也算不出来。base64 是无损的，代价只是眼睛不能直接读，
-- 而这张表本来就不是给人逐行读的，是给取证用的。
--
-- ## 为什么验签失败的也要落
--
-- 那是攻击证据。一个签名错的回调意味着要么有人在伪造投递，要么我们的密钥配错了 ——
-- 两种都必须看得见。吞掉它等于把唯一的信号丢进黑洞（口径同 `scripts/guard_bash.py`：
-- 拦下来的操作也要留痕）。所以本表**没有**任何「只存成功的」过滤。
--
-- ## 为什么没有唯一索引
--
-- 去重是 `processed_key` 的事（`store.claim_idempotency`，op = `shop.callback`）。
-- 本表要的是**全量流水**：同一个 event_id 被平台重投三次，这里就该有三行，
-- 那正是「平台重投了几次」这个问题的答案所在。在这里加 UNIQUE 等于让同一件事
-- 有两个真相，还会让重投这个事实本身消失。
--
-- event_id 允许为空：验签失败、或平台没带投递 id 的那些行，照样要落（见上一节）。

CREATE TABLE IF NOT EXISTS shop_callback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT NOT NULL,
    event_id      TEXT NOT NULL DEFAULT '',
    topic         TEXT NOT NULL DEFAULT '',
    verify_result TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    raw_body_b64  TEXT NOT NULL,
    headers_json  TEXT NOT NULL DEFAULT '{}',
    received_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_shop_callback_platform_event
    ON shop_callback (platform, event_id);
