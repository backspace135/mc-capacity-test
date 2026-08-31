#requires -Version 5.1
<#
一键容量压测(Windows 版,在服务器本机上直接运行)。
自动:下载 Purpur → 写配置(虚空世界/RCON/激活范围) → 启动服务器 → 执行测试 → 结果存 .\results\

两种运行方式:
  Docker 模式(默认): 需要 Docker Desktop(Linux 容器模式),与 Linux 版行为一致,支持绑核
  直跑模式(-NoDocker): 不需要 Docker,用本机 Java 25+ 直接启动 Purpur,绑核转为 CPU 亲和性

不熟悉命令行?直接双击同目录的「一键压测.bat」即可;测完双击「停服.bat」关服。

用法(PowerShell,在工具包根目录下):
  .\Windows\run_capacity_test.ps1 [选项] [capacity_test.py 的参数...]
例:
  .\Windows\run_capacity_test.ps1                               # 完整测试(僵尸,自适应步长,约 10 分钟)
  .\Windows\run_capacity_test.ps1 -NoDocker                     # 不用 Docker,本机 Java 直跑
  .\Windows\run_capacity_test.ps1 -NoDocker --preset armor_stand --step 2000
  .\Windows\run_capacity_test.ps1 --warmup 120 --measure 300 --interval 10  # 长窗口精测
  .\Windows\run_capacity_test.ps1 -Background                   # 后台运行,自行看 results\run-*.log
  .\Windows\run_capacity_test.ps1 -StopServer                   # 停服(容器或直跑进程)
选项:
  -Dir C:\path   服务器数据目录(默认 %USERPROFILE%\purpur-test)
  -CpuSet 0-7    绑核(保证测试间可比,别改来改去)
  -NoDocker      本机 Java 直跑(Docker 不可用时也会自动切到此模式)
  -Background    测试进程后台运行(长测试防终端断开)
  -StopServer    只停服,不测试
#>
[CmdletBinding()]
param(
  [string]$Dir = (Join-Path $env:USERPROFILE 'purpur-test'),
  [string]$CpuSet = '0-7',
  [switch]$NoDocker,
  [switch]$Background,
  [switch]$StopServer,
  [Parameter(ValueFromRemainingArguments = $true)][string[]]$TestArgs = @()
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-Location (Split-Path -Parent $PSScriptRoot)   # 工具包根目录:capacity_test.py 与 results\ 都在这里

function Write-Step([string]$Msg) {
  Write-Host ''
  Write-Host "== $Msg ==" -ForegroundColor Cyan
}
# PS 5.1 的 .NET 默认可能不启用 TLS1.2,下载 jar 会失败
try {
  [Net.ServicePointManager]::SecurityProtocol = `
    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

$Image = 'eclipse-temurin:25-jre'
$PurpurUrl = 'https://api.purpurmc.org/v2/purpur/26.2/latest/download'
$PidFile = Join-Path $Dir 'server.pid'

function Test-Docker {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }
  & docker info *> $null
  return ($LASTEXITCODE -eq 0)
}

function ConvertTo-AffinityMask([string]$Spec) {
  # "0-7" / "0,2,4" → 位掩码;超出实际核数的位忽略
  $limit = [Math]::Min([Environment]::ProcessorCount, 64)
  $mask = [uint64]0
  foreach ($part in ($Spec -split ',')) {
    if ($part -match '^\s*(\d+)\s*-\s*(\d+)\s*$') { $a = [int]$Matches[1]; $b = [int]$Matches[2] }
    elseif ($part -match '^\s*(\d+)\s*$') { $a = [int]$Matches[1]; $b = $a }
    else { continue }
    for ($i = $a; $i -le $b; $i++) {
      if ($i -lt $limit) { $mask = $mask -bor ([uint64]1 -shl $i) }
    }
  }
  return $mask
}

# ---- 找 Python 3(python / py -3 / python3;跳过 Microsoft Store 的占位 stub)----
$PyExe = $null; $PyPre = @()
foreach ($cand in @(
    @{ exe = 'python';  pre = @() },
    @{ exe = 'py';      pre = @('-3') },
    @{ exe = 'python3'; pre = @() })) {
  if (-not (Get-Command $cand.exe -ErrorAction SilentlyContinue)) { continue }
  try {
    $v = & $cand.exe @($cand.pre) '-c' 'import sys;print(sys.version_info[0])' 2>$null
    if ("$v".Trim() -eq '3') { $PyExe = $cand.exe; $PyPre = $cand.pre; break }
  } catch {}
}
if (-not $PyExe) { Write-Host '需要 Python 3(https://www.python.org/downloads/ 安装后重开终端)'; exit 1 }

# ---- -StopServer: 只停服 ----
if ($StopServer) {
  $done = $false
  if (Test-Docker) {
    & docker inspect purpur-test *> $null
    if ($LASTEXITCODE -eq 0) {
      & docker stop purpur-test | Out-Null
      Write-Host '已停止容器 purpur-test(数据保留,下次秒起)'
      $done = $true
    }
  }
  if (-not $done -and (Test-Path $PidFile)) {
    $srvPid = [int](Get-Content $PidFile | Select-Object -First 1)
    $proc = Get-Process -Id $srvPid -ErrorAction SilentlyContinue
    if ($proc) {
      # 先走 RCON 优雅关服,30s 不退再强杀
      & $PyExe @PyPre 'capacity_test.py' '--server-dir' $Dir '--stop' *> $null
      if (-not $proc.WaitForExit(30000)) { Stop-Process -Id $srvPid -Force }
      Write-Host "已停止直跑服务器(PID $srvPid)"
    } else {
      Write-Host '服务器未在运行'
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    $done = $true
  }
  if (-not $done) { Write-Host '未发现在运行的测试服' }
  exit 0
}

# ---- 选择运行模式 ----
$UseDocker = -not $NoDocker
if ($UseDocker -and -not (Test-Docker)) {
  Write-Host '[!] Docker 不可用(未安装或 Docker Desktop 未启动),改用本机 Java 直跑'
  $UseDocker = $false
}
if (-not $UseDocker) {
  $major = $null
  if (Get-Command java -ErrorAction SilentlyContinue) {
    $out = (& java -version 2>&1) -join "`n"
    if ($out -match 'version "(\d+)(?:\.(\d+))?') {
      $major = [int]$Matches[1]
      if ($major -eq 1 -and $Matches[2]) { $major = [int]$Matches[2] }  # "1.8.0" 老格式
    }
  }
  if (-not $major -or $major -lt 25) {
    $cur = if ($major) { "Java $major" } else { '未安装' }
    Write-Host "直跑模式需要 Java 25+(当前: $cur)。请装 Temurin 25: https://adoptium.net/"
    exit 1
  }
}

