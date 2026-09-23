#Requires -RunAsAdministrator
<#
.SYNOPSIS
  bigcat 一键安装脚本（Windows）

.DESCRIPTION
  服务端（主控）:
    irm https://raw.githubusercontent.com/zhanghaobo1987/bigcat/main/scripts/install.ps1 | iex
    # 实际上推荐先下载再执行，以便传参：
    #   powershell -ExecutionPolicy Bypass -File install.ps1 -Mode server
    #   powershell -ExecutionPolicy Bypass -File install.ps1 -Mode server -Port 8080

  被控端（agent，需先在主控注册拿到 token）:
    powershell -ExecutionPolicy Bypass -File install.ps1 -Mode agent -ServerUrl http://主控IP:25774 -Token <token>

  也支持 git clone 后本地运行。
#>
param(
  [ValidateSet("server", "agent")]
  [string]$Mode,
  [int]$Port = 25774,
  [string]$ServerUrl = "",
  [string]$Token = "",
  [string]$AdminPassword = ""
)

$ErrorActionPreference = "Stop"
$RepoUrl  = "https://github.com/zhanghaobo1987/bigcat"
$ZipUrl   = "https://github.com/zhanghaobo1987/bigcat/archive/refs/heads/main.zip"
$InstallDir = "C:\Program Files\bigcat"
$VenvPythonw = Join-Path $InstallDir "venv\Scripts\pythonw.exe"
$VenvPython  = Join-Path $InstallDir "venv\Scripts\python.exe"

function Log([string]$msg) { Write-Host "[bigcat] $msg" }
function Die([string]$msg) { Write-Host "[bigcat] 错误: $msg" -ForegroundColor Red; exit 1 }

function Ensure-Sources {
  # 返回包含 server/ 和 agent/ 的源码目录
  $scriptDir = $PSScriptRoot
  if ($scriptDir -and (Test-Path (Join-Path $scriptDir "..\server\app.py")) -and (Test-Path (Join-Path $scriptDir "..\agent\agent.py"))) {
    return (Resolve-Path (Join-Path $scriptDir "..")).Path
  }
  Log "未检测到本地仓库源码，正在从 GitHub 下载..."
  $tmp = Join-Path ([IO.Path]::GetTempPath()) "bigcat-src"
  if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
  New-Item -ItemType Directory -Path $tmp | Out-Null
  $zip = Join-Path $tmp "bigcat.zip"
  Invoke-WebRequest -Uri $ZipUrl -OutFile $zip
  Expand-Archive -Path $zip -DestinationPath $tmp
  return (Join-Path $tmp "bigcat-main")
}

function Ensure-Python {
  $candidates = @(
    @{ Exe = "py";      Args = @("-3") },
    @{ Exe = "python";  Args = @() },
    @{ Exe = "python3"; Args = @() }
  )
  foreach ($c in $candidates) {
    try {
      $v = & $c.Exe @($c.Args + "--version") 2>$null
      if ($LASTEXITCODE -eq 0) { Log "检测到 Python: $v"; return $c }
    } catch {}
  }
  Log "未找到 Python，尝试通过 winget 安装..."
  try {
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
  } catch { Die "winget 安装 Python 失败，请手动从 https://www.python.org/downloads/ 安装 Python 3.10+" }
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
  return Ensure-Python
}

function Setup-Venv {
  if (-not (Test-Path $VenvPython)) {
    Log "创建虚拟环境 $InstallDir\venv ..."
    $py = Ensure-Python
    & $py.Exe @($py.Args + "-m", "venv", (Join-Path $InstallDir "venv"))
  }
  Log "安装 Python 依赖..."
  & $VenvPython -m pip install -q --upgrade pip
  & $VenvPython -m pip install -q Flask flask-cors flask-sock psutil requests
}

function Install-Server {
  $src = Ensure-Sources
  Log "安装服务端（端口 $Port）..."
  New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir "data") | Out-Null
  Copy-Item -Recurse -Force (Join-Path $src "server") $InstallDir
  Setup-Venv

  if (-not $AdminPassword) {
    $sec = Read-Host "设置管理密码（留空则跳过，可稍后设置）" -AsSecureString
    if ($sec.Length -gt 0) {
      $AdminPassword = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    }
  }
  if ($AdminPassword) {
    Push-Location (Join-Path $InstallDir "server")
    & $VenvPython app.py --db (Join-Path $InstallDir "data\bigcat.db") --set-admin $AdminPassword | Out-Null
    Pop-Location
    Log "管理密码已设置"
  } else {
    Log "未设置管理密码，稍后可用以下命令设置："
    Log "  `"$VenvPython`" `"$InstallDir\server\app.py`" --db `"$InstallDir\data\bigcat.db`" --set-admin `"你的强密码`""
  }

  $taskName = "bigcat-server"
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
  $action = New-ScheduledTaskAction -Execute $VenvPythonw `
    -Argument "`"$InstallDir\server\app.py`" --port $Port --db `"$InstallDir\data\bigcat.db`"" `
    -WorkingDirectory "$InstallDir\server"
  $trigger = New-ScheduledTaskTrigger -AtStartup
  $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  $settings = New-ScheduledTaskSettingsSet -RestartCount 9999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
  Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description "bigcat monitoring server" | Out-Null
  Start-ScheduledTask -TaskName $taskName
  Log "计划任务 $taskName 已创建并启动（开机自启）"

  if (-not (Get-NetFirewallRule -DisplayName "bigcat-server" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "bigcat-server" -Direction Inbound `
      -LocalPort $Port -Protocol TCP -Action Allow | Out-Null
    Log "防火墙已放行 TCP 端口 $Port"
  }
  Log "服务端已启动: http://本机IP:$Port"
}

function Install-Agent {
  if (-not $ServerUrl) { Die "缺少 -ServerUrl，例如 -ServerUrl http://主控IP:25774" }
  if (-not $Token)     { Die "缺少 -Token（先在主控执行 /api/agent/register 注册节点）" }
  $src = Ensure-Sources
  Log "安装 agent，上报目标 $ServerUrl ..."
  New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
  Copy-Item -Force (Join-Path $src "agent\agent.py") $InstallDir
  Setup-Venv

  $taskName = "bigcat-agent"
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
  $action = New-ScheduledTaskAction -Execute $VenvPythonw `
    -Argument "`"$InstallDir\agent.py`" --server $ServerUrl --token $Token --interval 2" `
    -WorkingDirectory $InstallDir
  $trigger = New-ScheduledTaskTrigger -AtStartup
  $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  $settings = New-ScheduledTaskSettingsSet -RestartCount 9999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
  Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description "bigcat monitoring agent" | Out-Null
  Start-ScheduledTask -TaskName $taskName
  Log "计划任务 $taskName 已创建并启动（开机自启）"
  Log "agent 正在向 $ServerUrl 上报"
}

if (-not $Mode) {
  Write-Host @"
用法（请以管理员身份运行 PowerShell）:
  powershell -ExecutionPolicy Bypass -File install.ps1 -Mode server [-Port 端口] [-AdminPassword xxx]
  powershell -ExecutionPolicy Bypass -File install.ps1 -Mode agent -ServerUrl http://主控IP:25774 -Token <token>
"@
  exit 1
}

switch ($Mode) {
  "server" { Install-Server }
  "agent"  { Install-Agent }
}
