param(
  [Parameter(Position = 0)]
  [string]$Site = "",
  [switch]$DevTools
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  throw "python is required. Install Python first, then rerun this script."
}

python -c "import webview" 2>$null
if ($LASTEXITCODE -ne 0) {
  python -m pip install pywebview -i https://pypi.org/simple --timeout 30
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
