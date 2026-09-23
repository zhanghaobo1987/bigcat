# bigcat

bigcat 是一个轻量、开箱即用的 VPS 实时监控方案，API 与 [Komari](https://github.com/komari-monitor/komari) 兼容，前端直接使用 LuminaPlus 主题（Komari-Theme-LuminaPlus v1.3.4）的构建产物，界面与 Komari 一致。

- **服务端**：Python + Flask，SQLite 存储，无需编译 Go。
- **Agent**：Python + psutil，单文件，可在被监控的 VPS 上运行，定时上报 CPU / 内存 / 磁盘 / 网络 / 负载等指标。
- **前端**：LuminaPlus 主题静态文件，通过 `/api/rpc2` JSON-RPC 获取数据。

## 目录结构

```
bigcat/
├── server/
│   ├── app.py          # Flask 服务端：静态主题 + Komari 兼容 API
│   ├── storage.py      # SQLite 存储（节点 / 历史指标 / 设置）
│   └── static/         # LuminaPlus 主题构建产物（index.html + assets）
├── agent/
│   └── agent.py        # 被控端采集脚本（psutil）
├── scripts/
│   ├── install.sh      # 一键安装脚本（服务端 / 被控端）
│   └── bigcat.service  # systemd 服务单元（服务端）
├── requirements.txt
├── Dockerfile
└── README.md
```

## 快速开始

### 1. 启动服务端（主控端）

```bash
pip install -r requirements.txt
cd server
python3 app.py --port 25774 --db data/bigcat.db
```

首次启动后设置管理密码：

```bash
curl -X POST http://127.0.0.1:25774/api/admin/setup \
  -H "Content-Type: application/json" \
  -d '{"password":"你的强密码"}'
```

然后浏览器打开 `http://服务器IP:25774`。

### 2. 注册被控节点

在主控端上为每台被监控的 VPS 注册一个节点，拿到 `uuid` 和 `token`：

```bash
curl -X POST http://127.0.0.1:25774/api/agent/register \
  -H "Content-Type: application/json" \
  -d '{"name":"hk-1"}'
```

返回示例：

```json
{"uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx", "token": "64位随机token"}
```

### 3. 在被控 VPS 上运行 Agent

```bash
pip install psutil requests
python3 agent.py --server http://主控IP:25774 --token <token> --interval 2
```

也可以用一键安装脚本（见 `scripts/install.sh`），它会把 agent 注册为 systemd 服务开机自启。

## API 兼容性

服务端实现了 Komari 的公开 JSON-RPC 接口（`POST /api/rpc2`，`{"jsonrpc":"2.0","method":"public:xxx"}`）：

| 方法 | 说明 |
|---|---|
| `public:getVersion` | 服务端版本 |
| `public:getMe` | 当前登录态（访客返回 Guest） |
| `public:getNodesInformation` | 节点基本信息列表 |
| `public:getPublicSettings` | 公开站点设置 |
| `public:getClientRecentRecords` | 某节点的最近上报记录 |
| `public:getRecordsByUUID` | 某节点的历史记录（legacy 形状） |
| `public:queryMetrics` | 按 metric key 查询指标序列（图表用） |
| `public:listMetricDefinitions` | 可用指标定义列表 |

Agent 上报接口：

| 接口 | 说明 |
|---|---|
| `POST /api/agent/register` | 注册节点，返回 uuid + token |
| `POST /api/agent/report` | 上报基础信息（首次） |
| `POST /api/agent/metrics` | 上报实时指标 |

## 指标

Agent 每 2 秒采集并上报：CPU 使用率、内存、Swap、磁盘、网络上下行速率与总量、TCP/UDP 连接数、进程数、系统负载、开机时长。服务端保留最近记录，`queryMetrics` 支持 `cpu.usage`、`memory.used`、`disk.used`、`net.in.rate`、`net.out.rate`、`load.average` 等 key，前端图表直接可用。

## 安全建议

- 生产环境务必设置强管理密码，关闭公网直接暴露时建议前面加反向代理（Nginx/Caddy）并启用 HTTPS。
- 每个节点的 token 妥善保管，泄露后可在管理端重新生成。
- 默认管理接口仅监听时请注意防火墙规则。

## 许可证与致谢

- 本项目服务端与 Agent 代码为原创实现。
- 前端主题 `server/static/` 来自 [Komari-Theme-LuminaPlus](https://github.com/shanyang242/Komari-Theme-LuminaPlus)（作者 shark & shanyang），请遵守其原仓库的许可证与署名要求。
- API 设计兼容 [Komari](https://github.com/komari-monitor/komari)，感谢 Komari 团队的开源工作。
