#!/usr/bin/env bash
# scripts/reset-datapipeline.sh
#
# Truncate the mutable data-pipeline tables, confirm they are empty,
# then run the data-pipeline to re-populate them from source_policy_docs.
#
# Usage:
#   ./scripts/reset-datapipeline.sh                  # local Podman container (default)
#   ./scripts/reset-datapipeline.sh --target local   # explicit local mode
#   ./scripts/reset-datapipeline.sh --target rds     # AWS RDS — psql must be on PATH
#
# Requirements (both modes):
#   • .env in the repo root with DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
#   • .venv built from requirements.txt (needs python-dotenv)
#
# Additional requirements per mode:
#   local  — Podman at /opt/podman/bin/podman; container "aiadocuments" running
#   rds    — psql client on PATH; DB_HOST points to the RDS endpoint;
#            DB_SSL_MODE defaults to "require" (override via env if needed)
#
# After the script completes:
#   • data_pipeline.policy_document_sync  — 0 rows
#   • data_pipeline.questions             — 0 rows
#   • data_pipeline.policy_documents      — 0 rows
#   • data_pipeline.policydoc_costusage   — 0 rows
#   • data_pipeline.source_policy_docs     — unchanged (seed data)
#   • policy_documents + questions        — re-populated by the pipeline run

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
INIT_SQL="$REPO_ROOT/app/datapipeline/db/init.sql"

# ── Colours ────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

ok()     { printf "${GREEN}  ✓${NC}  %-36s %s\n" "$1" "${2:-}"; }
fail()   { printf "${RED}  ✗${NC}  %-36s %s\n" "$1" "${2:-}"; }
warn()   { printf "${YELLOW}  !${NC}  %-36s %s\n" "$1" "${2:-}"; }
banner() { echo -e "\n${BOLD}$1${NC}"; printf '─%.0s' {1..58}; echo; }

# ── Parse arguments ────────────────────────────────────────────────────────────
TARGET="local"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)   TARGET="$2"; shift 2 ;;
        --target=*) TARGET="${1#--target=}"; shift ;;
        *) echo -e "${RED}ERROR:${NC} Unknown argument: $1"; exit 1 ;;
    esac
done

if [[ "$TARGET" != "local" && "$TARGET" != "rds" ]]; then
    echo -e "${RED}ERROR:${NC} --target must be 'local' or 'rds'"
    exit 1
fi

# ── Require .venv (needed to load .env via python-dotenv) ──────────────────────
if [[ ! -x "$VENV_PYTHON" ]]; then
    echo -e "${RED}ERROR:${NC} .venv not found at $VENV_PYTHON"
    echo "  Build it:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

# ── Load .env via python-dotenv ────────────────────────────────────────────────
# Python shlex.quote handles special characters in passwords safely.
if [[ -f "$REPO_ROOT/.env" ]]; then
    eval "$("$VENV_PYTHON" -c "
import shlex, sys
from dotenv import dotenv_values
for k, v in dotenv_values('$REPO_ROOT/.env').items():
    if v is not None:
        print(f'export {k}={shlex.quote(v)}')
" 2>/dev/null)"
else
    echo -e "${RED}ERROR:${NC} .env not found at $REPO_ROOT/.env"
    exit 1
fi

# ── Resolve DB variables (with safe defaults) ──────────────────────────────────
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-aiadocuments}"
DB_USER="${DB_USER:-aiauser}"
DB_PASSWORD="${DB_PASSWORD:-}"

# ── Configure psql backend based on --target ───────────────────────────────────
if [[ "$TARGET" == "rds" ]]; then
    PSQL_BIN="$(command -v psql 2>/dev/null || true)"
    if [[ -z "$PSQL_BIN" ]]; then
        echo -e "${RED}ERROR:${NC} psql not found on PATH — install PostgreSQL client tools for --target rds"
        exit 1
    fi
    DB_SSL_MODE="${DB_SSL_MODE:-require}"

    # pg() — run a psql command directly against RDS
    pg() {
        PGPASSWORD="$DB_PASSWORD" PGSSLMODE="$DB_SSL_MODE" "$PSQL_BIN" \
            -h "$DB_HOST" -p "$DB_PORT" \
            -U "$DB_USER" -d "$DB_NAME" \
            -v ON_ERROR_STOP=1 \
            "$@"
    }

    # pg_pipe() — pipe SQL into psql (stdin passthrough; same as pg() for RDS)
    pg_pipe() { pg "$@"; }

