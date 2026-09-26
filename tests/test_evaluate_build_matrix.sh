#!/usr/bin/env bash
# Tests for .github/scripts/evaluate_build_matrix.sh
#
# Run from the repo root:   bash tests/test_evaluate_build_matrix.sh
# Or as a CI step.
#
# The script decides which Docker variants docker-publish.yml builds. It talks
# to the registry through `docker buildx imagetools inspect` and to the repo
# through `git log`, so both are replaced by stubs that answer from files in a
# per-case directory. `date` is stubbed too, so companion tags do not change
# if the suite runs across midnight UTC. jq is the real one from PATH, and
# python3 reads the results back. bash is required because the script under
# test is bash.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_ROOT/.github/scripts/evaluate_build_matrix.sh"

PASS=0
FAIL=0

red()   { printf '\033[31m%s\033[0m' "$*"; }
green() { printf '\033[32m%s\033[0m' "$*"; }

pass() { PASS=$((PASS+1)); printf '  %s %s\n' "$(green PASS)" "$1"; }
fail() { FAIL=$((FAIL+1)); printf '  %s %s\n' "$(red   FAIL)" "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }

assert_eq() {
    # assert_eq <name> <expected> <actual>
    if [ "$2" = "$3" ]; then
        pass "$1"
    else
        fail "$1" "expected: $2 / actual: $3"
    fi
}

for tool in jq python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "error: $tool is required to run these tests" >&2
        exit 2
    fi
done

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
STUBS="$SANDBOX/bin"
mkdir -p "$STUBS"

# --- Stubs --------------------------------------------------------------------
# Files per image live under $CASE_DIR, keyed by the image reference with '/'
# and ':' replaced by '_':
#   digest/<key>       printed for --format '{{.Manifest.Digest}}'
#   digest/<key>.fail  exit 1 (registry error, missing tag)
#   digest/<key>.junk  printed on stdout, then exit 1
#   digest/<key>.hang  sleep far past the script's lookup timeout
#   raw/<key>          printed for --raw (published image manifest)
# A missing file means "not found" (exit 1).

cat > "$STUBS/docker" <<'STUB'
#!/usr/bin/env bash
printf 'docker %s\n' "$*" >> "$CASE_DIR/calls.log"
[ "$1 $2 $3" = "buildx imagetools inspect" ] || { echo "docker stub: unsupported: $*" >&2; exit 99; }
shift 3
mode=""
while [ $# -gt 1 ]; do
    case "$1" in
        --format) mode=digest; shift 2 ;;
        --raw)    mode=raw; shift ;;
        *)        echo "docker stub: unknown arg $1" >&2; exit 99 ;;
    esac
done
f="$CASE_DIR/$mode/$(printf '%s' "$1" | tr '/:' '__')"
if [ -f "$f.fail" ]; then echo "ERROR: registry error" >&2; exit 1; fi
if [ -f "$f.junk" ]; then cat "$f.junk"; exit 1; fi
if [ -f "$f.hang" ]; then exec sleep 30; fi
if [ -f "$f" ]; then cat "$f"; exit 0; fi
echo "ERROR: $1: not found" >&2
exit 1
STUB

cat > "$STUBS/git" <<'STUB'
#!/usr/bin/env bash
printf 'git %s\n' "$*" >> "$CASE_DIR/calls.log"
[ "$1" = log ] || { echo "git stub: unsupported: $*" >&2; exit 99; }
cat "$CASE_DIR/git_head"
STUB

cat > "$STUBS/date" <<'STUB'
#!/usr/bin/env bash
echo 20260925
STUB

chmod +x "$STUBS/docker" "$STUBS/git" "$STUBS/date"

# --- Fixtures -----------------------------------------------------------------

HEAD_SHA=1111111111111111111111111111111111111111
OLD_SHA=2222222222222222222222222222222222222222
TRIGGER_SHA=abcdef0123456789abcdef0123456789abcdef01
D_LATEST="sha256:$(printf 'a%.0s' {1..64})"
D_BETA="sha256:$(printf 'b%.0s' {1..64})"
D_NIGHTLY="sha256:$(printf 'c%.0s' {1..64})"
D_NEW="sha256:$(printf 'd%.0s' {1..64})"
BASE=ghcr.io/music-assistant/server
PUBLISHED=ghcr.io/owner/repo-name
TODAY=20260925
KD=org.opencontainers.image.base.digest
KR=org.opencontainers.image.revision

key() { printf '%s' "$1" | tr '/:' '__'; }

