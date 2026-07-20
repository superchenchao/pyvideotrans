[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Uv = Join-Path $ProjectRoot "tools\uv.exe"
$VsrPython = Join-Path $ProjectRoot "vsr-venv310\Scripts\python.exe"

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

if (-not (Test-Path $Uv)) {
    throw "tools\uv.exe is missing. Extract this fix into the project root."
}
if (-not (Test-Path $VsrPython)) {
    throw "vsr-venv310 is missing. Run INSTALL_RTX5080.cmd first."
}

Write-Host "Removing the conflicting CUDA-enabled Paddle runtime..." -ForegroundColor Cyan
& $Uv "--no-config" "pip" "uninstall" "--python" $VsrPython "paddlepaddle-gpu"
if ($LASTEXITCODE -ne 0) {
    Write-Host "paddlepaddle-gpu was not installed; continuing."
}

Write-Host "Installing the stable CPU PaddleOCR runtime..." -ForegroundColor Cyan
Invoke-Checked $Uv "--no-config" "pip" "install" "--python" $VsrPython "--reinstall" "paddlepaddle==3.2.0" "--index-url" "https://www.paddlepaddle.org.cn/packages/stable/cpu/"

Write-Host "Restoring the validated Torch CUDA 12.8 inpaint runtime..." -ForegroundColor Cyan
Invoke-Checked $Uv "--no-config" "pip" "install" "--python" $VsrPython "--reinstall" "torch==2.7.1" "torchvision==0.22.1" "--index-url" "https://download.pytorch.org/whl/cu128"

Write-Host "Verifying PaddleOCR CPU plus Torch GPU in the same process..." -ForegroundColor Cyan
Invoke-Checked $VsrPython "-c" "import torch, paddle, cv2; from paddleocr import TextDetection; assert torch.cuda.is_available(); assert not paddle.is_compiled_with_cuda(); print('Torch inpaint:', torch.__version__, 'CUDA:', torch.version.cuda, torch.cuda.get_device_name(0)); print('PaddleOCR:', paddle.__version__, 'CPU')"

Write-Host "VSR runtime repair completed. Run INSTALL_RTX5080.cmd again." -ForegroundColor Green
