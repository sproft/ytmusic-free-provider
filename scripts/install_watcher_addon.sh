#!/bin/sh
# Install the Provider Watcher add-on for the ytmusic_free provider.
#
# Portable across HAOS (BusyBox ash) and Supervised installs. Uses curl + tar
# instead of git so it runs on HAOS, where git is not available.
#
# Usage:
#   sh install_watcher_addon.sh [--force] [--repo-owner OWNER] [--ref REF] [--ma-id ID]
#                               [--python-version VER] [--addons-dir DIR]
#
# See WATCHER_ADDON.md for the underlying manual procedure.

set -eu

REPO_OWNER="sproft"
REPO_NAME="ytmusic-free-provider"
ADDON_SLUG="ma_provider_watcher"
ADDON_NAME="Provider Watcher"
# The add-on's own version line, deliberately NOT the provider's.
#
# It cannot track the provider version, because it has to be strictly greater
# than what existing installs already carry and those carry "1.0.<14-digit
# timestamp>" from the scheme this replaces. Home Assistant orders 1.0.0.x BELOW
# 1.0.20260813112233 -- it compares the third section, 0 against a 14-digit
# number -- so anything on the 1.x line would strand every current user on a
# version they can never update from. Starting a 2.x line clears that.
#
# The provider version is what people actually need in a bug report, so it is
# reported separately: in the add-on description below, and by the watcher on
# every start. Issue #68.
ADDON_VERSION_LINE="2.0.0"

# Still timestamped, so the version is strictly greater on every run and Home
# Assistant rebuilds the image. Without that, re-running the installer to fix
# the Python version or MA ID silently keeps the stale cached run.sh (issue
# #22). A content hash would avoid the occasional update prompt for identical
# code, but hashes have no order, and Home Assistant's update entity needs a
# strictly greater version to enable the Install button. A missed rebuild is
# the more expensive failure, so the timestamp stays.
ADDON_VERSION="$ADDON_VERSION_LINE.$(date +%Y%m%d%H%M%S)"

# Empty means "resolve the newest published release". --ref pins to a branch,
# tag or commit, and pinning also stops auto-update from following releases.
REF=""
RELEASE_ONLY=0
FORCE=0
MA_ID=""
PYTHON_VERSION=""
ADDONS_DIR=""

log()  { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

# Print the newest published release tag, or fail if there is not one.
#
# Follows the redirect on the HTML /releases/latest rather than reading the JSON
# API, because that needs no jq: jq is not a dependency of this script and is
# absent from a stock HAOS BusyBox. GitHub excludes prereleases from "latest",
# so publishing a release candidate does not change what new installs get. When
# no non-prerelease exists the redirect lands on /releases with no /tag/
# segment, which is the "no releases yet" case.
latest_release_tag() {
    _resolved="$(curl -fsSL -o /dev/null -w '%{url_effective}' \
        "https://github.com/$REPO_OWNER/$REPO_NAME/releases/latest" 2>/dev/null)" \
        || return 1
    case "$_resolved" in
        */releases/tag/*) printf '%s\n' "${_resolved##*/releases/tag/}" ;;
        *) return 1 ;;
    esac
}

# Pick the ref to install when --ref was not given, and record whether the
# add-on should keep following releases afterwards.
#
# Falls back to main rather than aborting: no release yet, a GitHub outage and a
# rate-limited host all land here, and refusing to install would be a worse
# answer than installing branch head and saying so loudly. TRACK_RELEASES stays
# 1 in that case, so a watcher installed during an outage starts following
# releases as soon as it can reach GitHub again.
TRACK_RELEASES=0
resolve_ref() {
    if [ "$RELEASE_ONLY" = "1" ] && [ -n "$REF" ]; then
        die "--release-only cannot be combined with --ref; pin a reviewed tag explicitly without --release-only"
    fi
    if [ -n "$REF" ]; then
        log "Pinned to --ref $REF; auto-update will follow it rather than releases."
        return 0
    fi
    TRACK_RELEASES=1
    if REF="$(latest_release_tag)" && [ -n "$REF" ]; then
        log "Installing the latest release: $REF"
    else
        [ "$RELEASE_ONLY" = "0" ] || die "No stable release could be resolved; refusing to install unverified branch head"
        REF="main"
        log "WARN: could not resolve a published release (none yet, or GitHub"
        log "      unreachable). Falling back to branch head: $REF"
    fi
}

