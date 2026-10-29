"""Check the labels of a built app image (stdlib only; used by pytest and by the CI build job).

Usage: ``python3 check_image_labels.py <image> <amd64|aarch64>``. Exits non-zero on a problem.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).parents[1]
BASE_PROJECT = "https://github.com/home-assistant/docker-base"


def read_version() -> str:
    match = re.search(r'^version:\s*"([^"]+)"', (APP / "config.yaml").read_text(), re.M)
    assert match
    return match[1]


def labels_of(image: str) -> dict[str, str]:
    out = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{json .Config.Labels}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return dict(json.loads(out))


def problems(labels: dict[str, str], arch: str, version: str) -> list[str]:
    out: list[str] = []

    def expect(key: str, want: str) -> None:
        if labels.get(key) != want:
            out.append(f"{key}={labels.get(key)!r}, expected {want!r}")

    expect("io.hass.type", "app")
    expect("io.hass.arch", arch)
    expect("io.hass.version", version)
    expect("org.opencontainers.image.version", version)
    source = labels.get("org.opencontainers.image.source", "")
    if not source or source.startswith(BASE_PROJECT):
        out.append(f"org.opencontainers.image.source belongs to the base project: {source!r}")
    if labels.get("org.opencontainers.image.url") != source:
        out.append("org.opencontainers.image.url differs from source")
    for key in ("io.hass.base.version", "io.hass.base.image", "io.hass.base.name"):
        if not labels.get(key):
            out.append(f"base provenance label {key} is missing")
    if labels.get("io.hass.base.version") == version:
        out.append("base version and app version must be kept separately")
    return out


def main() -> int:
    image, arch = sys.argv[1], sys.argv[2]
    found = problems(labels_of(image), arch, read_version())
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
