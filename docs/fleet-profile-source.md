# Fleet profile sources

TartCI uses the checked-in `profiles/` and `fleet/advertised-labels.json` by
default. This keeps a normal checkout self-contained.

An operator can select an installed private source for offline fleet tooling:

```sh
export TARTCI_FLEET_SOURCE_ROOT=/path/to/fleet-source
export TARTCI_FLEET_SOURCE_COMMIT=0123456789abcdef0123456789abcdef01234567
```

The source directory must contain:

```text
profiles/*-macos-fleet.toml
fleet/advertised-labels.json
```

TartCI validates both paths before reading the source. If an explicit source
is missing, empty, or invalid, the command fails. It does not silently use the
public checkout instead. `TARTCI_FLEET_SOURCE_COMMIT` is optional for local
read-only commands and should be set by an installed private source so receipts
can identify the exact desired-state revision.

This setting does not move or publish fleet profiles. Keep host identities,
paths, labels, receipts, and snapshots in a private operator repository. Keep
generic schemas and examples in TartCI.
