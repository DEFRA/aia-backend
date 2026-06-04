# scripts/cleanup-orphan-s3-files.ps1
#
# Multi-mode S3 / DB cleanup utility. S3 keys follow the format:
#     {S3_UPLOAD_PREFIX}{doc_id}_{file_name}
# where {doc_id} is a 36-character UUID and {file_name} is the original
# filename. The portion before the FIRST underscore (after the prefix) is
# treated as the doc_id.
#
# Modes (mutually exclusive - pick one or none):
#
#   (default)            Delete S3 files whose doc_id is NOT present in
#                        backend.document_uploads. Safe orphan cleanup.
#
#   -DeleteAllS3         Delete EVERY object under the configured S3 prefix.
#                        Does not touch the DB. Requires -Force (unless -DryRun).
#
#   -DeleteAllDb         Delete EVERY row in backend.document_uploads
#                        (cascades to backend.cost_usage). Does not touch S3.
#                        Requires -Force (unless -DryRun).
#
#   -DeleteByTimeline    Filter S3 objects by their LastModified (S3 upload
#       -Before <dt>     time), then delete BOTH the matching S3 keys AND the
#       -After  <dt>     corresponding backend.document_uploads rows (doc_id
#                        parsed from the key). At least one of -Before / -After
#                        is required.
#
# Usage:
#   Edit the variables in the CONFIG block below, then run:
#       .\scripts\cleanup-orphan-s3-files.ps1                                    # orphan cleanup
#       .\scripts\cleanup-orphan-s3-files.ps1 -DryRun                            # orphan preview
#       .\scripts\cleanup-orphan-s3-files.ps1 -DeleteAllS3 -DryRun
#       .\scripts\cleanup-orphan-s3-files.ps1 -DeleteAllS3 -Force
#       .\scripts\cleanup-orphan-s3-files.ps1 -DeleteAllDb -DryRun
#       .\scripts\cleanup-orphan-s3-files.ps1 -DeleteAllDb -Force
#       .\scripts\cleanup-orphan-s3-files.ps1 -DeleteByTimeline -Before '2026-01-01' -DryRun
#       .\scripts\cleanup-orphan-s3-files.ps1 -DeleteByTimeline -After  '2025-12-01' -Before '2026-01-01'
#
# Requirements:
#   * .venv built from requirements.txt (needs psycopg2 + python-dotenv)
#   * aws.exe (AWS CLI v2) on PATH

param(
    [switch]$DryRun,
    # Refuse to delete if more than this fraction of inspected files would be
    # removed - guards against a misconfigured comparison wiping the bucket.
    # Override with -Force to bypass. Also required to actually run the
    # -DeleteAllS3 and -DeleteAllDb modes (they would not otherwise run
    # outside -DryRun, since they are total wipes).
    [double]$MaxDeleteFraction = 0.5,
    [switch]$Force,

    # ── Alternative modes (mutually exclusive). If none are set, the script
    # performs the default orphan-cleanup (delete S3 files whose doc_id is not
    # in backend.document_uploads).
    [switch]$DeleteAllS3,
    [switch]$DeleteAllDb,
    [switch]$DeleteByTimeline,

    # Timeline range for -DeleteByTimeline. At least one of -Before / -After
    # must be supplied. Filters on the S3 object's LastModified (its upload
    # time as recorded by S3), NOT on backend.document_uploads.uploaded_ts.
    # Datetimes without an explicit zone are interpreted as local time and
    # converted to UTC before comparison.
    [Nullable[datetime]]$Before,
    [Nullable[datetime]]$After
)

# Mutual-exclusion check for the alternative modes.
$_SelectedModes = @()
if ($DeleteAllS3)      { $_SelectedModes += '-DeleteAllS3' }
if ($DeleteAllDb)      { $_SelectedModes += '-DeleteAllDb' }
if ($DeleteByTimeline) { $_SelectedModes += '-DeleteByTimeline' }
if ($_SelectedModes.Count -gt 1) {
    Write-Host "ERROR: the following switches are mutually exclusive:" -ForegroundColor Red
    Write-Host ("  " + ($_SelectedModes -join ', ')) -ForegroundColor Red
    exit 1
}
if ($DeleteByTimeline -and -not ($PSBoundParameters.ContainsKey('Before') -or $PSBoundParameters.ContainsKey('After'))) {
    Write-Host "ERROR: -DeleteByTimeline requires -Before and/or -After." -ForegroundColor Red
    Write-Host "  Example: -DeleteByTimeline -Before '2026-01-01' -After '2025-12-01'" -ForegroundColor Yellow
    exit 1
}

# ─── CONFIG ───────────────────────────────────────────────────────────────────
# Fill in the values below before running the script.
$DB_HOST     = ''
$DB_USER     = ''
$DB_PORT     = '5432'
$DB_PASSWORD = ''
$DB_NAME     = ''

$AWS_DEFAULT_REGION    = 'eu-west-2'
$AWS_ACCESS_KEY_ID     = ''
$AWS_SECRET_ACCESS_KEY = ''
$AWS_SESSION_TOKEN     = ''

$S3_BUCKET_NAME   = ''
$S3_UPLOAD_PREFIX = 'uploaded_docs'
# ──────────────────────────────────────────────────────────────────────────────

