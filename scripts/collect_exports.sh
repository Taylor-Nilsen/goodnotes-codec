#!/bin/bash
# Sends GoodNotes' re-exports of the test-kit notebooks back to the PR
# branch, so the import can be checked against what GoodNotes kept.
#
#   In GoodNotes: select the notebooks 01_blank ... 10_audio_note,
#   Export > Goodnotes format, save to a folder. Then:
#
#   curl -fsSL https://raw.githubusercontent.com/Taylor-Nilsen/goodnotes-codec/feat/full-format/scripts/collect_exports.sh | bash
#   (or: ... | bash -s /path/to/that/folder)
#
# Installs nothing; works in a temp folder deleted on exit. Looks in the
# given folder, else in ~/Downloads, ~/Desktop and ~/Documents, for files
# named like the kit (01_..., 10_...) exported in the last day.
set -u
BRANCH="${BRANCH:-feat/full-format}"
REPO="Taylor-Nilsen/goodnotes-codec"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/goodnotes-export.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

if [ $# -ge 1 ]; then DIRS=("$1"); else DIRS=("$HOME/Downloads" "$HOME/Desktop" "$HOME/Documents"); fi
mkdir -p "$WORK/exports"
found=0
for d in "${DIRS[@]}"; do
  [ -d "$d" ] || continue
  while IFS= read -r f; do
    base="$(basename "$f")"
    cp "$f" "$WORK/exports/$base" && found=$((found + 1))
  done < <(find "$d" -maxdepth 3 -type f -name '*.goodnotes' -mtime -1 2>/dev/null | grep -E '/(0[1-9]|10)_[A-Za-z_]+.*\.goodnotes$')
done
# GoodNotes may export a single zip when several notebooks are selected
for d in "${DIRS[@]}"; do
  [ -d "$d" ] || continue
  while IFS= read -r z; do
    if unzip -l "$z" 2>/dev/null | grep -qE '(0[1-9]|10)_[A-Za-z_]+.*\.goodnotes'; then
      unzip -q -o -j "$z" '*.goodnotes' -d "$WORK/exports" && found=$((found + 1))
    fi
  done < <(find "$d" -maxdepth 2 -type f -name '*.zip' -mtime -1 2>/dev/null)
done
if [ "$found" = 0 ]; then
  echo "No exported kit notebooks found. Export them from GoodNotes (Goodnotes format) first,"
  echo "then run this with the folder as an argument:  ... | bash -s /path/to/folder"
  exit 1
fi
say "Found:"; ls -la "$WORK/exports"

sent=0
if command -v git >/dev/null; then
  cd "$WORK" && git init -q repo && cd repo \
    && git remote add origin "https://github.com/$REPO.git" \
    && GIT_TERMINAL_PROMPT=0 git fetch -q --depth 1 origin "$BRANCH" 2>/dev/null \
    && git checkout -q -b "$BRANCH" FETCH_HEAD \
    && mkdir -p reports/exports && cp "$WORK"/exports/*.goodnotes reports/exports/ \
    && git add -f reports/exports \
    && git -c user.name="import-test" -c user.email="import-test@localhost" commit -q -m "Add GoodNotes re-exports of the test kit" \
    && GIT_TERMINAL_PROMPT=0 git push -q origin "$BRANCH" 2>/dev/null && sent=1
fi
if [ "$sent" = 1 ]; then
  say "Exports pushed to $REPO@$BRANCH (reports/exports/). Claude's session picks them up from there."
else
  out="$HOME/Desktop/goodnotes-exports.zip"
  (cd "$WORK/exports" && zip -q -r "$out" .) && say "Could not push (no git credentials). Attach $out in the chat instead."
fi
