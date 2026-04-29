---
name: formflow-router
description: formflow 统一路由骨架：对自然语言先做 route_plan，再分发到 register/draft/organize/reminder/general。
---

# formflow-router（统一路由）

这个 skill 只做“路由判定”，不直接写表、不直接整理。

主要脚本：

- `solutions/formflow-agent/skills/formflow-router/scripts/router.py`

示例：

```bash
python3 solutions/formflow-agent/skills/formflow-router/scripts/router.py --text "我今天要准备什么水果"
```

输出 JSON 示例：

```json
{
  "ok": true,
  "route": "organize_query",
  "confidence": 0.92,
  "need_clarify": false,
  "clarify_question": "",
  "reason": "..."
}
```