# A candidate is only usable if it is a directory we can actually write into.
# Some add-ons mount the add-ons share read-only (or owned by another uid), so
# [ -d ] alone would pick a dir the install then fails to write -- probe for real.
writable_dir() {
    [ -d "$1" ] || return 1
    _probe="$1/.maw_write_test.$$"
    if ( : > "$_probe" ) 2>/dev/null; then
        rm -f "$_probe" 2>/dev/null
        return 0
    fi
    return 1
}

usage() {
    cat <<EOF
Usage: sh install_watcher_addon.sh [options]

Options:
  --force, -f               Overwrite existing add-on directory without prompting
  --repo-owner OWNER        Repository owner (default: sproft)
  --release-only            Require a stable release; never fall back to main
  --ref REF                 Pin a branch/tag/commit (default: latest stable release)
  --ma-id ID                Music Assistant container ID (default: auto-detect)
  --python-version VER      MA Python version, e.g. python3.13 (default: auto-detect)
  --addons-dir DIR          Local add-ons directory (default: auto-detect HAOS vs. Supervised)
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
        --addons-dir) shift; ADDONS_DIR="${1:-}" ;;
        --help|-h) usage; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
    shift || true
done

# URL of this script, used in re-run hints (including the one baked into the
# generated run.sh) so they are copy-pasteable. Honors --repo-owner / --ref, and
# shows the "sh -s --" pipe form: the documented install is "curl ... | sh",
# where a bare "--flag" is parsed by sh itself and fails with "sh: bad option".
SCRIPT_URL="https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/$REF/scripts/install_watcher_addon.sh"

# --- Preflight ---------------------------------------------------------------

log "Preflight checks..."
need curl
need tar
need mkdir
need cp
need rm

# --- Resolve the ref to install ----------------------------------------------
#
# After the preflight, because it needs curl.
resolve_ref

# --- Detect add-ons directory -----------------------------------------------

