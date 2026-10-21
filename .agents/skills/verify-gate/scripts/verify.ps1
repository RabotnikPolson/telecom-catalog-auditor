param(
    [switch]$Strict = $false
)

# Guard against PowerShell 7.3+ treating native stderr warnings as fatal errors
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$ErrorActionPreference = "Stop"
Write-Host "=== RUNNING VERIFICATION GATE ===" -ForegroundColor Cyan

Write-Host "`n[1/4] Running ruff (syntax, modern typing & imports via pyproject.toml)..." -ForegroundColor Yellow
python -m ruff check src/
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "`n[2/4] Running pytest (live combat tests via pyproject.toml)..." -ForegroundColor Yellow
python -m pytest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "`n[3/4] Running mypy (strict type safety via pyproject.toml)..." -ForegroundColor Yellow
python -m mypy
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "`n[4/4] Running CLI smoke test on a COPY of catalog_audit.db (photos-only, no LLM)..." -ForegroundColor Yellow
$SmokeDb = "scratch_smoke.db"
$SmokeFiles = @($SmokeDb, "$SmokeDb-wal", "$SmokeDb-shm")
try {
    if (Test-Path "catalog_audit.db") {
        # Consistent online copy via SQLite backup API (safe with WAL)
        python -c "import sqlite3; s=sqlite3.connect('catalog_audit.db'); d=sqlite3.connect('$SmokeDb'); s.backup(d); d.close(); s.close()"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    python main.py audit --db $SmokeDb --limit 1 --photos-only
    $SmokeExit = $LASTEXITCODE
}
finally {
    foreach ($f in $SmokeFiles) { if (Test-Path $f) { Remove-Item $f -Force } }
}
if ($SmokeExit -ne 0) { exit $SmokeExit }

Write-Host "`n>>> ALL VERIFICATION GATES PASSED <<<" -ForegroundColor Green
