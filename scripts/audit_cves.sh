#!/usr/bin/env bash
# =============================================================================
# scripts/audit_cves.sh — Unified security audit
# =============================================================================
#
# Runs four complementary security checks and writes a combined report:
#
#   1. pip-audit  — checks every locked dependency against the OSV / PyPI
#                   advisory databases for known CVEs.
#
#   2. bandit     — static analysis of Python source code in src/ for common
#                   insecure patterns (SQL injection, shell injection, weak
#                   crypto, hardcoded passwords, etc.).
#
#   3. gitleaks   — scans the working tree AND full git history for accidentally
#                   committed secrets (API keys, tokens, private keys).
#
#   4. trivy      — broad vulnerability and misconfiguration scanner:
#                     fs scan   : Python dependency CVEs (cross-checks pip-audit)
#                                 plus any secrets embedded in source files.
#                     config scan: IaC misconfigurations in Dockerfile and
#                                 docker-compose.yml (CIS benchmarks, best
#                                 practices for least-privilege containers).
#
# Usage:
#   ./scripts/audit_cves.sh               # print results, always exit 0
#   ./scripts/audit_cves.sh --fail-on-vuln  # exit 1 if any tool finds issues
#                                            # (useful for CI pipelines)
#
# Output:
#   reports/cve_audit.txt   — human-readable combined report
#   reports/gitleaks.json   — machine-readable gitleaks findings (JSON)
#
# First-time setup:
#   brew install pipx gitleaks trivy
#   pipx install pip-audit
#   pipx install bandit
#   pipx ensurepath   # adds ~/.local/bin to PATH in your shell profile
#
# =============================================================================

set -euo pipefail  # exit on error (-e), unset variable (-u), or pipe failure (-o pipefail)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPORT_DIR="reports"
FAIL_ON_VULN=false

# Resolve the project root from the script's location so the script works
# regardless of the working directory from which it is invoked.
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Parse CLI flags
for arg in "$@"; do
  [[ "$arg" == "--fail-on-vuln" ]] && FAIL_ON_VULN=true
done

# Create the reports directory if it doesn't exist yet
mkdir -p "$PROJECT_ROOT/$REPORT_DIR"
REPORT_FILE="$PROJECT_ROOT/$REPORT_DIR/cve_audit.txt"

# Write (and display) the report header
echo "=== Security Audit ===" | tee "$REPORT_FILE"
echo "Date: $(date -u '+%Y-%m-%dT%H:%M:%SZ')" | tee -a "$REPORT_FILE"
echo "Project: $(basename "$PROJECT_ROOT")" | tee -a "$REPORT_FILE"
echo "" | tee -a "$REPORT_FILE"

# ---------------------------------------------------------------------------
# Tool discovery helpers
# ---------------------------------------------------------------------------

