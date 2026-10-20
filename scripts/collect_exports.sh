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
PAT='/(0[1-9]|1[0-9])[a-z]?_[A-Za-z0-9_]+.*\.goodnotes$'
# oldest first, so when the same notebook was exported twice the newest copy wins
list_candidates() {
  for d in "${DIRS[@]}"; do
    [ -d "$d" ] || continue
    find "$d" -maxdepth 3 -type f \( -name '*.goodnotes' -o -name '*.zip' \) -mtime -2 2>/dev/null
  done | while IFS= read -r f; do printf '%s\t%s\n' "$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f")" "$f"; done | sort -n | cut -f2-
}
while IFS= read -r f; do
  case "$f" in
    *.goodnotes)
      if echo "$f" | grep -qE "$PAT"; then cp "$f" "$WORK/exports/$(basename "$f")" && found=$((found + 1)); fi ;;
    *.zip)  # GoodNotes exports several selected notebooks as one zip
      if unzip -l "$f" 2>/dev/null | grep -qE '(0[1-9]|1[0-9])[a-z]?_[A-Za-z0-9_]+.*\.goodnotes'; then
        unzip -q -o -j "$f" '*.goodnotes' -d "$WORK/exports" && found=$((found + 1))
      fi ;;
  esac
done < <(list_candidates)
# drop anything from a zip that is not a kit notebook
for f in "$WORK"/exports/*.goodnotes; do
  echo "$f" | grep -qE "$PAT" || rm -f "$f"
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
