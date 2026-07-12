param(
  [string]$Version = "0.1.10"
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
$versionInfo = Join-Path $buildRoot "version_info.txt"

$privateNames = @(
  "output",
  "price-sites.json",
  "price-latest.json",
  "price-latest.csv",
  "price-history",
  "price-webview-profile",
  "logs",
  "app.log",
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
if ($Version -notmatch '^\d+\.\d+\.\d+(?:\.\d+)?$') {
  throw "Version must use numeric dotted form, for example 0.1.8"
}
$sourceVersionMatch = Select-String -LiteralPath $entry -Pattern '^APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $sourceVersionMatch) { throw "APP_VERSION was not found in $entry" }
$sourceVersion = $sourceVersionMatch.Matches[0].Groups[1].Value
if ($sourceVersion -ne $Version) {
  throw "Build version $Version does not match APP_VERSION $sourceVersion"
}

python -m pip install -r $requirements "pyinstaller==6.11.1" "pyinstaller-hooks-contrib<2025" -i https://pypi.org/simple --timeout 30
if ($LASTEXITCODE -ne 0) { throw "pip install failed with exit code $LASTEXITCODE" }

if (Test-Path $distRoot) { Remove-Item -LiteralPath $distRoot -Recurse -Force }
if (Test-Path $buildRoot) { Remove-Item -LiteralPath $buildRoot -Recurse -Force }
New-Item -ItemType Directory -Force $distRoot | Out-Null
New-Item -ItemType Directory -Force $pyinstallerDistDir | Out-Null

$versionParts = @($Version.Split('.') | ForEach-Object { [int]$_ })
while ($versionParts.Count -lt 4) { $versionParts += 0 }
$versionTuple = $versionParts[0..3] -join ', '
$versionMetadata = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($versionTuple),
    prodvers=($versionTuple),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'ULing19'),
        StringStruct('FileDescription', 'Sub2API Relay Price Monitor'),
        StringStruct('FileVersion', '$Version'),
        StringStruct('InternalName', 'Sub2APIPriceMonitor'),
        StringStruct('OriginalFilename', 'Sub2APIPriceMonitor.exe'),
        StringStruct('ProductName', 'Sub2API Price Monitor'),
        StringStruct('ProductVersion', '$Version')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
[System.IO.File]::WriteAllText(
  $versionInfo,
  $versionMetadata,
  [System.Text.UTF8Encoding]::new($false)
)

$addData = "$collector;."
python -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --onefile `
  --name "Sub2APIPriceMonitor" `
  --version-file $versionInfo `
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
