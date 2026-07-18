"""Secure staging and immutable installation of local plugin packages."""

from __future__ import annotations

import os
import shutil
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import yaml

from personal_agent.plugins.core.models import PluginManifest
from personal_agent.plugins.runtime.identity import package_digest

_MANIFEST_NAMES = ("plugin.yaml", "plugin.yml", "plugin.json")
_FORBIDDEN_INSTALL_FILES = {"install.sh", "postinstall.py", "post_install.py"}


@dataclass(frozen=True)
class PreparedPluginPackage:
    manifest: PluginManifest
    digest: str
    path: Path
    source: str
    newly_created: bool


class PluginInstaller:
    def __init__(
        self,
        root: Path,
        *,
        max_files: int = 2_000,
        max_total_bytes: int = 100 * 1024 * 1024,
        max_file_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        self.root = Path(root)
        self.packages_root = self.root / "packages"
        self.staging_root = self.root / "staging"
        self.data_root = self.root / "data"
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self.max_file_bytes = max_file_bytes

    def prepare(self, source: Path | str) -> PreparedPluginPackage:
        source_path = Path(source).expanduser().resolve()
        if not source_path.exists():
            raise ValueError(f"Plugin source does not exist: {source_path}")
        self.staging_root.mkdir(parents=True, exist_ok=True)
        transaction = self.staging_root / uuid4().hex
        transaction.mkdir(parents=True)
        try:
            extracted = transaction / "package"
            if source_path.is_dir():
                self._copy_directory(source_path, extracted)
            elif source_path.suffix.lower() == ".zip":
                self._extract_zip(source_path, extracted)
            elif source_path.suffix.lower() in {".tar", ".gz", ".tgz", ".bz2", ".xz"}:
                self._extract_tar(source_path, extracted)
            else:
                raise ValueError("Plugin source must be a directory, zip, or tar archive")
            package_root = self._resolve_package_root(extracted)
            self._validate_tree(package_root)
            manifest = self._read_manifest(package_root)
            digest = package_digest(package_root)
            destination = self.packages_root / _safe_key(manifest.key) / digest
            if destination.exists():
                manifest.path = destination
                return PreparedPluginPackage(manifest, digest, destination, str(source_path), False)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(package_root, destination)
            manifest.path = destination
            return PreparedPluginPackage(manifest, digest, destination, str(source_path), True)
        finally:
            shutil.rmtree(transaction, ignore_errors=True)

    def discard(self, package: PreparedPluginPackage) -> None:
        if package.newly_created:
            shutil.rmtree(package.path, ignore_errors=True)

    def cleanup_staging(self) -> None:
        if not self.staging_root.exists():
            return
        for child in self.staging_root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)

    def _copy_directory(self, source: Path, destination: Path) -> None:
        for path in source.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"Plugin package contains a symbolic link: {path}")
        shutil.copytree(source, destination)

    def _extract_zip(self, source: Path, destination: Path) -> None:
        destination.mkdir(parents=True)
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            self._validate_archive_sizes(
                (info.file_size for info in members if not info.is_dir()),
            )
            for info in members:
                target = _safe_archive_target(destination, info.filename)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as reader, target.open("wb") as writer:
                    shutil.copyfileobj(reader, writer)

    def _extract_tar(self, source: Path, destination: Path) -> None:
        destination.mkdir(parents=True)
        with tarfile.open(source) as archive:
            members = archive.getmembers()
            self._validate_archive_sizes(
                (member.size for member in members if member.isfile()),
            )
            for member in members:
                if member.issym() or member.islnk():
                    raise ValueError(f"Plugin archive contains a link: {member.name}")
                target = _safe_archive_target(destination, member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                reader = archive.extractfile(member)
                if reader is None:
                    continue
                with reader, target.open("wb") as writer:
                    shutil.copyfileobj(reader, writer)

    def _validate_archive_sizes(self, sizes) -> None:
        count = 0
        total = 0
        for size in sizes:
            count += 1
            total += int(size)
            if int(size) > self.max_file_bytes:
                raise ValueError("Plugin archive contains an oversized file")
            if count > self.max_files or total > self.max_total_bytes:
                raise ValueError("Plugin archive exceeds installation limits")

    def _resolve_package_root(self, extracted: Path) -> Path:
        direct = [extracted / name for name in _MANIFEST_NAMES if (extracted / name).is_file()]
        if len(direct) == 1:
            return extracted
        manifests = sorted(
            path for path in extracted.rglob("*") if path.is_file() and path.name in _MANIFEST_NAMES
        )
        if len(manifests) != 1:
            raise ValueError(f"Plugin source must contain exactly one manifest, found {len(manifests)}")
        return manifests[0].parent

    def _validate_tree(self, root: Path) -> None:
        files = 0
        total = 0
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"Plugin package contains a symbolic link: {path}")
            if not path.is_file():
                continue
            files += 1
            size = path.stat().st_size
            total += size
            if path.name.lower() in _FORBIDDEN_INSTALL_FILES:
                raise ValueError(f"Plugin package contains a forbidden install script: {path.name}")
            if size > self.max_file_bytes:
                raise ValueError(f"Plugin file is too large: {path}")
            if files > self.max_files or total > self.max_total_bytes:
                raise ValueError("Plugin package exceeds installation limits")

    @staticmethod
    def _read_manifest(root: Path) -> PluginManifest:
        manifests = [root / name for name in _MANIFEST_NAMES if (root / name).is_file()]
        if len(manifests) != 1:
            raise ValueError("Plugin package must contain exactly one manifest at its root")
        path = manifests[0]
        if path.suffix == ".json":
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        manifest = PluginManifest.from_mapping(data, source="installed", path=root)
        module = manifest.entrypoint.partition(":")[0].split(".", 1)[0]
        if not (root / f"{module}.py").is_file() and not (root / module / "__init__.py").is_file():
            raise ValueError(f"Plugin entrypoint module is outside or missing from package: {module}")
        return manifest


def _safe_archive_target(root: Path, member: str) -> Path:
    candidate = (root / member).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ValueError(f"Plugin archive path escapes package root: {member}")
    return candidate


def _safe_key(plugin_key: str) -> str:
    return plugin_key.replace("/", "__")