if [ -z "$ADDONS_DIR" ]; then
    # Probe known local add-ons locations, most-standard first. Notes:
    #  - Inside the SSH / Samba / Terminal add-on (the common case) the local
    #    repo is mapped to /addons; host paths below are invisible there.
    #  - HA renamed the Supervisor "addons" tree to "apps" (HAOS 18+, mirroring
    #    `ha apps` replacing the deprecated `ha addons`), so the modern layout is
    #    .../apps/local while older installs still use .../addons/local.
    #  - Supervised reads its data share from /etc/hassio.json ("data" key);
    #    the default moved from /usr/share/hassio to /var/lib/homeassistant.
    # /root/addons (a prior fallback) is intentionally dropped: it is not used by
    # any supported install type.
    _data_share=""
    if [ -r /etc/hassio.json ]; then
        _data_share="$(sed -n 's/.*"data" *: *"\([^"]*\)".*/\1/p' \
                       /etc/hassio.json 2>/dev/null | head -n1)"
    fi
    for _cand in \
        /addons \
        /addons/local \
        /data/apps/local \
        /data/addons/local \
        /mnt/data/supervisor/apps/local \
        /mnt/data/supervisor/addons/local \
        ${_data_share:+"$_data_share/apps/local" "$_data_share/addons/local"} \
        /var/lib/homeassistant/apps/local \
        /var/lib/homeassistant/addons/local \
        /usr/share/hassio/apps/local \
        /usr/share/hassio/addons/local
    do
        [ -d "$_cand" ] || continue
        if writable_dir "$_cand"; then
            ADDONS_DIR="$_cand"
            log "Detected local add-ons path: $ADDONS_DIR"
            break
        fi
        log "WARN: $_cand exists but is not writable; skipping."
    done
    if [ -z "$ADDONS_DIR" ]; then
        die "could not find a writable local add-ons directory (probed /addons, /data/{apps,addons}/local, /mnt/data/supervisor/{apps,addons}/local, /var/lib/homeassistant/{apps,addons}/local, /usr/share/hassio/{apps,addons}/local). Pass --addons-dir explicitly. Inside the SSH/Samba add-on use --addons-dir /addons; on the HAOS host console use --addons-dir /mnt/data/supervisor/apps/local. Re-run e.g.: curl -fsSL $SCRIPT_URL | sh -s -- --addons-dir /addons"
    fi
else
    [ -d "$ADDONS_DIR" ] || die "add-ons directory does not exist: $ADDONS_DIR"
fi

ADDON_DIR="$ADDONS_DIR/$ADDON_SLUG"

# --- Detect MA container & Python version (best effort) ---------------------

# Kept byte-identical to install_provider.sh on purpose: both scripts target
# the same container, and the two copies drifting is what made issue #22's
# addons/local -> apps/local rename get fixed in one script and missed in the
# other. See the comment there for why both prefixes are matched. Issue #54.
MA_NAME_RE='^(addon|app)_[0-9a-f]+_music_assistant(_beta|_nightly|_dev)?$'

if [ -z "$MA_ID" ]; then
    if command -v docker >/dev/null 2>&1; then
        MA_ID="$(docker ps --format '{{.Names}}' 2>/dev/null \
                 | grep -E "$MA_NAME_RE" \
                 | head -n1 || true)"
        if [ -z "$MA_ID" ]; then
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
        # Unlike install_provider.sh this cannot be fatal: the watcher is often
        # installed from a shell with no Docker access at all, and the generated
        # run.sh re-detects at runtime anyway, where it does have the socket.
        MA_ID="app_d5369777_music_assistant"
        log "WARN: could not auto-detect MA container; baking in '$MA_ID'."
        log "      The watcher re-detects at runtime if that name is not there,"
        log "      so this is usually harmless. To pin it explicitly:"
        log "        curl -fsSL $SCRIPT_URL | sh -s -- --ma-id <ID>"
    else
        log "Detected MA container: $MA_ID"
    fi
fi

if [ -z "$PYTHON_VERSION" ]; then
    if command -v docker >/dev/null 2>&1 && [ -n "$MA_ID" ]; then
        PYTHON_VERSION="$(docker exec "$MA_ID" sh -c 'ls /app/venv/lib/ 2>/dev/null' \
                          | grep -E '^python3\.[0-9]+$' \
                          | head -n1 || true)"
    fi
    if [ -z "$PYTHON_VERSION" ]; then
        PYTHON_VERSION="python3.13"
        log "WARN: could not auto-detect Python version; using fallback '$PYTHON_VERSION'."
    else
        log "Detected MA Python version: $PYTHON_VERSION"
    fi
fi

# --- Idempotency check ------------------------------------------------------

if [ -e "$ADDON_DIR" ]; then
    if [ "$FORCE" -ne 1 ]; then
        printf '%s already exists. Overwrite? [y/N] ' "$ADDON_DIR"
        read -r reply
        case "$reply" in
            y|Y|yes|YES) ;;
            *) die "aborted by user (use --force to skip this prompt)" ;;
        esac
    fi
    log "Removing existing $ADDON_DIR"
    rm -rf "$ADDON_DIR"
fi

# --- Download repo tarball --------------------------------------------------

TMPDIR="$(mktemp -d 2>/dev/null || mktemp -d -t maw)"
trap 'rm -rf "$TMPDIR"' EXIT INT TERM

# Bare ref rather than refs/heads/$REF: this form resolves a branch, a tag and a
# full commit sha identically. refs/heads/ 404s on every tag.
TARBALL_URL="https://codeload.github.com/$REPO_OWNER/$REPO_NAME/tar.gz/$REF"
log "Downloading $TARBALL_URL"
curl -fsSL "$TARBALL_URL" -o "$TMPDIR/repo.tar.gz" \
    || die "download failed (check --ref or your network)"