new_case() {
    CASE_DIR="$SANDBOX/case-$1"
    export CASE_DIR
    mkdir -p "$CASE_DIR/digest" "$CASE_DIR/raw"
    : > "$CASE_DIR/calls.log"
    printf '%s\n' "$HEAD_SHA" > "$CASE_DIR/git_head"
    set_base latest "$D_LATEST"
    set_base beta "$D_BETA"
    set_base nightly "$D_NIGHTLY"
    printf '\n== %s ==\n' "$2"
}

set_base()  { printf '%s\n' "$2" > "$CASE_DIR/digest/$(key "$BASE:$1")"; }
fail_base() { rm -f "$CASE_DIR/digest/$(key "$BASE:$1")"; : > "$CASE_DIR/digest/$(key "$BASE:$1").fail"; }
junk_base() { rm -f "$CASE_DIR/digest/$(key "$BASE:$1")"; printf '%s\n' "$2" > "$CASE_DIR/digest/$(key "$BASE:$1").junk"; }
hang_base() { mv "$CASE_DIR/digest/$(key "$BASE:$1")" "$CASE_DIR/digest/$(key "$BASE:$1").hang"; }

# publish <variant> <base digest> <revision> [index|descriptor]
# Writes the manifest a previous build left behind, with the annotations on
# the index or only on the platform descriptors (behind an attestation entry).
publish() {
    python3 - "$CASE_DIR/raw/$(key "$PUBLISHED:$1")" "$2" "$3" "${4:-index}" "$KD" "$KR" <<'PY'
import json, sys
out, digest, rev, where, kd, kr = sys.argv[1:]
ann = {kd: digest, kr: rev}
attestation = {"annotations": {"vnd.docker.reference.type": "attestation-manifest"}}
doc = {"schemaVersion": 2, "manifests": [{"platform": {"architecture": "amd64"}},
                                         {"platform": {"architecture": "arm64"}}, attestation]}
if where == "index":
    doc["annotations"] = ann
else:
    doc["manifests"] = [attestation] + [dict(m, annotations=ann) for m in doc["manifests"][:2]]
json.dump(doc, open(out, "w"))
PY
}

publish_all_current() {
    publish edge "$D_LATEST" "$HEAD_SHA"
    publish beta "$D_BETA" "$HEAD_SHA"
    publish nightly "$D_NIGHTLY" "$HEAD_SHA"
}

# run [VAR=value ...]: runs the script with schedule defaults, overridable.
# Sets RC, and RESULT to "has_builds=... variants=... tags=... unresolved=..."
# where tags lists each leg's tags plus its companion tag joined by '|', and
# legs by ';'. DETAILS holds "ma=<ma_version per leg> rev=<revision per leg>".
run() {
    OUT="$CASE_DIR/github_output"
    : > "$OUT"
    env -i PATH="$STUBS:$PATH" HOME="$SANDBOX" CASE_DIR="$CASE_DIR" GITHUB_OUTPUT="$OUT" \
        EVENT=schedule SHA="$TRIGGER_SHA" GIT_REF=refs/heads/main GIT_REF_TYPE=branch \
        RELEASE_TAG= RUN_ID=987654321 FORCE_BUILD=false \
        REGISTRY=ghcr.io GITHUB_REPOSITORY=Owner/Repo-Name \
        BASE_IMAGE_LATEST="$BASE:latest" BASE_IMAGE_BETA="$BASE:beta" BASE_IMAGE_DEV="$BASE:nightly" \
        ANNOTATION_BASE_DIGEST="$KD" ANNOTATION_REVISION="$KR" \
        "$@" bash "$SCRIPT" > "$CASE_DIR/stdout" 2> "$CASE_DIR/stderr"
    RC=$?
    python3 - "$OUT" "$CASE_DIR/result" "$CASE_DIR/details" <<'PY'
import json, sys
out, result, details = sys.argv[1:]
kv = {}
for line in open(out):
    k, _, v = line.rstrip("\n").partition("=")
    kv[k] = v
legs = json.loads(kv["matrix"])["include"] if "matrix" in kv else []
def tags(leg):
    return "|".join(leg["tags"].split("\n") + ([leg["companion"]] if leg["companion"] else []))
with open(result, "w") as fh:
    fh.write("has_builds=%s variants=%s tags=%s unresolved=%s" % (
        kv.get("has_builds", "<unset>"),
        ",".join(l["variant"] for l in legs) or "-",
        ";".join(tags(l) for l in legs) or "-",
        kv.get("unresolved", "<unset>") or "-"))
with open(details, "w") as fh:
    fh.write("ma=%s rev=%s" % (
        ",".join(l["ma_version"] for l in legs) or "-",
        ",".join(sorted(set(l["revision"] for l in legs))) or "-"))
PY
    RESULT="$(cat "$CASE_DIR/result")"
    DETAILS="$(cat "$CASE_DIR/details")"
}

