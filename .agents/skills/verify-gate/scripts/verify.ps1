param(
    [switch]$Strict = $false
)

if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}
$ErrorActionPreference = "Stop"
if ($Strict) {
    Write-Warning "-Strict is deprecated and has no separate mode: mypy is already strict in pyproject.toml."
}

$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../../../.."))
$ScratchRoot = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot "scratch"))
$RunRoot = Join-Path $ScratchRoot ("verify-" + [guid]::NewGuid().ToString("N"))
$PytestReport = Join-Path $RunRoot "pytest.xml"
$SmokeDb = Join-Path $RunRoot "smoke.db"
$OwnedFiles = @($PytestReport, $SmokeDb, "$SmokeDb-wal", "$SmokeDb-shm", "$SmokeDb-journal")
$Failures = [System.Collections.Generic.List[string]]::new()
$Incomplete = [System.Collections.Generic.List[string]]::new()
$PreviousLocation = Get-Location
$RunCreated = $false

Write-Host "=== RUNNING VERIFICATION GATE ===" -ForegroundColor Cyan
try {
    if (-not (Test-Path -LiteralPath $ScratchRoot)) {
        New-Item -ItemType Directory -Path $ScratchRoot | Out-Null
    }
    if ((Get-Item -LiteralPath $ScratchRoot).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw "Verification scratch root must not be a reparse point."
    }
    New-Item -ItemType Directory -Path $RunRoot | Out-Null
    $RunCreated = $true
    Set-Location -LiteralPath $RepoRoot
    Write-Host "Run workspace: $RunRoot"

    Write-Host "`n[1/4] Running ruff (configuration: pyproject.toml)..." -ForegroundColor Yellow
    try {
        python -m ruff check src/
        if ($LASTEXITCODE -ne 0) { $Failures.Add("ruff exited $LASTEXITCODE") }
    } catch { $Failures.Add("ruff could not run: $($_.Exception.Message)") }

    Write-Host "`n[2/4] Running full pytest suite, including live tests..." -ForegroundColor Yellow
    try {
        python -m pytest -q --tb=short "--junitxml=$PytestReport"
        if ($LASTEXITCODE -ne 0) { $Failures.Add("pytest exited $LASTEXITCODE") }
    } catch { $Failures.Add("pytest could not run: $($_.Exception.Message)") }

    try {
        if (-not (Test-Path -LiteralPath $PytestReport -PathType Leaf)) {
            $Incomplete.Add("pytest report missing; live results and skips could not be verified")
        } else {
            [xml]$Report = Get-Content -LiteralPath $PytestReport -Raw
            $Cases = @($Report.SelectNodes("//testcase"))
            $Skipped = @($Cases | Where-Object { $_.SelectSingleNode("skipped") })
            Write-Host "Pytest report: $($Cases.Count) cases, $($Skipped.Count) skipped."
            foreach ($Case in $Skipped) {
                Write-Warning "SKIPPED: $($Case.classname)::$($Case.name): $($Case.skipped.message)"
            }
            if ($Skipped.Count -gt 0) { $Incomplete.Add("pytest skipped $($Skipped.Count) test(s); see reasons above") }

            $ExpectedLive = @("test_mysql_reader_live_connection", "test_mysql_reader_fetch_by_sku_and_specs", "test_mysql_reader_streaming")
            $LivePassed = 0
            $LiveSkipped = 0
            $LiveFailed = 0
            $LiveMissing = 0
            foreach ($Expected in $ExpectedLive) {
                $Matches = @($Cases | Where-Object {
                    $_.classname -match '(^|\.)test_mysql_reader$' -and
                    ($_.name -eq $Expected -or $_.name.StartsWith("$Expected["))
                })
                if ($Matches.Count -eq 0) { $LiveMissing++; continue }
                foreach ($Case in $Matches) {
                    if ($Case.SelectSingleNode("skipped")) { $LiveSkipped++ }
                    elseif ($Case.SelectSingleNode("failure") -or $Case.SelectSingleNode("error")) { $LiveFailed++ }
                    else { $LivePassed++ }
                }
            }
            Write-Host "Live MySQL: passed=$LivePassed skipped=$LiveSkipped failed=$LiveFailed missing=$LiveMissing"
            if ($LiveFailed -gt 0) { $Failures.Add("live MySQL: $LiveFailed failed") }
            if ($LiveSkipped -gt 0 -or $LiveMissing -gt 0) {
                $Incomplete.Add("live MySQL incomplete: $LiveSkipped skipped, $LiveMissing required test(s) missing")
            }
        }
    } catch { $Incomplete.Add("pytest report unreadable; live verification incomplete: $($_.Exception.Message)") }

    Write-Host "`n[3/4] Running mypy (already strict via pyproject.toml)..." -ForegroundColor Yellow
    try {
        python -m mypy
        if ($LASTEXITCODE -ne 0) { $Failures.Add("mypy exited $LASTEXITCODE") }
    } catch { $Failures.Add("mypy could not run: $($_.Exception.Message)") }

    Write-Host "`n[4/4] Running CLI smoke on a private SQLite copy (photos-only, no LLM)..." -ForegroundColor Yellow
    $SourceDb = Join-Path $RepoRoot "catalog_audit.db"
    if (-not (Test-Path -LiteralPath $SourceDb -PathType Leaf)) {
        Write-Warning "SKIPPED CLI smoke: source catalog_audit.db is absent; no empty replacement will be created."
        $Incomplete.Add("CLI smoke skipped: source catalog_audit.db absent")
    } else {
        try {
            # Include committed WAL data while keeping the source connection read-only.
            $BackupCode = @'
import sqlite3, sys
from pathlib import Path
source = sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + '?mode=ro', uri=True)
try:
    destination = sqlite3.connect(sys.argv[2])
    try:
        source.backup(destination)
    finally:
        destination.close()
finally:
    source.close()
'@
            python -c $BackupCode $SourceDb $SmokeDb
            if ($LASTEXITCODE -ne 0) {
                $Failures.Add("SQLite backup exited $LASTEXITCODE; CLI smoke could not run")
            } else {
                python main.py audit --db $SmokeDb --limit 1 --photos-only
                if ($LASTEXITCODE -ne 0) { $Failures.Add("CLI smoke exited $LASTEXITCODE") }
            }
        } catch { $Failures.Add("CLI smoke could not run: $($_.Exception.Message)") }
    }
} catch {
    $Failures.Add("Verification setup failed: $($_.Exception.Message)")
} finally {
    Set-Location -LiteralPath $PreviousLocation.Path
    if ($RunCreated) {
        try {
            $ResolvedRun = (Resolve-Path -LiteralPath $RunRoot).ProviderPath
            $ExpectedRun = [System.IO.Path]::GetFullPath($RunRoot)
            $AllowedPrefix = $ScratchRoot.TrimEnd('\') + '\'
            if ($ResolvedRun -ne $ExpectedRun -or -not $ResolvedRun.StartsWith($AllowedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Cleanup refused: run directory is outside the intended scratch root."
            }
            if ((Get-Item -LiteralPath $RunRoot).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                throw "Cleanup refused: run directory is a reparse point."
            }
            foreach ($OwnedFile in $OwnedFiles) {
                if (-not (Test-Path -LiteralPath $OwnedFile)) { continue }
                $ResolvedFile = (Resolve-Path -LiteralPath $OwnedFile).ProviderPath
                $Item = Get-Item -LiteralPath $OwnedFile -Force
                if ([System.IO.Path]::GetDirectoryName($ResolvedFile) -ne $ResolvedRun -or $Item.PSIsContainer -or
                    ($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
                    throw "Cleanup refused: unexpected owned-file path."
                }
                Remove-Item -LiteralPath $ResolvedFile -Force
            }
            if (@(Get-ChildItem -LiteralPath $ResolvedRun -Force).Count -eq 0) {
                Remove-Item -LiteralPath $ResolvedRun
            } else {
                Write-Warning "Run directory retained because it contains files not created by this gate: $ResolvedRun"
            }
        } catch { $Failures.Add("Cleanup failed: $($_.Exception.Message)") }
    }
}

foreach ($Failure in $Failures) { Write-Host "FAILED: $Failure" -ForegroundColor Red }
foreach ($Reason in $Incomplete) { Write-Warning "INCOMPLETE: $Reason" }
if ($Failures.Count -gt 0) { exit 1 }
if ($Incomplete.Count -gt 0) { exit 2 }
Write-Host "`n>>> ALL VERIFICATION GATES PASSED (INCLUDING LIVE MYSQL) <<<" -ForegroundColor Green
exit 0
