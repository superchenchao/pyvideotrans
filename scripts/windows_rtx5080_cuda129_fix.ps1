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
    throw "tools\uv.exe is missing. Extract this fix into the pyVideoTrans project root."
}
if (-not (Test-Path $VsrPython)) {
    throw "vsr-venv310 is missing. Run INSTALL_RTX5080.cmd first."
}

Write-Host "Updating subtitle-removal Torch runtime to CUDA 12.9..." -ForegroundColor Cyan
Invoke-Checked $Uv "--no-config" "pip" "install" "--python" $VsrPython "--reinstall" "torch==2.8.0" "torchvision==0.23.0" "--index-url" "https://download.pytorch.org/whl/cu129"

Write-Host "Verifying Torch and Paddle in the same process..." -ForegroundColor Cyan
Invoke-Checked $VsrPython "-c" "import torch, paddle, cv2; assert torch.cuda.is_available(); assert paddle.is_compiled_with_cuda(); print('Torch:', torch.__version__, 'CUDA:', torch.version.cuda, torch.cuda.get_device_name(0)); print('Paddle:', paddle.__version__, 'CUDA:', paddle.is_compiled_with_cuda())"

Write-Host "CUDA 12.9 repair completed. Run INSTALL_RTX5080.cmd again." -ForegroundColor Green

