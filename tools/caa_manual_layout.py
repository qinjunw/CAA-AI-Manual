"""Release registry, distribution paths, and local source profiles."""

from __future__ import annotations

import json
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_registry = json.loads((PROJECT_ROOT / "config/releases.json").read_text(encoding="utf-8-sig"))
RELEASES = _registry["releases"]
DEFAULT_RELEASE = _registry["default_release"]
RELEASE_ID = re.compile(r"R[1-9][0-9]*")
if DEFAULT_RELEASE not in RELEASES or any(not RELEASE_ID.fullmatch(key) for key in RELEASES):
    raise ValueError("Invalid config/releases.json: use release IDs such as R21 and a registered default_release")


def release_id(value: str) -> str:
    if not isinstance(value, str) or not RELEASE_ID.fullmatch(value):
        raise ValueError(f"Unsupported source_release {value!r}; expected R followed by a positive integer")
    return value


def release_metadata(source_release: str) -> dict[str, str]:
    release_id(source_release)
    if source_release in RELEASES:
        entry = RELEASES[source_release]
        return {"source_release": source_release, "source_baseline": entry["source_baseline"],
                "version_policy": entry["version_policy"]}
    return {"source_release": source_release, "source_baseline": f"Local {source_release} CAADoc",
            "version_policy": f"User-supplied {source_release} documentation; verify the source installation."}


def indexed_release(metadata: dict[str, str]) -> str:
    """Older indexes record the V5 release only in the documentation baseline."""
    legacy = re.fullmatch(r"CATIA V5(R[0-9]+) CAADoc", metadata.get("source_baseline", ""))
    return metadata.get("source_release") or (legacy[1] if legacy else "unknown")


def public_database(root: Path, release: str) -> Path:
    release_id(release)
    return root / "data" / release.lower() / "manual.sqlite"


def load_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.strip().partition("=")
        key = key.strip()
        if sep and key and not key.startswith("#"):
            value = value.strip().strip('"').strip("'")
            if value and key in ("CAA_CAADOC_ROOT", "CAA_AI_MANUAL_ROOT", "CAA_AI_MANUAL_DB"):
                value = str((path.parent / value).resolve())
            values[key] = value
    return values


def release_source(root: Path, release: str) -> str | None:
    release_id(release)
    profile = root / f".env.{release.lower()}"
    if not profile.is_file() and release == DEFAULT_RELEASE:
        profile = root / ".env"
    return load_env_file(profile).get("CAA_CAADOC_ROOT")


def installation_releases(source: Path) -> set[str]:
    return {f"R{match[1]}" for part in source.resolve().parts
            if (match := re.fullmatch(r"B([0-9]+)", part, flags=re.I))}