# Validate that required values were set
$RequiredVars = @{
    'DB_HOST'               = $DB_HOST
    'DB_USER'               = $DB_USER
    'DB_PORT'               = $DB_PORT
    'DB_PASSWORD'           = $DB_PASSWORD
    'DB_NAME'               = $DB_NAME
    'AWS_DEFAULT_REGION'    = $AWS_DEFAULT_REGION
    'AWS_ACCESS_KEY_ID'     = $AWS_ACCESS_KEY_ID
    'AWS_SECRET_ACCESS_KEY' = $AWS_SECRET_ACCESS_KEY
    'AWS_SESSION_TOKEN'     = $AWS_SESSION_TOKEN
    'S3_BUCKET_NAME'        = $S3_BUCKET_NAME
}
$Missing = @($RequiredVars.GetEnumerator() | Where-Object { [string]::IsNullOrWhiteSpace($_.Value) } | ForEach-Object { $_.Key })
if ($Missing.Count -gt 0) {
    Write-Host "ERROR: the following CONFIG variables are empty:" -ForegroundColor Red
    foreach ($name in $Missing) { Write-Host "  - $name" -ForegroundColor Red }
    Write-Host "Edit the CONFIG block at the top of this script." -ForegroundColor Red
    exit 1
}

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
    Write-Host "  Build it:  python -m venv .venv ; .venv\Scripts\pip install -r requirements.txt"
    exit 1
}

# -- Require AWS CLI ------------------------------------------------------------
$awsCmd = Get-Command aws -ErrorAction SilentlyContinue
if (-not $awsCmd) {
    Write-Host "ERROR: aws CLI not found on PATH" -ForegroundColor Red
    exit 1
}

# Force UTF-8 console I/O so PowerShell reads python/aws stdout cleanly
# instead of the legacy OEM code page (which can mangle bytes silently).
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding           = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING     = 'utf-8'

# -- Export AWS credentials to environment --------------------------------------
$env:AWS_DEFAULT_REGION    = $AWS_DEFAULT_REGION
$env:AWS_ACCESS_KEY_ID     = $AWS_ACCESS_KEY_ID
$env:AWS_SECRET_ACCESS_KEY = $AWS_SECRET_ACCESS_KEY
$env:AWS_SESSION_TOKEN     = $AWS_SESSION_TOKEN

# -- Export DB credentials to environment for the python runner ----------------
$env:DB_HOST     = $DB_HOST
$env:DB_USER     = $DB_USER
$env:DB_PORT     = $DB_PORT
$env:DB_PASSWORD = $DB_PASSWORD
$env:DB_NAME     = $DB_NAME

# -- Python helper: fetch (doc_id, file_name) pairs from backend.document_uploads
# Emits one line per row in the exact S3 filename form: "{doc_id}_{file_name}".
$DocIdFetcherPath = Join-Path $env:TEMP "aia_doc_id_fetcher.py"
$DocIdFetcherCode = @'
import os
import sys
import psycopg2

try:
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )
    cur = conn.cursor()
    cur.execute("SELECT doc_id::text, file_name FROM backend.document_uploads;")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for doc_id, file_name in cur.fetchall():
        # Match the exact S3 key form: "{doc_id}_{file_name}".
        # Trim file_name to defend against trailing whitespace in the column.
        fn = (file_name or "").strip()
        print(f"{doc_id}_{fn}")
    conn.close()
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
'@
Set-Content -Path $DocIdFetcherPath -Value $DocIdFetcherCode -Encoding UTF8

# ══════════════════════════════════════════════════════════════════════════════
# Shared helpers for the alternative modes (-DeleteAllS3 / -DeleteAllDb /
# -DeleteByTimeline). The default orphan-cleanup path below does NOT use these
# (it has its own inline batching) - intentionally kept separate to leave the
# existing, validated orphan logic untouched.
# ══════════════════════════════════════════════════════════════════════════════

