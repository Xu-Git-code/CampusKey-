<#
.SYNOPSIS
    安装 / 卸载「校园网自动登录」的开机自启（Windows 任务计划程序）。

.DESCRIPTION
    默认：当前用户登录 Windows 后 30 秒，静默运行一次 campus_login.py 或 CampusLoginSilent.exe。
    只看 logs\campus_login.log 就能知道结果，不会弹黑框。

.PARAMETER DelaySeconds
    登录后延迟多少秒再运行，默认 30。网络慢/无线网卡启动慢可以调到 60。

.PARAMETER TaskName
    计划任务名称，默认 CampusAutoLogin。

.PARAMETER Uninstall
    删除这个计划任务。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\安装开机自启.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\安装开机自启.ps1 -DelaySeconds 60

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\安装开机自启.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [int]$DelaySeconds = 30,
    [string]$TaskName = "CampusAutoLogin"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Find-Pythonw {
    try {
        $exe = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe) {
            $candidate = Join-Path (Split-Path $exe.Trim()) "pythonw.exe"
            if (Test-Path $candidate) { return $candidate }
        }
    } catch {
    }
    $command = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($command -and (Test-Path $command.Source)) { return $command.Source }
    return $null
}

function Resolve-Program {
    $silent = Join-Path $scriptDir "CampusLoginSilent.exe"
    if (Test-Path $silent) {
        return @{ Path = $silent; Args = ""; Kind = "silent exe" }
    }
    $console = Join-Path $scriptDir "CampusLogin.exe"
    if (Test-Path $console) {
        return @{ Path = $console; Args = ""; Kind = "console exe" }
    }
    $script = Join-Path $scriptDir "campus_login.py"
    if (Test-Path $script) {
        $pythonw = Find-Pythonw
        if (-not $pythonw) {
            throw "找到了 campus_login.py，但找不到 pythonw.exe。请先安装 Python，或先运行 build_exe.bat 打包成 exe。"
        }
        return @{ Path = $pythonw; Args = "`"$script`""; Kind = "python source" }
    }
    throw "没找到 CampusLoginSilent.exe / CampusLogin.exe / campus_login.py。请把本脚本放在程序目录下运行。"
}

if ($Uninstall) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已删除计划任务：$TaskName" -ForegroundColor Green
    } else {
        Write-Host "没有找到计划任务：$TaskName" -ForegroundColor Yellow
    }
    return
}

try {
    $program = Resolve-Program
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "提示：本脚本需要和 campus_login.py 或 exe 放在同一个目录。" -ForegroundColor Yellow
    exit 1
}

$action = New-ScheduledTaskAction -Execute $program.Path -Argument $program.Args -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser

$delayApplied = $true
try {
    $trigger.Delay = "PT${DelaySeconds}S"
} catch {
    $delayApplied = $false
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "校园网自动登录：用户登录后延迟运行一次（配置见 config.ini）" `
        -Force | Out-Null
} catch {
    Write-Host "注册计划任务失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "请右键“以管理员身份运行” PowerShell，再执行本脚本。" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "已安装开机自启（任务计划程序）" -ForegroundColor Green
Write-Host "  任务名  : $TaskName"
Write-Host "  程序    : $($program.Path)"
Write-Host "  参数    : $($program.Args)"
Write-Host "  运行方式: $($program.Kind)"
Write-Host "  触发条件: 用户 $currentUser 登录后 $DelaySeconds 秒"

if (-not $delayApplied) {
    Write-Host "  注意    : 当前系统不支持任务延迟，请在 config.ini 里把 startup_delay 调到 30~60。" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "立即测试 : Start-ScheduledTask -TaskName $TaskName"
Write-Host "查看日志 : notepad `"$scriptDir\logs\campus_login.log`""
Write-Host "卸载自启 : powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Uninstall"
