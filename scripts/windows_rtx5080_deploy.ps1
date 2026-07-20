[CmdletBinding()]
param(
    [switch]$LauncherSelfTest
)

if ($LauncherSelfTest) {
    Write-Output "RTX5080 launcher self-test passed."
    exit 0
}

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LogDir = Join-Path $ProjectRoot "deploy-logs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$LogFile = Join-Path $LogDir ("install-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host ("==== {0} ====" -f $Message) -ForegroundColor Cyan
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')"
    }
}

function Ensure-Ffmpeg {
    $FfmpegDir = Join-Path $ProjectRoot "ffmpeg"
    $FfmpegExe = Join-Path $FfmpegDir "ffmpeg.exe"
    $FfprobeExe = Join-Path $FfmpegDir "ffprobe.exe"
    if ((Test-Path $FfmpegExe) -and (Test-Path $FfprobeExe)) {
        return
    }

    Write-Step "下载支持 NVENC 的 FFmpeg"
    $Archive = Join-Path $env:TEMP "pyvideotrans-ffmpeg.zip"
    $ExtractDir = Join-Path $env:TEMP ("pyvideotrans-ffmpeg-{0}" -f ([guid]::NewGuid().ToString("N")))
    $Url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
    New-Item -ItemType Directory -Path $FfmpegDir -Force | Out-Null
    Invoke-WebRequest -Uri $Url -OutFile $Archive -UseBasicParsing
    Expand-Archive -LiteralPath $Archive -DestinationPath $ExtractDir
    $BinDir = Get-ChildItem -LiteralPath $ExtractDir -Directory |
        Select-Object -First 1 |
        ForEach-Object { Join-Path $_.FullName "bin" }
    Copy-Item -LiteralPath (Join-Path $BinDir "ffmpeg.exe") -Destination $FfmpegExe
    Copy-Item -LiteralPath (Join-Path $BinDir "ffprobe.exe") -Destination $FfprobeExe
}

Set-Location $ProjectRoot
Start-Transcript -Path $LogFile | Out-Null
try {
    Write-Step "检查 RTX 5080 与磁盘空间"
    $NvidiaSmi = Get-Command "nvidia-smi.exe" -ErrorAction Stop
    $GpuInfo = & $NvidiaSmi.Source --query-gpu=name,driver_version,memory.total,memory.free --format=csv,noheader,nounits
    if ($LASTEXITCODE -ne 0 -or -not $GpuInfo) {
        throw "nvidia-smi failed; NVIDIA driver is unavailable."
    }
    Write-Host $GpuInfo
    if ($GpuInfo -notmatch "RTX 5080") {
        Write-Warning "当前 GPU 不是 RTX 5080，脚本仍会继续，但需要重新核对兼容性。"
    }

    $Drive = [System.IO.DriveInfo]::new((Split-Path $ProjectRoot -Qualifier))
    $FreeGB = [math]::Round($Drive.AvailableFreeSpace / 1GB, 1)
    Write-Host "项目盘剩余空间：$FreeGB GB"
    if ($FreeGB -lt 25) {
        throw "至少需要 25 GB 可用空间。"
    }

    $Uv = Join-Path $ProjectRoot "tools\uv.exe"
    if (-not (Test-Path $Uv)) {
        throw "部署包缺少 tools\uv.exe。"
    }

    Write-Step "安装 Python 3.10 与主程序依赖"
    Invoke-Checked $Uv "python" "install" "3.10"
    Invoke-Checked $Uv "sync" "--frozen"

    Write-Step "安装字幕消除独立 GPU 环境"
    $VsrRoot = Join-Path $ProjectRoot "video-subtitle-remover"
    $VsrVenv = Join-Path $ProjectRoot "vsr-venv310"
    $VsrPython = Join-Path $VsrVenv "Scripts\python.exe"
    if (-not (Test-Path $VsrPython)) {
        Invoke-Checked $Uv "venv" $VsrVenv "--python" "3.10"
    }

    # SubtitleDetect currently invokes PaddleOCR with device="cpu". Installing
    # a CUDA-enabled Paddle beside Torch makes both frameworks load different
    # cuDNN builds into one Windows process and fails with WinError 127. Keep
    # OCR on the stable CPU runtime and reserve the RTX 5080 for Torch inpaint.
    & $Uv "--no-config" "pip" "uninstall" "--python" $VsrPython "paddlepaddle-gpu"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "paddlepaddle-gpu was not installed; continuing."
    }
    Invoke-Checked $Uv "--no-config" "pip" "install" "--python" $VsrPython "paddlepaddle==3.2.0" "--index-url" "https://www.paddlepaddle.org.cn/packages/stable/cpu/"
    Invoke-Checked $Uv "--no-config" "pip" "install" "--python" $VsrPython "torch==2.7.1" "torchvision==0.22.1" "--index-url" "https://download.pytorch.org/whl/cu128"
    Invoke-Checked $Uv "--no-config" "pip" "install" "--python" $VsrPython "-r" (Join-Path $VsrRoot "requirements.txt")

    Ensure-Ffmpeg

    Write-Step "验证 CUDA、OCR、模型与 NVENC"
    $MainPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    Invoke-Checked $MainPython "-c" "import torch; assert torch.cuda.is_available(); print('Main CUDA:', torch.__version__, torch.version.cuda, torch.cuda.get_device_name(0))"
    Invoke-Checked $VsrPython "-c" "import torch, paddle, cv2; from paddleocr import TextDetection; assert torch.cuda.is_available(); assert not paddle.is_compiled_with_cuda(); print('VSR inpaint CUDA:', torch.__version__, torch.version.cuda, torch.cuda.get_device_name(0)); print('PaddleOCR runtime:', paddle.__version__, 'CPU')"

    $RequiredModels = @(
        "video-subtitle-remover\backend\models\propainter\ProPainter.pth",
        "video-subtitle-remover\backend\models\propainter\raft-things.pth",
        "video-subtitle-remover\backend\models\propainter\recurrent_flow_completion.pth",
        "video-subtitle-remover\backend\models\big-lama\big-lama.pt",
        "models\models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"
    )
    foreach ($RelativePath in $RequiredModels) {
        if (-not (Test-Path (Join-Path $ProjectRoot $RelativePath))) {
            throw "缺少模型：$RelativePath"
        }
    }

    $Ffmpeg = Join-Path $ProjectRoot "ffmpeg\ffmpeg.exe"
    $NvencOutput = & $Ffmpeg -hide_banner -encoders 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0 -or $NvencOutput -notmatch "h264_nvenc") {
        throw "FFmpeg 未检测到 h264_nvenc。"
    }

    $Launcher = Join-Path $ProjectRoot "START_pyVideoTrans.cmd"
    if (-not (Test-Path $Launcher)) {
        throw "部署包缺少启动器。"
    }
    $Desktop = [Environment]::GetFolderPath("Desktop")
    $ShortcutPath = Join-Path $Desktop "pyVideoTrans RTX5080.lnk"
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $Launcher
    $Shortcut.WorkingDirectory = $ProjectRoot
    $Shortcut.Save()

    Write-Step "部署完成"
    Write-Host "桌面快捷方式：$ShortcutPath" -ForegroundColor Green
    Write-Host "安装日志：$LogFile" -ForegroundColor Green
}
catch {
    Write-Host ""
    Write-Host ("部署失败：{0}" -f $_.Exception.Message) -ForegroundColor Red
    Write-Host "请保留日志：$LogFile" -ForegroundColor Yellow
    throw
}
finally {
    Stop-Transcript | Out-Null
}