# Delete an arbitrary list of S3 keys in batches of 1000 (S3 delete-objects
# limit). Returns a hashtable with Deleted/Errored counts.
function Invoke-S3DeleteKeys {
    param(
        [Parameter(Mandatory = $true)][string[]]$Keys,
        [Parameter(Mandatory = $true)][string]$Bucket
    )
    $BatchSize = 1000
    $Deleted   = 0
    $Errored   = 0

    for ($i = 0; $i -lt $Keys.Count; $i += $BatchSize) {
        $end   = [Math]::Min($i + $BatchSize - 1, $Keys.Count - 1)
        $batch = $Keys[$i..$end]

        $payload = @{
            Objects = @($batch | ForEach-Object { @{ Key = $_ } })
            Quiet   = $true
        } | ConvertTo-Json -Depth 4 -Compress

        $tmpFile       = New-TemporaryFile
        $delStderrFile = New-TemporaryFile
        try {
            [System.IO.File]::WriteAllText(
                $tmpFile,
                $payload,
                (New-Object System.Text.UTF8Encoding($false))
            )

            $prevEAP = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            $delOutput = & aws s3api delete-objects `
                --bucket $Bucket `
                --delete "file://$tmpFile" `
                --output json 2>$delStderrFile
            $delExit = $LASTEXITCODE
            $ErrorActionPreference = $prevEAP

            $delStderr = ''
            if (Test-Path $delStderrFile) {
                $delStderr = (Get-Content $delStderrFile -Raw -ErrorAction SilentlyContinue)
            }

            if ($delExit -ne 0) {
                fail "Batch $i..$end" "delete-objects failed (exit $delExit)"
                if ($delStderr) { Write-Host $delStderr -ForegroundColor Red }
                $Errored += $batch.Count
                continue
            }

            $delJoined   = ($delOutput | Out-String).Trim()
            $batchErrors = 0
            if ($delJoined) {
                try {
                    $delParsed = $delJoined | ConvertFrom-Json
                    if ($delParsed.PSObject.Properties.Name -contains 'Errors' -and $delParsed.Errors) {
                        $batchErrors = @($delParsed.Errors).Count
                        foreach ($err in $delParsed.Errors) {
                            warn "Delete error" "$($err.Key): $($err.Code) - $($err.Message)"
                        }
                    }
                } catch { }
            }

            $Deleted += ($batch.Count - $batchErrors)
            $Errored += $batchErrors
            ok "Batch deleted" "$($batch.Count - $batchErrors)/$($batch.Count) (keys $i..$end)"
        }
        finally {
            Remove-Item -Path $tmpFile       -Force -ErrorAction SilentlyContinue
            Remove-Item -Path $delStderrFile -Force -ErrorAction SilentlyContinue
        }
    }
    return @{ Deleted = $Deleted; Errored = $Errored }
}

# Run a small inline Python snippet against the configured DB. Returns the
# captured stdout lines (array). Aborts the script on non-zero exit.
function Invoke-DbPython {
    param(
        [Parameter(Mandatory = $true)][string]$Code,
        [string]$StdinText = $null,
        [string]$Label = 'DB'
    )
    $scriptPath = Join-Path $env:TEMP ("aia_db_{0}.py" -f ([guid]::NewGuid().ToString('N')))
    Set-Content -Path $scriptPath -Value $Code -Encoding UTF8

    $stderrFile = New-TemporaryFile
    try {
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        if ($null -ne $StdinText) {
            $stdinFile = New-TemporaryFile
            try {
                [System.IO.File]::WriteAllText(
                    $stdinFile,
                    $StdinText,
                    (New-Object System.Text.UTF8Encoding($false))
                )
                $output = Get-Content -Raw $stdinFile | & $VenvPython $scriptPath 2>$stderrFile
            } finally {
                Remove-Item -Path $stdinFile -Force -ErrorAction SilentlyContinue
            }
        } else {
            $output = & $VenvPython $scriptPath 2>$stderrFile
        }
        $exit = $LASTEXITCODE
        $ErrorActionPreference = $prevEAP

        $stderr = ''
        if (Test-Path $stderrFile) {
            $stderr = (Get-Content $stderrFile -Raw -ErrorAction SilentlyContinue)
        }
        if ($exit -ne 0) {
            fail $Label "python helper failed (exit $exit)"
            if ($stderr) { Write-Host $stderr -ForegroundColor Red }
            exit 1
        }
        return @($output | ForEach-Object {
            $s = [string]$_
            if ($s.Length -gt 0 -and $s[0] -eq [char]0xFEFF) { $s = $s.Substring(1) }
            $s
        })
    } finally {
        Remove-Item -Path $scriptPath -Force -ErrorAction SilentlyContinue
        Remove-Item -Path $stderrFile -Force -ErrorAction SilentlyContinue
    }
}

# List every object under the configured S3 prefix. Returns an array of objects
# with Key and LastModified (DateTime, UTC). AWS CLI v2 auto-paginates.
function Get-S3ObjectsUnderPrefix {
    param(
        [Parameter(Mandatory = $true)][string]$Bucket,
        [Parameter(Mandatory = $true)][string]$Prefix
    )
    $stderrFile = New-TemporaryFile
    try {
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $listOutput = & aws s3api list-objects-v2 `
            --bucket $Bucket `
            --prefix $Prefix `
            --output json 2>$stderrFile
        $listExit = $LASTEXITCODE
        $ErrorActionPreference = $prevEAP

        $listStderr = ''
        if (Test-Path $stderrFile) {
            $listStderr = (Get-Content $stderrFile -Raw -ErrorAction SilentlyContinue)
        }
        if ($listExit -ne 0) {
            fail "S3 list" "list-objects-v2 failed (exit $listExit)"
            if ($listStderr) { Write-Host $listStderr -ForegroundColor Red }
            exit 1
        }

        $joined = ($listOutput | Out-String)
        if (-not $joined.Trim()) { return @() }
        $parsed = $joined | ConvertFrom-Json
        if (-not ($parsed.PSObject.Properties.Name -contains 'Contents') -or -not $parsed.Contents) {
            return @()
        }
        return @($parsed.Contents | ForEach-Object {
            [pscustomobject]@{
                Key          = $_.Key
                LastModified = [datetime]$_.LastModified
            }
        })
    } finally {
        Remove-Item -Path $stderrFile -Force -ErrorAction SilentlyContinue
    }
}

# Normalize a prefix to always end with '/' (unless empty), to match keys.
function ConvertTo-NormalizedPrefix {
    param([string]$P)
    if ($P -and -not $P.EndsWith('/')) { return $P + '/' }
    return $P
}

# ══════════════════════════════════════════════════════════════════════════════
# Mode: -DeleteAllS3
# Delete every object under the configured S3 prefix. Does NOT touch the DB.
# Requires -Force (unless -DryRun).
# ══════════════════════════════════════════════════════════════════════════════
function Invoke-DeleteAllS3Mode {
    banner "MODE: -DeleteAllS3 - wipe s3://$S3_BUCKET_NAME/$S3_UPLOAD_PREFIX"

    if (-not $DryRun -and -not $Force) {
        fail "ABORT" "-DeleteAllS3 requires -Force (or use -DryRun first)"
        Write-Host "  This mode deletes EVERY object under the prefix unconditionally." -ForegroundColor Yellow
        Write-Host "  Re-run with -DryRun to preview, or add -Force to proceed."        -ForegroundColor Yellow
        exit 1
    }

    $objects = Get-S3ObjectsUnderPrefix -Bucket $S3_BUCKET_NAME -Prefix $S3_UPLOAD_PREFIX
    $keys = @(
        $objects |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_.Key) -and -not $_.Key.EndsWith('/') } |
            ForEach-Object { $_.Key }
    )
    ok "S3 objects found" "$($keys.Count) keys under prefix"
    if ($keys.Count -eq 0) {
        Write-Host ""
        Write-Host "Nothing to delete." -ForegroundColor White
        return
    }

    if ($DryRun) {
        Write-Host ""
        Write-Host "  DRY RUN - the following keys WOULD be deleted:" -ForegroundColor Yellow
        $preview = @($keys | Select-Object -First 20)
        foreach ($k in $preview) { Write-Host "    KEY: $k" -ForegroundColor DarkGray }
        if ($keys.Count -gt 20) {
            Write-Host "    ... ($($keys.Count - 20) more)" -ForegroundColor DarkGray
        }
        Write-Host ""
        Write-Host "Re-run without -DryRun (and with -Force) to actually delete." -ForegroundColor Yellow
        return
    }

    banner "Deleting $($keys.Count) S3 objects"
    $r = Invoke-S3DeleteKeys -Keys $keys -Bucket $S3_BUCKET_NAME
    Write-Host ""
    ok "Total deleted" "$($r.Deleted) keys"
    if ($r.Errored -gt 0) {
        fail "Errors" "$($r.Errored) keys failed to delete"
        exit 1
    }
    Write-Host ""
    Write-Host "S3 wipe complete." -ForegroundColor White
}

