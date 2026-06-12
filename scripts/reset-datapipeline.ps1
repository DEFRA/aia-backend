# scripts/reset-datapipeline.ps1
#
# Truncate the three mutable data-pipeline tables, confirm they are empty,
# then run the data-pipeline to re-populate them from source_policy_docs.
#
# Usage (run from repo root or scripts/ folder):
#   .\scripts\reset-datapipeline.ps1
#
# Requirements:
#   * app/datapipeline/.env with DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
#     (plus SHAREPOINT_* and MODEL_ID for the pipeline run in Step 4)
#   * .venv built from requirements.txt (needs python-dotenv)
#   * psql.exe on PATH  OR  python psycopg2 available in .venv
#
# After the script completes:
#   * data_pipeline.policy_document_sync  - 0 rows
#   * data_pipeline.questions             - 0 rows
#   * data_pipeline.policy_documents      - 0 rows
#   * data_pipeline.source_policy_docs    - unchanged (seed data)
#   * policy_documents + questions        - re-populated by the pipeline run

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

# -- Resolve repo root ----------------------------------------------------------
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot   = Split-Path -Parent $ScriptDir
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

# -- Color helpers --------------------------------------------------------------
function ok($label, $detail = '') {
    Write-Host "  " -NoNewline
    Write-Host "[OK]" -ForegroundColor Green -NoNewline
    Write-Host ("  {0,-38} {1}" -f $label, $detail)
}
function fail($label, $detail = '') {
    Write-Host "  " -NoNewline
    Write-Host "[X]" -ForegroundColor Red -NoNewline
    Write-Host ("  {0,-38} {1}" -f $label, $detail)
}
function warn($label, $detail = '') {
    Write-Host "  " -NoNewline
    Write-Host "!" -ForegroundColor Yellow -NoNewline
    Write-Host ("  {0,-38} {1}" -f $label, $detail)
}
function banner($text) {
    Write-Host ""
    Write-Host $text -ForegroundColor White
    Write-Host ("-" * 58)
}

# -- Require .venv --------------------------------------------------------------
if (-not (Test-Path $VenvPython)) {
    Write-Host "ERROR: .venv not found at $VenvPython" -ForegroundColor Red
    Write-Host "  Build it:  python -m venv .venv && .venv\Scripts\pip install -r requirements.txt"
    exit 1
}

# -- Load .env via python-dotenv ------------------------------------------------
# Python handles special characters in passwords safely.
$EnvFile = Join-Path $RepoRoot "app\datapipeline\.env"
if (-not (Test-Path $EnvFile)) {
    Write-Host "ERROR: .env not found at $EnvFile" -ForegroundColor Red
    exit 1
}

$EnvVars = & $VenvPython -c @"
import json, sys
from dotenv import dotenv_values
vals = {k: v for k, v in dotenv_values(r'$EnvFile').items() if v is not None}
print(json.dumps(vals))
"@ 2>$null

if ($LASTEXITCODE -ne 0 -or -not $EnvVars) {
    Write-Host "ERROR: failed to load .env - is python-dotenv installed in .venv?" -ForegroundColor Red
    exit 1
}

$Parsed = $EnvVars | ConvertFrom-Json
foreach ($prop in $Parsed.PSObject.Properties) {
    Set-Item -Path "Env:\$($prop.Name)" -Value $prop.Value
}

# -- Resolve DB variables (with safe defaults) ----------------------------------

# Python SQL runner is written to a temp file to avoid multiline -c quoting issues.
$SqlRunnerPath = Join-Path $env:TEMP "aia_sql_runner.py"
$SqlRunnerCode = @'
import os
import sys
import psycopg2

sql = sys.stdin.read()
scalar = os.environ.get("AIA_SQL_SCALAR", "0") == "1"

try:
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )
    if not scalar:
        conn.autocommit = True

    cur = conn.cursor()
    cur.execute(sql)

    if scalar:
        row = cur.fetchone()
        print(0 if row is None else row[0])
    else:
        print("OK")

    conn.close()
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
'@
Set-Content -Path $SqlRunnerPath -Value $SqlRunnerCode -Encoding UTF8

# -- DB helper: run SQL via Python psycopg2 -------------------------------------
# Using Python avoids psql.exe dependency and handles special chars in passwords.
function Invoke-Sql {
    param(
        [string]$Sql,
        [switch]$ScalarInt   # return a single integer result
    )
    if ($ScalarInt) {
        $env:AIA_SQL_SCALAR = "1"
        $result = $Sql | & $VenvPython $SqlRunnerPath 2>&1
        Remove-Item Env:\AIA_SQL_SCALAR -ErrorAction SilentlyContinue
        if ($LASTEXITCODE -ne 0) { return -1 }
        return [int](($result -join "`n").Trim())
    } else {
        Remove-Item Env:\AIA_SQL_SCALAR -ErrorAction SilentlyContinue
        $output = $Sql | & $VenvPython $SqlRunnerPath 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Host $output -ForegroundColor Red
            return $false
        }
        return $true
    }
}

function Get-RowCount($Table) {
    return Invoke-Sql -Sql "SELECT COUNT(*) FROM $Table;" -ScalarInt
}

Set-Location $RepoRoot

# ──────────────────────────────────────────────────────────────────────────────
banner "Step 1 - verify PostgreSQL connection"

