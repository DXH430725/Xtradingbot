# 机器人总结与服务器迁移指南

本文汇总了已完成工作、监控/跳板服务的兼容性说明与本地联调方法，并提供将机器人迁移到服务器运行的步骤清单。

## 已完成工作总结

- 入口与配置：入口脚本 `python mm_bot/bin/run_trend_ladder.py`，加载 `mm_bot/conf/bot.yaml` 与环境变量；日志按 `mm_bot/conf/logging.yaml` 输出到 `logs/bot.log`。
- 核心框架：自研 `TradingCore`（轻量时钟、单策略、生命周期管理、统一 cancel_all），策略 tick 由时钟驱动。
- 交易连接器：`LighterConnector` 封装 REST+WS，含速率限制器、签名下单、全量 WS 状态维护（订单、成交、持仓）、定期与 REST 对账、便捷查询（盘口、开仓单、持仓），支持 `cancel_all` / `cancel_order` / 市价单 / 限价单。
- 策略实现：`TrendAdaptiveLadderStrategy` 梯度/阶梯策略，支持固定方向（long/short）、分批入场、部分止盈、flush 快速平仓、订单重引价（requote）、节奏控制（基于活动平仓单的冷却时间）、不对称失衡自检矫正、可选遥测上报。
- 配置与环境：参数支持 YAML 与 `XTB_*` 环境变量覆盖；`Lighter_key.txt` 读取 API/ETH 私钥；`lighter-python/` SDK 直接在本地路径加载。

## 监控/跳板服务兼容性与本地测试

本仓库 `deploy/` 目录已包含两种可选的遥测接收方式：

1) Node 版实时仪表板（你新放入的监控程序）

- 文件：`deploy/server.js` + `deploy/card.html`
- 端口：默认 `10123`（环境变量 `PORT` 可改）
- 上报入口：`POST /ingest/:botName`（JSON），可选鉴权请求头 `x-auth-token`（通过环境变量 `TELEMETRY_TOKEN` 启用）。
- 看板接口：`GET /api/status`（被 `card.html` 轮询）；主页 `/` 自动渲染卡片。
- 兼容性：与策略遥测契约完全匹配（我们的策略以 JSON POST，无自带鉴权头）。如需鉴权，请勿在 `server.js` 设置 `TELEMETRY_TOKEN`，或另行改造机器人加请求头。
- 本地运行：
  - `cd deploy && npm init -y && npm i express`
  - `node server.js`
  - 浏览器访问 `http://127.0.0.1:10123/`；或 `curl http://127.0.0.1:10123/api/status`

2) 轻量 Python 版（CLI/系统服务友好）

- 文件：`deploy/telemetry_receiver.py`（已提供）。
- 端口：默认 `10123`
- 上报入口：`POST /ingest/<name>`；健康检查：`GET /health`。
- 本地运行：`python deploy/telemetry_receiver.py`

机器人接入遥测（两种接收端通用）：

- 在 `mm_bot/conf/bot.yaml` 中启用：

  ```yaml
  strategy:
    trend_ladder:
      telemetry_enabled: 1
      telemetry_url: "http://127.0.0.1:10123/ingest/Lighter01"
  ```

- 或用环境变量覆盖：`XTB_TL_TELEM_ENABLED=1`，`XTB_TL_TELEM_URL=http://<host>:10123/ingest/<Name>`。

- 快速自测（不连交易所）：
  - 模拟上报：`curl -X POST http://127.0.0.1:10123/ingest/Lighter01 -H 'Content-Type: application/json' -d '{"ts":1690000000,"symbol":"BTC","direction":"long","position_base":0,"tp_active":0}'`
  - Node 看板：刷新主页或拉取 `/api/status`；Python 接收端：拉取 `/health`。

## 服务器迁移步骤

1) 服务器准备
- 安装 Python 3.11+（建议 3.12），允许访问 `https://mainnet.zklighter.elliot.ai`。
- 可选安装 Node.js（如使用 `server.js` 看板）。
- 创建工作目录，例如 `/opt/xtradingbot`。

