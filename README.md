# 微信飞书写表助手 (WeChat + Feishu Bitable)

这个仓库是一个“可直接公开”的最小版本：
- 不包含任何密钥
- 不包含任何真实表/真实台账数据

它的作用：把微信/企业微信里的消息写入飞书多维表（待确认），并支持确认/作废与导出台账 CSV。

## 快速开始（先按这个跑通）

完整说明在：`solutions/formflow-agent/docs/README.md`

最短步骤：

1. 复制并填写环境变量

- 复制：`solutions/formflow-agent/.env.example` -> `solutions/formflow-agent/.env`
- 在 `.env` 里填写：
  - `FEISHU_APP_ID`
  - `FEISHU_APP_SECRET`
  - `DEEPSEEK_API_KEY`（这里只是“模型接口 API Key”的变量名）
  - `SMALLBIZ_MODEL_BASE_URL`
  - `SMALLBIZ_MODEL`

2. 启动

在仓库根目录运行：

```bash
FORMFLOW_ENV_FILE="solutions/formflow-agent/.env" ./solutions/formflow-agent/scripts/run-gateway-wecom.sh
```

3. 在微信/企微里测试

- 发飞书多维表链接：登记当前表
- 发一条订单文本：写入待确认
- 发 `台账`：导出台账 CSV

## 注意

- 这是依托 OpenClaw（小龙虾）运行的工程；如果你完全没装过 OpenClaw，请先按 `solutions/formflow-agent/docs/README.md` 的“准备事项”走。
