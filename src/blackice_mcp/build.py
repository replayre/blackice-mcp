from __future__ import annotations

import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BuildResult:
    ok: bool
    command: list[str]
    output: str
    artifact_dir: Path | None = None
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "ok": self.ok,
            "command": " ".join(self.command),
            "artifact_dir": str(self.artifact_dir) if self.artifact_dir else None,
            "output": self.output,
        }
        if self.hint:
            payload["hint"] = self.hint
        return payload


@dataclass
class DeployResult:
    ok: bool
    bundle_dir: Path
    copied: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "bundle_dir": str(self.bundle_dir),
            "copied": sorted(self.copied),
            "missing": sorted(self.missing),
        }


RESTORE_HINT = (
    "A package could not be resolved. If a newer version was published, refresh the "
    "floating versions yourself with 'dotnet restore --force-evaluate' in the project "
    "directory, then build again. This tool does not restore for you: force-evaluate "
    "moves floating versions and can pull a package whose ABI differs from your server build."
)

RESTORE_MARKERS = ("NU1101", "NU1102", "NU1103")


def find_dist_dir(project_dir: Path, config: str = "Release") -> Path | None:
    for candidate in (project_dir / "dist", project_dir / "bin" / config):
        if candidate.is_dir():
            return candidate
    return None


def run_dotnet_build(project_dir: Path, config: str = "Release", timeout: float = 900.0) -> BuildResult:
    command = ["dotnet", "build", "-c", config, "--nologo"]

    try:
        proc = subprocess.run(
            command,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return BuildResult(ok=False, command=command, output="dotnet was not found on PATH")
    except subprocess.TimeoutExpired:
        return BuildResult(ok=False, command=command, output=f"dotnet build timed out after {timeout:.0f}s")

    output = (proc.stdout or "") + (proc.stderr or "")
    dist = find_dist_dir(project_dir, config) if proc.returncode == 0 else None

    unresolved = proc.returncode != 0 and any(marker in output for marker in RESTORE_MARKERS)

    return BuildResult(
        ok=proc.returncode == 0,
        command=command,
        output=output,
        artifact_dir=dist,
        hint=RESTORE_HINT if unresolved else None,
    )


def _manifest_paths(source_dir: Path) -> list[str]:
    config = source_dir / "config.toml"

    try:
        with config.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []

    files = data.get("ReUi", {}).get("files", [])

    return [str(entry) for entry in files] if isinstance(files, list) else []


def _landed(bundle_dir: Path, entry: str) -> bool:
    if any(character in entry for character in "*?["):
        return any(bundle_dir.glob(entry))

    return (bundle_dir / entry).is_file()


def stage_bundle(source_dir: Path, server_dir: Path, name: str) -> DeployResult:
    if not source_dir.is_dir():
        raise FileNotFoundError(f"bundle source '{source_dir}' does not exist")

    if not (source_dir / "config.toml").is_file():
        raise FileNotFoundError(f"'{source_dir}' has no config.toml; not a bundle directory")

    bundle_dir = server_dir / "data" / "ugc" / name
    bundle_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []

    for entry in sorted(source_dir.rglob("*")):
        if not entry.is_file():
            continue

        relative = entry.relative_to(source_dir)
        destination = bundle_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry, destination)
        copied.append(relative.as_posix())

    missing = [entry for entry in [*_manifest_paths(source_dir), "config.toml"] if not _landed(bundle_dir, entry)]

    return DeployResult(ok=not missing, bundle_dir=bundle_dir, copied=copied, missing=missing)
