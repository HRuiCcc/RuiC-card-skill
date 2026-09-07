#!/usr/bin/env bash
# Rebuild app.bundle.js after editing app.js / icons.data.js.
# The page must be served as a SINGLE js file: per-module URLs under
# node_modules/ (e.g. .../fingerprint.js) are blocked by ad blockers.
cd "$(dirname "$0")"
BUN="${BUN:-bun}"
"$BUN" build ./app.js --outfile=./app.bundle.js --target=browser