log "Extracting..."
tar -xzf "$TMPDIR/repo.tar.gz" -C "$TMPDIR" \
    || die "extraction failed (corrupt archive?)"

# Discover the extracted directory rather than computing it. It is normally
# "<repo>-<ref>", but GitHub strips a leading "v" from tag names, so v1.0.0
# extracts to "<repo>-1.0.0" and the computed guess misses by one character.
# Looking for the directory that actually holds the provider makes branch, tag
# and commit behave the same.
SRC_ROOT=""
for _candidate in "$TMPDIR"/*/; do
    if [ -d "$_candidate""ytmusic_free" ]; then
        SRC_ROOT="${_candidate%/}"
        break
    fi
done
if [ -z "$SRC_ROOT" ] || [ ! -d "$SRC_ROOT/ytmusic_free" ]; then
    die "ytmusic_free/ not found in the archive downloaded from $TARBALL_URL"
fi

# The number a bug report needs. The add-on carries its own version line (see
# ADDON_VERSION_LINE above), so this is surfaced in the description instead.
PROVIDER_VERSION="$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' \
    "$SRC_ROOT/ytmusic_free/__init__.py" 2>/dev/null | head -n1)"
[ -n "$PROVIDER_VERSION" ] || PROVIDER_VERSION="unknown"
log "Bundling provider version $PROVIDER_VERSION"

# --- Build the add-on directory ---------------------------------------------

log "Creating $ADDON_DIR"
mkdir -p "$ADDON_DIR"
cp -R "$SRC_ROOT/ytmusic_free" "$ADDON_DIR/ytmusic_free"

# Guard values that get interpolated into run.sh (unquoted heredoc) + TARBALL_URL
# against shell metacharacters, so operator input can't inject code into the
# root-run watcher. Conservative charset covers real owners/refs/ids/versions.
for _pair in "repo-owner:$REPO_OWNER" "ref:$REF" "ma-id:$MA_ID" "python-version:$PYTHON_VERSION"; do
    _val="${_pair#*:}"
    case "$_val" in
        ""|*[!A-Za-z0-9._/-]*) die "invalid --${_pair%%:*} value '$_val' (allowed: letters digits . _ / -)" ;;
    esac
done

log "Writing config.yaml"
cat > "$ADDON_DIR/config.yaml" <<EOF
name: "$ADDON_NAME"
description: "Re-installs the ytmusic_free provider into the media server after every container restart. Bundles provider $PROVIDER_VERSION (from $REF)."
version: "$ADDON_VERSION"
slug: $ADDON_SLUG
init: false
boot: auto
docker_api: true
arch:
  - aarch64
  - amd64
  - armhf
  - armv7
  - i386
options:
  auto_update: false
  update_interval_hours: 24
schema:
  auto_update: bool
  update_interval_hours: int(1,)
EOF

log "Writing translations/en.yaml"
mkdir -p "$ADDON_DIR/translations"
cat > "$ADDON_DIR/translations/en.yaml" <<'EOF'
configuration:
  auto_update:
    name: Keep the ytmusic_free provider up to date
    description: >-
      Off by default. When enabled, periodically check GitHub for a newer
      ytmusic_free provider and reinstall it (restarting the server) only
      when the code actually changed. Note this downloads and runs branch-head
      code inside the server unattended. This is NOT the add-on's own "Auto
      update" control on the Info tab, which updates the watcher add-on itself;
      this option updates the music provider inside the server.
  update_interval_hours:
    name: Check the provider for updates every (hours)
    description: >-
      How often to check GitHub for a newer provider, in hours. 24 = once a
      day, 168 = weekly, 1 = hourly. Minimum 1 hour.
EOF

log "Writing build.yaml"
cat > "$ADDON_DIR/build.yaml" <<'EOF'
build_from:
  aarch64: ghcr.io/home-assistant/aarch64-base:latest
  amd64: ghcr.io/home-assistant/amd64-base:latest
  armhf: ghcr.io/home-assistant/armhf-base:latest
  armv7: ghcr.io/home-assistant/armv7-base:latest
  i386: ghcr.io/home-assistant/i386-base:latest
EOF

log "Writing Dockerfile"
cat > "$ADDON_DIR/Dockerfile" <<'EOF'
ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache docker-cli bash curl tar jq

COPY ytmusic_free/ /provider/ytmusic_free/

COPY watcher_lib.sh /watcher_lib.sh
COPY run.sh /run.sh
RUN chmod +x /run.sh && sed -i 's/\r//' /run.sh /watcher_lib.sh

ENTRYPOINT ["/run.sh"]
EOF

log "Writing watcher_lib.sh"
# Sourceable helpers, unit-testable without docker/network. Quoted heredoc: no
# install-time interpolation — pure runtime logic. Callers set CACHE / BUNDLED /
# HASHFILE / TARBALL_URL first.
cat > "$ADDON_DIR/watcher_lib.sh" <<'LIBEOF'
#!/usr/bin/env bash
# Helpers for the Provider Watcher. Source this, then call read_options.

# read_options [options.json path] -> sets AUTO_UPDATE, UPDATE_INTERVAL_HOURS, UPDATE_INTERVAL.
# Boolean is parsed WITHOUT jq's `//` (which coerces an explicit false to the
# default); auto-update is opt-in, so anything but an explicit true is false.
read_options() {
    f="${1:-/data/options.json}"
    AUTO_UPDATE="false"; UPDATE_INTERVAL_HOURS=24
    if [ -r "$f" ]; then
        AUTO_UPDATE="$(jq -r 'if .auto_update == true then "true" else "false" end' "$f" 2>/dev/null || echo false)"
        UPDATE_INTERVAL_HOURS="$(jq -r 'if (.update_interval_hours|type)=="number" then (.update_interval_hours|floor) else 24 end' "$f" 2>/dev/null || echo 24)"
    fi
    case "$UPDATE_INTERVAL_HOURS" in ''|*[!0-9-]*) UPDATE_INTERVAL_HOURS=24 ;; esac   # non-integer -> default
    [ "$UPDATE_INTERVAL_HOURS" -lt 1 ] 2>/dev/null && UPDATE_INTERVAL_HOURS=1          # 0/negative -> 1
    UPDATE_INTERVAL=$((UPDATE_INTERVAL_HOURS * 3600))
}

# Inject source: the auto-updated cache in /data when auto-update is ENABLED and a
# cache exists, else the copy baked into the image at build time. Disabling
# auto-update reverts to the bundled copy (the cache is kept for re-enabling), so
# opting out actually stops running fetched branch-head code. /data survives
# add-on rebuilds.
provider_src() { if [ "${AUTO_UPDATE:-false}" = "true" ] && [ -d "$CACHE" ]; then printf '%s' "$CACHE"; else printf '%s' "$BUNDLED"; fi; }

# Re-resolve the newest release before each fetch, unless the install was
# pinned with --ref. Without this the watcher would sit on whatever tag was
# current when it was installed and never see a later release.
#
# Always returns 0. A failure here keeps the URL we already had, which is the
# documented contract: a fetch that cannot reach GitHub leaves the running
# provider alone rather than failing the watcher.
resolve_tarball_url() {
    [ "${TRACK_RELEASES:-0}" = "1" ] || return 0
    _u="$(curl -fsSL --connect-timeout 10 --max-time 30 -o /dev/null \
        -w '%{url_effective}' \
        "https://github.com/$REPO_OWNER/$REPO_NAME/releases/latest" 2>/dev/null)" || return 0
    case "$_u" in
        */releases/tag/*)
            REF="${_u##*/releases/tag/}"
            TARBALL_URL="https://codeload.github.com/$REPO_OWNER/$REPO_NAME/tar.gz/$REF"
            ;;
    esac
    return 0
}

