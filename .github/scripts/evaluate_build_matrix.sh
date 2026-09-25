#!/usr/bin/env bash
# Evaluate which Docker image variants need to be built.
#
# Called by the `check` job of .github/workflows/docker-publish.yml. All
# inputs are passed as environment variables (see the workflow step); the
# resulting build matrix is written to $GITHUB_OUTPUT as `matrix` and
# `has_builds` (falls back to stdout when GITHUB_OUTPUT is unset, which
# makes the script easy to test locally). The repository must be checked
# out with full history (the workflow passes fetch-depth: 0) so that the
# revision lookup below can walk the log.
#
# Outputs:
#   matrix     - JSON object {"include": [...]} for the build job matrix
#   has_builds - "true" when at least one variant should be built
set -euo pipefail

# --- Inputs (environment) ----------------------------------------------------
EVENT="${EVENT:?EVENT is required}"                 # github.event_name
SHA="${SHA:?SHA is required}"                       # github.sha
GIT_REF="${GIT_REF:-}"                              # github.ref
GIT_REF_TYPE="${GIT_REF_TYPE:-}"                    # github.ref_type
RELEASE_TAG="${RELEASE_TAG:-}"                      # release_tag workflow_call input (set by release.yml)
RUN_ID="${RUN_ID:-}"                                # github.run_id, part of the companion tag suffix
FORCE_BUILD="${FORCE_BUILD:-false}"                 # workflow_dispatch input
REGISTRY="${REGISTRY:?REGISTRY is required}"
GITHUB_REPOSITORY="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
BASE_IMAGE_LATEST="${BASE_IMAGE_LATEST:?BASE_IMAGE_LATEST is required}"
BASE_IMAGE_BETA="${BASE_IMAGE_BETA:?BASE_IMAGE_BETA is required}"
BASE_IMAGE_DEV="${BASE_IMAGE_DEV:?BASE_IMAGE_DEV is required}"
ANNOTATION_BASE_DIGEST="${ANNOTATION_BASE_DIGEST:?ANNOTATION_BASE_DIGEST is required}"
ANNOTATION_REVISION="${ANNOTATION_REVISION:?ANNOTATION_REVISION is required}"

# Companion tag suffix for rollback: <tag>-YYYYMMDD-<shortsha>-<run_id>.
# The run id keeps the tag unique even for several builds of the same commit
# on the same day. Empty run id (local testing) leaves it off.
COMPANION_SUFFIX="$(date -u +%Y%m%d)-${SHA::7}"
if [ -n "$RUN_ID" ]; then
  COMPANION_SUFFIX="${COMPANION_SUFFIX}-${RUN_ID}"
fi

# Last commit that touched anything the image build consumes. Stamped as the
# revision annotation and used for the skip comparison, so docs-only commits
# no longer force a rebuild of the channel variants.
LAST_BUILD_COMMIT="$(git log -1 --format=%H -- ytmusic_free Dockerfile .dockerignore)"

# --- Helpers -----------------------------------------------------------------

# Resolve the real multi-arch index digest (a `sha256:...` digest) of an
# image tag. Stable across architectures and independent of the local
# platform.
get_digest() {
  local digest
  digest="$(docker buildx imagetools inspect --format '{{.Manifest.Digest}}' "$1")"
  if [ -z "$digest" ]; then
    echo "ERROR: empty digest for '$1'" >&2
    exit 1
  fi
  printf '%s' "$digest"
}

# Read the base-digest / revision annotations from a published multi-arch
# image. Prints "<digest> <revision>" (either may be empty when the image
# or the annotation does not exist yet).
get_annotations() {
  local image="$1" raw digest revision
  if raw="$(docker buildx imagetools inspect --raw "$image" 2>/dev/null)"; then
    digest="$(printf '%s' "$raw" | jq -r --arg k "$ANNOTATION_BASE_DIGEST" \
      '.annotations[$k] // empty, (.manifests[]? | .annotations[$k] // empty)' 2>/dev/null | head -n1 || true)"
    revision="$(printf '%s' "$raw" | jq -r --arg k "$ANNOTATION_REVISION" \
      '.annotations[$k] // empty, (.manifests[]? | .annotations[$k] // empty)' 2>/dev/null | head -n1 || true)"
  fi
  printf '%s %s' "${digest:-}" "${revision:-}"
}

# Compare a variant against its published image: build when either the base
# digest or the stamped revision differs (or nothing was published yet).
# This also catches builds missed because a previous push build failed.
needs_build() {
  local base_digest="$1" pub="$2"
  local pub_digest="${pub%% *}" pub_rev="${pub#* }"
  [ -z "$pub_digest" ] || [ "$pub_digest" != "$base_digest" ] \
    || [ -z "$pub_rev" ] || [ "$pub_rev" != "$LAST_BUILD_COMMIT" ]
}

