#!/usr/bin/env bash
# codex-review: run the local Codex CLI as a reviewer.
#
# Two modes:
#   * SESSION-BOUND (default): a standing reviewer for the current repo, bound to
#     the current Claude Code session. The first call bootstraps a Codex
#     conversation (it reads AGENTS.md/CLAUDE.md once and learns the codebase);
#     every later call RESUMES that same conversation, so you never re-send the
#     project background and Codex keeps the code + prior rounds in context.
#   * STANDALONE (--standalone): a one-off, ephemeral review with NO session
#     binding and NO persistence — for review tasks UNRELATED to the current
#     session's main work (e.g. reviewing a different project, or this skill
#     itself). Always a fresh Codex thread; nothing is stored.
#
# Usage:
#   review.sh [--standalone] [--new] [--base <ref>] [free-text focus ...]
#
#   (no args)        Review uncommitted changes; resume the session's Codex
#                    thread if one exists, else bootstrap a new one.
#   --standalone     One-off ephemeral review, not tied to the Claude session.
#   --new            Force a fresh session-bound thread (re-bootstrap).
#   --base <ref>     Review this branch vs <ref> instead of uncommitted changes.
#   free-text        Extra focus, e.g. "check the capture invariant" — or, in a
#                    non-git directory, what to review (Codex reads the files).
#
# Codex binary discovery (first hit wins): $CODEX_BIN, then `codex` on PATH, then
# common install locations. Set CODEX_BIN to override anywhere.
#
# Version note: depends only on Codex's stable `exec` / `exec resume` interface
# (`--json`, `-o`, `-s`, `--skip-git-repo-check`). The only schema assumption is
# the session id in the `--json` stream — captured leniently
# (thread_id|session_id|conversation_id). If `resume` ever fails, it falls back
# to a fresh bootstrap, so a Codex upgrade can't hard-break this.
set -uo pipefail

# ---- resolve the codex binary (no hardcoded version) -------------------------
find_codex() {
  if [ -n "${CODEX_BIN:-}" ] && [ -x "${CODEX_BIN}" ]; then echo "$CODEX_BIN"; return; fi
  local p; p="$(command -v codex 2>/dev/null || true)"
  if [ -n "$p" ]; then echo "$p"; return; fi
  local c
  for c in \
    "$HOME/.codex/packages/standalone/current/codex" \
    "$HOME/.local/bin/codex" \
    "/opt/homebrew/bin/codex" \
    "/usr/local/bin/codex" \
    "/Applications/Codex.app/Contents/Resources/codex"; do
    [ -x "$c" ] && { echo "$c"; return; }
  done
}
CODEX="$(find_codex)"
if [ -z "$CODEX" ]; then
  echo "[codex-review] ERROR: codex CLI not found. Put it on PATH or set CODEX_BIN=/path/to/codex." >&2
  exit 127
fi

# A captured/stored thread id is fed back to `codex exec resume <id>`. Reject
# anything that isn't a plain token (no leading '-', no shell/option-unsafe
# chars) so a corrupt state file can't smuggle an option or injection.
is_safe_id() {
  case "$1" in
    ''|-*|*[!A-Za-z0-9._-]*) return 1 ;;
    *) return 0 ;;
  esac
}

# ---- parse args --------------------------------------------------------------
STANDALONE=0
FORCE_NEW=0
BASE=""
FOCUS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --standalone) STANDALONE=1; shift ;;
    --new)        FORCE_NEW=1; shift ;;
    --base)
      if [ $# -lt 2 ] || [ -z "$2" ]; then
        echo "[codex-review] ERROR: --base needs a <ref> argument." >&2; exit 2
      fi
      BASE="$2"; shift 2 ;;
    *)            FOCUS="${FOCUS:+$FOCUS }$1"; shift ;;
  esac
done

# ---- locate the working root + detect git ------------------------------------
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT" || exit 1
IS_GIT=0; git rev-parse --is-inside-work-tree >/dev/null 2>&1 && IS_GIT=1

# The base ref is rendered into a command string the reviewer may run, so it must
# not carry shell metacharacters, and must be a real revision.
if [ -n "$BASE" ]; then
  if [ "$IS_GIT" -eq 0 ]; then
    echo "[codex-review] ERROR: --base only applies inside a git repository." >&2; exit 2
  fi
  case "$BASE" in
    -*|*[!A-Za-z0-9._/-]*)
      echo "[codex-review] ERROR: --base ref '$BASE' has unsafe characters." >&2; exit 2 ;;
  esac
  if ! git rev-parse --verify --quiet "$BASE^{commit}" >/dev/null 2>&1; then
    echo "[codex-review] ERROR: --base ref '$BASE' is not a valid git revision." >&2; exit 2
  fi
fi

# ---- session binding (skipped in standalone) ---------------------------------
SESSION="${CLAUDE_CODE_SESSION_ID:-local}"
STATE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/claude-codex-review"
KEY="$(printf '%s::%s' "$SESSION" "$ROOT" | cksum | cut -d' ' -f1)"
STATE_FILE="$STATE_DIR/$KEY.thread"

TID=""
if [ "$STANDALONE" -eq 0 ] && [ "$FORCE_NEW" -eq 0 ] && [ -f "$STATE_FILE" ]; then
  TID="$(cat "$STATE_FILE" 2>/dev/null)"
  if ! is_safe_id "$TID"; then
    echo "[codex-review] WARN: ignoring malformed stored thread id." >&2
    TID=""
  fi
