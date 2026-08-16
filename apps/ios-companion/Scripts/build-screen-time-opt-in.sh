#!/usr/bin/env bash
set -euo pipefail

action="${1:-build}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "${action}" in
  build|test|analyze|archive)
    ;;
  *)
    echo "usage: bash Scripts/build-screen-time-opt-in.sh [build|test|analyze|archive] [xcodebuild args...]" >&2
    exit 64
    ;;
esac

sdk="${HEALTHMES_SCREENTIME_SDK:-iphonesimulator}"
configuration="${HEALTHMES_SCREENTIME_CONFIGURATION:-ScreenTimeOptInDebug}"
destination="${HEALTHMES_SCREENTIME_DESTINATION:-generic/platform=iOS Simulator}"
sdk_path="$(xcrun --sdk "${sdk}" --show-sdk-path)"
probe_root="$(mktemp -d "${TMPDIR:-/tmp}/healthmes-screentime-sdk.XXXXXX")"
trap 'rm -rf "${probe_root}"' EXIT

case "${configuration}" in
  ScreenTimeOptInDebug|ScreenTimeOptInRelease)
    ;;
  *)
    echo "unsupported HEALTHMES_SCREENTIME_CONFIGURATION=${configuration}; expected ScreenTimeOptInDebug or ScreenTimeOptInRelease" >&2
    exit 64
    ;;
esac

case "${sdk}" in
  iphonesimulator*)
    target="arm64-apple-ios17.0-simulator"
    ;;
  iphoneos*)
    target="arm64-apple-ios17.0"
    ;;
  *)
    echo "unsupported HEALTHMES_SCREENTIME_SDK=${sdk}; expected iphonesimulator or iphoneos" >&2
    exit 64
    ;;
esac

cat >"${probe_root}/ScreenTimeSDKProbe.swift" <<'SWIFT'
import Combine
import DeviceActivity
import FamilyControls
import Foundation

@available(iOS 26.4, *)
func probeAppAndWebsiteUsageExport() {
    _ = AuthorizationStatus.approvedWithDataAccess
    _ = AuthorizationCenter.shared.$authorizationStatus
    let filter = DeviceActivityFilter(
        segment: .hourly(
            during: DateInterval(start: Date(), duration: 3_600)
        )
    )
    _ = DeviceActivityData.activityData(filteredBy: filter, using: .live)
}
SWIFT

capability_condition=""
if env \
  CLANG_MODULE_CACHE_PATH="${probe_root}/ModuleCache" \
  SWIFT_MODULECACHE_PATH="${probe_root}/ModuleCache" \
  xcrun --sdk "${sdk}" swiftc \
    -typecheck \
    -target "${target}" \
    -sdk "${sdk_path}" \
    "${probe_root}/ScreenTimeSDKProbe.swift" \
    >"${probe_root}/probe.log" 2>&1
then
  capability_condition="HEALTHMES_APP_WEBSITE_USAGE_SDK_AVAILABLE"
  echo "Screen Time SDK capability: available; compiling the Apple export collector"
else
  echo "Screen Time SDK capability: unavailable; building the explicit fail-closed adapter"
  sed -n '1,8p' "${probe_root}/probe.log" >&2
fi

exec xcodebuild \
  -project HealthMesCompanion.xcodeproj \
  -scheme HealthMesCompanionScreenTimeOptIn \
  -configuration "${configuration}" \
  -sdk "${sdk}" \
  -destination "${destination}" \
  "${action}" \
  CODE_SIGNING_ALLOWED=NO \
  "HEALTHMES_SCREENTIME_SDK_CONDITION=${capability_condition}" \
  "$@"