leg() { printf '%s|%s-%s-abcdef0-987654321' "$1" "$1" "$TODAY"; }
E="$(leg edge)"; B="$(leg beta)"; N="$(leg nightly)"

# --- Scheduled runs -----------------------------------------------------------

new_case s1 "schedule, nothing published yet"
run
assert_eq "exit status" 0 "$RC"
assert_eq "builds all three" "has_builds=true variants=edge,beta,nightly tags=$E;$B;$N unresolved=-" "$RESULT"
assert_eq "base channel and revision per leg" "ma=latest,beta,nightly rev=$HEAD_SHA" "$DETAILS"
assert_eq "revision comes from the build inputs only" 1 \
    "$(grep -c '^git log -1 --format=%H -- ytmusic_free Dockerfile .dockerignore$' "$CASE_DIR/calls.log")"

new_case s2 "schedule, everything current (index annotations)"
publish_all_current
run
assert_eq "builds nothing" "has_builds=false variants=- tags=- unresolved=-" "$RESULT"
assert_eq "reads each published image" 3 "$(grep -c 'inspect --raw' "$CASE_DIR/calls.log")"

new_case s3 "schedule, everything current (annotations only on descriptors)"
publish edge "$D_LATEST" "$HEAD_SHA" descriptor
publish beta "$D_BETA" "$HEAD_SHA" descriptor
publish nightly "$D_NIGHTLY" "$HEAD_SHA" descriptor
run
assert_eq "builds nothing" "has_builds=false variants=- tags=- unresolved=-" "$RESULT"

new_case s4 "schedule, upstream beta moved"
publish_all_current
set_base beta "$D_NEW"
run
assert_eq "builds beta only" "has_builds=true variants=beta tags=$B unresolved=-" "$RESULT"
assert_eq "beta leg carries the new base digest" 1 "$(grep -c "\"base_digest\":\"$D_NEW\"" "$OUT")"

new_case s5 "schedule, provider changed since the last build"
publish edge "$D_LATEST" "$OLD_SHA"
publish beta "$D_BETA" "$HEAD_SHA"
publish nightly "$D_NIGHTLY" "$OLD_SHA"
run
assert_eq "builds the stale variants" "has_builds=true variants=edge,nightly tags=$E;$N unresolved=-" "$RESULT"

new_case s6 "schedule, reading the published beta image fails"
publish_all_current
rm -f "$CASE_DIR/raw/$(key "$PUBLISHED:beta")"
run
assert_eq "rebuilds beta" "has_builds=true variants=beta tags=$B unresolved=-" "$RESULT"

# --- Push and manual runs -----------------------------------------------------

new_case p1 "push to main, everything current"
publish_all_current
run EVENT=push
assert_eq "always builds all three" "has_builds=true variants=edge,beta,nightly tags=$E;$B;$N unresolved=-" "$RESULT"

new_case d1 "dispatch on main without force_build, everything current"
publish_all_current
run EVENT=workflow_dispatch
assert_eq "builds nothing" "has_builds=false variants=- tags=- unresolved=-" "$RESULT"

new_case d2 "dispatch on main with force_build"
publish_all_current
run EVENT=workflow_dispatch FORCE_BUILD=true
assert_eq "builds all three" "has_builds=true variants=edge,beta,nightly tags=$E;$B;$N unresolved=-" "$RESULT"

new_case d3 "dispatch on a feature branch"
run EVENT=workflow_dispatch GIT_REF=refs/heads/feature/x FORCE_BUILD=true
assert_eq "exit status" 0 "$RC"
assert_eq "builds nothing" "has_builds=false variants=- tags=- unresolved=<unset>" "$RESULT"
assert_eq "never touches the registry" 0 "$(grep -c '^docker' "$CASE_DIR/calls.log")"

new_case w1 "any other event (pull_request)"
run EVENT=pull_request
assert_eq "builds nothing" "has_builds=false variants=- tags=- unresolved=-" "$RESULT"

# --- Releases -------------------------------------------------------------------

