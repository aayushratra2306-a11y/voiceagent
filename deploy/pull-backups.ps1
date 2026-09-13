<#
Backup layer 2: copy the server's database snapshots to this PC.

Layer 1 (the `backup` service in docker-compose.yml) keeps snapshots on the
VM. That protects against a bad command or a wiped database, but not against
losing the VM itself. This script copies every snapshot the PC does not
already have into -Destination.

What it changes:
  - On the VM: nothing. It only lists and reads ~/voiceagent-backups.
  - On this PC: adds new snapshot folders under -Destination. It never
    deletes or overwrites a local copy.

One-time setup:
  1. Install the Google Cloud CLI: https://cloud.google.com/sdk/docs/install
  2. In a new PowerShell window:  gcloud auth login
  3. Run once by hand:  powershell -ExecutionPolicy Bypass -File deploy\pull-backups.ps1

To run it daily, use Task Scheduler: Create Basic Task > Daily > Start a
program: powershell.exe, arguments:
  -ExecutionPolicy Bypass -File "D:\Voice Agent\deploy\pull-backups.ps1"
#>
param(
    [string]$Instance = "voiceagent",
    [string]$Zone = "asia-south1-a",
    [string]$Project = "project-26367bcc-a0f5-4bb3-b3c",
    [string]$RemoteUser = "aayush_ratra2306",
    [string]$Destination = "D:\voiceagent-backups"
)

$ErrorActionPreference = "Stop"
$remoteDir = "/home/$RemoteUser/voiceagent-backups"
$target = "$RemoteUser@$Instance"

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Write-Error "gcloud is not installed. See the one-time setup at the top of this script."
}
New-Item -ItemType Directory -Force -Path $Destination | Out-Null

# Finished snapshots only: a folder counts once its manifest.json exists, so a
# snapshot still being written on the server is never copied half-done.
$listing = gcloud compute ssh $target --zone $Zone --project $Project `
    --command "cd $remoteDir 2>/dev/null && for d in */; do d=`${d%/}; [ -f `$d/manifest.json ] && echo `$d; done"
$remote = @($listing | Where-Object { $_ -match '^\d{8}T\d{6}Z$' })

if ($remote.Count -eq 0) {
    Write-Warning "No finished snapshots found in $remoteDir on the server. Is the backup service running?"
    exit 1
}

$copied = 0
foreach ($name in $remote) {
    $local = Join-Path $Destination $name
    if (Test-Path (Join-Path $local "manifest.json")) { continue }
    Write-Host "Copying $name ..."
    gcloud compute scp --recurse --zone $Zone --project $Project "${target}:$remoteDir/$name" $Destination
    if (-not (Test-Path (Join-Path $local "manifest.json"))) {
        Write-Error "Copy of $name finished without its manifest; treat it as failed."
    }
    $copied++
}

$latest = $remote | Sort-Object | Select-Object -Last 1
Write-Host "Done. Copied $copied new snapshot(s). Server has $($remote.Count); newest is $latest."
Write-Host "Local copies: $Destination"
