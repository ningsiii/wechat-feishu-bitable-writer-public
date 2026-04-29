---
name: formflow-draft
description: formflow 录入草稿状态机：收集→确认/作废（可退出不动待确认）。
metadata:
  {
    "openclaw":
      {
        "emoji": "🗂️",
        "notes":
          [
            "为企业微信对话演示设计：先回显草稿，再确认入账，避免误操作。",
            "草稿默认只在本地保存，不会自动对外发送。",
          ],
      },
  }
---

# formflow-draft（草稿/录入模式）

提供一个“录入草稿”的本地状态机，支持写入待确认、作废最新、确认全部，以及退出录入。

默认文件：

- 草稿：`solutions/formflow-agent/data/draft.json`
- 台账：`solutions/formflow-agent/data/ledger.jsonl`

常用命令（当前版本）：

```bash
# 进入录入模式
python3 solutions/formflow-agent/skills/formflow-draft/scripts/draft.py start

# 写入一条待确认（含动态字段提取）
python3 solutions/formflow-agent/skills/formflow-draft/scripts/draft.py ingest --text "客户：今晚要10斤麻辣…"

# 确认全部待确认
python3 solutions/formflow-agent/skills/formflow-draft/scripts/draft.py confirm_all

# 作废最新一条待确认
python3 solutions/formflow-agent/skills/formflow-draft/scripts/draft.py void_latest

# 退出录入模式（不动已有待确认）
python3 solutions/formflow-agent/skills/formflow-draft/scripts/draft.py exit
```

说明：
- `add/analyze/commit/delete/cancel` 为旧命令，已废弃，不再使用。
