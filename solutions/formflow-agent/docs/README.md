# 表单助手（formflow-agent）V1

这是一个依托 OpenClaw（小龙虾）运行的表单/台账录入 Agent。

V1 目标：把用户发来的订单/信息写入飞书多维表（待确认），并支持确认/作废与导出台账。

## 0. 你需要准备什么（一次性）

- 已安装 OpenClaw（小龙虾）并能启动 gateway
- 一个飞书开放平台应用（拿到 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`）
- 一个大模型接口（OpenAI 兼容）
  - `SMALLBIZ_MODEL_BASE_URL`：接口地址
  - `SMALLBIZ_MODEL`：模型名
  - `DEEPSEEK_API_KEY`：接口 Key（变量名沿用历史；实际表示“当前模型接口的 API Key”）

## 1. 配置（第一次用需要做）

1) 复制环境变量模板：

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
- 也可以不手动复制：直接在聊天里发送飞书多维表链接，系统会登记当前表

## 2. 启动

在仓库根目录执行：

```bash
FORMFLOW_ENV_FILE="solutions/formflow-agent/.env" ./solutions/formflow-agent/scripts/run-gateway-wecom.sh
```

如果提示端口占用（18789）：

```bash
ss -ltnp | grep ':18789'
# 然后 kill 对应 pid
```

## 3. 用户侧命令（V1）

- 发送飞书多维表链接：登记/替换当前表
- `列表`：查看已登记表
- 直接发送订单文本：录入到表格（状态：待确认）
- `确认`：确认全部待确认
- `作废`：作废最新一条待确认
- `退出`
- `台账` / `账本` / `查看台账`：导出台账 CSV（默认全部范围）
- `台账预览`：预览最近台账（仅摘要）
- `最近错误`
- `删除表 <序号或表名>`

## 4. 数据与文件位置

- 环境变量：`solutions/formflow-agent/.env`
- 绑定表（运行时）：`solutions/formflow-agent/config/bindings.json`
- 草稿（运行时）：`solutions/formflow-agent/data/draft.json`
- 台账（运行时）：`solutions/formflow-agent/data/ledger.jsonl`
- 台账导出目录（运行时）：`solutions/formflow-agent/exports/`

注：上述运行时文件默认不会提交到 git（避免密钥/业务数据泄露）。

## 5. 飞书应用与权限（非常重要）

你填的 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 来自「飞书开放平台」的企业自建应用。

常见踩坑：
- 能登记表，但写入失败：通常是**飞书应用没有该多维表的编辑权限**（需要在飞书里给应用授权）。
- URL 里的 `app_token` / `table_id` 不是密钥，只是资源标识；真正的密钥是 `App Secret`。（详见仓库内 `产品调研.md` 的解释）

参考（官方/可搜索）：
- 飞书开放平台：在控制台查看应用凭证（App ID / App Secret）
- 获取 tenant_access_token 的示例与说明（含 app_id/app_secret 与接口地址）：
  - https://www.feishu.cn/content/843510681973

建议搜索关键词（直接复制到搜索引擎）：
- “飞书 开放平台 企业自建应用 App ID App Secret”
- “飞书 多维表 bitable 应用 权限 编辑”
- “tenant_access_token internal app_id app_secret”
