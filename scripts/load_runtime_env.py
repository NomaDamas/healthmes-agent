#!/usr/bin/env python3
"""Emit dotenv files as NUL-delimited environment entries without shell evaluation."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from dotenv import dotenv_values

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def main() -> int:
    merged: dict[str, str] = {}
    for raw_path in sys.argv[1:]:
        path = Path(raw_path)
        if not path.is_file():
            continue
        for key, value in dotenv_values(path, interpolate=False).items():
            if not _ENV_NAME.fullmatch(key):
                raise ValueError(f"invalid environment variable name in {path}: {key!r}")
            if value is not None:
                if "\0" in value:
                    raise ValueError(f"NUL byte in environment value from {path}: {key}")
                merged[key] = value

    output = sys.stdout.buffer
    for key, value in merged.items():
        output.write(key.encode("utf-8"))
        output.write(b"\0")
        output.write(value.encode("utf-8"))
        output.write(b"\0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
