# install.ps1 - installs Awk Console into FL Studio for THIS Windows user.
# Run: double-click Install.bat (or: powershell -ExecutionPolicy Bypass -File install.ps1)
# Optional: -FLHardware "D:\...\FL Studio\Settings\Hardware"   -ArturiaDir "C:\path\to\KeyLab Essential V2 script folder"
param([string]$FLHardware = "", [string]$ArturiaDir = "")
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$docs = [Environment]::GetFolderPath('MyDocuments')
if (-not $FLHardware) { $FLHardware = Join-Path $docs 'Image-Line\FL Studio\Settings\Hardware' }
if (-not (Test-Path $FLHardware)) { throw "FL Studio hardware folder not found: $FLHardware`nOpen FL Studio once, or pass -FLHardware <path>." }
$userDocs = Split-Path (Split-Path (Split-Path (Split-Path $FLHardware))) -Parent   # ...\Documents
$stamp = Get-Date -Format yyyyMMdd-HHmmss

function Install-Folder($name) {
  $dest = Join-Path $FLHardware $name
  if (Test-Path $dest) { Rename-Item $dest "$name.backup-$stamp"; Write-Host "  kept your old $name as $name.backup-$stamp" }
  Copy-Item -Recurse (Join-Path $here "fl\$name") $dest
  # point the scripts at THIS user's Documents folder
  Get-ChildItem $dest -Filter *.py | ForEach-Object {
    $t = [IO.File]::ReadAllText($_.FullName)
    $t = $t.Replace('C:\Users\quick\Documents', $userDocs)
    [IO.File]::WriteAllText($_.FullName, $t)
  }
  Write-Host "installed $dest"
}
Write-Host "Awk Console installer`n  FL hardware folder: $FLHardware"
Install-Folder 'AwknodeConsole'
Install-Folder 'AwknodeKeyLabEss'

# KeyLab Essential V2 modules are Arturia's - not shipped here; copied from your own FL Studio / Arturia download.
$need = 'ArturiaCrossKeyboardEss','ArturiaVCOL','KeyLabEssDispatch','KeyLabEssDisplay','KeyLabEssNavigation','KeyLabEssPages','KeyLabEssPlugin','KeyLabEssProcess','KeyLabEssReturn'
if (-not $ArturiaDir) {
  $cands = @("$env:ProgramFiles\Image-Line", "${env:ProgramFiles(x86)}\Image-Line", $FLHardware)
  $hit = $cands | Where-Object { $_ -and (Test-Path $_) } | ForEach-Object {
    Get-ChildItem $_ -Recurse -Filter 'device_KeyLabEss.py' -ErrorAction SilentlyContinue } | Select-Object -First 1
  if ($hit) { $ArturiaDir = $hit.DirectoryName }
}
$kl = Join-Path $FLHardware 'AwknodeKeyLabEss'
if ($ArturiaDir -and (Test-Path (Join-Path $ArturiaDir 'device_KeyLabEss.py'))) {
  foreach ($n in $need) { Copy-Item (Join-Path $ArturiaDir "$n.py") $kl -Force }
  # ArturiaVCOL: drop the import of the stock entry script (we load V2 under another name)
  $p = Join-Path $kl 'ArturiaVCOL.py'
  (Get-Content $p) | Where-Object { $_ -notmatch '^\s*import device_KeyLabEss as KL\s*$' } | Set-Content -Encoding UTF8 $p
  # KeyLabEssV2Core = Arturia's device_KeyLabEss.py minus its '# name=' line (so FL doesn't list it twice)
  $src = Get-Content (Join-Path $ArturiaDir 'device_KeyLabEss.py')
  @("# KeyLab Essential V2 entry logic (Fares MEZDOUR / Arturia), unchanged except: no '# name=' line") + ($src | Select-Object -Skip 1) | Set-Content -Encoding UTF8 (Join-Path $kl 'KeyLabEssV2Core.py')
  Write-Host "copied Arturia KeyLab Essential V2 modules from $ArturiaDir"
} else {
  Write-Warning ("Arturia's KeyLab Essential V2 FL script wasn't found. 'Awknode Console' (Plugin mode) works without it.`n" +
    "  For DAW mode: get the KeyLab Essential FL Studio script from Arturia's KeyLab Essential downloads,`n" +
    "  unzip it, then re-run:  install.ps1 -ArturiaDir <folder containing device_KeyLabEss.py>")
}

# connector for AI agents (optional, needs Python 3.10+)
$con = Join-Path $userDocs 'Awknode\Console'
New-Item -ItemType Directory -Force $con | Out-Null
Copy-Item (Join-Path $here 'connector\*.py') $con -Force
Write-Host "connector installed in $con"
Write-Host "`nDONE. Next: open FL Studio > Options > MIDI settings (see README.md, 'Set up FL Studio')."
