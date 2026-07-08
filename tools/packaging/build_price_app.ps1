param(
  [string]$Version = "0.1.0"
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$toolsDir = Join-Path $repoRoot "tools"
$distRoot = Join-Path $repoRoot "dist\price-webview-app"
$buildRoot = Join-Path $repoRoot "build\price-webview-app"
$pyinstallerDistDir = Join-Path $buildRoot "pyinstaller-dist"
$entry = Join-Path $toolsDir "price_webview_app.py"
$collector = Join-Path $toolsDir "price_collector_snippet.js"
$requirements = Join-Path $toolsDir "price_webview_app_requirements.txt"

$privateNames = @(
  "output",
  "price-sites.json",
  "price-latest.json",
  "price-latest.csv",
  "price-history",
  "price-webview-profile",
  "chrome-profiles",
  "edge-profiles",
  "price-login-profiles"
)

function Assert-PathInside {
  param([string]$Path, [string]$Parent)
  $resolvedPath = [System.IO.Path]::GetFullPath($Path)
  $resolvedParent = [System.IO.Path]::GetFullPath($Parent)
  if (-not $resolvedPath.StartsWith($resolvedParent, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to operate outside expected directory: $resolvedPath"
  }
}

function Assert-NoPrivateFiles {
  param([string]$Path)
  $matches = Get-ChildItem -LiteralPath $Path -Recurse -Force | Where-Object {
    $privateNames -contains $_.Name
  }
  if ($matches) {
    $list = ($matches | Select-Object -ExpandProperty FullName) -join "`n"
    throw "Privacy check failed. Private runtime data is present in package output:`n$list"
  }
}

Assert-PathInside $distRoot (Join-Path $repoRoot "dist")
Assert-PathInside $buildRoot (Join-Path $repoRoot "build")

if (-not (Test-Path $entry)) { throw "Missing app entry: $entry" }
if (-not (Test-Path $collector)) { throw "Missing collector snippet: $collector" }
if (-not (Test-Path $requirements)) { throw "Missing requirements: $requirements" }

python -m pip install -r $requirements "pyinstaller==6.11.1" "pyinstaller-hooks-contrib<2025" -i https://pypi.org/simple --timeout 30
if ($LASTEXITCODE -ne 0) { throw "pip install failed with exit code $LASTEXITCODE" }

if (Test-Path $distRoot) { Remove-Item -LiteralPath $distRoot -Recurse -Force }
if (Test-Path $buildRoot) { Remove-Item -LiteralPath $buildRoot -Recurse -Force }
New-Item -ItemType Directory -Force $distRoot | Out-Null
New-Item -ItemType Directory -Force $pyinstallerDistDir | Out-Null

$addData = "$collector;."
python -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --onefile `
  --name "Sub2APIPriceMonitor" `
  --distpath $pyinstallerDistDir `
  --workpath $buildRoot `
  --specpath $buildRoot `
  --add-data $addData `
  --collect-all webview `
  --exclude-module webview.platforms.android `
  --exclude-module webview.platforms.gtk `
  --exclude-module webview.platforms.qt `
  --exclude-module webview.platforms.cocoa `
  $entry
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$builtExe = Join-Path $pyinstallerDistDir "Sub2APIPriceMonitor.exe"
if (-not (Test-Path $builtExe)) {
  throw "PyInstaller did not create expected exe: $builtExe"
}

Assert-NoPrivateFiles $pyinstallerDistDir

$releaseExe = Join-Path $distRoot "Sub2APIPriceMonitor-$Version.exe"
Copy-Item -LiteralPath $builtExe -Destination $releaseExe -Force

Assert-NoPrivateFiles $distRoot

Write-Host "Build complete."
Write-Host "Single EXE: $releaseExe"
exit 0
