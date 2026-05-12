#!/usr/bin/env bash
# check-build-no-secrets.sh — fail the build if a known secret
# token shape made it into the static bundle.
#
# Why this exists, plainly: anything prefixed `PUBLIC_*` in
# `import.meta.env.*` gets baked into the static JS at build
# time.  A future agent typing `import.meta.env.PUBLIC_META_CAPI_TOKEN`
# (instead of the correct server-only `META_CAPI_ACCESS_TOKEN`)
# would publish the access token to every browser visiting
# kiln3d.com.  Pre-commit hooks catch most flavors of this in
# source, but Astro's build can synthesize new strings via env
# substitution that the source doesn't literally contain.  This
# check sweeps the synthesized output as a backstop.
#
# Usage:
#   bash scripts/check-build-no-secrets.sh        # checks ./dist
#   bash scripts/check-build-no-secrets.sh /path  # checks /path
#
# Exits non-zero on any hit.  CI calls this after `astro build`.

set -eu

DIST="${1:-${BUILD_DIR:-dist}}"

if [ ! -d "$DIST" ]; then
  echo "✗ Build dir '$DIST' does not exist — run \`npm run build\` first." >&2
  exit 1
fi

# Patterns mirror scripts/install_pre_commit_secrets_hook.sh in
# kiln-pro.  Kept in sync manually because the repos are separate
# and we don't want a runtime dependency between them.
PATTERNS=(
  "EAA[A-Za-z0-9_-]{40,}"
  "ghp_[A-Za-z0-9]{36,}"
  "gho_[A-Za-z0-9]{36,}"
  "ghs_[A-Za-z0-9]{36,}"
  "ghr_[A-Za-z0-9]{36,}"
  "github_pat_[A-Za-z0-9_]{22,}"
  "AKIA[0-9A-Z]{16}"
  "sk_live_[A-Za-z0-9]{24,}"
  "rk_live_[A-Za-z0-9]{24,}"
  "sk-ant-[A-Za-z0-9_-]{40,}"
  "sk-proj-[A-Za-z0-9_-]{50,}"
  "sk-svcacct-[A-Za-z0-9_-]{40,}"
  "sb_secret_[A-Za-z0-9_-]{20,}"
  "xox[abrps]-[A-Za-z0-9-]{10,}"
  "-----BEGIN [A-Z ]*PRIVATE KEY-----"
)

# Files we expect to scan: HTML + JS + JSON + TXT inside dist/.
# Skip images / fonts / sourcemaps (those wouldn't carry inlined
# secrets unless something is very wrong).
violations=0
matches=$(
  for pattern in "${PATTERNS[@]}"; do
    grep -rEnH \
      --include='*.html' --include='*.js' --include='*.mjs' \
      --include='*.json' --include='*.txt' --include='*.xml' \
      --include='*.css' \
      "$pattern" "$DIST" 2>/dev/null || true
  done
)

if [ -n "$matches" ]; then
  echo "✗ Build-output secret check FAILED.  Hits found in $DIST:"
  echo ""
  echo "$matches" | head -20
  echo ""
  echo "  Server-only secrets must NEVER reach the marketing-site"
  echo "  build (Astro's PUBLIC_* env vars get baked into static JS"
  echo "  and shipped to every browser).  If the hit is a real leak,"
  echo "  rotate the secret immediately and remove it from the"
  echo "  emitting source file.  If it's a false positive (e.g. a"
  echo "  marketing-blog code sample), narrow the regex above."
  exit 1
fi

echo "✓ Build output clean — no secret token shapes in $DIST."
