from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config_service import ConfigService
from .container_service import ContainerService
from .dependency_service import DependencyService
from .doctor_service import DoctorService


class ProcessService:
    """
    Single-responsibility service for launching Windows executables/installers,
    provisioning tasks, diagnostic log translation, and .desktop runner export.
    """

    def __init__(
        self,
        cfg: ConfigService,
        container_service: ContainerService,
        dep_service: DependencyService,
        doctor_service: DoctorService,
    ) -> None:
        self.cfg = cfg
        self.containers = container_service
        self.dep = dep_service
        self.doctor = doctor_service
        self._lock = threading.Lock()
        self._processes: dict[str, dict[str, Any]] = {}

    def diagnose_log(self, lines: list[str]) -> list[dict[str, str]]:
        """Translates low-level Raven stderr lines using configured diagnostic_rules."""
        hints: list[dict[str, str]] = []
        joined = "\n".join(lines)
        for rule in self.cfg.data.get("diagnostic_rules", []):
            m = re.search(rule["pattern"], joined)
            if m:
                title = rule["title"]
                if m.groups():
                    title = f"{title}: {m.group(1)}"
                hints.append({
                    "severity": rule.get("severity", "error"),
                    "title": title,
                    "advice": rule["advice"],
                })
        return hints

    def list_processes(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "id": p["id"],
                    "pid": p["pid"],
                    "prefix_id": p["prefix_id"],
                    "name": p["name"],
                    "path": p["path"],
                    "is_msi": p["is_msi"],
                    "command": p["command"],
                    "started_at": p["started_at"],
                    "status": p["status"],
                    "exit_code": p["exit_code"],
                    "logs": p["logs"][-200:],
                    "diagnostics": self.diagnose_log(p["logs"][-60:]),
                }
                for p in reversed(list(self._processes.values()))
            ]

    def _spawn_tracked(
        self,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        prefix_id: str,
        name: str,
        path_str: str,
        is_msi: bool = False,
    ) -> dict[str, Any]:
        proc_id = str(uuid.uuid4())[:8]
        max_lines = int(self.cfg.data["server"].get("max_log_lines", 2000))
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        rec: dict[str, Any] = {
            "id": proc_id,
            "pid": proc.pid,
            "prefix_id": prefix_id,
            "name": name,
            "path": path_str,
            "is_msi": is_msi,
            "command": " ".join(shlex.quote(c) for c in cmd),
            "started_at": time.strftime("%H:%M:%S"),
            "status": "running",
            "exit_code": None,
            "logs": [
                f"[raven-ui] Scope: {prefix_id}",
                f"[raven-ui] Command: {' '.join(shlex.quote(c) for c in cmd)}",
            ],
            "process": proc,
        }
        with self._lock:
            self._processes[proc_id] = rec

        def reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                with self._lock:
                    if len(rec["logs"]) < max_lines:
                        rec["logs"].append(line.rstrip("\n"))
            rc = proc.wait()
            with self._lock:
                rec["status"] = "exited" if rc == 0 else "failed"
                rec["exit_code"] = rc
                rec["logs"].append(f"[raven-ui] Process finished with exit code {rc}")

        threading.Thread(target=reader, daemon=True).start()
        return {"ok": True, "process_id": proc_id, "pid": proc.pid, "command": rec["command"]}

    def launch_application(self, payload: dict[str, Any]) -> dict[str, Any]:
        prefix_id = payload.get("prefix_id", "default")
        exe_path_str = payload.get("path", "")
        args_str = payload.get("args", "")
        desktop = payload.get("desktop", "wayland")
        default_timeout = int(self.cfg.data["ui"].get("default_timeout_ms", 86400000))
        timeout_ms = int(payload.get("timeout_ms", default_timeout))
        elevated = bool(payload.get("elevated", False))
        auto_deploy = bool(payload.get("auto_deploy", True))

        prefix_path = self.cfg.path("prefixes_root") / prefix_id
        exe_path = Path(exe_path_str)
        if not exe_path.exists():
            return {"ok": False, "error": f"Executable not found: {exe_path_str}"}

        if auto_deploy:
            self.dep.auto_deploy(prefix_id, exe_path_str)

        with exe_path.open("rb") as f:
            magic = f.read(8)
        is_msi = exe_path.suffix.lower() == ".msi" or magic.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")

        raven_bin = self.cfg.path("raven_bin")
        native_dir = self.cfg.path("native_dir")
        cmd = [
            str(raven_bin),
            "--run-process",
            "--execution=native",
            f"--timeout-ms={timeout_ms}",
            f"--desktop={desktop}",
        ]
        if elevated:
            cmd.append("--elevated")

        if is_msi:
            msiexec_bin = native_dir / "msiexec.exe"
            win_path = "Z:" + str(exe_path.resolve()).replace("/", "\\")
            cmd.extend([str(msiexec_bin), "--", "/i", win_path])
            if args_str:
                cmd.extend(shlex.split(args_str))
        else:
            cmd.append(str(exe_path))
            if args_str:
                cmd.append("--")
                cmd.extend(shlex.split(args_str))

        meta = self.containers.load_meta(prefix_path)
        env = os.environ.copy()
        env["RAVEN_PREFIX"] = str(prefix_path)
        for k, v in meta.get("env", {}).items():
            env[str(k)] = str(v)
        if payload.get("backtrace", True):
            env["RAVEN_BACKTRACE"] = "1"

        return self._spawn_tracked(
            cmd=cmd,
            cwd=exe_path.parent,
            env=env,
            prefix_id=prefix_id,
            name=exe_path.name,
            path_str=str(exe_path),
            is_msi=is_msi,
        )

    def kill_process(self, proc_id: str) -> dict[str, Any]:
        with self._lock:
            rec = self._processes.get(proc_id)
            if rec and rec["status"] == "running":
                rec["process"].terminate()
                rec["status"] = "terminated"
                rec["logs"].append("[raven-ui] Process terminated by user.")
        return {"ok": True}

    def run_doctor_repair(self, action: str) -> dict[str, Any]:
        repo_root = self.cfg.path("repo_root")
        build_dir = self.cfg.path("build_dir")
        fonts = self.doctor.get_host_font_hashes()
        font_hash_arg = fonts["cmake_font_hashes"]

        commands: dict[str, list[str]] = {
            "provision_controls_64": ["python3", str(repo_root / "scripts/provision-controls.py"), "--output", str(self.cfg.path("controls64_dir"))],
            "provision_controls_32": [
                "python3", str(repo_root / "scripts/provision-controls.py"),
                "--arch", "x86",
                "--archive", str(self.cfg.path("controls64_dir") / "reactos-source.tar.gz"),
                "--output", str(self.cfg.path("controls32_dir")),
            ],
            "provision_installer_64": ["python3", str(repo_root / "scripts/provision-installer.py"), "--output", str(self.cfg.path("installer64_dir"))],
            "provision_installer_32": [
                "python3", str(repo_root / "scripts/provision-installer.py"),
                "--arch", "x86",
                "--archive", str(self.cfg.path("installer64_dir") / "reactos-source.tar.gz"),
                "--output", str(self.cfg.path("installer32_dir")),
            ],
            "provision_rtl": ["python3", str(repo_root / "scripts/provision-rtl.py"), "--output", str(self.cfg.path("rtl_component_path").parent)],
            "provision_text_roots": [
                "bash", "-c",
                f"python3 {shlex.quote(str(repo_root / 'scripts/provision-text.py'))} --output {shlex.quote(str(self.cfg.path('text_component_path').parent))} && "
                f"python3 {shlex.quote(str(repo_root / 'scripts/provision-roots.py'))} --output {shlex.quote(str(self.cfg.path('roots_dir')))}",
            ],
            "provision_graphics": ["python3", str(repo_root / "scripts/provision-graphics.py"), "--destination", str(self.cfg.path("graphics_dir"))],
            "reconfigure_cmake": [
                "cmake", "-S", str(repo_root), "-B", str(build_dir),
                f"-DRAVEN_INSTALLER_COMPONENT_DIR={self.cfg.path('installer64_dir')}",
                f"-DRAVEN_INSTALLER32_COMPONENT_DIR={self.cfg.path('installer32_dir')}",
                f"-DRAVEN_CONTROLS_COMPONENT_DIR={self.cfg.path('controls64_dir')}",
                f"-DRAVEN_CONTROLS32_COMPONENT_DIR={self.cfg.path('controls32_dir')}",
                f"-DRAVEN_RTL_COMPONENT_PATH={self.cfg.path('rtl_component_path')}",
                f"-DRAVEN_TEXT_COMPONENT_PATH={self.cfg.path('text_component_path')}",
                f"-DRAVEN_ROOT_CERTIFICATE_DIRECTORY={self.cfg.path('roots_dir')}",
                f"-DRAVEN_SKIA_SOURCE={self.cfg.path('skia_source_dir')}",
                f"-DRAVEN_SKIA_LIBRARY={self.cfg.path('skia_library_path')}",
                f"-DRAVEN_FONT_DIRECTORY={self.cfg.path('fonts_dir')}",
                f"-DFONT_HASHES={font_hash_arg}",
                f"-DRAVEN_DXVK_PE64_DIR={self.cfg.path('graphics_dir') / 'dxvk/pe64'}",
                f"-DRAVEN_DXVK_PE32_DIR={self.cfg.path('graphics_dir') / 'dxvk/pe32'}",
                f"-DRAVEN_VKD3D_PE64_DIR={self.cfg.path('graphics_dir') / 'vkd3d-proton/pe64'}",
            ],
            "build_runtime": [
                "cmake", "--build", str(build_dir), "-j4", "--target",
                "raven", "root_certificates", "installer_components_64",
                "installer_components_32", "graphics_components",
                "d3dcompiler_component", "x86_ucrt_component",
            ],
        }
        if action not in commands:
            return {"ok": False, "error": f"Unknown repair action: {action}"}

        return self._spawn_tracked(
            cmd=commands[action],
            cwd=repo_root,
            env=os.environ.copy(),
            prefix_id="system",
            name=f"Deployment Task: {action}",
            path_str=str(repo_root),
        )

    def export_desktop_runner(
        self, prefix_id: str, app_name: str, exe_path_str: str, desktop: str = "wayland", args: str = ""
    ) -> dict[str, Any]:
        """Idempotently writes a Linux launcher script and XDG .desktop entry."""
        slug = re.sub(r"[^a-z0-9]+", "-", f"raven-{prefix_id}-{app_name}".lower()).strip("-")
        bin_dir = self.cfg.path("local_bin_dir")
        apps_dir = self.cfg.path("desktop_entries_dir")
        bin_dir.mkdir(parents=True, exist_ok=True)
        apps_dir.mkdir(parents=True, exist_ok=True)

        prefix_path = self.cfg.path("prefixes_root") / prefix_id
        raven_bin = self.cfg.path("raven_bin")
        timeout = int(self.cfg.data["ui"].get("default_timeout_ms", 86400000))
        runner_script = bin_dir / slug
        extra_args = f" -- {args}" if args.strip() else ""
        script_content = (
            "#!/usr/bin/env bash\n"
            "# Auto-generated by Raven Nest UI\n"
            f"export RAVEN_PREFIX={shlex.quote(str(prefix_path))}\n"
            "export RAVEN_BACKTRACE=1\n"
            f"exec {shlex.quote(str(raven_bin))} --run-process --execution=native "
            f"--timeout-ms={timeout} --desktop={shlex.quote(desktop)} "
            f"{shlex.quote(exe_path_str)}{extra_args} \"$@\"\n"
        )
        if not runner_script.exists() or runner_script.read_text(encoding="utf-8") != script_content:
            runner_script.write_text(script_content, encoding="utf-8")
        runner_script.chmod(0o755)

        desktop_file = apps_dir / f"{slug}.desktop"
        desktop_content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={app_name} (Raven • {prefix_id})\n"
            f"Comment=Run {app_name} inside Raven sandbox '{prefix_id}'\n"
            f"Exec={runner_script}\n"
            "Icon=applications-system\n"
            "Terminal=false\n"
            "Categories=Utility;Application;\n"
        )
        if not desktop_file.exists() or desktop_file.read_text(encoding="utf-8") != desktop_content:
            desktop_file.write_text(desktop_content, encoding="utf-8")

        return {
            "ok": True,
            "runner_script": str(runner_script),
            "desktop_entry": str(desktop_file),
        }
