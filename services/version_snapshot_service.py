from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .config_service import ConfigService, sha256_file, write_json_idempotent
import json


class VersionSnapshotService:
    """
    Single-responsibility service for container versioning, snapshotting,
    state fingerprinting, rollback, and retention pruning.
    """

    def __init__(self, cfg: ConfigService) -> None:
        self.cfg = cfg

    def snapshots_dir(self, prefix_path: Path) -> Path:
        return prefix_path.parent / f"{prefix_path.name}.snapshots"

    def ledger_path(self, prefix_path: Path) -> Path:
        return self.snapshots_dir(prefix_path) / "versions.json"

    def fingerprint_prefix(self, root_dir: Path) -> dict[str, Any]:
        """Computes a lightweight, deterministic fingerprint of a prefix or snapshot."""
        reg = root_dir / "registry.dat"
        reg_hash = sha256_file(reg)[:12] if reg.exists() and reg.is_file() else "uninitialized"
        reg_kb = round(reg.stat().st_size / 1024, 1) if reg.exists() else 0.0
        sys32 = root_dir / "drive_c" / "windows" / "system32"
        sys32_count = sum(1 for p in sys32.iterdir() if p.is_file()) if sys32.exists() else 0
        return {
            "registry_hash": reg_hash,
            "registry_size_kb": reg_kb,
            "sys32_count": sys32_count,
        }

    def list_versions(self, prefix_path: Path) -> list[dict[str, Any]]:
        """
        Idempotently reconciles on-disk snapshot folders with `versions.json`
        and returns chronological version entries (newest first).
        """
        sdir = self.snapshots_dir(prefix_path)
        if not sdir.exists():
            return []

        ledger_file = self.ledger_path(prefix_path)
        ledger: dict[str, Any] = {"seq": 0, "entries": {}}
        if ledger_file.exists():
            try:
                ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
            except Exception:
                ledger = {"seq": 0, "entries": {}}

        entries_map: dict[str, dict[str, Any]] = ledger.get("entries", {})
        seq = int(ledger.get("seq", 0))

        # Discover all valid snapshot directories sorted oldest-to-newest for version numbering
        on_disk = sorted(
            [d for d in sdir.iterdir() if d.is_dir() and (d / "drive_c").exists()],
            key=lambda p: p.name,
        )

        live_fp = self.fingerprint_prefix(prefix_path)
        updated = False

        active_names = {d.name for d in on_disk}
        # Remove deleted snapshots from ledger
        for old_name in list(entries_map.keys()):
            if old_name not in active_names:
                entries_map.pop(old_name, None)
                updated = True

        for d in on_disk:
            if d.name not in entries_map:
                seq += 1
                parts = d.name.split("-", 2)
                tag = parts[2] if len(parts) == 3 else "snapshot"
                fp = self.fingerprint_prefix(d)
                entries_map[d.name] = {
                    "version": f"v{seq}",
                    "version_num": seq,
                    "name": d.name,
                    "tag": tag,
                    "notes": "Auto-captured before installer" if "install-" in tag else "Container checkpoint",
                    "pinned": False,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d.stat().st_mtime)),
                    "registry_hash": fp["registry_hash"],
                    "registry_size_kb": fp["registry_size_kb"],
                    "sys32_count": fp["sys32_count"],
                }
                updated = True

        if updated:
            write_json_idempotent(ledger_file, {"seq": seq, "entries": entries_map})

        result = []
        for d in reversed(on_disk):
            meta = dict(entries_map[d.name])
            meta["matches_live"] = (
                meta.get("registry_hash") != "uninitialized"
                and meta.get("registry_hash") == live_fp["registry_hash"]
            )
            result.append(meta)
        return result

    def create_snapshot(
        self,
        prefix_path: Path,
        tag: str = "manual",
        notes: str = "",
        pinned: bool = False,
    ) -> dict[str, Any]:
        """
        Creates a versioned snapshot of `prefix_path`.
        Ensures `drive_c` and `registry.dat` exist first so `raven --snapshot-prefix`
        succeeds idempotently even on brand-new containers.
        """
        (prefix_path / "drive_c" / "windows" / "system32").mkdir(parents=True, exist_ok=True)
        reg_file = prefix_path / "registry.dat"
        if not reg_file.exists():
            reg_file.write_bytes(b"")

        clean_tag = re.sub(r"[^a-zA-Z0-9._-]+", "-", tag.strip()).strip("-") or "manual"
        raven_bin = self.cfg.path("raven_bin")
        env = os.environ.copy()
        env["RAVEN_PREFIX"] = str(prefix_path)

        proc = subprocess.run(
            [str(raven_bin), "--snapshot-prefix", clean_tag],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or proc.stdout).strip()}

        snap_name = proc.stdout.strip().splitlines()[-1]
        # Reconcile ledger and attach user notes/pin state
        self.list_versions(prefix_path)
        ledger_file = self.ledger_path(prefix_path)
        if ledger_file.exists():
            ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
            if snap_name in ledger.get("entries", {}):
                if notes.strip():
                    ledger["entries"][snap_name]["notes"] = notes.strip()
                ledger["entries"][snap_name]["pinned"] = bool(pinned)
                ledger["entries"][snap_name]["tag"] = clean_tag
                write_json_idempotent(ledger_file, ledger)

        return {
            "ok": True,
            "snapshot_name": snap_name,
            "versions": self.list_versions(prefix_path),
        }

    def rollback(self, prefix_path: Path, snapshot_name: str | None = None) -> dict[str, Any]:
        """
        Rolls back `prefix_path` to `snapshot_name` (or the latest snapshot).
        Note: `raven --restore-prefix` automatically saves the current prefix state
        as a `before-restore` snapshot, making rollbacks 100% non-destructive!
        """
        raven_bin = self.cfg.path("raven_bin")
        env = os.environ.copy()
        env["RAVEN_PREFIX"] = str(prefix_path)
        cmd = [str(raven_bin), "--restore-prefix"]
        if snapshot_name:
            cmd.append(snapshot_name)

        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or proc.stdout).strip()}

        return {
            "ok": True,
            "message": proc.stdout.strip(),
            "versions": self.list_versions(prefix_path),
        }

    def update_version_meta(
        self, prefix_path: Path, snapshot_name: str, notes: str | None = None, pinned: bool | None = None
    ) -> dict[str, Any]:
        """Idempotently updates notes or pin flag on a version entry."""
        self.list_versions(prefix_path)
        ledger_file = self.ledger_path(prefix_path)
        if not ledger_file.exists():
            return {"ok": False, "error": "Version ledger not found"}
        ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
        entry = ledger.get("entries", {}).get(snapshot_name)
        if not entry:
            return {"ok": False, "error": f"Snapshot {snapshot_name} not found"}
        if notes is not None:
            entry["notes"] = notes.strip()
        if pinned is not None:
            entry["pinned"] = bool(pinned)
        write_json_idempotent(ledger_file, ledger)
        return {"ok": True, "versions": self.list_versions(prefix_path)}

    def delete_version(self, prefix_path: Path, snapshot_name: str) -> dict[str, Any]:
        """Idempotently deletes a specific snapshot version."""
        sdir = self.snapshots_dir(prefix_path) / snapshot_name
        if sdir.exists() and sdir.is_dir():
            shutil.rmtree(sdir)
        return {"ok": True, "versions": self.list_versions(prefix_path)}

    def prune_unpinned(self, prefix_path: Path, keep_count: int | None = None) -> dict[str, Any]:
        """Prunes old unpinned snapshots down to `keep_count`."""
        limit = keep_count if keep_count is not None else int(self.cfg.data["snapshots"]["max_retained_auto"])
        versions = self.list_versions(prefix_path)
        unpinned = [v for v in versions if not v.get("pinned", False)]
        removed = []
        for victim in unpinned[limit:]:
            target = self.snapshots_dir(prefix_path) / victim["name"]
            if target.exists():
                shutil.rmtree(target)
                removed.append(victim["name"])
        return {
            "ok": True,
            "removed": removed,
            "versions": self.list_versions(prefix_path),
        }
