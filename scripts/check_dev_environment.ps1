[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Stop-Check {
    param([int]$Code, [string]$Message)
    Write-Host ("ERROR [{0}]: {1}" -f $Code, $Message) -ForegroundColor Red
    exit $Code
}

function Write-Step {
    param([string]$Message)
    Write-Host ("== {0} ==" -f $Message) -ForegroundColor Cyan
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$currentDirectory = (Get-Location).Path
if (-not [string]::Equals(
        [IO.Path]::GetFullPath($currentDirectory).TrimEnd("\"),
        [IO.Path]::GetFullPath($repoRoot).TrimEnd("\"),
        [StringComparison]::OrdinalIgnoreCase
    )) {
    Stop-Check 2 "Run this script from the repository root: .\scripts\check_dev_environment.ps1"
}

Write-Step "Git"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Stop-Check 10 "Git is not available on PATH."
}
$gitVersion = @(& git --version 2>&1)
if ($LASTEXITCODE -ne 0) { Stop-Check 10 "Git version check failed." }
Write-Host ($gitVersion -join [Environment]::NewLine)

$insideWorkTree = @(& git rev-parse --is-inside-work-tree 2>&1)
if ($LASTEXITCODE -ne 0 -or ($insideWorkTree -join "").Trim() -ne "true") {
    Stop-Check 11 "The current directory is not a valid Git work tree."
}
$resolvedRoot = @(& git rev-parse --show-toplevel 2>&1)
if ($LASTEXITCODE -ne 0) { Stop-Check 11 "Git could not resolve the repository root." }
Write-Host ("Repository: {0}" -f (($resolvedRoot -join "").Trim()))

$branch = @(& git branch --show-current 2>&1)
if ($LASTEXITCODE -ne 0) { Stop-Check 12 "Git could not determine the current branch." }
$branchName = ($branch -join "").Trim()
if ([string]::IsNullOrWhiteSpace($branchName)) { $branchName = "(detached HEAD)" }
Write-Host ("Branch: {0}" -f $branchName)

$statusLines = @(& git status --short --branch 2>&1)
if ($LASTEXITCODE -ne 0) { Stop-Check 12 "Git could not read the working-tree status." }
Write-Host "Status:"
foreach ($line in $statusLines) {
    if ($line -match "(?i)(\.env|cookie|token|secret|credential|password|\.pem|\.key)") {
        Write-Host "  [sensitive path redacted]"
    } else {
        Write-Host ("  {0}" -f $line)
    }
}

Write-Step "Sensitive environment-variable presence"
foreach ($name in @(
    "NETEASE_COOKIE",
    "MCP_ACCESS_TOKEN",
    "MCP_OAUTH_PASSWORD",
    "MCP_STORAGE_PATH",
    "MCP_PUBLIC_URL"
)) {
    $value = [Environment]::GetEnvironmentVariable($name, "Process")
    $state = if ([string]::IsNullOrEmpty($value)) { "not set" } else { "set" }
    Write-Host ("{0}: {1}" -f $name, $state)
}

Write-Step "Python virtual environment"
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Stop-Check 20 "The .venv Python interpreter does not exist."
}
$pythonVersion = @(& $python --version 2>&1)
if ($LASTEXITCODE -ne 0) { Stop-Check 21 "The .venv Python interpreter could not start." }
Write-Host ($pythonVersion -join [Environment]::NewLine)

Write-Step "pip dependency consistency"
$pipCheck = @(& $python -m pip check 2>&1)
if ($LASTEXITCODE -ne 0) {
    Stop-Check 22 "pip check reported incompatible or missing packages."
}
Write-Host ($pipCheck -join [Environment]::NewLine)

Write-Step "requirements.txt imports"
$requirementsPath = Join-Path $repoRoot "requirements.txt"
if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) {
    Stop-Check 23 "requirements.txt does not exist."
}
$distributionNames = @(
    Get-Content -LiteralPath $requirementsPath |
        ForEach-Object {
            $line = ($_ -split "#", 2)[0].Trim()
            if ($line -and -not $line.StartsWith("-")) {
                $match = [regex]::Match($line, "^[A-Za-z0-9_.-]+")
                if ($match.Success) { $match.Value }
            }
        } |
        Sort-Object -Unique
)
$importCode = @"
import importlib.metadata
import importlib.util
import sys
name = sys.argv[1]
mapping = importlib.metadata.packages_distributions()
modules = sorted(m for m, ds in mapping.items() if any(d.lower() == name.lower() for d in ds))
if not modules or any(importlib.util.find_spec(m) is None for m in modules):
    print(f"{name}: not importable")
    raise SystemExit(1)
print(f"{name}: importable")
"@
foreach ($distribution in $distributionNames) {
    $importResult = @(& $python -c $importCode $distribution 2>&1)
    if ($LASTEXITCODE -ne 0) {
        Stop-Check 24 ("A declared dependency is not importable: {0}" -f $distribution)
    }
    Write-Host ($importResult -join [Environment]::NewLine)
}

Write-Step "unittest"
$testOutput = @(& $python -m unittest discover -s tests -v 2>&1)
$testExitCode = $LASTEXITCODE
$testSummary = @(
    $testOutput |
        Where-Object { $_ -cmatch "^(Ran [0-9]+ tests.*|OK|FAILED.*)$" }
)
if ($testSummary.Count -gt 0) { Write-Host ($testSummary -join [Environment]::NewLine) }
if ($testExitCode -ne 0) {
    Stop-Check 30 "The unittest suite failed. Sensitive test output was not echoed."
}

Write-Host "Environment check passed." -ForegroundColor Green
exit 0
