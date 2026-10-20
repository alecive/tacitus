#!/usr/bin/env bash
#
# Secrets lint: scan staged files for credential patterns
# Use as a pre-commit hook or in CI.
#
# Usage:
#   ./tests/secrets_lint.sh              # scan working tree
#   ./tests/secrets_lint.sh --staged     # scan only staged files (for pre-commit)
#
set -euo pipefail

STAGED=false
[[ "${1:-}" == "--staged" ]] && STAGED=true

# Patterns that should never appear in committed code
PATTERNS=(
    'xoxb-[0-9]'              # Slack bot token
    'xapp-1-[A-Z]'            # Slack app-level token
    'GOCSPX-'                 # Google OAuth client secret
    'ya29\.'                  # Google access token
    'eyJ[A-Za-z0-9_-]{20,}'  # JWT (Graph API manual token)
    'sk-[a-zA-Z0-9]{20,}'    # OpenAI/Anthropic API key
    'ghp_[a-zA-Z0-9]{36}'    # GitHub personal access token
    'AIza[0-9A-Za-z_-]{35}'  # Google API key
)

# File extensions to check
INCLUDE="*.py *.sh *.md *.json *.txt *.yaml *.yml *.toml *.cfg *.env.example"

FAIL=0

for pattern in "${PATTERNS[@]}"; do
    if $STAGED; then
        matches=$(git diff --cached --name-only | grep -v "tests/secrets_lint" | xargs grep -l "$pattern" 2>/dev/null || true)
    else
        matches=$(grep -rl "$pattern" --include="*.py" --include="*.sh" --include="*.md" --include="*.json" --include="*.txt" . 2>/dev/null | grep -v ".git/" | grep -v "__pycache__" | grep -v "/\.venv/" | grep -v "/venv/" | grep -v "node_modules/" | grep -v ".env$" | grep -v "tests/secrets_lint" || true)
    fi

    if [[ -n "$matches" ]]; then
        echo "FAIL: Pattern '$pattern' found in:"
        echo "$matches" | sed 's/^/  /'
        FAIL=$((FAIL+1))
    fi
done

if [[ $FAIL -eq 0 ]]; then
    echo "PASS: No secrets detected"
    exit 0
else
    echo ""
    echo "$FAIL secret pattern(s) found. Do not commit these files."
    exit 1
fi
