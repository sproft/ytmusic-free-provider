#!/bin/sh
# Install the ytmusic_free provider into the running Music Assistant container.
#
# Portable across HAOS (BusyBox ash) and Supervised installs. Uses curl + tar
# instead of git so it runs on HAOS, where git is not available.
#
# Usage:
#   sh install_provider.sh [--force] [--repo-owner OWNER] [--ref REF] [--ma-id ID]
#                          [--python-version VER] [--config-dir DIR]
#                          [--no-restart] [--no-stage]
#
# What it does:
#   1. Downloads ytmusic_free/ from the repository at the requested ref.
#   2. Stages it at /config/custom_components/mass/providers/ytmusic_free
#      (persistent location used by the watcher add-on).
#   3. Copies it into the live MA container at
#      /app/venv/lib/<pythonX.Y>/site-packages/music_assistant/providers/.
#   4. Restarts the MA container so it picks up the new files.
#
# For automatic re-install across HA restarts, see WATCHER_ADDON.md or run
# install_watcher_addon.sh.

set -eu

REPO_OWNER="sproft"
REPO_NAME="ytmusic-free-provider"
PROVIDER_DIR="ytmusic_free"

# Empty means "resolve the newest published release". --ref overrides it with a
# branch, tag or commit. Installing a release rather than branch head is what
# makes an install reproducible and lets a bug report name a version. See #68.
REF=""
RELEASE_ONLY=0
FORCE=0
MA_ID=""
PYTHON_VERSION=""
CONFIG_DIR=""
NO_RESTART=0
NO_STAGE=0

log()  { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

# Print the newest published release tag, or fail if there is not one.
#
# Follows the redirect on the HTML /releases/latest rather than reading the JSON
# API, because that needs no jq: jq is not a dependency of this script and is
# absent from a stock HAOS BusyBox. GitHub excludes prereleases from "latest",
# which is what lets a release candidate be published without becoming every
# new install's default. When no non-prerelease exists the redirect lands on
# /releases with no /tag/ segment, and that is the "no releases yet" case.
latest_release_tag() {
    _resolved="$(curl -fsSL -o /dev/null -w '%{url_effective}' \
        "https://github.com/$REPO_OWNER/$REPO_NAME/releases/latest" 2>/dev/null)" \
        || return 1
    case "$_resolved" in
        */releases/tag/*) printf '%s\n' "${_resolved##*/releases/tag/}" ;;
        *) return 1 ;;
    esac
}

# Pick the ref to install when --ref was not given.
#
# Falls back to main rather than aborting. A brand-new repo with no release yet,
# a GitHub outage and a rate-limited runner all land here, and refusing to
# install at all would be a worse answer than installing branch head and saying
# so. The message is deliberately loud, because an install that silently means
# something different from what the docs promise is how support threads start.
resolve_ref() {
    if [ "$RELEASE_ONLY" = "1" ] && [ -n "$REF" ]; then
        die "--release-only cannot be combined with --ref; pin a reviewed tag explicitly without --release-only"
    fi
    [ -n "$REF" ] && return 0
    if REF="$(latest_release_tag)" && [ -n "$REF" ]; then
        log "Installing the latest release: $REF"
    else
        [ "$RELEASE_ONLY" = "0" ] || die "No stable release could be resolved; refusing to install unverified branch head"
        REF="main"
        log "WARN: could not resolve a published release (none yet, or GitHub"
        log "      unreachable). Falling back to branch head: $REF"
    fi
}

# 'docker' missing is the single most common install failure on HAOS, because
# the official Terminal & SSH add-on is sandboxed and cannot reach the host
# Docker daemon. Give an actionable message instead of a bare "not found".
need_docker() {
    command -v docker >/dev/null 2>&1 && return 0
    die "the 'docker' command was not found.

This installer must run from a shell that can reach the host Docker daemon,
because it copies the provider into the Music Assistant container. The official
'Terminal & SSH' add-on is sandboxed and does not provide Docker, which is the
usual cause of this error on Home Assistant OS (for example HA Green or Yellow).

Two ways to proceed:
  A) Install the 'Advanced SSH & Web Terminal' community add-on, set its
     'Protection mode' to OFF, restart it, then re-run this one-liner there.
  B) Skip Docker in your shell: run install_watcher_addon.sh instead, then
     install and start the 'Provider Watcher' local add-on (Protection mode
     OFF). It injects the provider for you and also survives HA restarts.

See the README 'Installation' section for the full walkthrough."
}

