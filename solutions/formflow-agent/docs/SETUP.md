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


## 6. 飞书开放平台：创建应用与授权（按这几步做）

这一步的目标只有两个：
1) 拿到 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`（用于换取 tenant_access_token）
2) 让你的飞书应用对目标多维表有权限（否则会写入失败）

### 6.1 创建飞书应用并拿到凭证

1. 打开飞书开放平台：<https://open.feishu.cn/>
2. 创建「企业自建应用」
3. 在应用的“凭证与基础信息”里找到并复制：
   - App ID  -> 填到 `.env` 的 `FEISHU_APP_ID`
   - App Secret -> 填到 `.env` 的 `FEISHU_APP_SECRET`

说明：
- 飞书多维表链接里的 `app_token` / `table_id` 是“资源标识”，不是密钥。
- 真正的密钥是你应用的 `App Secret`。

### 6.2 给多维表授权（非常常见的坑）

如果你看到类似“写入失败：飞书应用没有该表的权限”，通常就是这里没做。

建议做法（原则）：
- 你需要在飞书里把目标 Base/Bitable 授权给你的应用，至少具备“编辑/写入记录”的权限。

不同组织的后台入口名称可能略有差异，你可以用下面的关键词搜索教程：
- “飞书 多维表 应用 授权 权限 编辑”
- “飞书 开放平台 应用 多维表 权限”

### 6.3 把信息填入 `.env`

在 `solutions/formflow-agent/.env` 中填写：
- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`

然后重启网关再试。

## 附：飞书多维表链接里的 app_token / table_id 是什么

下面这段摘自作者的调研笔记（用于避免误把资源标识当成密钥）：

## 附：飞书多维表格（Bitable）链接里哪些是 App Token / Table ID

当你拿到一条飞书多维表格（Base/Bitable）的网页链接时，常见格式类似：

```
https://kcn6wzbx5fga.feishu.cn/base/<APP_TOKEN>?table=<TABLE_ID>&view=<VIEW_ID>
```

以示例链接为例：

```
https://kcn6wzbx5fga.feishu.cn/base/CBOUbQlH4atVSvsYobicPT8ynzc?table=tblrsJe4T038fN1r&view=vewYp68pzS
```

- `APP token`（也称 `app_token`）：`/base/` 后面紧跟的那一段
  - 本例中为：`CBOUbQlH4atVSvsYobicPT8ynzc`
- `table ID`（也称 `table_id`）：URL 查询参数 `table=` 后面那一段
  - 本例中为：`tblrsJe4T038fN1r`
- `view ID`：URL 查询参数 `view=` 后面那一段（用于打开具体视图）
  - 本例中为：`vewYp68pzS`

> 备注：`APP token` / `table ID` 都是“资源标识”，不是密钥；真正的密钥是飞书自建应用的 `App Secret`。

## 常见报错：权限不足

下面这段摘自作者的调研笔记（最常见写入失败原因）：

### 10.1 权限不足

写入失败：飞书应用没有该表的编辑权限
表格：{表名}
链接：{open_url}

请先在飞书里把该表授权给应用可编辑，然后重试。

### 10.2 链接无效

登记失败：这不是可用的飞书多维表链接
请发送包含 /base/ 或 /wiki/ 的飞书多维表链接重试。

### 10.3 网络/超时/未知失败

写入失败：网络超时或服务暂时不可用
表格：{表名}
链接：{open_url}

请稍后重试。
