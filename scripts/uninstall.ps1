#Requires -RunAsAdministrator
<#
.SYNOPSIS
  BigCat 卸载脚本（Windows）

.DESCRIPTION
  以管理员身份运行 PowerShell:
    powershell -ExecutionPolicy Bypass -File uninstall.ps1 -Mode server  # 只卸载主控端
    powershell -ExecutionPolicy Bypass -File uninstall.ps1 -Mode agent   # 只卸载被控端
    powershell -ExecutionPolicy Bypass -File uninstall.ps1 -Mode all     # 全部卸载
    powershell -ExecutionPolicy Bypass -File uninstall.ps1 -Mode all -Purge  # 全部卸载并删除数据
#>
param(
  [ValidateSet("server", "agent", "all")]
  [string]$Mode,
  [switch]$Purge
)

$ErrorActionPreference = "Stop"
$InstallDir = "C:\Program Files\bigcat"

function Log([string]$msg) { Write-Host "[BigCat] $msg" }

function Remove-Task([string]$name) {
  $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
  if ($t) {
    Log "停止并删除计划任务 $name ..."
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
  }
}

if (-not $Mode) {
  Write-Host @"
用法（请以管理员身份运行 PowerShell）:
  powershell -ExecutionPolicy Bypass -File uninstall.ps1 -Mode server|agent|all [-Purge]
"@
  exit 1
}

switch ($Mode) {
  "server" {
    Remove-Task "bigcat-server"
    Remove-NetFirewallRule -DisplayName "bigcat-server" -ErrorAction SilentlyContinue
    Log "服务端已卸载"
  }
  "agent" {
    Remove-Task "bigcat-agent"
    Log "agent 已卸载"
  }
  "all" {
    Remove-Task "bigcat-server"
    Remove-Task "bigcat-agent"
    Remove-NetFirewallRule -DisplayName "bigcat-server" -ErrorAction SilentlyContinue
    if (Test-Path $InstallDir) {
      if ($Purge) {
        Log "删除 $InstallDir（含监控数据）..."
        Remove-Item -Recurse -Force $InstallDir
      } else {
        Log "保留监控数据 $InstallDir\data，如需彻底删除请加 -Purge"
        foreach ($p in @("server", "venv", "agent.py")) {
          $full = Join-Path $InstallDir $p
          if (Test-Path $full) { Remove-Item -Recurse -Force $full }
        }
      }
    }
    Log "卸载完成"
  }
}