usage() {
    cat <<EOF
Usage: sh install_provider.sh [options]

Options:
  --force, -f               Skip overwrite prompts
  --repo-owner OWNER        Repository owner (default: sproft)
  --release-only            Require a stable release; never fall back to main
  --ref REF                 Git ref (branch/tag/commit) to download
                            (default: the newest published release; use
                            --ref main to track branch head instead)
  --ma-id ID                Music Assistant container ID (default: auto-detect)
  --python-version VER      MA Python version, e.g. python3.13 (default: auto-detect)
  --config-dir DIR          /config directory on the host
                            (default: auto-detect HAOS vs. Supervised)
  --no-restart              Skip the docker restart at the end
  --no-stage                Skip copying to /config/custom_components/mass/providers
  --help, -h                Show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --force|-f) FORCE=1 ;;
        --repo-owner) shift; REPO_OWNER="${1:-}" ;;
        --release-only) RELEASE_ONLY=1 ;;
        --ref) shift; REF="${1:-}" ;;
        --ma-id) shift; MA_ID="${1:-}" ;;
        --python-version) shift; PYTHON_VERSION="${1:-}" ;;
        --config-dir) shift; CONFIG_DIR="${1:-}" ;;
        --no-restart) NO_RESTART=1 ;;
        --no-stage) NO_STAGE=1 ;;
        --help|-h) usage; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
    shift || true
done

# URL of this script, used in re-run hints so they are copy-pasteable. Honors
# --repo-owner / --ref, and (crucially) shows the "sh -s --" pipe form: the
# documented install is "curl ... | sh", where a bare "--flag" is parsed by sh
# itself and fails with "sh: bad option".
# ${REF:-main} because REF is empty until resolve_ref runs, and these hints are
# printed by failures that happen before it does.
SCRIPT_URL="https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/${REF:-main}/scripts/install_provider.sh"

# --- Preflight ---------------------------------------------------------------

log "Preflight checks..."
need curl
need tar
need mkdir
need cp
need rm
need_docker

# --- Resolve the ref to install ----------------------------------------------
#
# After the preflight, because it needs curl, and before anything that reports
# what is about to be installed.
resolve_ref

# --- Detect MA container -----------------------------------------------------

# Supervisor renamed add-on containers from "addon_*" to "app_*", the same
# rename that moved local add-ons from addons/local to apps/local (issue #22).
# Both spellings are in the field depending on Supervisor version, so match
# either. Still anchored at both ends with an explicit channel-suffix list, so
# the watcher's own container ("..._ma_provider_watcher") cannot match and have
# the installer target itself. Issue #54.
MA_NAME_RE='^(addon|app)_[0-9a-f]+_music_assistant(_beta|_nightly|_dev)?$'

if [ -z "$MA_ID" ]; then
    MA_ID="$(docker ps --format '{{.Names}}' 2>/dev/null \
             | grep -E "$MA_NAME_RE" \
             | head -n1 || true)"
    if [ -n "$MA_ID" ]; then
        log "Detected MA container: $MA_ID"
    else
        # Auto-detect found nothing, usually because MA is stopped: docker ps
        # lists only running containers. Probe the well-known name in both
        # spellings rather than guessing one, so the fallback either names a
        # container that really is there or reports that it found none.
        for _cand in app_d5369777_music_assistant addon_d5369777_music_assistant; do
            if docker inspect "$_cand" >/dev/null 2>&1; then
                MA_ID="$_cand"
                log "No running MA container matched; using existing '$MA_ID'."
                break
            fi
        done
    fi
fi

if [ -z "$MA_ID" ]; then
    die "could not find the Music Assistant container (looked for names matching
  $MA_NAME_RE
and probed app_d5369777_music_assistant / addon_d5369777_music_assistant).
List what is actually there with: docker ps -a | grep music
then re-run with the right id, e.g.:
  curl -fsSL $SCRIPT_URL | sh -s -- --ma-id <ID>"
fi

# Confirm the container exists before we go further. Reachable for an explicit
# --ma-id, and for a detected one if it disappeared in between.
docker inspect "$MA_ID" >/dev/null 2>&1 \
    || die "MA container '$MA_ID' not found. List what is there with:
  docker ps -a | grep music
then re-run with the right name, e.g.:
  curl -fsSL $SCRIPT_URL | sh -s -- --ma-id <ID>"

# --- Detect Python version ---------------------------------------------------

if [ -z "$PYTHON_VERSION" ]; then
    PYTHON_VERSION="$(docker exec "$MA_ID" sh -c 'ls /app/venv/lib/ 2>/dev/null' \
                      | grep -E '^python3\.[0-9]+$' \
                      | head -n1 || true)"
    if [ -z "$PYTHON_VERSION" ]; then
        PYTHON_VERSION="python3.13"
        log "WARN: could not auto-detect Python version; using fallback '$PYTHON_VERSION'."
    else
        log "Detected MA Python version: $PYTHON_VERSION"
    fi
fi

DST_DIR="/app/venv/lib/$PYTHON_VERSION/site-packages/music_assistant/providers"

# --- Detect /config directory (for staging) ---------------------------------