Write-Step "[1/4] 准备 $Dir(jar/eula/配置)"
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$jar = Join-Path $Dir 'purpur.jar'
if (-not (Test-Path $jar)) {
  Write-Host '  下载 purpur.jar(约 64MB,仅首次)...'
  $ok = $false
  $curl = Get-Command curl.exe -ErrorAction SilentlyContinue   # Win10 1803+ 自带,有进度条
  foreach ($i in 1..3) {
    try {
      if ($curl) {
        & curl.exe -fL --retry 3 --progress-bar -o $jar $PurpurUrl
        if ($LASTEXITCODE -ne 0) { throw "curl 退出码 $LASTEXITCODE" }
      } else {
        Invoke-WebRequest -Uri $PurpurUrl -OutFile $jar
      }
      $ok = $true; break
    }
    catch { Write-Host "  下载失败($i/3): $($_.Exception.Message)"; Start-Sleep 3 }
  }
  if (-not $ok) { Remove-Item $jar -ErrorAction SilentlyContinue; exit 1 }
  Write-Host ("  下载完成({0:n1} MB)" -f ((Get-Item $jar).Length / 1MB))
} else {
  Write-Host '  purpur.jar 已存在,跳过下载'
}
Set-Content -Path (Join-Path $Dir 'eula.txt') -Value 'eula=true' -Encoding ascii
$props = Join-Path $Dir 'server.properties'
if (-not (Test-Path $props)) {
  # RCON 密码随机生成,记录在 server.properties 里
  $rconPw = -join ((1..24) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })
  @"
level-type=minecraft:flat
generator-settings={"layers":[],"biome":"minecraft:the_void"}
enable-rcon=true
rcon.port=25575
rcon.password=$rconPw
online-mode=false
white-list=true
spawn-protection=0
view-distance=8
simulation-distance=8
max-players=5
motd=capacity-test
"@ | Set-Content -Path $props -Encoding ascii
}
# 关键:entity-activation-range 全 0(禁用降频),否则无玩家在线时 AI 被跳过,僵尸负载失真
$spigot = Join-Path $Dir 'spigot.yml'
if (-not (Test-Path $spigot)) {
  @'
world-settings:
  default:
    entity-activation-range:
      animals: 0
      monsters: 0
      raiders: 0
      misc: 0
      water: 0
      villagers: 0
      flying-monsters: 0
'@ | Set-Content -Path $spigot -Encoding ascii
}