fi

# ---- build the review scope + output contract --------------------------------
if [ "$IS_GIT" -eq 1 ] && [ -n "$BASE" ]; then
  SCOPE="Review the changes on the current branch versus '$BASE'. Run: \`git --no-pager diff $BASE...HEAD\`, and \`git --no-pager diff HEAD\` for anything uncommitted."
elif [ "$IS_GIT" -eq 1 ]; then
  SCOPE="Review ALL uncommitted changes. Run: \`git --no-pager diff HEAD\` for tracked changes, and \`git status --porcelain\` to find untracked files — read each untracked file yourself."
else
  SCOPE="This is NOT a git repository. Review the files under $ROOT that are relevant to the request below; read them yourself."
fi
[ -n "$FOCUS" ] && SCOPE="$SCOPE Focus from the requester: $FOCUS"

CONTRACT='End with a clear verdict line "**Verdict: GO**" or "**Verdict: NO-GO**". List must-fix issues (with file:line) separately from optional nits. Be concise; do not paste the whole diff/files back.'

# ---- assemble the prompt -----------------------------------------------------
if [ "$STANDALONE" -eq 1 ]; then
  PROMPT="Independent one-off code review (no prior context). Orient yourself by reading any AGENTS.md/CLAUDE.md/README present if helpful.

$SCOPE

$CONTRACT"
elif [ -z "$TID" ]; then
  PROMPT="You are the standing code reviewer for the repository at $ROOT, across this whole session — I will come back to you for several rounds, so remember the codebase and your earlier findings.

First, orient yourself ONCE: read AGENTS.md and/or CLAUDE.md if they exist (architecture + any hard invariants / DO-NOT-BREAK rules). Check every future diff against them.

Now do this round: $SCOPE

$CONTRACT"
else
  PROMPT="New review round, same repository ($ROOT) — you already know the codebase and your earlier findings; don't re-read everything, just look at what changed.

$SCOPE

Also confirm the project's hard invariants (from AGENTS.md/CLAUDE.md) are not regressed. $CONTRACT"
fi

# ---- run codex ---------------------------------------------------------------
JSONL="$(mktemp)"; VERDICT="$(mktemp)"
trap 'rm -f "$JSONL" "$VERDICT"' EXIT

run_bootstrap() {
  local extra=()
  [ "$STANDALONE" -eq 1 ] && extra+=(--ephemeral)
  "$CODEX" exec --json -s read-only --skip-git-repo-check "${extra[@]}" \
    -o "$VERDICT" "$PROMPT" < /dev/null > "$JSONL" 2>&1
}
run_resume() {
  # `exec resume` has no -s flag, so enforce read-only via config override
  # (otherwise a resumed review could inherit a writable default sandbox).
  "$CODEX" exec resume "$TID" --json --skip-git-repo-check \
    -c sandbox_mode="read-only" \
    -o "$VERDICT" "$PROMPT" < /dev/null > "$JSONL" 2>&1
}

MODE=""; RC=0
if [ "$STANDALONE" -eq 1 ]; then
  MODE="standalone"; run_bootstrap || RC=$?
elif [ -n "$TID" ]; then
  MODE="resume"
  if ! run_resume; then
    echo "[codex-review] resume of $TID failed; bootstrapping a new thread." >&2
    TID=""; MODE="bootstrap"; run_bootstrap || RC=$?
  fi
else
  MODE="bootstrap"; run_bootstrap || RC=$?
fi

# ---- capture / persist the thread id (session-bound bootstrap only) ----------
# Parse leniently: prefer jq, fall back to a whitespace-tolerant grep that
# accepts any string id (not just compact UUIDs), so a JSON-format change in a
# future Codex doesn't silently break persistence.
extract_thread_id() {
  if command -v jq >/dev/null 2>&1; then
    jq -r 'select(type=="object")
           | (.thread_id // .session_id // .conversation_id // .thread.id? // empty)' \
       "$JSONL" 2>/dev/null | head -1
    return
  fi
  grep -oE '"(thread_id|session_id|conversation_id)"[[:space:]]*:[[:space:]]*"[^"]+"' "$JSONL" \
    | head -1 | sed -E 's/.*:[[:space:]]*"([^"]+)".*/\1/'
}
if [ "$MODE" = "bootstrap" ]; then
  NEWID="$(extract_thread_id)"
  if is_safe_id "$NEWID"; then
    if mkdir -p "$STATE_DIR" 2>/dev/null && printf '%s' "$NEWID" > "$STATE_FILE" 2>/dev/null; then
      TID="$NEWID"
    else
      echo "[codex-review] WARN: could not persist thread id ($NEWID) to $STATE_FILE; next run won't resume." >&2
      TID="(unsaved:$NEWID)"
    fi
  fi
fi

# ---- emit result -------------------------------------------------------------
echo "[codex-review] mode=$MODE thread=${TID:-n/a} root=$ROOT session=$SESSION codex=$CODEX"
echo "-----"
if [ -s "$VERDICT" ]; then
  cat "$VERDICT"
  [ "$RC" -ne 0 ] && echo "[codex-review] WARN: codex exited $RC despite producing a message." >&2
  exit "$RC"
else
  echo "[codex-review] codex produced no final message (exit $RC). Raw tail:" >&2
  tail -40 "$JSONL" >&2
  [ "$RC" -ne 0 ] && exit "$RC"
  exit 1
fi