2) 上传文件/目录（保持相对结构）
- `mm_bot/`（完整目录）
- `lighter-python/`（完整目录；连接器从本地路径加载 SDK）
- `deploy/`（含 `server.js`、`card.html`、或/和 `telemetry_receiver.py`）
- `mm_bot/conf/bot.yaml`（如有自定义）
- `Lighter_key.txt`（包含 API key index / API 私钥 / ETH 私钥；谨慎权限）
- 可选：`logs/`（若无会自动创建）

3) 依赖安装（推荐虚拟环境）

```bash
cd /opt/xtradingbot
python -m venv .venv && source .venv/bin/activate
pip install -r lighter-python/requirements.txt
pip install pyyaml
```

Node 看板（可选）：

```bash
cd deploy
npm init -y && npm i express
node server.js
# 若启用鉴权：export TELEMETRY_TOKEN=...（机器人当前未发送该请求头，默认请不要设置）
```

4) 配置机器人
- 使用 `mm_bot/conf/bot.yaml` 或环境变量配置：
  - `XTB_LIGHTER_BASE_URL`, `XTB_LIGHTER_KEYS_FILE`, `XTB_LIGHTER_ACCOUNT_INDEX`, `XTB_SYMBOL`, `XTB_TICK_SIZE`, 以及 `XTB_TL_*`（详见 `mm_bot/bin/run_trend_ladder.py` 与 `mm_bot/conf/bot.yaml`）。
- 遥测地址指向你的监控服务：`telemetry_url: http://<host>:10123/ingest/<Name>`。

5) 启动顺序

```bash
# 监控（二选一）
python deploy/telemetry_receiver.py
# 或
node deploy/server.js

# 机器人
python mm_bot/bin/run_trend_ladder.py
```

6) 可选：systemd 管理

- 监控服务（示例 `/etc/systemd/system/xtb-telemetry.service`）：
  - ExecStart：`/opt/xtradingbot/.venv/bin/python /opt/xtradingbot/deploy/telemetry_receiver.py`
- 机器人（示例 `/etc/systemd/system/xtb-trend-ladder.service`）：
  - Environment：`XTB_LIGHTER_KEYS_FILE=/opt/xtradingbot/Lighter_key.txt` 等
  - ExecStart：`/opt/xtradingbot/.venv/bin/python /opt/xtradingbot/mm_bot/bin/run_trend_ladder.py`

```bash
systemctl daemon-reload
systemctl enable --now xtb-telemetry xtb-trend-ladder
```

## 注意事项

- 遥测鉴权：当前机器人未发送 `x-auth-token` 头。如需鉴权，请保持 `server.js` 的 `TELEMETRY_TOKEN` 为空，或改造机器人发送该头。
- 日志：默认写入 `logs/bot.log`，可通过环境变量 `XTB_LOG_DIR` 等覆盖。
- 速率限制：`lighter-python` 的 `RateLimiter` 已按权重配置，避免触发 API 限制。

明白了，你的脚本路径在 /MMBOT/mm_bot/bin/run_trend_ladder.py。我帮你写一个 systemd 服务配置，这样你的机器人退出终端后还能自动运行，并且掉线会自动重启。

📄 配置文件

在服务器上新建：

sudo nano /etc/systemd/system/trendbot.service


写入以下内容（注意路径已经替换成你提供的）：

[Unit]
Description=Trend Ladder Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/MMBOT/mm_bot/bin
ExecStart=/usr/bin/python3 /MMBOT/mm_bot/bin/run_trend_ladder.py
Restart=always
RestartSec=5
StandardOutput=append:/var/log/trendbot.log
StandardError=append:/var/log/trendbot.log
User=root

[Install]
WantedBy=multi-user.target

🚀 启动与管理

重新加载 systemd：

sudo systemctl daemon-reload


开机自启：

sudo systemctl enable trendbot


启动服务：

sudo systemctl start trendbot


查看状态：

systemctl status trendbot


查看日志：

tail -f /var/log/trendbot.log

🔒 小提示

如果你不是用 root 跑机器人，建议把 User=root 改成普通用户。

如果你的 Python 在虚拟环境里，不要直接用 /usr/bin/python3，改成虚拟环境路径，例如：

ExecStart=/MMBOT/venv/bin/python /MMBOT/mm_bot/bin/run_trend_ladder.py