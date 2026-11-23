<#
.SYNOPSIS
  Tek komutla dist\KickIndirici.exe üretir.
.DESCRIPTION
  venv kurar, bağımlılıkları yükler, ffmpeg/ffprobe'u (gyan.dev essentials) indirir,
  ikonu üretir, testleri çalıştırır ve PyInstaller --onefile --noconsole ile paketler.
  UPX kullanılmaz (antivirüs yanlış pozitiflerini artırıyor).
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\build.ps1
  powershell -ExecutionPolicy Bypass -File .\build.ps1 -SkipTests
#>
param(
    [switch]$SkipTests,
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest'i çok hızlandırır
Set-Location $PSScriptRoot

function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Komut başarısız ($LASTEXITCODE): $Exe $($Arguments -join ' ')" }
}

# 1) venv + bağımlılıklar
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "==> venv oluşturuluyor"
    Invoke-Checked $Python @("-m", "venv", ".venv")
}
$py = (Resolve-Path ".venv\Scripts\python.exe").Path
Write-Host "==> Bağımlılıklar yükleniyor"
Invoke-Checked $py @("-m", "pip", "install", "--disable-pip-version-check", "-q", "--upgrade", "pip")
Invoke-Checked $py @("-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", "requirements-dev.txt")

# 2) ffmpeg + ffprobe (gyan.dev essentials build)
New-Item -ItemType Directory -Force -Path vendor | Out-Null
if (-not ((Test-Path "vendor\ffmpeg.exe") -and (Test-Path "vendor\ffprobe.exe"))) {
    $zip = Join-Path $env:TEMP "ffmpeg-essentials.zip"
    $sources = @("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip")
    try {
        $rel = Invoke-RestMethod "https://api.github.com/repos/GyanD/codexffmpeg/releases/latest" -Headers @{ "User-Agent" = "KickIndirici-build" }
        $asset = $rel.assets | Where-Object { $_.name -like "*essentials_build.zip" } | Select-Object -First 1
        if ($asset) { $sources += $asset.browser_download_url }
    } catch { Write-Host "GitHub yedeği alınamadı: $_" }
    $ok = $false
    foreach ($url in $sources) {
        for ($i = 1; $i -le 3 -and -not $ok; $i++) {
            try {
                Write-Host "==> ffmpeg indiriliyor ($i): $url"
                Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 600
                $ok = $true
            } catch { Write-Host "   hata: $_"; Start-Sleep -Seconds (5 * $i) }
        }
        if ($ok) { break }
    }
    if (-not $ok) { throw "ffmpeg indirilemedi" }
    $tmp = Join-Path $env:TEMP ("ffmpeg-" + [guid]::NewGuid())
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    foreach ($n in @("ffmpeg.exe", "ffprobe.exe")) {
        $f = Get-ChildItem -Path $tmp -Recurse -Filter $n | Select-Object -First 1
        if (-not $f) { throw "$n arşivde yok" }
        Copy-Item $f.FullName "vendor\$n" -Force
    }
    Remove-Item $tmp -Recurse -Force
    Remove-Item $zip -Force
}
& "vendor\ffmpeg.exe" -hide_banner -version | Select-Object -First 1

# 3) ikon
Invoke-Checked $py @("tools\make_icon.py")

# 4) testler
if (-not $SkipTests) {
    Write-Host "==> Testler"
    Invoke-Checked $py @("-m", "pytest", "-q")
}

# 5) sürüm bilgisi
$version = (Select-String -Path "kickindirici\__init__.py" -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
$parts = ($version.Split(".") + @("0", "0", "0", "0"))[0..3] -join ", "
$versionFile = "build\version_info.txt"
New-Item -ItemType Directory -Force -Path build | Out-Null
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($parts), prodvers=($parts), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041F04B0', [
      StringStruct('CompanyName', 'kick-vod-downloader'),
      StringStruct('FileDescription', 'Kick VOD Indirici'),
      StringStruct('FileVersion', '$version'),
      StringStruct('InternalName', 'KickIndirici'),
      StringStruct('OriginalFilename', 'KickIndirici.exe'),
      StringStruct('ProductName', 'Kick VOD Indirici'),
      StringStruct('ProductVersion', '$version')])]),
    VarFileInfo([VarStruct('Translation', [0x041F, 1200])])
  ]
)
"@ | Set-Content -Encoding UTF8 $versionFile

# 6) PyInstaller
Write-Host "==> PyInstaller ($version)"
Invoke-Checked $py @(
    "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--noconsole", "--noupx",
    "--name", "KickIndirici",
    "--icon", "kickindirici\assets\icon.ico",
    "--version-file", $versionFile,
    "--add-binary", "vendor\ffmpeg.exe;bin",
    "--add-binary", "vendor\ffprobe.exe;bin",
    "--add-data", "kickindirici\templates;kickindirici\templates",
    "--add-data", "kickindirici\static;kickindirici\static",
    "--add-data", "kickindirici\assets;kickindirici\assets",
    "--hidden-import", "pystray._win32",
    "run.py"
)
$exe = Get-Item "dist\KickIndirici.exe"
Write-Host ("==> Hazır: {0} ({1:N1} MB)" -f $exe.FullName, ($exe.Length / 1MB))
