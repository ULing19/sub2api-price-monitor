param(
  [Parameter(Position = 0)]
  [string]$Site = "",
  [switch]$DevTools
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$requirements = Join-Path $scriptDir "price_webview_app_requirements.txt"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  throw "python is required. Install Python first, then rerun this script."
}

python -c "import webview, psutil" 2>$null
if ($LASTEXITCODE -ne 0) {
  python -m pip install -r $requirements -i https://pypi.org/simple --timeout 30
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to install application requirements."
  }
}

$pythonArgs = @((Join-Path $scriptDir "price_webview_app.py"))
if ($Site) {
  $pythonArgs += @("--site", $Site)
}
if ($DevTools) {
  $pythonArgs += "--devtools"
}

python @pythonArgs
exit $LASTEXITCODE
