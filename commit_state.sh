#!/usr/bin/env bash
# Commit watcher state back to the repo (used by the GitHub Actions runner).
set -e
git config user.name "train-watch-bot"
git config user.email "train-watch-bot@users.noreply.github.com"
git add state.json alerts.log
git diff --cached --quiet && exit 0
git commit -q -m "state $(date -u +%FT%TZ)"
git pull -q --rebase origin main || true
git push -q origin main