# ══════════════════════════════════════════════════════════════════════════════
# Mode: -DeleteAllDb
# Delete every row in backend.document_uploads. Cascades to backend.cost_usage
# via ON DELETE CASCADE. Does NOT touch S3. Requires -Force (unless -DryRun).
# ══════════════════════════════════════════════════════════════════════════════
function Invoke-DeleteAllDbMode {
    banner "MODE: -DeleteAllDb - wipe backend.document_uploads (cascades to cost_usage)"

    if (-not $DryRun -and -not $Force) {
        fail "ABORT" "-DeleteAllDb requires -Force (or use -DryRun first)"
        Write-Host "  This mode deletes EVERY row in backend.document_uploads." -ForegroundColor Yellow
        Write-Host "  backend.cost_usage rows are removed via ON DELETE CASCADE." -ForegroundColor Yellow
        Write-Host "  Re-run with -DryRun to preview, or add -Force to proceed."  -ForegroundColor Yellow
        exit 1
    }

    $countCode = @'
import os, sys, psycopg2
conn = psycopg2.connect(host=os.environ["DB_HOST"], port=int(os.environ["DB_PORT"]),
                       dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
                       password=os.environ["DB_PASSWORD"])
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM backend.document_uploads;")
print(cur.fetchone()[0])
conn.close()
'@
    $countOut = Invoke-DbPython -Code $countCode -Label 'PostgreSQL count'
    $rowCount = [int]($countOut | Select-Object -First 1)
    ok "Rows in document_uploads" "$rowCount"

    if ($rowCount -eq 0) {
        Write-Host ""
        Write-Host "Nothing to delete." -ForegroundColor White
        return
    }

    if ($DryRun) {
        $sampleCode = @'
import os, sys, psycopg2
conn = psycopg2.connect(host=os.environ["DB_HOST"], port=int(os.environ["DB_PORT"]),
                       dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
                       password=os.environ["DB_PASSWORD"])
cur = conn.cursor()
cur.execute("SELECT doc_id::text, file_name, uploaded_ts::text FROM backend.document_uploads ORDER BY uploaded_ts DESC LIMIT 20;")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
for did, fn, ts in cur.fetchall():
    print(f"{ts}\t{did}\t{fn}")
conn.close()
'@
        $sample = Invoke-DbPython -Code $sampleCode -Label 'PostgreSQL sample'
        Write-Host ""
        Write-Host "  DRY RUN - the following rows WOULD be deleted (first 20 by uploaded_ts DESC):" -ForegroundColor Yellow
        foreach ($r in $sample) { Write-Host "    ROW: $r" -ForegroundColor DarkGray }
        if ($rowCount -gt 20) {
            Write-Host "    ... ($($rowCount - 20) more rows)" -ForegroundColor DarkGray
        }
        Write-Host ""
        Write-Host "Re-run without -DryRun (and with -Force) to actually delete." -ForegroundColor Yellow
        return
    }

    $deleteCode = @'
import os, sys, psycopg2
conn = psycopg2.connect(host=os.environ["DB_HOST"], port=int(os.environ["DB_PORT"]),
                       dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
                       password=os.environ["DB_PASSWORD"])
cur = conn.cursor()
cur.execute("DELETE FROM backend.document_uploads;")
print(cur.rowcount)
conn.commit()
conn.close()
'@
    $delOut = Invoke-DbPython -Code $deleteCode -Label 'PostgreSQL delete-all'
    $deleted = [int]($delOut | Select-Object -First 1)
    Write-Host ""
    ok "Rows deleted" "$deleted (cost_usage rows cascaded)"
    Write-Host ""
    Write-Host "DB wipe complete." -ForegroundColor White
}