$connTest = Invoke-Sql -Sql "SELECT 1;" -ScalarInt
if ($connTest -ne 1) {
    fail "PostgreSQL" "cannot connect - check DB_* vars in .env"
    exit 1
}
ok "PostgreSQL" "$($env:DB_USER)@$($env:DB_HOST):$($env:DB_PORT)/$($env:DB_NAME)"

# ──────────────────────────────────────────────────────────────────────────────
banner "Step 2 - truncate mutable tables (CASCADE)"

$DbSchema = if ($env:DB_SCHEMA) { $env:DB_SCHEMA } else { "data_pipeline" }

$TruncateSql = @"
TRUNCATE TABLE
    $DbSchema.policy_document_sync,
    $DbSchema.questions,
    $DbSchema.policy_documents
CASCADE;
"@

$truncOk = Invoke-Sql -Sql $TruncateSql
if (-not $truncOk) {
    fail "Truncate failed" "see error above"
    exit 1
}
ok "Truncated" "policy_document_sync, questions, policy_documents"

# ──────────────────────────────────────────────────────────────────────────────
banner "Step 3 - confirm row counts"

$failCount = 0

function Assert-Empty($Table) {
    $n = Get-RowCount $Table
    if ($n -eq 0) {
        ok $Table "0 rows"
    } else {
        fail $Table "$n rows (expected 0)"
        $script:failCount++
    }
}

Assert-Empty "$DbSchema.policy_document_sync"
Assert-Empty "$DbSchema.questions"
Assert-Empty "$DbSchema.policy_documents"

$seedCount = Get-RowCount "$DbSchema.source_policy_docs"
if ($seedCount -gt 0) {
    ok "$DbSchema.source_policy_docs" "$seedCount rows (seed intact)"
} else {
    warn "$DbSchema.source_policy_docs" "0 rows - seed table is empty; pipeline will produce no output"
}

if ($failCount -gt 0) {
    Write-Host ""
    Write-Host "Truncate verification failed for $failCount table(s). Aborting pipeline run." -ForegroundColor Red
    exit 1
}

# ──────────────────────────────────────────────────────────────────────────────
banner "Step 4 - run data pipeline"

Write-Host ""
Write-Host "  `$ .venv\Scripts\python -m app.datapipeline.src.entrypoints.main"
Write-Host ""

# Temporarily relax error handling — Python logging writes to stderr, which
# PowerShell treats as errors under $ErrorActionPreference='Stop'.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$pipelineOutput = & $VenvPython -m app.datapipeline.src.entrypoints.main 2>&1
$pipelineExit = $LASTEXITCODE
$ErrorActionPreference = $prevEAP

# Pass all output through to the console and extract the PIPELINE_USAGE line.
$pipelineOutput | ForEach-Object { Write-Host $_ }
$usageLine = $pipelineOutput | Where-Object { $_ -match '^PIPELINE_USAGE: ' } | Select-Object -Last 1
$pipelineUsage = $null
if ($usageLine) {
    try {
        $pipelineUsage = ($usageLine -replace '^PIPELINE_USAGE: ', '') | ConvertFrom-Json
    } catch { $pipelineUsage = $null }
}

Write-Host ""
if ($pipelineExit -eq 0) {
    ok "Pipeline exited cleanly" "exit code 0"
} else {
    fail "Pipeline exited with errors" "exit code $pipelineExit"
}

# ──────────────────────────────────────────────────────────────────────────────
banner "Step 5 - final row counts"

$pdCount   = Get-RowCount "$DbSchema.policy_documents"
$qCount    = Get-RowCount "$DbSchema.questions"
$syncCount = Get-RowCount "$DbSchema.policy_document_sync"

if ($pdCount -gt 0) {
    ok "policy_documents" "$pdCount rows"
} else {
    warn "policy_documents" "0 rows - pipeline may not have produced output"
}

if ($qCount -gt 0) {
    ok "questions" "$qCount rows"
} else {
    warn "questions" "0 rows - pipeline may not have produced output"
}

ok "policy_document_sync" "$syncCount rows"
ok "source_policy_docs"   "$seedCount rows (unchanged)"

# -- Token usage summary -------------------------------------------------------
if ($pipelineUsage) {
    Write-Host ""
    Write-Host "  Token Usage Summary" -ForegroundColor White
    Write-Host ("  " + "-" * 44)
    Write-Host ("  {0,-22} {1,10}" -f "Input tokens",  ("{0:N0}" -f $pipelineUsage.total_input_tokens))
    Write-Host ("  {0,-22} {1,10}" -f "Output tokens", ("{0:N0}" -f $pipelineUsage.total_output_tokens))
    Write-Host ("  {0,-22} {1,10}" -f "Total tokens",  ("{0:N0}" -f $pipelineUsage.total_tokens))
    Write-Host ("  {0,-22} {1,10}" -f "Estimated cost", ("`$" + ("{0:F6}" -f $pipelineUsage.total_cost_usd)))
}

Write-Host ""
if ($pipelineExit -eq 0) {
    Write-Host "Reset complete." -ForegroundColor White
} else {
    Write-Host "Reset finished with pipeline errors. Check the output above." -ForegroundColor Yellow
    exit $pipelineExit
}