# Fetch the latest provider from GitHub into $CACHE.
# Return: 0 = updated (changed), 2 = unchanged, 1 = fetch/parse failed.
# On 0, FETCHED_VERSION holds the version that was cached.
fetch_latest() {
    resolve_tarball_url
    tmp="$(mktemp -d 2>/dev/null || mktemp -d -t maw)" || return 1
    if ! curl -fsSL --connect-timeout 10 --max-time 120 "$TARBALL_URL" -o "$tmp/p.tgz" 2>/dev/null; then
        echo "auto-update: download failed"; rm -rf "$tmp"; return 1
    fi
    if ! tar -xzf "$tmp/p.tgz" -C "$tmp" 2>/dev/null; then
        echo "auto-update: extract failed"; rm -rf "$tmp"; return 1
    fi
    nd="$(find "$tmp" -maxdepth 3 -type d -name ytmusic_free 2>/dev/null | head -n1)"
    if [ -z "$nd" ]; then
        echo "auto-update: ytmusic_free not found in tarball"; rm -rf "$tmp"; return 1
    fi
    # hash file contents by RELATIVE path (cd into $nd) so a random tmp dir name
    # doesn't change the digest -> stable across fetches of identical code
    nh="$( (cd "$nd" && find . -type f -exec sha256sum {} \; 2>/dev/null | sort) | sha256sum | awk '{print $1}')"
    oh="$(cat "$HASHFILE" 2>/dev/null || echo none)"
    if [ "$nh" = "$oh" ] && [ -d "$CACHE" ]; then rm -rf "$tmp"; return 2; fi
    # stage-and-swap so a failed copy never wipes a good cache
    rm -rf "$CACHE.new"
    mkdir -p "$CACHE.new" && cp -a "$nd/." "$CACHE.new/" || { rm -rf "$tmp" "$CACHE.new"; return 1; }
    rm -rf "$CACHE" && mv "$CACHE.new" "$CACHE" || { rm -rf "$tmp"; return 1; }
    printf '%s\n' "$nh" > "$HASHFILE"
    # Read it from the cache rather than $nd, which is about to be deleted.
    FETCHED_VERSION="$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' \
        "$CACHE/__init__.py" 2>/dev/null | head -n1)"
    [ -n "$FETCHED_VERSION" ] || FETCHED_VERSION="unknown"
    # ${REF:-} because run.sh defines REF but this library is also sourced on
    # its own by the test suite, which runs under set -u.
    echo "auto-update: cached provider $FETCHED_VERSION from ${REF:-the configured ref} ($nh)"
    rm -rf "$tmp"; return 0
}
LIBEOF