# ══════════════════════════════════════════════════════════════════════════════
# Mode: -DeleteByTimeline
# Filter S3 objects by their LastModified (the S3 upload time), then delete
# both the S3 objects AND the matching backend.document_uploads rows
# (doc_id parsed from the S3 key: "{prefix}{doc_id}_{file_name}").
#
#   -After  <datetime>   keep only keys with LastModified >= After  (UTC)
#   -Before <datetime>   keep only keys with LastModified <  Before (UTC)
# ══════════════════════════════════════════════════════════════════════════════
function Invoke-DeleteByTimelineMode {
    $afterUtc  = $null
    $beforeUtc = $null
    if ($null -ne $After)  { $afterUtc  = $After.ToUniversalTime() }
    if ($null -ne $Before) { $beforeUtc = $Before.ToUniversalTime() }

    $windowText = @()
    if ($afterUtc)  { $windowText += "LastModified >= $($afterUtc.ToString('yyyy-MM-ddTHH:mm:ssZ'))" }
    if ($beforeUtc) { $windowText += "LastModified <  $($beforeUtc.ToString('yyyy-MM-ddTHH:mm:ssZ'))" }
    banner ("MODE: -DeleteByTimeline - filter on S3 upload time ({0})" -f ($windowText -join ' AND '))

    $objects = Get-S3ObjectsUnderPrefix -Bucket $S3_BUCKET_NAME -Prefix $S3_UPLOAD_PREFIX
    ok "S3 objects listed" "$($objects.Count) keys under prefix"

    $prefix = ConvertTo-NormalizedPrefix -P $S3_UPLOAD_PREFIX

    # Filter by LastModified window AND ignore folder-placeholder keys.
    $matched = @(
        $objects | Where-Object {
            $k = $_.Key
            if ([string]::IsNullOrWhiteSpace($k) -or $k.EndsWith('/')) { return $false }
            $lm = $_.LastModified.ToUniversalTime()
            if ($afterUtc  -and $lm -lt $afterUtc)  { return $false }
            if ($beforeUtc -and $lm -ge $beforeUtc) { return $false }
            return $true
        }
    )
    ok "Matched window" "$($matched.Count) keys"

    if ($matched.Count -eq 0) {
        Write-Host ""
        Write-Host "Nothing matches the timeline window - nothing to delete." -ForegroundColor White
        return
    }

    # Build the list of doc_ids parsed from key tails (post-prefix, before first '_').
    $docIds = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($obj in $matched) {
        $rel = $obj.Key
        if ($prefix -and $rel.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            $rel = $rel.Substring($prefix.Length)
        }
        $u = $rel.IndexOf('_')
        if ($u -gt 0) {
            $did = $rel.Substring(0, $u)
            # A doc_id is a 36-char UUID; ignore anything else to avoid wiping
            # unrelated rows just because someone uploaded a stray filename.
            if ($did.Length -eq 36) { [void]$docIds.Add($did) }
        }
    }
    ok "Distinct doc_ids parsed" "$($docIds.Count) (from matched S3 keys)"

    if ($DryRun) {
        Write-Host ""
        Write-Host "  DRY RUN - matching S3 keys (showing up to 20):" -ForegroundColor Yellow
        $previewKeys = @($matched | Select-Object -First 20)
        foreach ($o in $previewKeys) {
            $ts = $o.LastModified.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            Write-Host ("    [{0}] {1}" -f $ts, $o.Key) -ForegroundColor DarkGray
        }
        if ($matched.Count -gt 20) {
            Write-Host "    ... ($($matched.Count - 20) more keys)" -ForegroundColor DarkGray
        }
        Write-Host ""
        Write-Host "  DRY RUN - doc_ids that WOULD be deleted from backend.document_uploads (up to 20):" -ForegroundColor Yellow
        $previewDocs = @(@($docIds) | Select-Object -First 20)
        foreach ($d in $previewDocs) { Write-Host "    DOC: $d" -ForegroundColor DarkGray }
        if ($docIds.Count -gt 20) {
            Write-Host "    ... ($($docIds.Count - 20) more doc_ids)" -ForegroundColor DarkGray
        }
        Write-Host ""
        Write-Host "Re-run without -DryRun to actually delete." -ForegroundColor Yellow
        return
    }

    # 1) Delete S3 keys.
    banner "Deleting $($matched.Count) S3 objects in the timeline window"
    $keys = @($matched | ForEach-Object { $_.Key })
    $r = Invoke-S3DeleteKeys -Keys $keys -Bucket $S3_BUCKET_NAME
    Write-Host ""
    ok "S3 keys deleted" "$($r.Deleted) keys"
    if ($r.Errored -gt 0) {
        fail "S3 errors" "$($r.Errored) keys failed to delete"
        # Continue to DB delete anyway - leftover S3 keys can be retried later.
    }

    # 2) Delete matching DB rows by doc_id (cost_usage cascades).
    if ($docIds.Count -eq 0) {
        Write-Host ""
        warn "No doc_ids parsed" "skipping DB delete - S3 keys did not match {doc_id}_{file_name} pattern"
        return
    }
    banner "Deleting $($docIds.Count) rows from backend.document_uploads"
    $deleteCode = @'
import os, sys, json, psycopg2
ids = json.load(sys.stdin)
conn = psycopg2.connect(host=os.environ["DB_HOST"], port=int(os.environ["DB_PORT"]),
                       dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
                       password=os.environ["DB_PASSWORD"])
cur = conn.cursor()
cur.execute("DELETE FROM backend.document_uploads WHERE doc_id::text = ANY(%s);", (ids,))
print(cur.rowcount)
conn.commit()
conn.close()
'@
    $idsJson = (@($docIds) | ConvertTo-Json -Compress)
    # Single-element JSON array: ConvertTo-Json may emit just the string - force array form.
    if ($docIds.Count -eq 1) { $idsJson = "[$idsJson]" }
    $delOut = Invoke-DbPython -Code $deleteCode -StdinText $idsJson -Label 'PostgreSQL delete-by-ids'
    $dbDeleted = [int]($delOut | Select-Object -First 1)
    Write-Host ""
    ok "DB rows deleted" "$dbDeleted (cost_usage rows cascaded)"
    if ($dbDeleted -lt $docIds.Count) {
        warn "DB/S3 drift" "$($docIds.Count - $dbDeleted) S3 doc_ids had no matching DB row"
    }
    Write-Host ""
    Write-Host "Timeline cleanup complete." -ForegroundColor White
}

