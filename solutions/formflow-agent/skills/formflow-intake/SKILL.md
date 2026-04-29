---
name: formflow-intake
description: formflow “复制粘贴 → AI 结构化 → 缺失信息清单 → 保存台账”的本地入口（DeepSeek/OpenAI 兼容）。
metadata:
  {
    "openclaw":
      {
        "emoji": "🧠",
        "notes":
          [
            "MVP 阶段先跑本地脚本；后续接飞书/企微时复用同一套结构化输出与台账落地。",
            "默认只生成草稿，不自动对外发送。",
          ],
      },
  }
---

# formflow-intake（AI 结构化入口）

把一段“客户对话/需求文本”交给大模型，输出 formflow 工作流 JSON，并自动保存到本地台账（JSONL）。

## 需要什么

- DeepSeek API Key（推荐），三选一即可：
  - 环境变量：`DEEPSEEK_API_KEY`（临时/会话内）
  - 项目内 `.env`：`solutions/formflow-agent/.env`（推荐做 demo，注意不要提交）
  - 仅密钥文件：`--api-key-file solutions/formflow-agent/.secrets/deepseek_api_key`
  - Base URL：默认 `https://api.deepseek.com`
  - Model：默认 `deepseek-chat`

> DeepSeek API 与 OpenAI 格式兼容，所以也可以换成你自己的 OpenAI-compatible 网关（改 base_url / model）。

## 运行示例

```bash
export DEEPSEEK_API_KEY="你的key"
python3 solutions/formflow-agent/skills/formflow-intake/scripts/intake.py \
  --text "客户：明天晚上想要10斤小龙虾，麻辣的。大概多少钱？能送到浦东吗？"
```

输出：

- 在终端打印结构化结果（含 missing_info、草稿等）
- 自动把条目写入 `solutions/formflow-agent/data/ledger.jsonl`
