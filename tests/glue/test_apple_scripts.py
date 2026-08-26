"""Contract tests for Apple companion build helpers."""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCREEN_TIME_SCRIPT = (
    REPO_ROOT
    / "apps"
    / "ios-companion"
    / "Scripts"
    / "build-screen-time-opt-in.sh"
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_screen_time_helper_does_not_force_iphone_sdk_on_embedded_watch_target(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    captured_args = tmp_path / "xcodebuild.args"

    _write_executable(
        fake_bin / "xcrun",
        """#!/bin/sh
set -eu
if [ "${3:-}" = "--show-sdk-path" ]; then
  printf '%s\\n' '/fake/iPhoneSimulator.sdk'
  exit 0
fi
if [ "${3:-}" = "swiftc" ]; then
  printf '%s\\n' 'probe intentionally unavailable' >&2
  exit 1
fi
printf '%s\\n' 'unexpected xcrun invocation' >&2
exit 2
""",
    )
    _write_executable(
        fake_bin / "xcodebuild",
        """#!/bin/sh
set -eu
printf '%s\\n' "$@" > "$HEALTHMES_CAPTURE_XCODEBUILD_ARGS"
""",
    )

    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["HEALTHMES_CAPTURE_XCODEBUILD_ARGS"] = str(captured_args)
    result = subprocess.run(
        ["bash", str(SCREEN_TIME_SCRIPT), "build"],
        cwd=SCREEN_TIME_SCRIPT.parent.parent.parent.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    args = captured_args.read_text(encoding="utf-8").splitlines()
    assert "-sdk" not in args
    assert "generic/platform=iOS Simulator" in args
    assert "HEALTHMES_SCREENTIME_SDK_CONDITION=" in args


def test_screen_time_helper_accepts_version_qualified_simulator_sdk_alias(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    captured_args = tmp_path / "xcodebuild.args"

    _write_executable(
        fake_bin / "xcrun",
        """#!/bin/sh
set -eu
if [ "${3:-}" = "--show-sdk-path" ]; then
  [ "${2:-}" = "iphonesimulator26.2" ]
  printf '%s\\n' '/fake/iPhoneSimulator.sdk'
  exit 0
fi
if [ "${3:-}" = "swiftc" ]; then
  exit 1
fi
printf '%s\\n' 'unexpected xcrun invocation' >&2
exit 2
""",
    )
    _write_executable(
        fake_bin / "xcodebuild",
        """#!/bin/sh
set -eu
printf '%s\\n' "$@" > "$HEALTHMES_CAPTURE_XCODEBUILD_ARGS"
""",
    )

    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["HEALTHMES_CAPTURE_XCODEBUILD_ARGS"] = str(captured_args)
    environment["HEALTHMES_SCREENTIME_SDK"] = "iphonesimulator26.2"
    result = subprocess.run(
        ["bash", str(SCREEN_TIME_SCRIPT), "build"],
        cwd=SCREEN_TIME_SCRIPT.parent.parent.parent.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    args = captured_args.read_text(encoding="utf-8").splitlines()
    assert "-sdk" not in args
    assert "generic/platform=iOS Simulator" in args


def test_screen_time_helper_accepts_version_qualified_device_sdk_alias(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    captured_args = tmp_path / "xcodebuild.args"

    _write_executable(
        fake_bin / "xcrun",
        """#!/bin/sh
set -eu
if [ "${3:-}" = "--show-sdk-path" ]; then
  [ "${2:-}" = "iphoneos26.2" ]
  printf '%s\\n' '/fake/iPhoneOS.sdk'
  exit 0
fi
if [ "${3:-}" = "swiftc" ]; then
  exit 1
fi
printf '%s\\n' 'unexpected xcrun invocation' >&2
exit 2
""",
    )
    _write_executable(
        fake_bin / "xcodebuild",
        """#!/bin/sh
set -eu
printf '%s\\n' "$@" > "$HEALTHMES_CAPTURE_XCODEBUILD_ARGS"
""",
    )

    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["HEALTHMES_CAPTURE_XCODEBUILD_ARGS"] = str(captured_args)
    environment["HEALTHMES_SCREENTIME_SDK"] = "iphoneos26.2"
    result = subprocess.run(
        ["bash", str(SCREEN_TIME_SCRIPT), "build"],
        cwd=SCREEN_TIME_SCRIPT.parent.parent.parent.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    args = captured_args.read_text(encoding="utf-8").splitlines()
    assert "-sdk" not in args
    assert "generic/platform=iOS" in args


def test_screen_time_helper_rejects_probe_and_destination_sdk_mismatch(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    _write_executable(
        fake_bin / "xcrun",
        """#!/bin/sh
set -eu
if [ "${3:-}" = "--show-sdk-path" ]; then
  printf '%s\\n' '/fake/iPhoneSimulator.sdk'
  exit 0
fi
printf '%s\\n' 'unexpected xcrun invocation' >&2
exit 2
""",
    )

    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["HEALTHMES_SCREENTIME_DESTINATION"] = "generic/platform=iOS"
    result = subprocess.run(
        ["bash", str(SCREEN_TIME_SCRIPT), "build"],
        cwd=SCREEN_TIME_SCRIPT.parent.parent.parent.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 64
    assert "requires an iOS Simulator destination" in result.stderr