log "Writing run.sh (MA=$MA_ID, $PYTHON_VERSION)"
cat > "$ADDON_DIR/run.sh" <<EOF
#!/usr/bin/env bash

MA="$MA_ID"
BUNDLED="/provider/ytmusic_free"
CACHE="/data/ytmusic_free"
HASHFILE="/data/ytmusic_free.sha256"
DST="/app/venv/lib/$PYTHON_VERSION/site-packages/music_assistant/providers"
# Where auto-update pulls the provider from. Baked from the installer's
# --repo-owner/--ref so a fork self-updates from its own source.
REPO_OWNER="$REPO_OWNER"
REPO_NAME="$REPO_NAME"
REF="$REF"
TARBALL_URL="https://codeload.github.com/$REPO_OWNER/$REPO_NAME/tar.gz/$REF"
# 1 when the install resolved a release rather than being pinned with --ref. The
# watcher then re-resolves the newest release on every poll, so a new release is
# picked up instead of the tag being frozen at install time. A --ref install
# stays on exactly what was asked for.
TRACK_RELEASES=$TRACK_RELEASES
# The provider version baked into this image, reported on start so a bug report
# can name it without digging through Music Assistant's own log.
BUNDLED_VERSION="$PROVIDER_VERSION"
# How long to wait for the configured MA container to appear before logging a
# loud ERROR. Catches the case where the installer's auto-detect fallback
# baked in a container name that does not exist on this host (issue #11).
MISSING_GRACE_SECONDS=60

# Add-on options (Configuration tab): opt-in auto-update from GitHub. Parsing +
# interval clamp live in a sourceable helper so they can be unit-tested.
. /watcher_lib.sh
read_options