else
    PODMAN="/opt/podman/bin/podman"
    PG_CONTAINER="${PG_CONTAINER:-aiadocuments}"

    if [[ ! -x "$PODMAN" ]]; then
        echo -e "${RED}ERROR:${NC} Podman not found at $PODMAN"
        exit 1
    fi

    # pg() — run a psql command inside the Podman container
    pg() {
        "$PODMAN" exec -e "PGPASSWORD=$DB_PASSWORD" "$PG_CONTAINER" psql \
            -h "$DB_HOST" -p "$DB_PORT" \
            -U "$DB_USER" -d "$DB_NAME" \
            -v ON_ERROR_STOP=1 \
            "$@"
    }

    # pg_pipe() — pipe SQL via stdin into the container (-i enables stdin on exec)
    pg_pipe() {
        "$PODMAN" exec -i -e "PGPASSWORD=$DB_PASSWORD" "$PG_CONTAINER" psql \
            -h "$DB_HOST" -p "$DB_PORT" \
            -U "$DB_USER" -d "$DB_NAME" \
            -v ON_ERROR_STOP=1 \
            "$@"
    }
fi

# row_count <schema.table>  →  prints the integer row count
row_count() {
    pg -t -A -c "SELECT COUNT(*) FROM $1;" 2>/dev/null | tr -d '[:space:]'
}

cd "$REPO_ROOT"

echo -e "\n${BOLD}target: ${TARGET}${NC}  ${DB_USER}@${DB_HOST}:${DB_PORT}/${DB_NAME}"

# ──────────────────────────────────────────────────────────────────────────────
banner "Step 1 — verify PostgreSQL connection"

if pg -c "SELECT 1;" > /dev/null 2>&1; then
    ok "PostgreSQL" "$DB_USER@$DB_HOST:$DB_PORT/$DB_NAME"
else
    fail "PostgreSQL" "cannot connect — check DB_* vars in .env"
    exit 1
fi

# ──────────────────────────────────────────────────────────────────────────────
banner "Step 2 — apply init.sql migrations"

if [[ ! -f "$INIT_SQL" ]]; then
    fail "init.sql" "not found at $INIT_SQL"
    exit 1
fi

INIT_OUTPUT=""
if INIT_OUTPUT="$(cat "$INIT_SQL" | pg_pipe 2>&1)"; then
    ok "init.sql applied" "schema/tables/migrations are up to date"
else
    fail "init.sql apply failed" "check DB permissions and SQL errors"
    echo "$INIT_OUTPUT"
    exit 1
fi

# ──────────────────────────────────────────────────────────────────────────────
banner "Step 3 — truncate mutable tables (CASCADE)"

# Truncate in dependency order: sync → questions → policy_documents.
# CASCADE covers any FK children we may have missed.
TRUNCATE_SQL="
TRUNCATE TABLE
    data_pipeline.policy_document_sync,
    data_pipeline.questions,
    data_pipeline.policy_documents,
    data_pipeline.policydoc_costusage
CASCADE;
"

if pg -c "$TRUNCATE_SQL" > /dev/null 2>&1; then
    ok "Truncated" "policy_document_sync, questions, policy_documents, policydoc_costusage"
else
    fail "Truncate failed" "check psql output above"
    exit 1
fi

# ──────────────────────────────────────────────────────────────────────────────
banner "Step 4 — confirm row counts"

FAIL_COUNT=0

check_empty() {
    local table="$1"
    local n
    n="$(row_count "$table")"
    if [[ "$n" == "0" ]]; then
        ok "$table" "0 rows"
    else
        fail "$table" "$n rows (expected 0)"
        FAIL_COUNT=$(( FAIL_COUNT + 1 ))
    fi
}

check_empty "data_pipeline.policy_document_sync"
check_empty "data_pipeline.questions"
check_empty "data_pipeline.policy_documents"
check_empty "data_pipeline.policydoc_costusage"

# source_policy_docs must have rows (seed table — never truncated by this script).
SEED_COUNT="$(row_count "data_pipeline.source_policy_docs")"
if [[ "$SEED_COUNT" -gt 0 ]]; then
    ok "data_pipeline.source_policy_docs" "$SEED_COUNT rows (seed intact)"
else
    warn "source_policy_docs" "empty — seeding from app/datapipeline/data/policy_sources.json"
    SEED_FILE="$REPO_ROOT/app/datapipeline/data/policy_sources.json"
    if [[ ! -f "$SEED_FILE" ]]; then
        fail "seed file not found" "$SEED_FILE"
        FAIL_COUNT=$(( FAIL_COUNT + 1 ))
    else
        SEED_SQL="$(SEED_FILE="$SEED_FILE" "$VENV_PYTHON" - <<'PY'
import json
import os

with open(os.environ["SEED_FILE"], encoding="utf-8") as f:
    rows = json.load(f)


def esc(value):
    return str(value).replace("'", "''")


def sql_timestamptz(value):
    if not value:
        return "NULL"
    return "'" + esc(value) + "'::timestamptz"


vals = []
categories = set()
for r in rows:
    isactive = "true" if r["isactive"] else "false"
    updated_at = sql_timestamptz(r.get("updatedAt") or r.get("updated_at"))
    categories.add(esc(r["category"]))
    vals.append(
        "(" + str(r["url_id"]) + ", '" + esc(r["url"]) + "', '" + esc(r["filename"]) + "', '"
        + esc(r["category"]) + "', '" + esc(r["source"]) + "', " + isactive + ", " + updated_at + ")"
    )