# ══════════════════════════════════════════════════════════════════════════════
# Mode dispatch - if any alternative mode is selected, run it and exit. Else
# fall through to the existing orphan-cleanup flow below.
# ══════════════════════════════════════════════════════════════════════════════
if ($DeleteAllS3)      { Invoke-DeleteAllS3Mode;      exit 0 }
if ($DeleteAllDb)      { Invoke-DeleteAllDbMode;      exit 0 }
if ($DeleteByTimeline) { Invoke-DeleteByTimelineMode; exit 0 }

# ──────────────────────────────────────────────────────────────────────────────
banner "Step 1 - fetch valid doc_ids from backend.document_uploads"

# Capture stderr to a file so PowerShell 5.1 does not wrap each line as a
# NativeCommandError - that masks the real Python traceback.
$DbStderrFile = New-TemporaryFile
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$dbOutput = & $VenvPython $DocIdFetcherPath 2>$DbStderrFile
$dbExit = $LASTEXITCODE
$ErrorActionPreference = $prevEAP

$dbStderr = ''
if (Test-Path $DbStderrFile) {
    $dbStderr = (Get-Content $DbStderrFile -Raw -ErrorAction SilentlyContinue)
    Remove-Item $DbStderrFile -Force -ErrorAction SilentlyContinue
}

if ($dbExit -ne 0) {
    fail "PostgreSQL" "could not fetch doc_ids (exit $dbExit)"
    if ($dbStderr) {
        Write-Host ""
        Write-Host "Python error:" -ForegroundColor Red
        Write-Host $dbStderr -ForegroundColor Red
    }
    Write-Host ""
    Write-Host "Common causes:" -ForegroundColor Yellow
    Write-Host "  - psycopg2 not installed in .venv  (fix: .venv\Scripts\pip install psycopg2-binary)" -ForegroundColor Yellow
    Write-Host "  - wrong DB host/port/credentials in the CONFIG block"            -ForegroundColor Yellow
    Write-Host "  - network/VPN/security-group blocking the DB endpoint"           -ForegroundColor Yellow
    exit 1
}

# HashSet of valid S3 filenames in the exact form "{doc_id}_{file_name}".
# Case-INSENSITIVE so casing differences in extensions don't cause a miss.
$ValidEntries = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
foreach ($line in $dbOutput) {
    if ($null -eq $line) { continue }
    # Avoid Out-String here: it appends CRLF and formats objects, which can
    # introduce surprises. The line is already a string from native stdout.
    $s = [string]$line
    # Strip UTF-8 BOM (U+FEFF) that occasionally appears on the first line.
    if ($s.Length -gt 0 -and $s[0] -eq [char]0xFEFF) { $s = $s.Substring(1) }
    $s = $s.Trim()
    if ($s) { [void]$ValidEntries.Add($s) }
}
ok "PostgreSQL" "$($DB_USER)@$($DB_HOST):$($DB_PORT)/$($DB_NAME)"
ok "Valid entries" "$($ValidEntries.Count) rows in backend.document_uploads"

# If the table is empty, the user wants every S3 file deleted - announce
# that loudly but do not abort. The percentage-based safety guard below is
# also skipped for this case.
if ($ValidEntries.Count -eq 0) {
    Write-Host ""
    warn "backend.document_uploads has 0 rows" "ALL S3 files under the prefix will be deleted"
}

# ──────────────────────────────────────────────────────────────────────────────
banner "Step 2 - list S3 objects under s3://$S3_BUCKET_NAME/$S3_UPLOAD_PREFIX"

