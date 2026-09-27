#!/usr/bin/env bash
# Build the iPad shell for a device.
#
#   ./scripts/build.sh                   # any connected iOS device
#   DEVICE_ID=<udid> ./scripts/build.sh  # one specific device
#   SKIP_APP_ICON=1 ./scripts/build.sh   # build without the app icon
#
# SKIP_APP_ICON works around a machine that has no simulator runtime
# matching the installed iOS SDK: actool needs one to compile an AppIcon
# even when the build targets a device. See ios/README.md.
#
# EXTRA is expanded as ${EXTRA[@]+"${EXTRA[@]}"}: under `set -u` the
# system bash 3.2 aborts on "${EXTRA[@]}" while the array is still empty,
# which is what the default invocation produces.
set -euo pipefail
IOS_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$IOS_DIR"

if [ ! -d Talknest.xcodeproj ]; then
    echo "[build] generating Xcode project"
    xcodegen generate
fi

DESTINATION="generic/platform=iOS"
if [ -n "${DEVICE_ID:-}" ]; then
    DESTINATION="platform=iOS,id=$DEVICE_ID"
fi

EXTRA=()
if [ -n "${SKIP_APP_ICON:-}" ]; then
    EXTRA+=(ASSETCATALOG_COMPILER_APPICON_NAME=)
fi

if [ -n "${DEVELOPMENT_TEAM:-}" ]; then
    EXTRA+=(DEVELOPMENT_TEAM="$DEVELOPMENT_TEAM")
fi

exec xcodebuild \
    -project Talknest.xcodeproj \
    -scheme Talknest \
    -destination "$DESTINATION" \
    ${EXTRA[@]+"${EXTRA[@]}"} \
    -allowProvisioningUpdates \
    build