new_case r1 "release v1.2.3 via release.yml (caller event is the tag push)"
run EVENT=push GIT_REF=refs/tags/v1.2.3 GIT_REF_TYPE=tag RELEASE_TAG=v1.2.3
assert_eq "publishes version and latest, no companion" "has_builds=true variants=release tags=1.2.3|latest unresolved=<unset>" "$RESULT"
assert_eq "builds on the stable base" "ma=latest rev=$HEAD_SHA" "$DETAILS"
assert_eq "never reads published images" 0 "$(grep -c 'inspect --raw' "$CASE_DIR/calls.log")"

new_case r2 "prerelease v1.2.3-rc.1"
run EVENT=push GIT_REF=refs/tags/v1.2.3-rc.1 GIT_REF_TYPE=tag RELEASE_TAG=v1.2.3-rc.1
assert_eq "never moves latest" "has_builds=true variants=release tags=1.2.3-rc.1 unresolved=<unset>" "$RESULT"

new_case r3 "tag push without release_tag (fallback)"
run EVENT=push GIT_REF=refs/tags/v2.0.0 GIT_REF_TYPE=tag
assert_eq "publishes version and latest" "has_builds=true variants=release tags=2.0.0|latest unresolved=<unset>" "$RESULT"

new_case r4 "release whose base image cannot be resolved"
fail_base latest
run EVENT=push GIT_REF=refs/tags/v1.2.3 GIT_REF_TYPE=tag RELEASE_TAG=v1.2.3
assert_eq "fails the run" 1 "$RC"
assert_eq "emits nothing" "has_builds=<unset> variants=- tags=- unresolved=<unset>" "$RESULT"

# --- Unresolvable base images -----------------------------------------------------

new_case u1 "schedule, upstream nightly lookup fails, edge stale"
publish edge "$D_LATEST" "$OLD_SHA"
publish beta "$D_BETA" "$HEAD_SHA"
fail_base nightly
run
assert_eq "exit status" 0 "$RC"
assert_eq "still builds edge, reports nightly" "has_builds=true variants=edge tags=$E unresolved=nightly" "$RESULT"
assert_eq "logs a workflow error" 1 "$(grep -c '^::error::Could not resolve ghcr.io/music-assistant/server:nightly' "$CASE_DIR/stdout")"

new_case u2 "push, upstream beta tag missing"
rm -f "$CASE_DIR/digest/$(key "$BASE:beta")"
run EVENT=push
assert_eq "builds the other two" "has_builds=true variants=edge,nightly tags=$E;$N unresolved=beta" "$RESULT"

new_case u3 "push, every lookup fails"
fail_base latest
fail_base beta
fail_base nightly
run EVENT=push
assert_eq "exit status" 0 "$RC"
assert_eq "builds nothing, reports all" "has_builds=false variants=- tags=- unresolved=edge beta nightly" "$RESULT"

new_case u4 "docker prints output but exits non-zero"
junk_base latest "partial-output"
run EVENT=push
assert_eq "output is not taken as a digest" "has_builds=true variants=beta,nightly tags=$B;$N unresolved=edge" "$RESULT"

new_case u5 "docker exits 0 with something that is not a digest"
set_base latest "<html>502</html>"
run EVENT=push
assert_eq "treated as unresolved" "has_builds=true variants=beta,nightly tags=$B;$N unresolved=edge" "$RESULT"

new_case u6 "lookup of the upstream beta image hangs"
hang_base beta
run EVENT=push LOOKUP_TIMEOUT=2
assert_eq "exit status" 0 "$RC"
assert_eq "times out and skips only beta" "has_builds=true variants=edge,nightly tags=$E;$N unresolved=beta" "$RESULT"

# --- Companion tags and output format -------------------------------------------

new_case c1 "no run id (local run)"
run EVENT=push RUN_ID=
assert_eq "suffix is date and sha only" "edge|edge-$TODAY-abcdef0" "$(printf '%s' "$RESULT" | sed 's/.*tags=\([^;]*\);.*/\1/')"

new_case o1 "GITHUB_OUTPUT format, automatic run"
run EVENT=push
assert_eq "one line per output" 3 "$(wc -l < "$OUT" | tr -d ' ')"

new_case o2 "GITHUB_OUTPUT format, release"
run EVENT=push GIT_REF=refs/tags/v1.2.3 GIT_REF_TYPE=tag RELEASE_TAG=v1.2.3
assert_eq "one line per output" 2 "$(wc -l < "$OUT" | tr -d ' ')"
assert_eq "newline between tags stays escaped" 1 "$(grep -c '"tags":"1.2.3\\nlatest"' "$OUT")"

# --- Summary ----------------------------------------------------------------------

printf '\n== Summary ==\n'
printf '  passed:  %s\n' "$PASS"
printf '  failed:  %s\n' "$FAIL"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
