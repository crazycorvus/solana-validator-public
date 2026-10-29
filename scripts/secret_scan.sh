#!/usr/bin/env sh
# Pre-commit secret scan: fails on 64-number JSON arrays (solana keypairs) or PEM private keys in staged files.
# This script is excluded from its own scan (it contains the patterns).
hits=$(git diff --cached --name-only -z --diff-filter=ACM -- . ':(exclude)scripts/secret_scan.sh' \
  | xargs -0 grep -InE '\[( *[0-9]{1,3} *,){63} *[0-9]{1,3} *\]|PRIVATE KEY' 2>/dev/null)
if [ -n "$hits" ]; then
  echo "$hits"; echo "possible secret staged"; exit 1
fi
exit 0
