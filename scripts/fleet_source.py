#!/usr/bin/env python3
"""Resolve the fleet source used by TartCI's offline tooling.

The public checkout remains the default. Operators may point a process at an
installed private source with ``TARTCI_FLEET_SOURCE_ROOT``. An explicit source
is validated and fails closed; it never silently falls back to the checkout.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


PROFILE_GLOB = "*-macos-fleet.toml"
COMMIT = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class FleetSource:
    root: Path
    profiles_dir: Path
    published_supply: Path
    configured: bool

    @property
    def source_commit(self) -> str | None:
        value = os.environ.get("TARTCI_FLEET_SOURCE_COMMIT", "").strip()
        return value or None


def resolve(default_root: Path, *, allow_environment: bool = True) -> FleetSource:
    """Return the active source, rejecting an invalid explicit source."""
    configured_root = (
        os.environ.get("TARTCI_FLEET_SOURCE_ROOT", "").strip()
        if allow_environment else ""
    )
    root = Path(configured_root).expanduser() if configured_root else default_root
    root = root.resolve()
    profiles_dir = root / "profiles"
    published_supply = root / "fleet" / "advertised-labels.json"
    if configured_root:
        if not profiles_dir.is_dir():
            raise ValueError(f"configured fleet source has no profiles directory: {profiles_dir}")
        if not published_supply.is_file():
            raise ValueError(
                "configured fleet source has no published supply: "
                f"{published_supply}"
            )
        if not list(profiles_dir.glob(PROFILE_GLOB)):
            raise ValueError(f"configured fleet source has no macOS fleet profiles: {profiles_dir}")
        configured_commit = os.environ.get("TARTCI_FLEET_SOURCE_COMMIT", "").strip()
        if not COMMIT.fullmatch(configured_commit):
            raise ValueError(
                "configured fleet source requires TARTCI_FLEET_SOURCE_COMMIT "
                "to be a 40-character commit"
            )
    return FleetSource(root, profiles_dir, published_supply, bool(configured_root))