echo "[\$(date)] Provider Watcher starting..."
echo "[\$(date)] Add-on $ADDON_VERSION, bundling provider \$BUNDLED_VERSION from $REF"
echo "[\$(date)] Watching for container name: \$MA"

if ! docker info > /dev/null 2>&1; then
    echo "[\$(date)] ERROR: No Docker socket (is Protection Mode off?)"
    sleep 300
    exit 1
fi
echo "[\$(date)] Docker OK"

log() { echo "[\$(date)] \$*"; }

# Re-resolve the container name at runtime rather than trusting what the
# installer baked in. Supervisor renamed add-on containers from "addon_*" to
# "app_*" (issue #54), and every watcher installed before that kept the old name
# in this file and silently stopped updating anyone: there is no install-time
# error to notice, because the name was correct when it was written.
#
# Anything auto-updating a container it addresses by a fixed name has to be able
# to recover when the platform renames it, so this now re-detects whenever the
# configured name is absent, and only falls back to complaining when nothing at
# all matches.
MA_NAME_RE='$MA_NAME_RE'

resolve_ma() {
    if docker inspect "\$MA" >/dev/null 2>&1; then
        return 0
    fi
    _found="\$(docker ps --format '{{.Names}}' 2>/dev/null \\
               | grep -E "\$MA_NAME_RE" | head -n1 || true)"
    if [ -n "\$_found" ] && [ "\$_found" != "\$MA" ]; then
        log "Configured container '\$MA' is not present; using '\$_found' instead."
        log "HINT: this usually means the add-on container was renamed. The"
        log "      watcher has adapted, but re-running the installer will make"
        log "      the change permanent."
        MA="\$_found"
        return 0
    fi
    return 1
}

# provider_src() and fetch_latest() come from /watcher_lib.sh (sourced above).

install_provider() {
    src="\$(provider_src)"
    echo "[\$(date)] Installing ytmusic_free provider from \$src ..."
    sleep 3
    # Clear any stale in-place copy so docker cp is a clean replace, not a merge:
    # files deleted upstream would otherwise linger across periodic auto-updates
    # (docker restart keeps the container filesystem). Mirrors install_provider.sh.
    docker exec "\$MA" rm -rf "\$DST/ytmusic_free" 2>/dev/null || true
    docker cp "\$src" "\$MA:\$DST/" && echo "[\$(date)] Copied OK" || { echo "[\$(date)] ERROR: cp failed"; return 1; }
    docker restart "\$MA" && echo "[\$(date)] MA restarted" || echo "[\$(date)] ERROR: restart failed"
}

warn_if_ma_misconfigured() {
    # If the configured container name doesn't match anything, give the
    # user enough information to fix it. Always check at least one known
    # candidate so the diagnostic surfaces even when nothing matches \$MA.
    found="\$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E 'music' || true)"
    echo "[\$(date)] ERROR: no container matched name '\$MA' after \${MISSING_GRACE_SECONDS}s."
    if [ -n "\$found" ]; then
        echo "[\$(date)] HINT: containers with 'music' in the name on this host:"
        printf '%s\n' "\$found" | sed "s/^/[\$(date)]   /"
        echo "[\$(date)] HINT: re-run the installer with the right --ma-id, then restart this add-on:"
        echo "[\$(date)]   curl -fsSL $SCRIPT_URL | sh -s -- --ma-id <name> --force"
    else
        echo "[\$(date)] HINT: docker ps shows no container with 'music' in the name. Is the Music Assistant add-on installed and running?"
    fi
}

# Prime the cache with the latest provider before the first inject (opt-in).
if [ "\$AUTO_UPDATE" = "true" ]; then
    log "auto-update enabled (checking every \${UPDATE_INTERVAL_HOURS}h); fetching latest..."
    fetch_latest || true
else
    log "auto-update disabled; using the bundled provider copy."
fi
log "provider source: \$(provider_src)"

# Adapt to a renamed container before the first inject, so a watcher installed
# under the old "addon_*" naming starts working again on its next restart
# instead of silently doing nothing.
resolve_ma || true

