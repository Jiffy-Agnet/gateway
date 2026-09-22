#!/bin/bash
# Build and optionally push the Jiffy generic sandbox image.
#
# Usage:
#   ./build.sh                          # build with default tag jiffy-sandbox:1.3.0
#   ./build.sh ghcr.io/org/jiffy-sandbox:1.3.0   # build + push to GHCR
set -e

IMAGE_TAG="${1:-jiffy-sandbox:1.3.0}"

echo "Building sandbox image: $IMAGE_TAG"
docker build -t "$IMAGE_TAG" "$(dirname "$0")"

# If a registry-qualified tag was given, push it
if [[ "$IMAGE_TAG" == *"."*"/"* ]]; then
  echo "Pushing $IMAGE_TAG"
  docker push "$IMAGE_TAG"
fi