# Capture stderr to a file so PowerShell 5.1 does not wrap each line as a
# NativeCommandError.
$ListStderrFile = New-TemporaryFile
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$listOutput = & aws s3api list-objects-v2 `
    --bucket $S3_BUCKET_NAME `
    --prefix $S3_UPLOAD_PREFIX `
    --output json 2>$ListStderrFile
$listExit = $LASTEXITCODE
$ErrorActionPreference = $prevEAP

$listStderr = ''
if (Test-Path $ListStderrFile) {
    $listStderr = (Get-Content $ListStderrFile -Raw -ErrorAction SilentlyContinue)
    Remove-Item $ListStderrFile -Force -ErrorAction SilentlyContinue
}

if ($listExit -ne 0) {
    fail "S3 list" "list-objects-v2 failed (exit $listExit)"
    if ($listStderr) { Write-Host $listStderr -ForegroundColor Red }
    exit 1
}

$listJoined = ($listOutput | Out-String)
if (-not $listJoined.Trim()) {
    ok "S3 list" "0 objects under prefix - nothing to do"
    exit 0
}

try {
    $listParsed = $listJoined | ConvertFrom-Json
} catch {
    fail "S3 list" "could not parse list-objects-v2 JSON output"
    Write-Host $listJoined -ForegroundColor Red
    exit 1
}

$AllKeys = @()
if ($listParsed.PSObject.Properties.Name -contains 'Contents' -and $listParsed.Contents) {
    $AllKeys = @($listParsed.Contents | ForEach-Object { $_.Key })
}
ok "S3 objects listed" "$($AllKeys.Count) keys under prefix"

# ──────────────────────────────────────────────────────────────────────────────
banner "Step 3 - identify orphan keys (doc_id not in DB)"

# Normalize prefix: guarantee a single trailing slash unless empty.
# Without this, prefix "uploads" applied to key "uploads/abc_file.pdf" would
# strip to "/abc_file.pdf" (extra leading slash) and never match the DB.
$Prefix = $S3_UPLOAD_PREFIX
if ($Prefix -and -not $Prefix.EndsWith('/')) {
    $Prefix = $Prefix + '/'
}

# Diagnostic preview - first 3 DB entries vs first 3 S3 keys (post-strip).
# If these don't look comparable, the comparison will miss everything.
# Wrap Select-Object results in @() so strict mode lets us index/.Count them
# even when only one (or zero) items are returned.
Write-Host ""
Write-Host "  Diagnostic preview (first 3 of each):" -ForegroundColor Cyan
$dbSample = @(@($ValidEntries) | Select-Object -First 3)
$s3Sample = @(@($AllKeys)     | Select-Object -First 3)
for ($i = 0; $i -lt 3; $i++) {
    $dbVal = if ($i -lt $dbSample.Count) { $dbSample[$i] } else { '(none)' }
    $s3Raw = if ($i -lt $s3Sample.Count) { $s3Sample[$i] } else { '(none)' }
    $s3Rel = $s3Raw
    if ($Prefix -and $s3Rel -ne '(none)' -and $s3Rel.StartsWith($Prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        $s3Rel = $s3Rel.Substring($Prefix.Length)
    }
    Write-Host "    DB[$i]: $dbVal"        -ForegroundColor DarkGray
    Write-Host "    S3[$i]: $s3Rel"        -ForegroundColor DarkGray
}
Write-Host ""

$Orphans   = New-Object 'System.Collections.Generic.List[string]'
$KeepCount = 0

foreach ($key in $AllKeys) {
    $relative = $key
    if ($Prefix -and $relative.StartsWith($Prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        $relative = $relative.Substring($Prefix.Length)
    }

    # Skip any "folder placeholder" zero-length keys (key ends with '/')
    if ([string]::IsNullOrWhiteSpace($relative) -or $relative.EndsWith('/')) {
        continue
    }

    # A file is kept ONLY if its exact key (post-prefix) matches a
    # "{doc_id}_{file_name}" row in backend.document_uploads.
    # Files without the expected pattern (no underscore, only filename, etc.)
    # cannot match and are treated as orphans - which is correct, since the
    # uploader always writes "{doc_id}_{file_name}".
    if ($ValidEntries.Contains($relative)) {
        $KeepCount++
    } else {
        $Orphans.Add($key)
    }
}

$TotalParsed = $KeepCount + $Orphans.Count
ok "Total files in S3"           "$($AllKeys.Count) keys (under prefix)"
ok "Total inspected"             "$TotalParsed keys"
ok "Keep (entry in DB)"          "$KeepCount keys"
ok "Orphan (entry NOT in DB)"    "$($Orphans.Count) keys"

# Safety guard: refuse to delete a high fraction of files unless -Force.
# Skipped when the DB is empty - that case is intentional "delete everything".
if ($TotalParsed -gt 0 -and -not $Force -and $ValidEntries.Count -gt 0) {
    $deleteFraction = [double]$Orphans.Count / [double]$TotalParsed
    if ($deleteFraction -gt $MaxDeleteFraction) {
        Write-Host ""
        fail "ABORT - safety guard tripped" ("{0:P1} of files would be deleted (>{1:P0})" -f $deleteFraction, $MaxDeleteFraction)
        Write-Host "  This usually means the comparison failed - the diagnostic preview above" -ForegroundColor Yellow
        Write-Host "  should show DB[i] and S3[i] lines that look identical. If they differ"   -ForegroundColor Yellow
        Write-Host "  (extra slash, different case, extra whitespace, etc.), fix that first."  -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  To override (NOT recommended until preview matches):"                    -ForegroundColor Yellow
        Write-Host "    re-run with -Force, or pass a higher -MaxDeleteFraction"               -ForegroundColor Yellow
        exit 1
    }
}

if ($Orphans.Count -eq 0) {
    Write-Host ""
    Write-Host "Nothing to delete." -ForegroundColor White
    exit 0
}

# ──────────────────────────────────────────────────────────────────────────────
banner "Step 4 - delete orphan keys"

if ($DryRun) {
    # Build a lookup: doc_id -> list of file_names from the DB.
    # When an orphan has a doc_id that DOES exist in the DB but the full
    # filename doesn't match, this reveals exactly how the two sides differ.
    $DbByDocId = @{}
    foreach ($entry in $ValidEntries) {
        $u = $entry.IndexOf('_')
        if ($u -lt 0) { continue }
        $did = $entry.Substring(0, $u)
        $fn  = $entry.Substring($u + 1)
        if (-not $DbByDocId.ContainsKey($did)) {
            $DbByDocId[$did] = New-Object 'System.Collections.Generic.List[string]'
        }
        [void]$DbByDocId[$did].Add($fn)
    }

    # Dump both sides to TEMP files so the user can diff byte-for-byte.
    $DbDumpPath = Join-Path $env:TEMP "aia_db_entries.txt"
    $S3DumpPath = Join-Path $env:TEMP "aia_s3_relative_keys.txt"
    $utf8NoBom  = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($DbDumpPath, [string[]]@($ValidEntries), $utf8NoBom)
    $S3Rel = foreach ($k in $AllKeys) {
        if ($Prefix -and $k.StartsWith($Prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            $k.Substring($Prefix.Length)
        } else { $k }
    }
    [System.IO.File]::WriteAllLines($S3DumpPath, [string[]]@($S3Rel), $utf8NoBom)
    Write-Host "  Wrote diagnostic dumps:" -ForegroundColor Cyan
    Write-Host "    $DbDumpPath" -ForegroundColor DarkGray
    Write-Host "    $S3DumpPath" -ForegroundColor DarkGray
    Write-Host "  Diff them in your editor or run:" -ForegroundColor Cyan
    Write-Host "    fc /n `"$DbDumpPath`" `"$S3DumpPath`"" -ForegroundColor DarkGray
    Write-Host ""

    Write-Host "  DRY RUN - the following keys WOULD be deleted:" -ForegroundColor Yellow
    Write-Host ""

    $shown = 0
    foreach ($k in $Orphans) {
        $rel = $k
        if ($Prefix -and $rel.StartsWith($Prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            $rel = $rel.Substring($Prefix.Length)
        }
        $u = $rel.IndexOf('_')
        $orphanDocId  = if ($u -ge 0) { $rel.Substring(0, $u)      } else { '(no underscore)' }
        $orphanFileNm = if ($u -ge 0) { $rel.Substring($u + 1)     } else { $rel }

        Write-Host "    KEY: $k" -ForegroundColor DarkGray
        if ($shown -lt 20) {
            # First 20 only: show the comparison detail so the user can see
            # exactly why the match failed.
            if ($DbByDocId.ContainsKey($orphanDocId)) {
                Write-Host "      doc_id matches DB rows; file_name does NOT match any of:" -ForegroundColor Yellow
                foreach ($f in $DbByDocId[$orphanDocId]) {
                    Write-Host "        DB file_name : [$f]"     -ForegroundColor Yellow
                }
                Write-Host     "        S3 file_name : [$orphanFileNm]" -ForegroundColor Yellow
            } else {
                Write-Host "      doc_id NOT present in backend.document_uploads" -ForegroundColor DarkGray
            }
            $shown++
        }
    }
    if ($Orphans.Count -gt 20) {
        Write-Host ""
        Write-Host "    (detailed comparison shown for first 20 of $($Orphans.Count) orphans)" -ForegroundColor DarkGray
    }

    Write-Host ""
    Write-Host "Re-run without -DryRun to actually delete." -ForegroundColor Yellow
    exit 0
}