LAST_ID=\$(docker ps -q --no-trunc --filter name="\$MA" 2>/dev/null)
if [ -n "\$LAST_ID" ]; then
    echo "[\$(date)] MA running (\${LAST_ID:0:12}), installing provider..."
    install_provider
else
    echo "[\$(date)] MA not running, waiting..."
fi

echo "[\$(date)] Polling for MA container changes every 10s..."
MISSING_SINCE=0
MISSING_WARNED=0
LAST_UPDATE=\$(date +%s)
[ -z "\$LAST_ID" ] && MISSING_SINCE=\$(date +%s)
while true; do
    sleep 10
    CUR_ID=\$(docker ps -q --no-trunc --filter name="\$MA" 2>/dev/null)
    if [ -n "\$CUR_ID" ] && [ "\$CUR_ID" != "\$LAST_ID" ]; then
        echo "[\$(date)] New MA container (\${CUR_ID:0:12}), reinstalling..."
        LAST_ID="\$CUR_ID"
        install_provider
        MISSING_SINCE=0
        MISSING_WARNED=0
    elif [ -z "\$CUR_ID" ] && [ -n "\$LAST_ID" ]; then
        echo "[\$(date)] MA stopped"
        LAST_ID=""
        MISSING_SINCE=\$(date +%s)
    elif [ -z "\$CUR_ID" ] && [ "\$MISSING_WARNED" -eq 0 ] && [ "\$MISSING_SINCE" -gt 0 ]; then
        if [ \$((\$(date +%s) - MISSING_SINCE)) -ge "\$MISSING_GRACE_SECONDS" ]; then
            # A container that has gone missing and stayed missing is exactly
            # the symptom of a rename, so try to re-resolve before concluding
            # the user has misconfigured something.
            if resolve_ma; then
                MISSING_SINCE=\$(date +%s)
            else
                warn_if_ma_misconfigured
                MISSING_WARNED=1
            fi
        fi
    fi
    # Periodic auto-update: fetch latest; reinject + restart MA only on change.
    if [ "\$AUTO_UPDATE" = "true" ]; then
        now=\$(date +%s)
        if [ \$((now - LAST_UPDATE)) -ge "\$UPDATE_INTERVAL" ]; then
            LAST_UPDATE=\$now
            if fetch_latest; then
                # Names the version rather than claiming one. Before releases
                # existed this line said "new provider version detected" when
                # what it had actually seen was a changed sha256 (issue #68).
                log "auto-update: provider \$FETCHED_VERSION is new here -> reinstalling"
                CUR_ID=\$(docker ps -q --no-trunc --filter name="\$MA" 2>/dev/null)
                if [ -n "\$CUR_ID" ]; then LAST_ID="\$CUR_ID"; install_provider; fi
            fi
        fi
    fi
done
EOF
chmod +x "$ADDON_DIR/run.sh" 2>/dev/null || true

# --- Done -------------------------------------------------------------------

log "Install complete: $ADDON_DIR"
cat <<EOF

Next steps:
  1. In Home Assistant: Settings -> Add-ons -> Add-on Store
     (three-dot menu) -> Check for updates.
  2. Open "$ADDON_NAME" under Local add-ons.
       First install:  click Install.
       Re-installing:  click Rebuild (three-dot menu) so the new run.sh and
                       provider files are baked into the image. A running
                       add-on keeps its old cached image until you rebuild.
  3. On the Info tab, turn Protection mode OFF (required for Docker socket access).
  4. Start the add-on and check the logs for "Copied OK" / "MA restarted".

This installer stamped version $ADDON_VERSION so Home Assistant detects the
change. If you re-ran to fix the MA container ID or Python version and the
add-on still uses the old value, Rebuild it (step 2) -- "Check for updates"
alone does not rebuild a cached local add-on image.

If MA container ID or Python version was wrong, re-run with:
  curl -fsSL $SCRIPT_URL | sh -s -- --force --ma-id <ID> --python-version <pythonX.Y>
EOF
