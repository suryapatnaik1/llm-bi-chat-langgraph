# =============================================================================
# Makefile — developer shortcuts for llm-bi-chat-langgraph
# =============================================================================
# Usage:
#   make security-scan          run all four security checks, exit 0 always
#   make security-scan-strict   same, exit 1 if any tool finds issues (CI mode)
#   make security-install       install all required security tools
# =============================================================================

.DEFAULT_GOAL := help

# Resolve the directory containing this Makefile (works regardless of cwd)
ROOT := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))

# ── Help ──────────────────────────────────────────────────────────────────────

.PHONY: help
help:
	@echo "Available targets:"
	@echo "  make security-scan          Run pip-audit, bandit, gitleaks, trivy"
	@echo "  make security-scan-strict   Same, exit 1 on any finding (for CI)"
	@echo "  make security-install       Install all required security tools"

# ── Security scan ─────────────────────────────────────────────────────────────

.PHONY: security-scan
security-scan:  ## Run all four security checks (pip-audit, bandit, gitleaks, trivy)
	@bash $(ROOT)scripts/audit_cves.sh

.PHONY: security-scan-strict
security-scan-strict:  ## Same as security-scan but exit 1 on any finding (CI mode)
	@bash $(ROOT)scripts/audit_cves.sh --fail-on-vuln

# ── Tool installation ─────────────────────────────────────────────────────────

.PHONY: security-install
security-install:  ## Install pip-audit, bandit (pipx) and gitleaks, trivy (brew)
	@echo "[*] Installing security tools..."
	@command -v pipx  >/dev/null || brew install pipx
	@pipx ensurepath --force >/dev/null
	@pipx install pip-audit --force --quiet
	@pipx install bandit  --force --quiet
	@command -v gitleaks >/dev/null || brew install gitleaks
	@command -v trivy    >/dev/null || brew install trivy
	@echo "[*] Done. Run 'make security-scan' to audit the project."