if [ "$NO_STAGE" -ne 1 ] && [ -z "$CONFIG_DIR" ]; then
    # Two candidates was too few: the reporter in issue #54 got "could not
    # detect /config path" on a current HAOS and the install silently went
    # unstaged, which means the next MA add-on update wipes the provider. Probe
    # the same breadth install_watcher_addon.sh uses for the add-ons directory,
    # including /config itself, which is what this looks like from inside the
    # SSH or Terminal add-on.
    for _cand in \
        /config \
        /homeassistant \
        /mnt/data/supervisor/homeassistant \
        /usr/share/hassio/homeassistant \
        /var/lib/homeassistant/homeassistant \
        /data/homeassistant
    do
        [ -d "$_cand" ] || continue
        CONFIG_DIR="$_cand"
        log "Detected config path: $CONFIG_DIR"
        break
    done
    if [ -z "$CONFIG_DIR" ]; then
        log "WARN: could not detect the /config path; skipping the staging step."
        log "      The provider is installed, but staging is what lets it survive"
        log "      a Music Assistant add-on update, so without it the next MA"
        log "      update removes it again."
        log "      Probed: /config /homeassistant /mnt/data/supervisor/homeassistant"
        log "              /usr/share/hassio/homeassistant"
        log "              /var/lib/homeassistant/homeassistant /data/homeassistant"
        log "      Re-run with --config-dir DIR to enable staging."
        NO_STAGE=1
    fi
fi

# --- Download repo tarball --------------------------------------------------

TMPDIR="$(mktemp -d 2>/dev/null || mktemp -d -t mip)"
trap 'rm -rf "$TMPDIR"' EXIT INT TERM

# Bare ref rather than refs/heads/$REF: this form resolves a branch, a tag and a
# full commit sha identically, which is what --ref has claimed to accept all
# along. refs/heads/ 404s on every tag.
TARBALL_URL="https://codeload.github.com/$REPO_OWNER/$REPO_NAME/tar.gz/$REF"
log "Downloading $TARBALL_URL"
curl -fsSL "$TARBALL_URL" -o "$TMPDIR/repo.tar.gz" \
    || die "download failed (check --ref or your network)"

log "Extracting..."
tar -xzf "$TMPDIR/repo.tar.gz" -C "$TMPDIR" \
    || die "extraction failed (corrupt archive?)"

# Discover the extracted directory rather than computing it. It is normally
# "<repo>-<ref>", but GitHub strips a leading "v" from tag names, so a tag like
# v1.0.0 extracts to "<repo>-1.0.0" and the computed guess misses by one
# character. Looking for the directory that actually contains the provider
# makes branch, tag and commit behave the same.
SRC_ROOT=""
for _candidate in "$TMPDIR"/*/; do
    if [ -d "$_candidate$PROVIDER_DIR" ]; then
        SRC_ROOT="${_candidate%/}"
        break
    fi
done
if [ -z "$SRC_ROOT" ] || [ ! -d "$SRC_ROOT/$PROVIDER_DIR" ]; then
    die "$PROVIDER_DIR/ not found in the archive downloaded from $TARBALL_URL"
fi

# --- Stage to /config -------------------------------------------------------

if [ "$NO_STAGE" -ne 1 ]; then
    STAGE_DIR="$CONFIG_DIR/custom_components/mass/providers"
    STAGE_TARGET="$STAGE_DIR/$PROVIDER_DIR"

    if [ -e "$STAGE_TARGET" ]; then
        if [ "$FORCE" -ne 1 ]; then
            printf '%s already exists. Overwrite? [y/N] ' "$STAGE_TARGET"
            read -r reply
            case "$reply" in
                y|Y|yes|YES) ;;
                *) die "aborted by user (use --force to skip this prompt)" ;;
            esac
        fi
        log "Removing existing $STAGE_TARGET"
        rm -rf "$STAGE_TARGET"
    fi

    log "Staging to $STAGE_TARGET"
    mkdir -p "$STAGE_DIR"
    cp -R "$SRC_ROOT/$PROVIDER_DIR" "$STAGE_TARGET"
fi

# --- Copy into MA container -------------------------------------------------

log "Copying provider into $MA_ID:$DST_DIR/"
# Remove any stale copy inside the container so docker cp doesn't merge into it.
docker exec "$MA_ID" rm -rf "$DST_DIR/$PROVIDER_DIR" 2>/dev/null || true

docker cp "$SRC_ROOT/$PROVIDER_DIR" "$MA_ID:$DST_DIR/" \
    || die "docker cp failed. Is the MA container running?"
log "Provider files copied OK"

# --- Restart MA -------------------------------------------------------------

if [ "$NO_RESTART" -ne 1 ]; then
    log "Restarting $MA_ID..."
    docker restart "$MA_ID" >/dev/null \
        || die "docker restart failed"
    log "MA restarted. It may take ~10s to come back up."
else
    log "Skipping restart (--no-restart). Run 'docker restart $MA_ID' to apply."
fi

# --- Done -------------------------------------------------------------------

cat <<EOF

Install complete.

Next steps:
  1. Open Music Assistant -> Settings -> Music sources -> Add
     and select "YouTube Music (Free)".
  2. (Optional) For library sync, set Authentication to "Browser cookie"
     and paste your music.youtube.com cookie. See README.md -> Authentication.
  3. To survive Home Assistant restarts, install the watcher add-on:
     sh install_watcher_addon.sh

Re-run anytime to upgrade (pass --force so the overwrite prompt does not stall a
curl-pipe run). If autodetect picked wrong values, override with:
  curl -fsSL $SCRIPT_URL | sh -s -- --force --ma-id <ID> --python-version <pythonX.Y>
EOF
