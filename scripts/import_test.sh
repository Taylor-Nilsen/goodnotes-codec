#!/bin/bash
# Import test for GoodNotes on macOS. Builds the feature test kit, imports
# each document into GoodNotes one at a time, collects GoodNotes' own log
# lines and crash reports after each import, and reports back.
#
#   curl -fsSL https://raw.githubusercontent.com/Taylor-Nilsen/goodnotes-codec/feat/full-format/scripts/import_test.sh | bash
#
# Installs nothing. Everything happens in a temporary folder that is deleted
# on exit. Uses only what macOS ships: bash, curl, tar, python3, open, log,
# pbcopy, git (for sending the report; optional).
set -u
BRANCH="${BRANCH:-feat/full-format}"
REPO="Taylor-Nilsen/goodnotes-codec"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/goodnotes-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
if ! command -v python3 >/dev/null; then echo "python3 not found (it comes with the Xcode command line tools)."; exit 1; fi
PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "python3 is $PYV; 3.9 or newer is needed."; exit 1; fi

say "Fetching $REPO@$BRANCH into a temp folder"
curl -fsSL "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz" | tar xz || { echo "download failed"; exit 1; }
cd "$WORK"/goodnotes-codec-* || exit 1
SRC="$PWD"

say "Building the test kit"
python3 -m goodnotes testkit "$WORK/kit" || { echo "building the kit failed"; exit 1; }

REPORT="$WORK/import-report.txt"
{
  echo "GoodNotes import test report"
  echo "date: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  echo "macOS: $(sw_vers -productVersion 2>/dev/null)  python: $PYV  branch: $BRANCH"
  echo "goodnotes version: $(defaults read /Applications/Goodnotes.app/Contents/Info.plist CFBundleShortVersionString 2>/dev/null || defaults read /Applications/GoodNotes.app/Contents/Info.plist CFBundleShortVersionString 2>/dev/null || echo unknown)"
  echo
} > "$REPORT"

open_in_goodnotes() {
  open -a Goodnotes "$1" 2>/dev/null || open -a GoodNotes "$1" 2>/dev/null || open "$1"
}

CRASHDIR="$HOME/Library/Logs/DiagnosticReports"
say "Importing the kit into GoodNotes, one document at a time."
echo "For each one: confirm the import dialog in GoodNotes, open the notebook, look at the page(s),"
echo "then come back here and answer. Answer 'ok', 'crash', or describe what is wrong."
for f in "$WORK"/kit/*.goodnotes; do
  name="$(basename "$f" .goodnotes)"
  start="$(date '+%Y-%m-%d %H:%M:%S')"
  before="$(ls -1 "$CRASHDIR" 2>/dev/null | grep -i goodnotes | sort)"
  say "[$name]"
  open_in_goodnotes "$f"
  printf 'Result for %s (ok / crash / what you saw): ' "$name"
  read -r verdict </dev/tty
  sleep 1
  {
    echo "=== $name: $verdict"
    # GoodNotes' own log lines since the import started (errors, faults, and its serializer messages)
    log show --start "$start" --predicate 'process CONTAINS[c] "goodnotes"' --style compact 2>/dev/null \
      | grep -iE 'error|fault|fatal|deserial|outline|unable|invalid|unexpected|corrupt|reject|failed' \
      | grep -viE 'CFNetwork|network|keychain|sandbox|NSURL|cloudkit|analytics|metrics' | head -40
    after="$(ls -1 "$CRASHDIR" 2>/dev/null | grep -i goodnotes | sort)"
    new="$(comm -13 <(echo "$before") <(echo "$after"))"
    for c in $new; do
      echo "--- crash report $c"
      # exception, termination reason and the crashed thread's first frames
      python3 - "$CRASHDIR/$c" <<'PY'
import json, sys
raw = open(sys.argv[1], encoding="utf-8", errors="replace").read()
lines = raw.split("\n", 1)
try:
    hdr = json.loads(lines[0]); body = json.loads(lines[1])
    print("app:", hdr.get("app_name"), hdr.get("app_version"), "os:", hdr.get("os_version"))
    print("exception:", body.get("exception"))
    print("termination:", body.get("termination"))
    print("asi:", body.get("asi"))
    thr = body.get("threads", [])
    idx = body.get("faultingThread", 0)
    imgs = body.get("usedImages", [])
    if thr:
        for fr in thr[idx].get("frames", [])[:25]:
            img = imgs[fr.get("imageIndex", 0)].get("name") if imgs else "?"
            print("  ", img, fr.get("symbol", ""), "+", fr.get("imageOffset", ""), fr.get("sourceFile", ""), fr.get("sourceLine", ""))
except Exception:
    print(raw[:4000])
PY
    done
    echo
  } >> "$REPORT"
done

say "Report"
cat "$REPORT"

# Send it back: a push to the PR branch reaches the Claude session watching it.
sent=0
if command -v git >/dev/null && git -C "$SRC" init -q 2>/dev/null; then
  cd "$SRC"
  mkdir -p reports && cp "$REPORT" "reports/import-report-$(date -u '+%Y%m%d-%H%M%S').txt"
  git remote add origin "https://github.com/$REPO.git" 2>/dev/null
  git fetch -q --depth 1 origin "$BRANCH" 2>/dev/null \
    && git checkout -q -b "$BRANCH" FETCH_HEAD 2>/dev/null \
    && git add reports \
    && git -c user.name="import-test" -c user.email="import-test@localhost" commit -q -m "Add GoodNotes import test report" \
    && GIT_TERMINAL_PROMPT=0 git push -q origin "$BRANCH" 2>/dev/null && sent=1
fi
if [ "$sent" = 1 ]; then
  say "Report pushed to $REPO@$BRANCH (reports/). Claude's session picks it up from there."
else
  pbcopy < "$REPORT" 2>/dev/null && say "Could not push (no git credentials). The report is in your clipboard: paste it into the chat."
fi
say "Cleanup: the temp folder is removed on exit. The imported notebooks stay in GoodNotes; trash these when done:"
ls "$WORK"/kit | sed 's/\.goodnotes$//; s/^/  /'