if ($UseDocker) {
  Write-Step "[2/4] 确保容器 purpur-test 运行(绑核 $CpuSet)"
  & docker inspect purpur-test *> $null
  if ($LASTEXITCODE -eq 0) {
    Write-Host '  容器已存在,启动...'
    & docker start purpur-test | Out-Null
  } else {
    & docker image inspect $Image *> $null
    if ($LASTEXITCODE -ne 0) {
      Write-Host "  拉取镜像 $Image(仅首次,视网速 1-2 分钟)..."
      & docker pull $Image
      if ($LASTEXITCODE -ne 0) { Write-Host 'docker pull 失败'; exit 1 }
    }
    Write-Host '  创建并启动容器...'
    & docker run -d --name purpur-test --memory 6g --cpuset-cpus $CpuSet `
      -p 127.0.0.1:25575:25575 -p 25565:25565 -v "${Dir}:/data" -w /data $Image `
      java -Xms4G -Xmx4G -XX:+UseG1GC '-Xlog:gc*:file=/data/gc.log:time,uptime' `
      -jar purpur.jar nogui | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host 'docker run 失败'; exit 1 }
  }
} else {
  Write-Step "[2/4] 确保本机 Java 服务器运行(CPU 亲和 $CpuSet)"
  $alive = $false
  if (Test-Path $PidFile) {
    $srvPid = [int](Get-Content $PidFile | Select-Object -First 1)
    if (Get-Process -Id $srvPid -ErrorAction SilentlyContinue) { $alive = $true }
  }
  if (-not $alive) {
    $jArgs = @('-Xms4G', '-Xmx4G', '-XX:+UseG1GC', '-Xlog:gc*:file=gc.log:time,uptime',
               '-jar', 'purpur.jar', 'nogui')
    $proc = Start-Process -FilePath 'java' -ArgumentList $jArgs -WorkingDirectory $Dir `
      -WindowStyle Hidden -PassThru `
      -RedirectStandardOutput (Join-Path $Dir 'server.log') `
      -RedirectStandardError (Join-Path $Dir 'server-err.log')
    Set-Content -Path $PidFile -Value $proc.Id
    $mask = ConvertTo-AffinityMask $CpuSet
    if ($mask -ne 0) {
      try { $proc.ProcessorAffinity = [IntPtr][int64]$mask }
      catch { Write-Host "  [!] 设置 CPU 亲和失败(不影响本次测试,跨机对比时注意): $($_.Exception.Message)" }
    }
    Write-Host "  已启动 java(PID $($proc.Id)),日志: $(Join-Path $Dir 'server.log')"
    Write-Host '  [!] 直跑模式 RCON(25575)监听所有网卡,密码随机;机器有公网 IP 的话请在防火墙拦掉该端口'
  } else {
    Write-Host '  服务器已在运行,直接复用'
  }
}

Write-Step '[3/4] 等待 RCON 就绪(首次启动要生成世界,通常 10-40s)'
$ready = $false
$t0 = Get-Date
foreach ($i in 1..60) {
  & $PyExe @PyPre 'capacity_test.py' '--server-dir' $Dir '--ping' *> $null
  if ($LASTEXITCODE -eq 0) { $ready = $true; break }
  Write-Host -NoNewline ("`r  等待中 {0}s / 最多 180s " -f [int]((Get-Date) - $t0).TotalSeconds)
  Start-Sleep 3
}
Write-Host ''
if (-not $ready) {
  $hint = if ($UseDocker) { 'docker logs purpur-test' } else { Join-Path $Dir 'server.log' }
  Write-Host "RCON 180s 未就绪,查日志: $hint"
  exit 1
}
Write-Host ("  RCON 就绪(用时 {0}s)" -f [int]((Get-Date) - $t0).TotalSeconds)

Write-Step '[4/4] 执行容量测试(结果在 .\results\;每级打印 预热→测量→P95/TPS,默认约 4 级 10 分钟)'
New-Item -ItemType Directory -Force -Path 'results' | Out-Null
$runArgs = @($PyPre) + @('-u', 'capacity_test.py', '--server-dir', $Dir, '--outdir', '.\results') + @($TestArgs)
if ($Background) {
  $ts = Get-Date -Format 'yyyyMMdd-HHmmss'
  $log = "results\run-$ts.log"
  # Start-Process 不会自动给含空格的参数加引号
  $quoted = $runArgs | ForEach-Object { if ($_ -match '\s') { '"{0}"' -f $_ } else { $_ } }
  $bg = Start-Process -FilePath $PyExe -ArgumentList $quoted -WorkingDirectory $PSScriptRoot `
    -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $log -RedirectStandardError "results\run-$ts.err.log"
  Write-Host "已后台运行(PID $($bg.Id))。看进度: Get-Content -Wait $log"
  exit 0
}
& $PyExe @runArgs
$code = $LASTEXITCODE

Write-Host ''
Write-Host '完成。停服: 双击 Windows\停服.bat,或 .\Windows\run_capacity_test.ps1 -StopServer' -ForegroundColor Cyan
exit $code