# find_tool <name>
#   Searches common install locations for a binary and prints its path.
#   Returns 1 if not found.
#
#   Why multiple locations?
#   - pipx symlinks to ~/.local/bin, but only after `pipx ensurepath` has
#     been run and a new shell session started — so it may not be on PATH yet.
#   - pipx also keeps the real binary inside the venv at
#     ~/.local/pipx/venvs/<tool>/bin/<tool>, which always exists after install.
#   - /usr/local/bin is the fallback for Homebrew on Intel Macs.
find_tool() {
  local tool="$1"
  for candidate in \
      "$(command -v "$tool" 2>/dev/null || true)" \
      "$HOME/.local/bin/$tool" \
      "$HOME/.local/pipx/venvs/$tool/bin/$tool" \
      "/usr/local/bin/$tool"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

# ensure_tool <name>
#   Calls find_tool; if not found and pipx is available, installs the tool
#   automatically. Exits with an error message if neither is possible.
ensure_tool() {
  local tool="$1"
  local found
  found=$(find_tool "$tool" || true)
  if [[ -n "$found" ]]; then
    echo "$found"
    return
  fi
  if command -v pipx &>/dev/null; then
    echo "[*] Installing $tool via pipx..." | tee -a "$REPORT_FILE"
    pipx install "$tool" --quiet
    echo "$HOME/.local/bin/$tool"
  else
    echo "ERROR: $tool not found." | tee -a "$REPORT_FILE"
    echo "       Install: brew install pipx && pipx install $tool" | tee -a "$REPORT_FILE"
    exit 1
  fi
}

# Resolve pip-audit and bandit (both installed via pipx)
PIP_AUDIT=$(ensure_tool pip-audit)
BANDIT=$(ensure_tool bandit)

# Gitleaks and Trivy are distributed as pre-built binaries via Homebrew,
# not pipx, so we look them up directly on PATH.
GITLEAKS=$(command -v gitleaks 2>/dev/null || true)
if [[ -z "$GITLEAKS" ]]; then
  echo "ERROR: gitleaks not found." | tee -a "$REPORT_FILE"
  echo "       Install: brew install gitleaks" | tee -a "$REPORT_FILE"
  exit 1
fi

TRIVY=$(command -v trivy 2>/dev/null || true)
if [[ -z "$TRIVY" ]]; then
  echo "ERROR: trivy not found." | tee -a "$REPORT_FILE"
  echo "       Install: brew install trivy" | tee -a "$REPORT_FILE"
  exit 1
fi

# ---------------------------------------------------------------------------
# 1. pip-audit — dependency CVE scan
# ---------------------------------------------------------------------------
# pip-audit resolves each package against the OSV (Open Source Vulnerabilities)
# database and the PyPI advisory database. We feed it a requirements.txt
# derived from poetry.lock (which pins exact versions) rather than scanning
# the active virtual environment, so the results are reproducible regardless
# of what is currently installed.
# ---------------------------------------------------------------------------

echo "=== pip-audit: Dependency CVE Scan ===" | tee -a "$REPORT_FILE"
echo "" | tee -a "$REPORT_FILE"

# Verify the lockfile exists before attempting to parse it
LOCKFILE="$PROJECT_ROOT/poetry.lock"
if [[ ! -f "$LOCKFILE" ]]; then
  echo "ERROR: poetry.lock not found in $PROJECT_ROOT" | tee -a "$REPORT_FILE"
  exit 1
fi

# Create a temp file to hold the extracted requirements; clean it up on exit
REQUIREMENTS_TMP=$(mktemp /tmp/audit_req_XXXXXX.txt)
trap 'rm -f "$REQUIREMENTS_TMP"' EXIT

# Parse poetry.lock with an inline Python script.
#
# Why not `poetry export`?
#   The `poetry export` command requires the poetry-plugin-export plugin,
#   which is not bundled with Poetry 2.x and may not be installed. Parsing
#   the lockfile directly avoids that dependency.
#
# What the parser does:
#   - Splits the TOML file on [[package]] section boundaries.
#   - Extracts name and pinned version for each package.
#   - Includes only packages in the "main" group (skips dev dependencies).
#   - Filters out packages whose top-level `markers` field restricts them to
#     a different OS (e.g. pywin32 on macOS/Linux), because pip-audit would
#     fail trying to resolve a Windows-only wheel on the current platform.
#
# Note on marker parsing:
#   TOML encodes embedded double-quotes as \" in basic strings. Python reads
#   these as literal backslash+quote pairs, so we unescape them before
#   matching against the human-readable marker expressions.
python3 - "$LOCKFILE" "$REQUIREMENTS_TMP" <<'PYEOF'
import sys, re, platform

lockfile, outfile = sys.argv[1], sys.argv[2]
text = open(lockfile).read()

CURRENT_PLATFORM = platform.system().lower()  # 'darwin', 'linux', 'windows'

def is_platform_compatible(markers_str):
    """Return False if markers explicitly exclude the current platform."""
    if not markers_str:
        return True
    # Unescape TOML \" so marker strings are human-readable for matching
    m = markers_str.replace('\\"', '"')
    # Windows-only packages (e.g. pywin32, colorama on win32)
    if 'sys_platform == "win32"' in m or "sys_platform == 'win32'" in m or \
       'platform_system == "Windows"' in m or "platform_system == 'Windows'" in m:
        return CURRENT_PLATFORM == "windows"
    # Linux-only packages
    if 'sys_platform == "linux"' in m or "sys_platform == 'linux'" in m:
        return CURRENT_PLATFORM == "linux"
    return True

packages = []
skipped = []
for block in re.split(r'\n\[\[package\]\]\n', text):
    name    = re.search(r'^name = "([^"]+)"', block, re.M)
    version = re.search(r'^version = "([^"]+)"', block, re.M)
    groups  = re.search(r'^groups = \[([^\]]*)\]', block, re.M)
    # Top-level markers field uses escaped quotes: markers = "sys_platform == \"win32\""
    markers = re.search(r'^markers = "((?:[^"\\]|\\.)*)"', block, re.M)

    if not (name and version):
        continue
    # Skip dev-only packages — we only audit production dependencies
    if groups and '"main"' not in groups.group(1) and "'main'" not in groups.group(1):
        continue
    markers_val = markers.group(1) if markers else ""
    if not is_platform_compatible(markers_val):
        skipped.append(name.group(1))
        continue
    packages.append(f"{name.group(1)}=={version.group(1)}")

with open(outfile, 'w') as f:
    f.write('\n'.join(sorted(packages)) + '\n')

print(f"[*] Parsed {len(packages)} packages from poetry.lock "
      f"(skipped {len(skipped)} platform-incompatible: {', '.join(skipped) or 'none'})")
PYEOF

DEP_COUNT=$(wc -l < "$REQUIREMENTS_TMP" | tr -d ' ')
echo "[*] Scanning $DEP_COUNT packages..." | tee -a "$REPORT_FILE"
echo "" | tee -a "$REPORT_FILE"

# --format=columns produces a human-readable table; pipe failures are captured
# by set +e so we can record the exit code and continue to the next tool.
set +e
"$PIP_AUDIT" -r "$REQUIREMENTS_TMP" --format=columns 2>&1 | tee -a "$REPORT_FILE"
AUDIT_EXIT=$?
set -e

echo "" | tee -a "$REPORT_FILE"
if [[ $AUDIT_EXIT -eq 0 ]]; then
  echo "RESULT (pip-audit): No known vulnerabilities in dependencies." | tee -a "$REPORT_FILE"
else
  echo "RESULT (pip-audit): Vulnerabilities detected (exit code $AUDIT_EXIT)." | tee -a "$REPORT_FILE"
fi

# ---------------------------------------------------------------------------
# 2. bandit — Python static analysis
# ---------------------------------------------------------------------------
# Bandit walks the AST of every .py file under src/ and flags patterns from
# the OWASP Top 10 and CWE catalogue: SQL/shell injection, use of weak hash
# algorithms, insecure deserialization, hardcoded passwords, etc.
#
# Flags used:
#   -r         recursive scan
#   -ll        report only Medium and High severity (suppress Low noise)
#   --format txt  human-readable output (bandit uses 'txt', not 'text')
# ---------------------------------------------------------------------------

echo "" | tee -a "$REPORT_FILE"
echo "---" | tee -a "$REPORT_FILE"
echo "=== Bandit: Python Static Analysis ===" | tee -a "$REPORT_FILE"
echo "" | tee -a "$REPORT_FILE"

SRC_DIR="$PROJECT_ROOT/src"
if [[ ! -d "$SRC_DIR" ]]; then
  # Graceful skip — the project may not have a src/ layout
  echo "WARNING: src/ directory not found — skipping Bandit." | tee -a "$REPORT_FILE"
  BANDIT_EXIT=0
else
  echo "[*] Scanning $SRC_DIR for insecure code patterns..." | tee -a "$REPORT_FILE"
  echo "" | tee -a "$REPORT_FILE"
  set +e
  "$BANDIT" -r "$SRC_DIR" -ll --format txt 2>&1 | tee -a "$REPORT_FILE"
  BANDIT_EXIT=$?
  set -e
fi

echo "" | tee -a "$REPORT_FILE"
if [[ $BANDIT_EXIT -eq 0 ]]; then
  echo "RESULT (bandit): No issues found." | tee -a "$REPORT_FILE"
else
  echo "RESULT (bandit): Issues detected (exit code $BANDIT_EXIT)." | tee -a "$REPORT_FILE"
fi

# ---------------------------------------------------------------------------
# 3. gitleaks — secret and credential scanning
# ---------------------------------------------------------------------------
# Gitleaks detects secrets (API keys, tokens, private keys, connection strings)
# using a library of 150+ regex rules. We run it in two passes:
#
#   detect --no-git   Scans every file in the working tree as plain text,
#                     including untracked files. Catches secrets that were
#                     never committed but exist on disk.
#
#   git               Replays the full git history (all commits, all branches)
#                     looking for secrets that were committed and later deleted.
#                     "Deleted" does not mean "gone" — git history is permanent.
#
# Flags used:
#   --redact          Replaces the matched secret value in output with REDACTED
#                     so the audit report itself does not contain live credentials.
#   --config          Points to .gitleaks.toml, which allowlists .env files
#                     (they are already gitignored and are the intended place
#                     to store local credentials — flagging them adds no value).
#   --report-format json / --report-path
#                     Writes a machine-readable JSON report for the working-tree
#                     scan (useful for CI tooling or SIEM ingestion).
# ---------------------------------------------------------------------------

echo "" | tee -a "$REPORT_FILE"
echo "---" | tee -a "$REPORT_FILE"
echo "=== Gitleaks: Secret Scanning ===" | tee -a "$REPORT_FILE"
echo "" | tee -a "$REPORT_FILE"

GITLEAKS_REPORT="$PROJECT_ROOT/$REPORT_DIR/gitleaks.json"

# Pass 1 — working tree (all files, including untracked)
echo "[*] Scanning working tree for secrets..." | tee -a "$REPORT_FILE"
set +e
"$GITLEAKS" detect \
  --source "$PROJECT_ROOT" \
  --config "$PROJECT_ROOT/.gitleaks.toml" \
  --report-format json \
  --report-path "$GITLEAKS_REPORT" \
  --redact \
  --no-git \
  2>&1 | tee -a "$REPORT_FILE"
GITLEAKS_DETECT_EXIT=$?
set -e

echo "" | tee -a "$REPORT_FILE"

# Pass 2 — full git history
echo "[*] Scanning git history for secrets..." | tee -a "$REPORT_FILE"
set +e
"$GITLEAKS" git \
  --config "$PROJECT_ROOT/.gitleaks.toml" \
  --redact \
  "$PROJECT_ROOT" \
  2>&1 | tee -a "$REPORT_FILE"
GITLEAKS_GIT_EXIT=$?
set -e

# Combine exit codes with bitwise OR — non-zero if either pass found something
GITLEAKS_EXIT=$(( GITLEAKS_DETECT_EXIT | GITLEAKS_GIT_EXIT ))

echo "" | tee -a "$REPORT_FILE"
if [[ $GITLEAKS_EXIT -eq 0 ]]; then
  echo "RESULT (gitleaks): No secrets found." | tee -a "$REPORT_FILE"
else
  echo "RESULT (gitleaks): Secrets detected — review $GITLEAKS_REPORT" | tee -a "$REPORT_FILE"
fi

# ---------------------------------------------------------------------------
# 4. trivy — filesystem CVE scan + IaC misconfiguration scan
# ---------------------------------------------------------------------------
# Trivy is a multi-purpose scanner from Aqua Security. We use two sub-commands:
#
#   fs (filesystem) scan
#     Walks the project directory and detects:
#       - Python dependency vulnerabilities (via pyproject.toml / poetry.lock),
#         cross-checking pip-audit with a second advisory source (GitHub Advisories
#         + OSV + NVD).
#       - Secrets embedded in source files (complementary to gitleaks).
#     Flags:
#       --scanners vuln,secret  focus on CVEs and secrets only (skip misconfigs
#                               here — handled separately by the config scanner).
#       --severity HIGH,CRITICAL  suppress Low/Medium noise; tune as needed.
#       --format table          human-readable output for the report.
#       --exit-code 1           non-zero if any findings at the requested severity.
#       --timeout 15m           the vuln DB download (~88 MB) can exceed the
#                               default 5m timeout on a slow connection.
#
#   config scan
#     Evaluates Dockerfile and docker-compose.yml against CIS Docker Benchmark
#     rules and Trivy's built-in IaC policies:
#       - Running containers as root
#       - Privileged mode enabled
#       - Missing HEALTHCHECK / USER / etc.
#       - Exposed sensitive ports
#     Flags:
#       (no --scanners flag needed — trivy config implies misconfig-only scan).
#       --severity HIGH,CRITICAL same threshold as above.
# ---------------------------------------------------------------------------

echo "" | tee -a "$REPORT_FILE"
echo "---" | tee -a "$REPORT_FILE"
echo "=== Trivy: Filesystem CVE + Secret Scan ===" | tee -a "$REPORT_FILE"
echo "" | tee -a "$REPORT_FILE"

echo "[*] Scanning filesystem for CVEs and secrets..." | tee -a "$REPORT_FILE"
echo "" | tee -a "$REPORT_FILE"
set +e
"$TRIVY" fs "$PROJECT_ROOT" \
  --scanners vuln,secret \
  --severity HIGH,CRITICAL \
  --format table \
  --exit-code 1 \
  --timeout 15m \
  2>&1 | tee -a "$REPORT_FILE"
TRIVY_FS_EXIT=$?
set -e

echo "" | tee -a "$REPORT_FILE"
echo "---" | tee -a "$REPORT_FILE"
echo "=== Trivy: IaC Misconfiguration Scan ===" | tee -a "$REPORT_FILE"
echo "" | tee -a "$REPORT_FILE"

echo "[*] Scanning Dockerfile and docker-compose.yml for misconfigurations..." | tee -a "$REPORT_FILE"
echo "" | tee -a "$REPORT_FILE"
set +e
"$TRIVY" config "$PROJECT_ROOT" \
  --severity HIGH,CRITICAL \
  --format table \
  --exit-code 1 \
  2>&1 | tee -a "$REPORT_FILE"
TRIVY_CONFIG_EXIT=$?
set -e

TRIVY_EXIT=$(( TRIVY_FS_EXIT | TRIVY_CONFIG_EXIT ))

echo "" | tee -a "$REPORT_FILE"
if [[ $TRIVY_EXIT -eq 0 ]]; then
  echo "RESULT (trivy): No HIGH/CRITICAL issues found." | tee -a "$REPORT_FILE"
else
  echo "RESULT (trivy): Issues detected (exit code $TRIVY_EXIT)." | tee -a "$REPORT_FILE"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
# Bitwise OR of all exit codes: non-zero if any tool reported findings.
# This lets --fail-on-vuln work correctly even if only one tool flagged issues.
# ---------------------------------------------------------------------------

echo "" | tee -a "$REPORT_FILE"
echo "---" | tee -a "$REPORT_FILE"
OVERALL_EXIT=$(( AUDIT_EXIT | BANDIT_EXIT | GITLEAKS_EXIT | TRIVY_EXIT ))
if [[ $OVERALL_EXIT -eq 0 ]]; then
  echo "OVERALL: Clean — no issues found." | tee -a "$REPORT_FILE"
else
  echo "OVERALL: Issues detected — review report for details." | tee -a "$REPORT_FILE"
fi

echo "" | tee -a "$REPORT_FILE"
echo "Full report saved to: $REPORT_FILE"

# Exit 1 only when explicitly requested (e.g. in CI to block merges)
if $FAIL_ON_VULN && [[ $OVERALL_EXIT -ne 0 ]]; then
  exit 1
fi