# delete-objects supports up to 1000 keys per call. Chunk accordingly.
$BatchSize = 1000
$Deleted   = 0
$Errored   = 0

for ($i = 0; $i -lt $Orphans.Count; $i += $BatchSize) {
    $end   = [Math]::Min($i + $BatchSize - 1, $Orphans.Count - 1)
    $batch = $Orphans[$i..$end]

    $payload = @{
        Objects = @($batch | ForEach-Object { @{ Key = $_ } })
        Quiet   = $true
    } | ConvertTo-Json -Depth 4 -Compress

    $tmpFile = New-TemporaryFile
    $delStderrFile = New-TemporaryFile
    try {
        # Write UTF-8 WITHOUT BOM - the AWS CLI JSON parser does not accept a
        # BOM and will fail with: "Expected: '=', received: '<BOM-char>'".
        [System.IO.File]::WriteAllText(
            $tmpFile,
            $payload,
            (New-Object System.Text.UTF8Encoding($false))
        )

        # Capture stderr to a file so PowerShell 5.1 does not wrap each line
        # as a NativeCommandError.
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $delOutput = & aws s3api delete-objects `
            --bucket $S3_BUCKET_NAME `
            --delete "file://$tmpFile" `
            --output json 2>$delStderrFile
        $delExit = $LASTEXITCODE
        $ErrorActionPreference = $prevEAP

        $delStderr = ''
        if (Test-Path $delStderrFile) {
            $delStderr = (Get-Content $delStderrFile -Raw -ErrorAction SilentlyContinue)
        }

        if ($delExit -ne 0) {
            fail "Batch $i..$end" "delete-objects failed (exit $delExit)"
            if ($delStderr) { Write-Host $delStderr -ForegroundColor Red }
            $Errored += $batch.Count
            continue
        }

        $delJoined = ($delOutput | Out-String).Trim()
        $batchErrors = 0
        if ($delJoined) {
            try {
                $delParsed = $delJoined | ConvertFrom-Json
                if ($delParsed.PSObject.Properties.Name -contains 'Errors' -and $delParsed.Errors) {
                    $batchErrors = @($delParsed.Errors).Count
                    foreach ($err in $delParsed.Errors) {
                        warn "Delete error" "$($err.Key): $($err.Code) - $($err.Message)"
                    }
                }
            } catch {
                # quiet mode means empty body is normal; ignore parse error if empty
            }
        }

        $Deleted += ($batch.Count - $batchErrors)
        $Errored += $batchErrors
        ok "Batch deleted" "$($batch.Count - $batchErrors)/$($batch.Count) (keys $i..$end)"
    }
    finally {
        Remove-Item -Path $tmpFile       -Force -ErrorAction SilentlyContinue
        Remove-Item -Path $delStderrFile -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
ok "Total deleted" "$Deleted keys"
if ($Errored -gt 0) {
    fail "Errors" "$Errored keys failed to delete"
    exit 1
}

Write-Host ""
Write-Host "Cleanup complete." -ForegroundColor White
