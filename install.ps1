# Installs Handoff and registers it with the coding agents on this computer.
#   powershell -ExecutionPolicy ByPass -c "irm <url of this file> | iex"
# Settings (optional environment variables):
#   HANDOFF_SOURCE      where to install from (a package name, git URL or folder)
#   HANDOFF_LITE=1      skip search by meaning (smaller install, words-only search)
#   HANDOFF_NO_SETUP=1  install only; do not register with agents
$ErrorActionPreference = "Stop"

$source = if ($env:HANDOFF_SOURCE) { $env:HANDOFF_SOURCE } else { "git+https://github.com/temic137/handoff" }
$extra = if ($env:HANDOFF_LITE -eq "1") { "" } else { "[meaning]" }
if ($source -match '^(git\+|http|[A-Za-z]:\\|\\\\|\.|/)') {
    $package = "handoff$extra @ $source"
} else {
    $package = "$source$extra"
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv (a Python package installer from astral.sh)..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

Write-Host "Installing Handoff..."
uv tool install --force --python 3.13 $package
uv tool update-shell 2>$null | Out-Null
$bin = Join-Path (uv tool dir --bin) "handoff.exe"

if ($env:HANDOFF_NO_SETUP -ne "1") {
    Write-Host ""
    & $bin setup
}
Write-Host ""
Write-Host "Done. Open a project in any coding agent and ask: where did we stop?"
Write-Host "Open a new terminal to use the 'handoff' command."
