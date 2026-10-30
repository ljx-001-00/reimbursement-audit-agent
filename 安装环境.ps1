param([string]$Python = "", [switch]$Launch)
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$projectRoot = $PSScriptRoot
$venvRoot = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
Set-Location -LiteralPath $projectRoot

# requirements.txt 的 wheel 是按 CPython 3.10 锁定的（见该文件头注释），3.10～3.12 的 64 位
# 解释器都能装齐。原来只放行 3.12，会把本该能跑的 3.10 挡在安装脚本之外，与依赖清单互相矛盾。
$supportedVersions = @("3.12", "3.11", "3.10")

function Test-PythonRuntime([string]$Command, [string[]]$Arguments = @()) {
    try {
        & $Command @Arguments -c "import sys,struct,tkinter; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,12) and struct.calcsize('P') == 8 else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

try {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        if (Test-Path -LiteralPath $venvRoot) {
            throw "现有 .venv 不完整。请先将它改名保留，再重新安装；本程序不会删除你的环境。"
        }
        $candidates = @()
        if ($Python) {
            $candidates += @{ Command = $Python; Arguments = @() }
        } else {
            $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
            if ($launcher) {
                # 从高版本往低版本试，谁装得齐依赖就用谁。
                foreach ($version in $supportedVersions) {
                    $candidates += @{ Command = $launcher.Source; Arguments = @("-$version") }
                }
                foreach ($line in (& $launcher.Source -0p 2>$null)) {
                    if ($line -match '([A-Za-z]:\\.+\\python(?:3\.1[0-2])?\.exe)\s*$') {
                        $candidates += @{ Command = $Matches[1]; Arguments = @() }
                    }
                }
            }
            foreach ($name in @("python3.12.exe", "python3.11.exe", "python3.10.exe", "python.exe")) {
                $candidate = Get-Command $name -ErrorAction SilentlyContinue
                if ($candidate -and $candidate.Source -notlike '*\WindowsApps\*') {
                    $candidates += @{ Command = $candidate.Source; Arguments = @() }
                }
            }
        }
        $selected = $null
        foreach ($candidate in $candidates) {
            if (Test-PythonRuntime $candidate.Command $candidate.Arguments) {
                $selected = $candidate
                break
            }
        }
        if (-not $selected) {
            throw "未找到含 Tk 的 64 位 Python 3.10～3.12。请从 https://www.python.org/downloads/windows/ 安装 Python 3.10.11（推荐，与便携版内置运行时一致；3.11 / 3.12 亦可），安装时勾选 Tcl/Tk、pip 和 py launcher，然后再次双击启动。"
        }
        Write-Host "正在建立项目独立环境……"
        $command = $selected.Command
        $arguments = $selected.Arguments
        & $command @arguments -m venv $venvRoot
        if ($LASTEXITCODE -ne 0) { throw "创建 .venv 失败。" }
    }
    if (-not (Test-PythonRuntime $venvPython)) {
        throw ".venv 需要 Windows 64 位 Python 3.10～3.12 和 Tk。请将旧 .venv 改名保留，再重新安装。"
    }
    Write-Host "正在从公开 PyPI 安装锁定依赖；首次需要联网……"
    # 依赖里有 10 MB 级的 wheel，pip 默认 15 秒读超时在慢网下极易中断；--isolated 又会忽略
    # PIP_DEFAULT_TIMEOUT 等环境变量，所以超时和重试只能写在命令行上。中断后自动再试一次，
    # 已下载的部分会命中缓存。
    $pipArguments = @("-m", "pip", "--isolated", "install", "--disable-pip-version-check",
                      "--only-binary=:all:", "--index-url", "https://pypi.org/simple",
                      "--timeout", "60", "--retries", "10",
                      "-r", (Join-Path $projectRoot "requirements.txt"))
    & $venvPython @pipArguments
    if ($LASTEXITCODE -ne 0) {
        Write-Host "依赖下载中断，正在重试一次……"
        & $venvPython @pipArguments
    }
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败。请检查网络后重试，已下载内容和原有数据会保留。" }
    # onnxruntime 的扩展模块依赖 VC++ 运行库，系统自带的往往版本偏旧（如 14.32），加载时只会报
    # “DLL 初始化例程失败”，看不出缺什么。便携版由 build.py 把 assets/msvc 拷进 _internal，
    # 源码路径必须做同一件事，否则环境检查永远过不去。
    $msvcSource = Join-Path $projectRoot "assets\msvc"
    $onnxCapi = Join-Path $venvRoot "Lib\site-packages\onnxruntime\capi"
    if ((Test-Path -LiteralPath $msvcSource) -and (Test-Path -LiteralPath $onnxCapi)) {
        Get-ChildItem -LiteralPath $msvcSource -Filter "*.dll" | ForEach-Object {
            $target = Join-Path $onnxCapi $_.Name
            if (-not (Test-Path -LiteralPath $target)) {
                Copy-Item -LiteralPath $_.FullName -Destination $target
            }
        }
    }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw "依赖一致性检查失败。" }
    & $venvPython (Join-Path $projectRoot "environment_check.py")
    if ($LASTEXITCODE -ne 0) { throw "环境检查失败，请按上述提示修复。" }
    Write-Host "安装完成。以后双击“启动开发预览.cmd”即可运行。"
    if ($Launch) {
        Start-Process -FilePath (Join-Path $venvRoot "Scripts\pythonw.exe") -ArgumentList ('"' + (Join-Path $projectRoot "main.py") + '"') -WorkingDirectory $projectRoot
    }
    exit 0
} catch {
    Write-Host ("安装未完成：" + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
