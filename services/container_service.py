from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .config_service import ConfigService, sha256_file, write_json_idempotent
from .dependency_service import DependencyService
from .version_snapshot_service import VersionSnapshotService


class ContainerService:
    """
    Single-responsibility service for container CRUD, cloning/forking,
    environment management, soft-trash archiving, and deletion.
    """

    def __init__(
        self,
        cfg: ConfigService,
        dep_service: DependencyService,
        ver_service: VersionSnapshotService,
    ) -> None:
        self.cfg = cfg
        self.dep = dep_service
        self.ver = ver_service

    def _default_meta_for(self, prefix_path: Path) -> dict[str, Any]:
        name = prefix_path.name
        blueprints = self.cfg.data.get("blueprints", [])
        chosen_bp = blueprints[0] if "chief" in name.lower() and blueprints else (blueprints[1] if len(blueprints) > 1 else {"id": "app_sandbox", "name": "Application Sandbox", "default_env": {"RAVEN_BACKTRACE": "1"}})
        default_desktop = "wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11"
        return {
            "id": name,
            "display_name": name.replace("-", " ").title(),
            "blueprint_id": chosen_bp.get("id", "app_sandbox"),
            "profile": chosen_bp.get("name", "Application Sandbox"),
            "desktop": default_desktop,
            "notes": "",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(prefix_path.stat().st_mtime)),
            "env": dict(chosen_bp.get("default_env", {"RAVEN_BACKTRACE": "1"})),
            "apps": [],
        }

    def load_meta(self, prefix_path: Path) -> dict[str, Any]:
        meta_file = prefix_path / "nest.json"
        defaults = self._default_meta_for(prefix_path)
        if meta_file.exists():
            try:
                stored = json.loads(meta_file.read_text(encoding="utf-8"))
                defaults.update(stored)
            except Exception:
                pass
        return defaults

    def save_meta(self, prefix_path: Path, meta: dict[str, Any]) -> bool:
        return write_json_idempotent(prefix_path / "nest.json", meta)

    def discover_apps(self, prefix_path: Path, meta: dict[str, Any]) -> list[dict[str, Any]]:
        drive_c = prefix_path / "drive_c"
        configured_apps = {a["path"]: a for a in meta.get("apps", [])}
        discovered: list[dict[str, Any]] = []
        skip_names = {n.lower() for n in self.cfg.data.get("discovery_excluded_executables", [])}
        default_timeout = int(self.cfg.data["ui"].get("default_timeout_ms", 86400000))

        for root in (drive_c / "Program Files", drive_c / "Program Files (x86)"):
            if not root.exists():
                continue
            for exe in sorted(root.rglob("*.exe")):
                if not exe.is_file():
                    continue
                low = exe.name.lower()
                if low in skip_names or "uninstall" in low:
                    continue
                full_str = str(exe)
                if full_str in configured_apps:
                    entry = dict(configured_apps.pop(full_str))
                    entry["exists"] = True
                    entry["pinned"] = True
                    entry["size_mb"] = round(exe.stat().st_size / (1024 * 1024), 2)
                    discovered.append(entry)
                else:
                    rel_parts = exe.relative_to(root).parts
                    vendor = rel_parts[0] if len(rel_parts) > 1 else "Installed Application"
                    discovered.append({
                        "id": hashlib.sha1(full_str.encode()).hexdigest()[:10],
                        "name": exe.stem,
                        "vendor": vendor,
                        "path": full_str,
                        "dos_path": f"C:\\{exe.relative_to(drive_c).as_posix().replace('/', chr(92))}",
                        "args": "",
                        "desktop": meta.get("desktop", "wayland"),
                        "timeout_ms": default_timeout,
                        "elevated": False,
                        "category": "application",
                        "pinned": False,
                        "exists": True,
                        "size_mb": round(exe.stat().st_size / (1024 * 1024), 2),
                    })

        for path_str, app_entry in configured_apps.items():
            p = Path(path_str)
            entry = dict(app_entry)
            entry["exists"] = p.exists()
            entry["pinned"] = True
            entry["size_mb"] = round(p.stat().st_size / (1024 * 1024), 2) if p.exists() else 0
            discovered.append(entry)

        sys_utils = [
            ("regedit.exe", "Registry Editor", drive_c / "windows" / "regedit.exe", "C:\\windows\\regedit.exe"),
            ("control.exe", "Control Panel", drive_c / "windows" / "system32" / "control.exe", "C:\\windows\\system32\\control.exe"),
            ("msiexec.exe", "Windows Installer (MSI)", drive_c / "windows" / "system32" / "msiexec.exe", "C:\\windows\\system32\\msiexec.exe"),
        ]
        for exe_name, label, exe_path, dos_path in sys_utils:
            if exe_path.exists():
                discovered.append({
                    "id": f"sys-{exe_name.replace('.', '-')}",
                    "name": label,
                    "vendor": "Raven System Tool",
                    "path": str(exe_path),
                    "dos_path": dos_path,
                    "args": "",
                    "desktop": meta.get("desktop", "wayland"),
                    "timeout_ms": 3600000,
                    "elevated": False,
                    "category": "system_tool",
                    "pinned": False,
                    "exists": True,
                    "size_mb": round(exe_path.stat().st_size / (1024 * 1024), 2),
                })

        return discovered

    def inspect_container(self, prefix_path: Path) -> dict[str, Any]:
        meta = self.load_meta(prefix_path)
        drive_c = prefix_path / "drive_c"
        reg_file = prefix_path / "registry.dat"
        registered_marker = drive_c / "windows" / "raven-registered.txt"

        sys32 = drive_c / "windows" / "system32"
        syswow64 = drive_c / "windows" / "syswow64"
        sys32_files = {p.name.lower() for p in sys32.iterdir() if p.is_file()} if sys32.exists() else set()
        syswow64_files = {p.name.lower() for p in syswow64.iterdir() if p.is_file()} if syswow64.exists() else set()

        redists_status = []
        for fam_id, fam in self.cfg.data.get("redistributables", {}).items():
            expected_dlls = {d.lower() for d in fam.get("dlls", [])}
            present64 = sorted(expected_dlls & sys32_files)
            present32 = sorted(expected_dlls & syswow64_files)
            missing64 = sorted(expected_dlls - sys32_files)
            status = "installed" if len(present64) >= max(1, len(expected_dlls) // 2) else ("partial" if present64 else "not_installed")
            redists_status.append({
                "id": fam_id,
                "name": fam["name"],
                "status": status,
                "present64": present64,
                "present32": present32,
                "missing64": missing64,
                "total_expected": len(expected_dlls),
            })

        versions = self.ver.list_versions(prefix_path)
        current_version = next((v["version"] for v in versions if v.get("matches_live")), versions[0]["version"] if versions else "v0 (live)")
        reg_hash = sha256_file(reg_file)[:12] if reg_file.exists() and reg_file.is_file() else "uninitialized"

        return {
            "id": prefix_path.name,
            "display_name": meta.get("display_name", prefix_path.name.replace("-", " ").title()),
            "blueprint_id": meta.get("blueprint_id", "app_sandbox"),
            "profile": meta.get("profile", "Application Sandbox"),
            "desktop": meta.get("desktop", "wayland"),
            "notes": meta.get("notes", ""),
            "created_at": meta.get("created_at", ""),
            "path": str(prefix_path),
            "env": meta.get("env", {"RAVEN_BACKTRACE": "1"}),
            "initialized": drive_c.exists() and reg_file.exists(),
            "registry_size_kb": round(reg_file.stat().st_size / 1024, 1) if reg_file.exists() else 0,
            "registry_hash": reg_hash,
            "current_version": current_version,
            "raven_version": meta.get("raven_version"),
            "com_registered": registered_marker.exists() and registered_marker.stat().st_size > 0,
            "sys32_count": len(sys32_files),
            "syswow64_count": len(syswow64_files),
            "redistributables": redists_status,
            "snapshots": versions,
            "apps": self.discover_apps(prefix_path, meta),
        }

    def list_containers(self) -> list[dict[str, Any]]:
        prefixes_root = self.cfg.path("prefixes_root")
        prefixes_root.mkdir(parents=True, exist_ok=True)
        result = []
        for item in sorted(prefixes_root.iterdir()):
            if not item.is_dir() or item.name.startswith(".") or item.name.endswith(".snapshots"):
                continue
            result.append(self.inspect_container(item))
        return result

    def list_trash(self) -> list[dict[str, Any]]:
        trash_root = self.cfg.path("trash_root")
        if not trash_root.exists():
            return []
        items = []
        for entry in sorted(trash_root.iterdir(), reverse=True):
            if not entry.is_dir() or entry.name.endswith(".snapshots"):
                continue
            meta = self.load_meta(entry)
            items.append({
                "trash_id": entry.name,
                "original_id": meta.get("id", entry.name),
                "display_name": meta.get("display_name", entry.name),
                "profile": meta.get("profile", "Sandbox"),
                "deleted_at": meta.get("deleted_at", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.stat().st_mtime))),
                "path": str(entry),
            })
        return items

    def create_container(self, body: dict[str, Any]) -> dict[str, Any]:
        """Idempotently creates or updates a container and initializes its baseline snapshot."""
        raw_name = body.get("name", "new-sandbox").strip()
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw_name.lower()).strip("-") or "sandbox"
        prefixes_root = self.cfg.path("prefixes_root")
        prefix_path = prefixes_root / slug
        prefix_path.mkdir(parents=True, exist_ok=True)

        blueprint_id = body.get("blueprint_id", "gaming_cad")
        blueprints = {b["id"]: b for b in self.cfg.data.get("blueprints", [])}
        bp = blueprints.get(blueprint_id, {"id": blueprint_id, "name": body.get("profile", "Application Sandbox"), "default_env": {"RAVEN_BACKTRACE": "1"}, "auto_deploy_redist": True})

        meta = self.load_meta(prefix_path)
        meta.update({
            "id": slug,
            "display_name": raw_name,
            "blueprint_id": bp["id"],
            "profile": bp["name"],
            "desktop": body.get("desktop", bp.get("default_desktop", "wayland")),
            "notes": body.get("notes", ""),
            "env": dict(bp.get("default_env", {"RAVEN_BACKTRACE": "1"})),
        })
        if isinstance(body.get("env"), dict):
            meta["env"].update(body["env"])

        if body.get("add_notepad"):
            samples = self.cfg.data.get("sample_applications", [])
            if samples and "folder" in samples[0]:
                npp_src = Path(self.cfg.expand(samples[0]["folder"]))
                npp_dst = prefix_path / "drive_c" / "Program Files" / samples[0].get("install_folder_name", "Notepad++")
                if npp_src.exists() and not npp_dst.exists():
                    shutil.copytree(npp_src, npp_dst)

        self.save_meta(prefix_path, meta)
        deploy_res = self.dep.auto_deploy(slug, deploy_redist=bool(bp.get("auto_deploy_redist", True)))

        # Create baseline v1 snapshot if none exists yet
        if not self.ver.list_versions(prefix_path):
            self.ver.create_snapshot(prefix_path, tag="baseline-v1", notes="Initial container creation baseline", pinned=True)

        return {
            "ok": True,
            "prefix": self.inspect_container(prefix_path),
            "deploy": deploy_res,
        }

    def update_container(self, prefix_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Idempotently updates container settings and environment variables."""
        prefix_path = self.cfg.path("prefixes_root") / prefix_id
        if not prefix_path.exists():
            return {"ok": False, "error": f"Container '{prefix_id}' not found"}

        meta = self.load_meta(prefix_path)
        for field in ("display_name", "profile", "blueprint_id", "desktop", "notes"):
            if field in body and body[field] is not None:
                meta[field] = str(body[field]).strip()
        if "env" in body and isinstance(body["env"], dict):
            meta["env"] = {str(k).strip(): str(v) for k, v in body["env"].items() if str(k).strip()}

        self.save_meta(prefix_path, meta)
        return {"ok": True, "prefix": self.inspect_container(prefix_path)}

    def clone_container(self, source_id: str, new_name: str, snapshot_name: str | None = None) -> dict[str, Any]:
        """
        Clones a container (either from its live state or from a historical snapshot)
        into a new isolated sandbox container.
        """
        prefixes_root = self.cfg.path("prefixes_root")
        src_prefix = prefixes_root / source_id
        if not src_prefix.exists():
            return {"ok": False, "error": f"Source container '{source_id}' does not exist"}

        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", new_name.lower()).strip("-")
        if not slug:
            return {"ok": False, "error": "Invalid target container name"}
        dst_prefix = prefixes_root / slug
        if dst_prefix.exists():
            return {"ok": False, "error": f"Container '{slug}' already exists"}

        source_tree = (
            self.ver.snapshots_dir(src_prefix) / snapshot_name
            if snapshot_name
            else src_prefix
        )
        if not source_tree.exists():
            return {"ok": False, "error": f"Source snapshot '{snapshot_name}' not found"}

        shutil.copytree(source_tree, dst_prefix, symlinks=True)
        meta = self.load_meta(src_prefix)
        meta["id"] = slug
        meta["display_name"] = new_name.strip()
        meta["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        meta["notes"] = f"Cloned from {source_id}" + (f" ({snapshot_name})" if snapshot_name else "")
        self.save_meta(dst_prefix, meta)

        self.ver.create_snapshot(
            dst_prefix,
            tag=f"cloned-from-{source_id}",
            notes=meta["notes"],
            pinned=True,
        )
        return {"ok": True, "prefix": self.inspect_container(dst_prefix)}

    def delete_container(
        self,
        prefix_id: str,
        mode: str = "trash",
        delete_snapshots: bool = False,
    ) -> dict[str, Any]:
        """
        Idempotently deletes or soft-trashes a container.
        - `mode == "trash"`: moves container and its snapshots into `.trash/` (recoverable).
        - `mode == "permanent"`: permanently removes the container directory (and snapshots if requested).
        """
        prefixes_root = self.cfg.path("prefixes_root")
        prefix_path = prefixes_root / prefix_id
        if not prefix_path.exists():
            return {"ok": True, "idempotent_noop": True, "message": f"Container '{prefix_id}' already removed."}

        snap_dir = self.ver.snapshots_dir(prefix_path)

        if mode == "trash":
            trash_root = self.cfg.path("trash_root")
            trash_root.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            trash_id = f"{prefix_id}-{stamp}"
            dst_trash = trash_root / trash_id
            meta = self.load_meta(prefix_path)
            meta["deleted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.save_meta(prefix_path, meta)
            shutil.move(str(prefix_path), str(dst_trash))
            if snap_dir.exists():
                shutil.move(str(snap_dir), str(trash_root / f"{trash_id}.snapshots"))
            return {
                "ok": True,
                "mode": "trash",
                "trash_id": trash_id,
                "message": f"Moved '{prefix_id}' to Trash (recoverable).",
            }

        # Permanent deletion
        shutil.rmtree(prefix_path)
        if delete_snapshots and snap_dir.exists():
            shutil.rmtree(snap_dir)
        return {
            "ok": True,
            "mode": "permanent",
            "message": f"Permanently deleted container '{prefix_id}'.",
        }

    def restore_from_trash(self, trash_id: str) -> dict[str, Any]:
        """Restores a soft-deleted container from `.trash/` back into active prefixes."""
        trash_root = self.cfg.path("trash_root")
        src_trash = trash_root / trash_id
        if not src_trash.exists():
            return {"ok": False, "error": f"Trash item '{trash_id}' not found"}

        meta = self.load_meta(src_trash)
        orig_id = meta.get("id") or re.sub(r"-\d{8}-\d{6}$", "", trash_id)
        prefixes_root = self.cfg.path("prefixes_root")
        dst_prefix = prefixes_root / orig_id
        if dst_prefix.exists():
            orig_id = f"{orig_id}-restored"
            dst_prefix = prefixes_root / orig_id
            meta["id"] = orig_id

        meta.pop("deleted_at", None)
        self.save_meta(src_trash, meta)
        shutil.move(str(src_trash), str(dst_prefix))

        src_snaps = trash_root / f"{trash_id}.snapshots"
        if src_snaps.exists():
            shutil.move(str(src_snaps), str(self.ver.snapshots_dir(dst_prefix)))

        return {"ok": True, "prefix": self.inspect_container(dst_prefix)}

    def purge_trash_item(self, trash_id: str) -> dict[str, Any]:
        """Permanently deletes an item from `.trash/`."""
        trash_root = self.cfg.path("trash_root")
        target = trash_root / trash_id
        snaps = trash_root / f"{trash_id}.snapshots"
        if target.exists():
            shutil.rmtree(target)
        if snaps.exists():
            shutil.rmtree(snaps)
        return {"ok": True, "trash": self.list_trash()}

    def remove_pinned_app(self, prefix_id: str, app_id: str) -> dict[str, Any]:
        prefix_path = self.cfg.path("prefixes_root") / prefix_id
        if not prefix_path.exists():
            return {"ok": False, "error": "Container not found"}
        meta = self.load_meta(prefix_path)
        meta["apps"] = [a for a in meta.get("apps", []) if a.get("id") != app_id]
        self.save_meta(prefix_path, meta)
        return {"ok": True, "prefix": self.inspect_container(prefix_path)}
