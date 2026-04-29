---
name: formflow-wecombot
description: formflow 企业微信群机器人推送：把 `ledger.py digest` 的 `digest_text` 发到群里（Webhook）。
metadata:
  {
    "openclaw":
      {
        "emoji": "📣",
        "notes":
          [
            "这是最轻量的企业微信接入：只做群内推送提醒（出站），不承接外部客户会话。",
            "把 webhook URL 当作密钥保护，别发到群外/别提交到 git。",
          ],
      },
  }
---

# formflow-wecombot（企业微信群机器人推送）

## 你能得到什么

- 每次运行都会生成 formflow 摘要（`digest_text`）
- 然后通过企业微信群机器人的 Webhook 推送到指定群

适合 MVP：先做“每日摘要提醒”这个最强共鸣点。

## 准备（企业微信里做一次）

1) 创建/注册企业微信（你自己也能注册一个企业用于 demo）
2) 建一个内部群
3) 群设置 → 添加群机器人 → 自定义机器人
4) 复制机器人 Webhook URL（长这样）：
   - `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...`

## 运行（本地）

方式 A：环境变量（一次性）

```bash
WECOM_WEBHOOK_URL="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." \
python3 solutions/formflow-agent/skills/formflow-wecombot/scripts/push_digest.py
```

方式 B：写进 `solutions/formflow-agent/.env`（推荐 demo）

在 `.env` 里加一行：

`WECOM_WEBHOOK_URL=...`

然后运行：

```bash
python3 solutions/formflow-agent/skills/formflow-wecombot/scripts/push_digest.py
```

