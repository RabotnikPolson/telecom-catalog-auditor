param(
    [switch]$Strict = $false
)

# Guard against PowerShell 7.3+ treating native stderr warnings as fatal errors
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$ErrorActionPreference = "Stop"
Write-Host "=== RUNNING VERIFICATION GATE ===" -ForegroundColor Cyan

Write-Host "`n[1/3] Running ruff (syntax & fatal errors)..." -ForegroundColor Yellow
python -m ruff check src/ --select F,E9
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "`n[2/3] Running pytest (live combat tests)..." -ForegroundColor Yellow
python -m pytest tests/ -v
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "`n[3/3] Running mypy (--strict)..." -ForegroundColor Yellow
python -m mypy src/ --strict
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "`n>>> ALL VERIFICATION GATES PASSED <<<" -ForegroundColor Green