if categories:
    print("INSERT INTO data_pipeline.policy_source_categories (category) VALUES")
    print(",\n".join(["('" + c + "')" for c in sorted(categories)]))
    print("ON CONFLICT (category) DO NOTHING;")

if vals:
    print("INSERT INTO data_pipeline.source_policy_docs (url_id, url, filename, category, source, isactive, updated_at) VALUES")
    print(",\n".join(vals))
    print("ON CONFLICT DO NOTHING;")

print("SELECT setval(pg_get_serial_sequence('data_pipeline.source_policy_docs', 'url_id'), (SELECT COALESCE(MAX(url_id), 1) FROM data_pipeline.source_policy_docs), true);")
PY
)"
        if echo "$SEED_SQL" | pg_pipe > /dev/null 2>&1; then
            SEED_COUNT="$(row_count "data_pipeline.source_policy_docs")"
            ok "data_pipeline.source_policy_docs" "$SEED_COUNT rows (seeded from JSON)"
        else
            fail "data_pipeline.source_policy_docs" "seeding failed — check $SEED_FILE"
            FAIL_COUNT=$(( FAIL_COUNT + 1 ))
        fi
    fi
fi

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    echo ""
    echo -e "${RED}Truncate verification failed for $FAIL_COUNT table(s).${NC} Aborting pipeline run."
    exit 1
fi

# ──────────────────────────────────────────────────────────────────────────────
banner "Step 5 — validate AWS authentication"

# Bedrock is the default LLM provider in the datapipeline. Validate AWS auth
# upfront to fail fast on expired/invalid credentials before pipeline execution.
LLM_PROVIDER_LOWER="$(echo "${LLM_PROVIDER:-bedrock}" | tr '[:upper:]' '[:lower:]')"
if [[ "$LLM_PROVIDER_LOWER" == "bedrock" ]]; then
    if AWS_IDENTITY="$($VENV_PYTHON - <<'PY'
import os
import sys

try:
    import boto3
except Exception as exc:
    print(f"boto3 unavailable for AWS auth validation: {exc}")
    sys.exit(2)

region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION")
if not region:
    print("Missing AWS_DEFAULT_REGION/AWS_REGION for Bedrock authentication")
    sys.exit(2)

session_kwargs = {"region_name": region}
access_key = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
session_token = os.environ.get("AWS_SESSION_TOKEN", "").strip()

if access_key and secret_key:
    session_kwargs["aws_access_key_id"] = access_key
    session_kwargs["aws_secret_access_key"] = secret_key
    if session_token:
        session_kwargs["aws_session_token"] = session_token

try:
    session = boto3.Session(**session_kwargs)
    identity = session.client("sts").get_caller_identity()
except Exception as exc:
    print(f"AWS authentication failed: {exc}")
    sys.exit(1)

print(f"account={identity.get('Account')} arn={identity.get('Arn')}")
PY
    )"; then
        ok "AWS authentication" "$AWS_IDENTITY"
    else
        fail "AWS authentication" "$AWS_IDENTITY"
        echo -e "${RED}Aborting pipeline run due to AWS authentication failure.${NC}"
        exit 1
    fi
else
    warn "AWS authentication" "skipped (LLM_PROVIDER=${LLM_PROVIDER:-bedrock})"
fi

# ──────────────────────────────────────────────────────────────────────────────
banner "Step 6 — run data pipeline"

echo ""
echo "  $ .venv/bin/python -m app.datapipeline.src.entrypoints.main"
echo ""

"$VENV_PYTHON" -m app.datapipeline.src.entrypoints.main
PIPELINE_EXIT=$?

echo ""
if [[ "$PIPELINE_EXIT" -eq 0 ]]; then
    ok "Pipeline exited cleanly" "exit code 0"
else
    fail "Pipeline exited with errors" "exit code $PIPELINE_EXIT"
fi

# ──────────────────────────────────────────────────────────────────────────────
banner "Step 7 — final row counts"

PD_COUNT="$(row_count "data_pipeline.policy_documents")"
Q_COUNT="$(row_count "data_pipeline.questions")"
SYNC_COUNT="$(row_count "data_pipeline.policy_document_sync")"

if [[ "$PD_COUNT" -gt 0 ]]; then
    ok "policy_documents" "$PD_COUNT rows"
else
    warn "policy_documents" "0 rows — pipeline may not have produced output"
fi

if [[ "$Q_COUNT" -gt 0 ]]; then
    ok "questions" "$Q_COUNT rows"
else
    warn "questions" "0 rows — pipeline may not have produced output"
fi

ok "policy_document_sync" "$SYNC_COUNT rows"
ok "source_policy_docs"   "$SEED_COUNT rows (unchanged)"

echo ""
if [[ "$PIPELINE_EXIT" -eq 0 ]]; then
    echo -e "${BOLD}Reset complete.${NC}"
else
    echo -e "${YELLOW}Reset finished with pipeline errors.${NC} Check the output above."
    exit "$PIPELINE_EXIT"
fi
