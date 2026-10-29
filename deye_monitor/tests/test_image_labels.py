"""Label policy of the app image (PKG-05). The pure checks always run; the image inspection runs
when ``DEYE_IMAGE`` (and optionally ``DEYE_IMAGE_ARCH``, default amd64) names a built image:

    docker build --platform linux/amd64 -t deye-monitor:labels deye_monitor
    DEYE_IMAGE=deye-monitor:labels uv run pytest tests/test_image_labels.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tests.check_image_labels import labels_of, problems, read_version

APP = Path(__file__).parents[1]
GOOD = {
    "io.hass.type": "app",
    "io.hass.arch": "amd64",
    "io.hass.version": "0.1.0",
    "io.hass.base.version": "2026.10.3",
    "io.hass.base.image": "ghcr.io/home-assistant/base:3.24",
    "io.hass.base.name": "python",
    "org.opencontainers.image.version": "0.1.0",
    "org.opencontainers.image.source": "https://github.com/privatecoder/deye-sg04lp3-ha-addon",
    "org.opencontainers.image.url": "https://github.com/privatecoder/deye-sg04lp3-ha-addon",
}


def test_dockerfile_declares_the_app_labels() -> None:
    text = (APP / "Dockerfile").read_text()
    for key in ("source", "url", "version", "documentation"):
        assert f"org.opencontainers.image.{key}=" in text, key
    assert 'org.opencontainers.image.version="${BUILD_VERSION}"' in text
    assert 'io.hass.version="${BUILD_VERSION}"' in text
    default = re.search(r"^ARG BUILD_VERSION=(\S+)$", text, re.M)
    assert default
    assert default[1] == read_version()  # the default build matches the manifest version


def test_label_policy_accepts_a_correct_image() -> None:
    assert problems(GOOD, "amd64", "0.1.0") == []


def test_label_policy_rejects_inherited_base_metadata() -> None:
    bad = {
        **GOOD,
        "org.opencontainers.image.source": "https://github.com/home-assistant/docker-base",
        "org.opencontainers.image.version": "2026.10.3",
    }
    found = problems(bad, "amd64", "0.1.0")
    assert any("base project" in p for p in found)
    assert any("org.opencontainers.image.version" in p for p in found)
    assert any("differs" in p for p in found)
    assert problems({**GOOD, "io.hass.arch": "aarch64"}, "amd64", "0.1.0")
    assert problems(
        {k: v for k, v in GOOD.items() if k != "io.hass.base.version"}, "amd64", "0.1.0"
    )
    assert problems({**GOOD, "io.hass.base.version": "0.1.0"}, "amd64", "0.1.0")


@pytest.mark.skipif(
    "DEYE_IMAGE" not in os.environ, reason="set DEYE_IMAGE to inspect a built image"
)
def test_built_image_labels() -> None:
    arch = os.environ.get("DEYE_IMAGE_ARCH", "amd64")
    found = problems(labels_of(os.environ["DEYE_IMAGE"]), arch, read_version())
    assert found == []
