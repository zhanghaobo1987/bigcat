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
│   ├── install.sh          # Debian/Ubuntu 一键安装（server/agent）
│   ├── uninstall.sh        # Debian/Ubuntu 卸载
│   ├── install-macos.sh    # macOS 一键安装（server/agent，launchd）
│   ├── uninstall-macos.sh  # macOS 卸载
│   ├── install.ps1         # Windows 一键安装（server/agent，计划任务）
│   ├── uninstall.ps1       # Windows 卸载
│   └── bigcat.service      # systemd 服务单元（服务端，供参考）
├── requirements.txt
├── Dockerfile
└── README.md
```

## 安装与卸载

> 一键脚本会自动：安装 Python 依赖 → 创建虚拟环境 → 复制程序文件 →
> 注册开机自启服务 → 放行防火墙端口。默认端口 `25774`，可用 `--port` /
> `-Port` 修改。

### Debian / Ubuntu

**安装主控端（服务端）：**

```bash
curl -fsSL https://raw.githubusercontent.com/zhanghaobo1987/bigcat/main/scripts/install.sh \
  | sudo bash -s -- server
# 指定端口： ... | sudo bash -s -- server --port 8080
# 预设管理密码： BIGCAT_ADMIN_PASSWORD=xxx ... | sudo bash -s -- server
```

**安装被控端（Agent，需先在主控注册拿到 token）：**

```bash
curl -fsSL https://raw.githubusercontent.com/zhanghaobo1987/bigcat/main/scripts/install.sh \
  | sudo bash -s -- agent http://主控IP:25774 <token>
```

**卸载：**

```bash
curl -fsSL https://raw.githubusercontent.com/zhanghaobo1987/bigcat/main/scripts/uninstall.sh \
  | sudo bash -s -- all            # 卸载全部（保留监控数据）
# sudo bash -s -- all --purge      # 卸载全部并删除数据
# sudo bash -s -- server           # 只卸载主控端
# sudo bash -s -- agent            # 只卸载被控端
```

### macOS

**安装主控端：**

```bash
curl -fsSL https://raw.githubusercontent.com/zhanghaobo1987/bigcat/main/scripts/install-macos.sh \
  | sudo bash -s -- server
# 指定端口： ... | sudo bash -s -- server --port 8080
```

**安装被控端：**

```bash
curl -fsSL https://raw.githubusercontent.com/zhanghaobo1987/bigcat/main/scripts/install-macos.sh \
  | sudo bash -s -- agent http://主控IP:25774 <token>
```

服务通过 launchd 注册（`com.bigcat.server` / `com.bigcat.agent`），开机自启。

**卸载：**

```bash
curl -fsSL https://raw.githubusercontent.com/zhanghaobo1987/bigcat/main/scripts/uninstall-macos.sh \
  | sudo bash -s -- all [--purge]
```

### Windows

请以**管理员身份**打开 PowerShell：

**安装主控端：**

```powershell
# 先下载脚本（以便传参）
Invoke-WebRequest -Uri https://raw.githubusercontent.com/zhanghaobo1987/bigcat/main/scripts/install.ps1 -OutFile $env:TEMP\install.ps1
powershell -ExecutionPolicy Bypass -File $env:TEMP\install.ps1 -Mode server
# 指定端口 / 预设密码：
# powershell -ExecutionPolicy Bypass -File $env:TEMP\install.ps1 -Mode server -Port 8080 -AdminPassword "你的强密码"
```

**安装被控端：**

```powershell
Invoke-WebRequest -Uri https://raw.githubusercontent.com/zhanghaobo1987/bigcat/main/scripts/install.ps1 -OutFile $env:TEMP\install.ps1
powershell -ExecutionPolicy Bypass -File $env:TEMP\install.ps1 -Mode agent -ServerUrl http://主控IP:25774 -Token <token>
```

服务注册为 Windows 计划任务（`bigcat-server` / `bigcat-agent`），系统启动时自动运行（无窗口，后台运行）。

**卸载：**

```powershell
Invoke-WebRequest -Uri https://raw.githubusercontent.com/zhanghaobo1987/bigcat/main/scripts/uninstall.ps1 -OutFile $env:TEMP\uninstall.ps1
powershell -ExecutionPolicy Bypass -File $env:TEMP\uninstall.ps1 -Mode all
# 彻底删除数据：加 -Purge
```

### 注册被控节点（拿 token）

在主控端上为每台被监控的机器注册一个节点：

```bash
curl -X POST http://主控IP:25774/api/agent/register \
  -H "Content-Type: application/json" \
  -d '{"name":"hk-1"}'
# 返回 {"uuid": "...", "token": "..."}，token 填给对应机器的安装命令
```

## 手动运行（不装服务）

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

### 2. 在被控机器上手动运行 Agent

```bash
pip install psutil requests
python3 agent/agent.py --server http://主控IP:25774 --token <token> --interval 2
```

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
