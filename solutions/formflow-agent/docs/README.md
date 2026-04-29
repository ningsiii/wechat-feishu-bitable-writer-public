# 表单助手（formflow-agent）V1（public 包）

这是一个依托 OpenClaw（小龙虾）运行的“写表 Agent”：把微信/企业微信里的文本写入飞书多维表，并提供待确认/确认/作废与台账导出。

## 0. 你需要准备什么（一次性）

- 已安装 OpenClaw（小龙虾）并能启动 gateway
  - 文档：<https://docs.openclaw.ai/>
- 一个飞书开放平台应用（企业自建应用）
  - 你需要拿到：`FEISHU_APP_ID` / `FEISHU_APP_SECRET`
  - 平台入口：<https://open.feishu.cn/>
- 一个大模型接口（OpenAI 兼容）
  - `SMALLBIZ_MODEL_BASE_URL`：接口地址
  - `SMALLBIZ_MODEL`：模型名
  - `DEEPSEEK_API_KEY`：接口 Key（变量名沿用历史；实际表示“当前模型接口的 API Key”）

## 1. 配置（第一次用需要做）

1) 复制环境变量模板

- 从 `solutions/formflow-agent/.env.example` 复制为 `solutions/formflow-agent/.env`
- 在 `solutions/formflow-agent/.env` 里填好：
  - `FEISHU_APP_ID`
  - `FEISHU_APP_SECRET`
  - `DEEPSEEK_API_KEY`
  - `SMALLBIZ_MODEL_BASE_URL`
  - `SMALLBIZ_MODEL`

2) 绑定表文件（可选）

- 运行时会使用：`solutions/formflow-agent/config/bindings.json`
- 初次使用可从 `solutions/formflow-agent/config/bindings.example.json` 复制一份
- 你也可以不手动复制：直接在聊天里发送飞书多维表链接，系统会自动登记

## 2. 启动

在仓库根目录执行：

```bash
FORMFLOW_ENV_FILE="solutions/formflow-agent/.env" ./solutions/formflow-agent/scripts/run-gateway-wecom.sh
```

## 3. 你怎么用（最常用）

- 发飞书多维表链接：登记表（支持登记多张表）
- 直接发一段订单/信息：自动识别写入哪个表（写入为“待确认”）
- `列表`：查看已登记表
- `确认`：确认全部待确认
- `作废`：作废最新一条待确认
- `台账`：导出台账 CSV

## 4. 飞书权限常见问题（高频）

如果出现“写入失败：飞书应用没有该表的权限”：
- 这通常不是代码问题，而是飞书侧没有把该多维表授权给你的应用。

补充说明：
- 飞书多维表链接里的 `app_token/table_id` 是资源标识，不是密钥。
- 真正的密钥是你飞书开放平台应用里的 `App Secret`。

建议搜索关键词（复制到搜索引擎即可）：
- “飞书 开放平台 企业自建应用 App ID App Secret”
- “飞书 多维表 bitable 应用 权限 编辑”

## 5. 数据与文件位置（运行时）

- 环境变量：`solutions/formflow-agent/.env`
- 绑定表（运行时）：`solutions/formflow-agent/config/bindings.json`
- 草稿（运行时）：`solutions/formflow-agent/data/draft.json`
- 台账（运行时）：`solutions/formflow-agent/data/ledger.jsonl`
- 台账导出目录（运行时）：`solutions/formflow-agent/exports/`

注：上述运行时文件建议不要提交到 git（里面可能包含业务数据或密钥）。