# Emit a key/value pair to $GITHUB_OUTPUT (or stdout when unset).
emit() {
  local key="$1" value="$2"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    printf '%s=%s\n' "$key" "$value" >> "$GITHUB_OUTPUT"
  else
    printf '%s=%s\n' "$key" "$value"
  fi
}

# --- Ref guard ---------------------------------------------------------------
# Manual runs are only allowed on main.
if [ "$EVENT" = "workflow_dispatch" ] && [ "$GIT_REF" != "refs/heads/main" ]; then
  echo "::warning::workflow_dispatch is restricted to the main branch (got '$GIT_REF'); skipping."
  emit "has_builds" "false"
  emit "matrix" '{"include":[]}'
  exit 0
fi

# --- Release build (workflow_call from release.yml / direct tag push) --------
# Tags: the version (git tag without the `v` prefix) plus, for stable
# (non-prerelease) releases, the moving `latest` tag. No digest-skip check
# applies.
#
# release.yml calls this workflow with the release_tag input while
# github.event_name is "workflow_call", so the input is the primary signal;
# a direct tag push to this workflow is kept as a defensive fallback.
TAG_NAME="${RELEASE_TAG}"
if [ -z "$TAG_NAME" ] && [ "$EVENT" = "push" ] && [ "$GIT_REF_TYPE" = "tag" ]; then
  TAG_NAME="${GIT_REF#refs/tags/}"
fi

if [ -n "$TAG_NAME" ]; then
  VERSION="${TAG_NAME#v}"
  case "$TAG_NAME" in
    *-*)
      # Prerelease (mirrors release.yml): publish :<version> only, never
      # move :latest.
      TAGS="$VERSION"
      echo "Prerelease tag $TAG_NAME detected - publishing :$VERSION only (no :latest)."
      ;;
    *)
      TAGS="$(printf '%s\nlatest' "$VERSION")"
      ;;
  esac
  base_digest="$(get_digest "$BASE_IMAGE_LATEST")"
  MATRIX="$(jq -nc \
    --arg tags "$TAGS" \
    --arg base_digest "$base_digest" \
    --arg revision "$LAST_BUILD_COMMIT" '{
    include: [{
      variant: "release",
      ma_version: "latest",
      tags: $tags,
      base_digest: $base_digest,
      revision: $revision
    }]
  }')"
  emit "matrix" "$MATRIX"
  emit "has_builds" "true"
  echo "Release build for tag $TAG_NAME scheduled (tags: ${TAGS//$'\n'/, }, base digest: $base_digest)."
  exit 0
fi

# --- Scheduled / push / manual runs -------------------------------------------
# Evaluate all three automatic variants. These never publish the `latest`
# tag - that tag is reserved for official (tested) tag-push releases.
declare -A BASE_IMAGE=(
  [edge]="$BASE_IMAGE_LATEST"
  [beta]="$BASE_IMAGE_BETA"
  [nightly]="$BASE_IMAGE_DEV"
)
declare -A MA_VERSION=( [edge]=latest [beta]=beta [nightly]=nightly )

legs="[]"
for variant in edge beta nightly; do
  base_digest="$(get_digest "${BASE_IMAGE[$variant]}")"
  published_image="$REGISTRY/${GITHUB_REPOSITORY,,}:${variant}"
  pub="$(get_annotations "$published_image")"
  pub_digest="${pub%% *}"
  pub_rev="${pub#* }"

  build=false
  case "$EVENT" in
    push)
      # Always build on code changes to main.
      build=true
      ;;
    schedule|workflow_dispatch)
      if [ "$FORCE_BUILD" = "true" ] || needs_build "$base_digest" "$pub"; then
        build=true
      fi
      ;;
  esac

  echo "variant=$variant base_digest=$base_digest published=($pub_digest, ${pub_rev:-<none>}) should_build=$build"

  if [ "$build" = "true" ]; then
    legs="$(printf '%s' "$legs" | jq -c \
      --arg variant "$variant" \
      --arg ma_version "${MA_VERSION[$variant]}" \
      --arg tags "$(printf '%s\n%s-%s' "$variant" "$variant" "$COMPANION_SUFFIX")" \
      --arg base_digest "$base_digest" \
      --arg revision "$LAST_BUILD_COMMIT" \
      '. + [{
        variant: $variant,
        ma_version: $ma_version,
        tags: $tags,
        base_digest: $base_digest,
        revision: $revision
      }]')"
  fi
done

MATRIX="$(jq -nc --argjson include "$legs" '{include: $include}')"
emit "matrix" "$MATRIX"
if [ "$legs" = "[]" ]; then
  emit "has_builds" "false"
  echo "All variants are up to date - nothing to build."
else
  emit "has_builds" "true"
fi
