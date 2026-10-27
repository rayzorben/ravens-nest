#!/usr/bin/env python3
"""
Raven Nest UI — Thin HTTP Controller & Server Entry Point.

Follows DRY, KISS, SOLID, Idempotence, and Everything as Config.
All configuration lives in `ui/config/default.json` and `~/.config/raven-nest/config.json`.
Business logic is separated into single-responsibility services under `ui/services/`.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import socketserver
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any

from services.config_service import ConfigService
from services.container_service import ContainerService
from services.dependency_service import DependencyService
from services.doctor_service import DoctorService
from services.process_service import ProcessService
from services.version_snapshot_service import VersionSnapshotService

UI_DIR = Path(__file__).resolve().parent
STATIC_DIR = UI_DIR / "static"

CFG = ConfigService(UI_DIR)
VER_SVC = VersionSnapshotService(CFG)
DEP_SVC = DependencyService(CFG)
CONTAINER_SVC = ContainerService(CFG, DEP_SVC, VER_SVC)
DOCTOR_SVC = DoctorService(CFG)
PROC_SVC = ProcessService(CFG, CONTAINER_SVC, DEP_SVC, DOCTOR_SVC)


class RavenAPIHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def send_json(self, data: Any, status: int = 200) -> None:
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/api/state":
            sample_apps = []
            for item in CFG.data.get("sample_applications", []):
                expanded = Path(CFG.expand(item["path"]))
                sample_apps.append({
                    "name": item["name"],
                    "path": str(expanded),
                    "exists": expanded.exists(),
                })
            self.send_json({
                "config": CFG.export_client_config(),
                "prefixes": CONTAINER_SVC.list_containers(),
                "trash": CONTAINER_SVC.list_trash(),
                "doctor": DOCTOR_SVC.evaluate(),
                "processes": PROC_SVC.list_processes(),
                "sample_apps": sample_apps,
            })
            return

        if path == "/api/config":
            self.send_json(CFG.export_client_config())
            return

        if path == "/api/inspect":
            prefix_id = query.get("prefix_id", ["default"])[0]
            exe_path = query.get("path", [""])[0]
            self.send_json(DEP_SVC.analyze_executable(prefix_id, exe_path))
            return

        if path == "/api/fs":
            dir_str = query.get("dir", [str(Path.home())])[0]
            target = Path(dir_str)
            if not target.exists() or not target.is_dir():
                target = Path.home()
            entries = []
            try:
                for item in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                    if item.name.startswith(".") and item.name != ".local":
                        continue
                    if item.is_dir():
                        entries.append({"name": item.name, "path": str(item), "type": "dir"})
                    elif item.suffix.lower() in (".exe", ".msi"):
                        entries.append({
                            "name": item.name,
                            "path": str(item),
                            "type": "file",
                            "size_mb": round(item.stat().st_size / (1024 * 1024), 2),
                        })
            except PermissionError:
                pass
            self.send_json({
                "current": str(target),
                "parent": str(target.parent),
                "entries": entries[:250],
            })
            return

        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self.read_json()

        # Config endpoints ("Everything as Config")
        if path == "/api/config/update":
            self.send_json({"ok": True, "config": CFG.update_overrides(body.get("overrides", body))})
            return

        if path == "/api/config/reset":
            self.send_json({"ok": True, "config": CFG.reset_overrides()})
            return

        # Container lifecycle endpoints
        if path == "/api/prefix/create":
            self.send_json(CONTAINER_SVC.create_container(body))
            return

        if path == "/api/prefix/update":
            prefix_id = body.get("prefix_id", "")
            self.send_json(CONTAINER_SVC.update_container(prefix_id, body))
            return

        if path == "/api/prefix/clone":
            self.send_json(CONTAINER_SVC.clone_container(
                body.get("source_id", ""),
                body.get("new_name", ""),
                body.get("snapshot_name"),
            ))
            return

        if path == "/api/prefix/delete":
            self.send_json(CONTAINER_SVC.delete_container(
                body.get("prefix_id", ""),
                mode=body.get("mode", "trash"),
                delete_snapshots=bool(body.get("delete_snapshots", False)),
            ))
            return

        if path == "/api/trash/restore":
            self.send_json(CONTAINER_SVC.restore_from_trash(body.get("trash_id", "")))
            return

        if path == "/api/trash/purge":
            self.send_json(CONTAINER_SVC.purge_trash_item(body.get("trash_id", "")))
            return

        if path == "/api/prefix/deploy":
            prefix_id = body.get("prefix_id", "default")
            exe_path = body.get("path")
            self.send_json(DEP_SVC.auto_deploy(prefix_id, exe_path))
            return

        if path == "/api/prefix/add-app":
            prefix_id = body.get("prefix_id", "default")
            exe_path = Path(body.get("path", ""))
            if not exe_path.exists():
                self.send_json({"ok": False, "error": f"File not found: {exe_path}"}, 400)
                return
            prefix_path = CFG.path("prefixes_root") / prefix_id
            meta = CONTAINER_SVC.load_meta(prefix_path)
            apps = [a for a in meta.get("apps", []) if a.get("path") != str(exe_path)]
            default_timeout = int(CFG.data["ui"].get("default_timeout_ms", 86400000))
            apps.append({
                "id": hashlib.sha1(str(exe_path).encode()).hexdigest()[:10],
                "name": body.get("name") or exe_path.stem,
                "vendor": body.get("vendor", "Custom Application"),
                "path": str(exe_path),
                "dos_path": f"Z:\\{str(exe_path).lstrip('/').replace('/', chr(92))}",
                "args": body.get("args", ""),
                "desktop": body.get("desktop", meta.get("desktop", "wayland")),
                "timeout_ms": int(body.get("timeout_ms", default_timeout)),
                "elevated": bool(body.get("elevated", False)),
                "category": "application",
            })
            meta["apps"] = apps
            CONTAINER_SVC.save_meta(prefix_path, meta)
            self.send_json({"ok": True, "prefix": CONTAINER_SVC.inspect_container(prefix_path)})
            return

        if path == "/api/prefix/remove-app":
            self.send_json(CONTAINER_SVC.remove_pinned_app(body.get("prefix_id", ""), body.get("app_id", "")))
            return

        # Versioning, Snapshotting & Rollback endpoints
        if path == "/api/prefix/snapshot":
            prefix_id = body.get("prefix_id", "default")
            action = body.get("action", "take")
            prefix_path = CFG.path("prefixes_root") / prefix_id

            if action == "take":
                res = VER_SVC.create_snapshot(
                    prefix_path,
                    tag=body.get("label", CFG.data["snapshots"]["default_manual_tag"]),
                    notes=body.get("notes", ""),
                    pinned=bool(body.get("pinned", False)),
                )
                res["output"] = res.get("snapshot_name") or res.get("error", "")
                res["prefix"] = CONTAINER_SVC.inspect_container(prefix_path)
                self.send_json(res)
                return

            if action == "restore":
                res = VER_SVC.rollback(prefix_path, body.get("snapshot_name"))
                res["output"] = res.get("message") or res.get("error", "")
                res["prefix"] = CONTAINER_SVC.inspect_container(prefix_path)
                self.send_json(res)
                return

            if action == "delete_version":
                res = VER_SVC.delete_version(prefix_path, body.get("snapshot_name", ""))
                res["prefix"] = CONTAINER_SVC.inspect_container(prefix_path)
                self.send_json(res)
                return

            if action == "update_version":
                res = VER_SVC.update_version_meta(
                    prefix_path,
                    body.get("snapshot_name", ""),
                    notes=body.get("notes"),
                    pinned=body.get("pinned"),
                )
                res["prefix"] = CONTAINER_SVC.inspect_container(prefix_path)
                self.send_json(res)
                return

            if action == "prune":
                res = VER_SVC.prune_unpinned(prefix_path, body.get("keep_count"))
                res["prefix"] = CONTAINER_SVC.inspect_container(prefix_path)
                self.send_json(res)
                return

            if action == "reboot":
                env = os.environ.copy()
                env["RAVEN_PREFIX"] = str(prefix_path)
                proc = subprocess.run(
                    [str(CFG.path("raven_bin")), "--reboot"],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.send_json({
                    "ok": proc.returncode == 0,
                    "output": (proc.stdout + proc.stderr).strip(),
                    "prefix": CONTAINER_SVC.inspect_container(prefix_path),
                })
                return

            self.send_json({"ok": False, "error": f"Unknown snapshot action: {action}"}, 400)
            return

        # Execution & Doctor endpoints
        if path == "/api/run":
            self.send_json(PROC_SVC.launch_application(body))
            return

        if path == "/api/process/kill":
            self.send_json(PROC_SVC.kill_process(body.get("process_id", "")))
            return

        if path == "/api/runner/export":
            self.send_json(PROC_SVC.export_desktop_runner(
                body.get("prefix_id", "default"),
                body.get("name", "App"),
                body.get("path", ""),
                body.get("desktop", "wayland"),
                body.get("args", ""),
            ))
            return

        if path == "/api/doctor/repair":
            self.send_json(PROC_SVC.run_doctor_repair(body.get("action", "")))
            return

        self.send_json({"ok": False, "error": "Not found"}, 404)


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_server() -> ThreadedHTTPServer:
    host = os.environ.get("RAVEN_UI_HOST", str(CFG.data["server"]["host"]))
    port = int(os.environ.get("RAVEN_UI_PORT", str(CFG.data["server"]["port"])))
    return ThreadedHTTPServer((host, port), RavenAPIHandler)


if __name__ == "__main__":
    srv = start_server()
    host, port = srv.server_address
    print(f"Raven Nest UI Backend listening on http://{host}:{port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
