# One-time setup of your Windows PC as the "home" checker (Romanian home IP -> eMAG, PC Garage, evoMAG ... work).
# Run in PowerShell **as Administrator**:
#   Set-ExecutionPolicy -Scope Process Bypass -Force; irm https://raw.githubusercontent.com/DimaDerepa/bfbot2/main/scripts/setup-home-windows.ps1 | iex
$ErrorActionPreference = "Stop"
$Repo = "DimaDerepa/bfbot2"

Write-Host "`n[1/4] Python and Git (for all users, so the runner service can use them)..." -ForegroundColor Cyan
winget install -e --id Python.Python.3.12 --scope machine --silent --accept-package-agreements --accept-source-agreements
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    winget install -e --id Git.Git --scope machine --silent --accept-package-agreements --accept-source-agreements
} else { Write-Host "Git already installed" }

Write-Host "`n[2/4] Downloading the GitHub Actions runner to C:\actions-runner ..." -ForegroundColor Cyan
$dir = "C:\actions-runner"
New-Item -ItemType Directory -Force $dir | Out-Null
Set-Location $dir
if (-not (Test-Path ".\config.cmd")) {
    $rel = Invoke-RestMethod "https://api.github.com/repos/actions/runner/releases/latest"
    $asset = $rel.assets | Where-Object { $_.name -like "actions-runner-win-x64-*.zip" } | Select-Object -First 1
    Invoke-WebRequest $asset.browser_download_url -OutFile runner.zip
    Expand-Archive runner.zip -DestinationPath . -Force
    Remove-Item runner.zip
}

Write-Host "`n[3/4] Registration token" -ForegroundColor Cyan
Write-Host "Opening https://github.com/$Repo/settings/actions/runners/new?arch=x64&os=win"
Write-Host "Copy the value after  --token  in the 'Configure' box (looks like AABCD...)."
Start-Process "https://github.com/$Repo/settings/actions/runners/new?arch=x64&os=win"
$token = Read-Host "Paste the token here"

.\config.cmd --url "https://github.com/$Repo" --token $token --unattended --replace --name "home-$env:COMPUTERNAME" --runasservice

Write-Host "`n[4/4] Last step in the browser: create the variable HOME_RUNNER = true" -ForegroundColor Cyan
Start-Process "https://github.com/$Repo/settings/variables/actions/new"
Write-Host "`nDone. Restart the computer once (so the runner service sees Git), then it works by itself whenever the PC is on." -ForegroundColor Green
