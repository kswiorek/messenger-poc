param(
    [string]$Version = "14.5.2",
    [ValidateSet("windows-x86_64", "windows-i686")]
    [string]$Arch = "windows-x86_64",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$torRoot = Join-Path $projectRoot "tor"
$torExe = Join-Path $torRoot "tor\tor.exe"
$torrcPath = Join-Path $torRoot "torrc"

if ((Test-Path $torExe) -and -not $Force) {
    Write-Host "Tor already exists at $torExe"
    Write-Host "Use -Force to re-download and re-extract."
    exit 0
}

New-Item -ItemType Directory -Path $torRoot -Force | Out-Null

$archiveName = "tor-expert-bundle-$Arch-$Version.tar.gz"
$downloadUrl = "https://dist.torproject.org/torbrowser/$Version/$archiveName"
$tempArchive = Join-Path $env:TEMP $archiveName

Write-Host "Downloading Tor Expert Bundle..."
Write-Host "URL: $downloadUrl"
Invoke-WebRequest -Uri $downloadUrl -OutFile $tempArchive

if ($Force) {
    $existingTorBin = Join-Path $torRoot "tor"
    $existingDocs = Join-Path $torRoot "docs"
    if (Test-Path $existingTorBin) {
        Remove-Item -Recurse -Force $existingTorBin
    }
    if (Test-Path $existingDocs) {
        Remove-Item -Recurse -Force $existingDocs
    }
}

Write-Host "Extracting bundle to $torRoot ..."
tar -xzf $tempArchive -C $torRoot

if (-not (Test-Path $torExe)) {
    throw "Tor executable was not found after extraction: $torExe"
}

$peerDataDir = Join-Path $torRoot "data\peer1"
$hiddenServiceDir = Join-Path $torRoot "data\peer1_hidden_service"
New-Item -ItemType Directory -Path $peerDataDir -Force | Out-Null
New-Item -ItemType Directory -Path $hiddenServiceDir -Force | Out-Null

if (-not (Test-Path $torrcPath)) {
    @"
# Tor config for messenger-poc peer instance
SocksPort 9050
DataDirectory .\\tor\\data\\peer1

# Onion service forwarding onion:7000 to local messenger listener 127.0.0.1:7000
HiddenServiceDir .\\tor\\data\\peer1_hidden_service
HiddenServiceVersion 3
HiddenServicePort 7000 127.0.0.1:7000

Log notice stdout
"@ | Set-Content -Path $torrcPath -Encoding UTF8

    Write-Host "Created default torrc at $torrcPath"
} else {
    Write-Host "torrc already exists at $torrcPath (left unchanged)"
}

Write-Host "Tor setup complete."
Write-Host "Executable: $torExe"
Write-Host "Config: $torrcPath"
