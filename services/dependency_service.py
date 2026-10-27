from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from .config_service import ConfigService, sync_file_idempotent


class DependencyService:
    """
    Single-responsibility service for PE64/PE32 & MSI inspection and
    idempotent deployment of Raven runtime components and redistributables.
    """

    def __init__(self, cfg: ConfigService) -> None:
        self.cfg = cfg
        self._inspect_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def find_donor_redist_dlls(self) -> dict[str, dict[str, Path]]:
        """
        Scans existing prefixes for redistributable DLLs defined in config
        so any container can have them deployed idempotently.
        """
        donors: dict[str, dict[str, Path]] = {"system32": {}, "syswow64": {}}
        prefixes_root = self.cfg.path("prefixes_root")
        if not prefixes_root.exists():
            return donors

        all_redist_dlls: set[str] = set()
        for fam in self.cfg.data.get("redistributables", {}).values():
            all_redist_dlls.update(d.lower() for d in fam.get("dlls", []))

        for pdir in sorted(prefixes_root.iterdir()):
            if not pdir.is_dir() or pdir.name.startswith(".") or pdir.name.endswith(".snapshots"):
                continue
            for folder in ("system32", "syswow64"):
                sys_folder = pdir / "drive_c" / "windows" / folder
                if not sys_folder.exists():
                    continue
                for item in sys_folder.iterdir():
                    low = item.name.lower()
                    if low in all_redist_dlls and low not in donors[folder] and item.is_file():
                        donors[folder][low] = item
        return donors

    def sync_prefix_system_utilities(self, prefix_path: Path) -> list[str]:
        """
        Idempotently ensures prefix directory hierarchy and system32/syswow64
        programs match the runtime build.
        """
        deployed: list[str] = []
        win_dir = prefix_path / "drive_c" / "windows"
        for sub in (
            win_dir / "system32",
            win_dir / "syswow64",
            win_dir / "WinSxS" / "Manifests",
            prefix_path / "drive_c" / "Program Files",
            prefix_path / "drive_c" / "Program Files (x86)",
            prefix_path / "drive_c" / "Users" / os.environ.get("USER", "user") / "AppData" / "Local" / "Temp",
        ):
            sub.mkdir(parents=True, exist_ok=True)

        native_dir = self.cfg.path("native_dir")
        programs = self.cfg.data.get("prefix_system_programs", [])
        for src_root, folder in [(native_dir, "system32"), (native_dir / "x86", "syswow64")]:
            for prog in programs:
                src_file = src_root / prog
                is_win_root = prog == "regedit.exe" and folder == "system32"
                dst_file = (win_dir / prog) if is_win_root else (win_dir / folder / prog)
                if sync_file_idempotent(src_file, dst_file):
                    deployed.append(f"{folder}/{prog}")
        return deployed

    def run_raven_inspect(self, pe_path: Path) -> dict[str, Any]:
        """Runs `raven --inspect <pe_path>` with mtime caching."""
        key = str(pe_path.resolve())
        try:
            mtime = pe_path.stat().st_mtime
        except OSError as exc:
            return {"error": str(exc)}

        cached = self._inspect_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]

        raven_bin = self.cfg.path("raven_bin")
        if not raven_bin.exists():
            return {"error": f"Raven binary not found at {raven_bin}"}

        proc = subprocess.run(
            [str(raven_bin), "--inspect", str(pe_path)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            return {"error": proc.stderr.strip() or "raven --inspect failed"}

        lines = proc.stdout.strip().splitlines()
        if not lines:
            return {"error": "Empty output from raven --inspect"}

        header = lines[0]
        is_64 = header.startswith("PE64")
        header_info: dict[str, Any] = {"arch": "PE64 (x86_64)" if is_64 else "PE32 (x86)", "is_64": is_64}
        for token in header.split()[1:]:
            if "=" in token:
                k, v = token.split("=", 1)
                header_info[k] = v

        imports: dict[str, int] = {}
        for line in lines[1:]:
            parts = line.rsplit(" ", 1)
            if len(parts) == 2:
                imports[parts[0]] = int(parts[1])

        res = {"header": header_info, "imports": imports}
        self._inspect_cache[key] = (mtime, res)
        return res

    def analyze_executable(self, prefix_id: str, exe_path_str: str) -> dict[str, Any]:
        """
        Inspects a PE executable or MSI installer against the container's
        installed libraries and Raven's configured runtime components.
        """
        prefix_path = self.cfg.path("prefixes_root") / prefix_id
        exe_path = Path(exe_path_str)
        if not exe_path.exists():
            return {"error": f"File does not exist: {exe_path_str}"}

        native_dir = self.cfg.path("native_dir")
        raven_bin = self.cfg.path("raven_bin")

        with exe_path.open("rb") as f:
            magic = f.read(8)
        is_msi = exe_path.suffix.lower() == ".msi" or magic.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")

        if is_msi:
            msi64_ok = (native_dir / "msiexec.exe").exists() and (native_dir / "msi.dll").exists()
            msi32_ok = (native_dir / "x86" / "msiexec.exe").exists() and (native_dir / "x86" / "msi.dll").exists()
            timeout = self.cfg.data["ui"]["installer_timeout_ms"]
            return {
                "file": str(exe_path),
                "name": exe_path.name,
                "is_msi": True,
                "arch": "Windows Installer Package (OLE Compound Document)",
                "is_64": True,
                "msi_readiness": {
                    "msi64_ready": msi64_ok,
                    "msi32_ready": msi32_ok,
                    "auto_wrapped_command": (
                        f"RAVEN_PREFIX=\"{prefix_path}\" "
                        f"\"{raven_bin}\" --run-process --execution=native --timeout-ms={timeout} "
                        f"--desktop=wayland \"{native_dir / 'msiexec.exe'}\" -- /i "
                        f"\"Z:{str(exe_path).replace('/', chr(92))}\""
                    ),
                },
                "summary": {
                    "total_imports": 2,
                    "core_count": 1,
                    "runtime_components_ready": (1 if msi64_ok else 0) + (1 if msi32_ok else 0),
                    "runtime_components_missing": (0 if msi64_ok else 1) + (0 if msi32_ok else 1),
                    "redist_ready": 1,
                    "redist_missing": 0,
                    "app_local_count": 0,
                    "unresolved_count": 0 if (msi64_ok and msi32_ok) else 1,
                    "ready_to_run": msi64_ok and msi32_ok,
                },
                "dependencies": [
                    {
                        "dll": "msiexec.exe (64-bit)",
                        "category": "runtime_component",
                        "status": "ready" if msi64_ok else "missing_deployable",
                        "provider": "build/native/msiexec.exe + msi.dll + cabinet.dll + sxs.dll",
                        "symbol_count": 1,
                    },
                    {
                        "dll": "SysWOW64\\msiexec.exe (32-bit Custom Actions - D9)",
                        "category": "runtime_component",
                        "status": "ready" if msi32_ok else "missing_deployable",
                        "provider": "build/native/x86/msiexec.exe + ucrtbase32.dll",
                        "symbol_count": 1,
                    },
                ],
            }

        main_inspect = self.run_raven_inspect(exe_path)
        if "error" in main_inspect:
            return main_inspect

        is_64 = main_inspect["header"]["is_64"]
        app_dir = exe_path.parent
        sys_folder_name = "system32" if is_64 else "syswow64"
        sys_dir = prefix_path / "drive_c" / "windows" / sys_folder_name

        app_dir_files = {p.name.lower(): p for p in app_dir.iterdir() if p.is_file()} if app_dir.exists() else {}
        sys_dir_files = {p.name.lower(): p for p in sys_dir.iterdir() if p.is_file()} if sys_dir.exists() else {}

        core_system_dlls = {d.lower() for d in self.cfg.data.get("core_system_dlls", [])}
        core_known_dlls = {d.lower() for d in self.cfg.data.get("core_known_dlls", [])}
        runtime_map = self.cfg.data.get("runtime_components", {})
        redist_families = self.cfg.data.get("redistributables", {})
        donors = self.find_donor_redist_dlls()[sys_folder_name]

        combined_imports: dict[str, int] = dict(main_inspect["imports"])
        transitive_limit = int(self.cfg.data["server"].get("inspect_transitive_limit", 12))
        inspected_local = 0
        for dll_name in list(main_inspect["imports"].keys()):
            low = dll_name.lower()
            if low in app_dir_files and not low.startswith("api-ms-win-") and low not in core_system_dlls:
                if inspected_local < transitive_limit:
                    sub = self.run_raven_inspect(app_dir_files[low])
                    inspected_local += 1
                    if "imports" in sub:
                        for sub_dll, cnt in sub["imports"].items():
                            combined_imports[sub_dll] = combined_imports.get(sub_dll, 0) + cnt

        dependencies: list[dict[str, Any]] = []
        counts = {
            "core": 0,
            "runtime_ready": 0,
            "runtime_missing": 0,
            "redist_ready": 0,
            "redist_missing": 0,
            "app_local": 0,
            "unresolved": 0,
        }

        for imp in self.cfg.data.get("implicit_runtime_components", []):
            if imp.get("only_64") and not is_64:
                continue
            target_key = "target64" if is_64 else "target32"
            source_key = "source64" if is_64 else "source32"
            runtime_target = Path(self.cfg.expand(imp.get(target_key, imp["target64"])))
            build_src = Path(self.cfg.expand(imp.get(source_key, imp["source64"])))
            ok = runtime_target.exists()
            if ok:
                counts["runtime_ready"] += 1
            else:
                counts["runtime_missing"] += 1
            dependencies.append({
                "dll": imp["dll"],
                "category": "runtime_component",
                "status": "ready" if ok else ("missing_deployable" if build_src.exists() else "missing"),
                "provider": f"Raven Runtime Component ({imp['description']})",
                "symbol_count": 0,
            })

        crt_api_sets_count = 0
        core_api_sets_count = 0

        for raw_dll, sym_count in sorted(combined_imports.items(), key=lambda x: x[0].lower()):
            low = raw_dll.lower()
            if not low.endswith((".dll", ".drv", ".tx", ".ocx")):
                low += ".dll"

            if low.startswith("api-ms-win-crt-"):
                crt_api_sets_count += sym_count
                continue
            if low.startswith(("api-ms-win-core-", "ext-ms-win-")):
                core_api_sets_count += sym_count
                continue

            if low in runtime_map:
                info = runtime_map[low]
                rel_comp = info["component64"] if is_64 else info.get("component32", info["component64"])
                target_file = native_dir / rel_comp
                src_tmpl = info["source64"] if is_64 else info.get("source32", info["source64"])
                src_file = Path(self.cfg.expand(src_tmpl)) if src_tmpl else None
                ok = target_file.exists()
                if ok:
                    counts["runtime_ready"] += 1
                    status = "ready"
                else:
                    counts["runtime_missing"] += 1
                    status = "missing_deployable" if (src_file and src_file.exists()) else "missing"
                dependencies.append({
                    "dll": raw_dll,
                    "category": "runtime_component",
                    "status": status,
                    "provider": f"build/native/{rel_comp} — {info['description']}",
                    "symbol_count": sym_count,
                })
                continue

            if low in core_system_dlls:
                has_local_override = low in app_dir_files and low not in core_known_dlls
                counts["core"] += 1
                dependencies.append({
                    "dll": raw_dll,
                    "category": "core_system",
                    "status": "ready",
                    "provider": (
                        f"App-local override ({app_dir_files[low].name}) + system32-{low} fallback (Issue 6)"
                        if has_local_override
                        else "Raven Built-in Syscall/Win32 Provider"
                    ),
                    "symbol_count": sym_count,
                })
                continue

            matched_redist = None
            for fam_id, fam in redist_families.items():
                if low in {d.lower() for d in fam.get("dlls", [])}:
                    matched_redist = (fam_id, fam["name"])
                    break

            if matched_redist:
                in_sys = low in sys_dir_files
                in_app = low in app_dir_files
                can_deploy = low in donors
                if in_sys or in_app:
                    counts["redist_ready"] += 1
                    status = "ready"
                    where = f"Container {sys_folder_name}/{low}" if in_sys else f"App folder ({raw_dll})"
                elif can_deploy:
                    counts["redist_missing"] += 1
                    status = "missing_deployable"
                    where = f"Missing in {sys_folder_name} (Auto-deployable from cached {matched_redist[1]})"
                else:
                    counts["redist_missing"] += 1
                    status = "missing"
                    where = f"Missing in {sys_folder_name} — Install {matched_redist[1]}"

                dependencies.append({
                    "dll": raw_dll,
                    "category": "redistributable",
                    "status": status,
                    "provider": f"{matched_redist[1]} • {where}",
                    "symbol_count": sym_count,
                })
                continue

            if low in app_dir_files:
                counts["app_local"] += 1
                dependencies.append({
                    "dll": raw_dll,
                    "category": "app_local",
                    "status": "ready",
                    "provider": f"App Directory ({app_dir_files[low].name})",
                    "symbol_count": sym_count,
                })
                continue

            if low in sys_dir_files:
                counts["redist_ready"] += 1
                dependencies.append({
                    "dll": raw_dll,
                    "category": "redistributable",
                    "status": "ready",
                    "provider": f"Container {sys_folder_name}/{sys_dir_files[low].name}",
                    "symbol_count": sym_count,
                })
                continue

            counts["unresolved"] += 1
            dependencies.append({
                "dll": raw_dll,
                "category": "unresolved",
                "status": "missing",
                "provider": f"Not found in App Folder, {sys_folder_name}, or Raven Providers",
                "symbol_count": sym_count,
            })

        if crt_api_sets_count > 0:
            ucrt_name = "ucrtbase.dll" if is_64 else "ucrtbase32.dll"
            ucrt_ok = (native_dir / ucrt_name).exists()
            if ucrt_ok:
                counts["runtime_ready"] += 1
            else:
                counts["runtime_missing"] += 1
            dependencies.insert(0, {
                "dll": f"api-ms-win-crt-* -> {ucrt_name}",
                "category": "runtime_component",
                "status": "ready" if ucrt_ok else "missing_deployable",
                "provider": f"Raven UCRT Component ({ucrt_name}, includes Issue 3 MSVC 2019/2022 exports)",
                "symbol_count": crt_api_sets_count,
            })

        if core_api_sets_count > 0:
            counts["core"] += 1
            dependencies.insert(1, {
                "dll": "api-ms-win-core-* (Windows API Sets)",
                "category": "core_system",
                "status": "ready",
                "provider": "Raven Built-in API Set Schema Router",
                "symbol_count": core_api_sets_count,
            })

        ready_to_run = (
            counts["runtime_missing"] == 0
            and counts["redist_missing"] == 0
            and counts["unresolved"] == 0
        )

        return {
            "file": str(exe_path),
            "name": exe_path.name,
            "is_msi": False,
            "arch": main_inspect["header"]["arch"],
            "is_64": is_64,
            "header": main_inspect["header"],
            "summary": {
                "total_imports": len(dependencies),
                "core_count": counts["core"],
                "runtime_components_ready": counts["runtime_ready"],
                "runtime_components_missing": counts["runtime_missing"],
                "redist_ready": counts["redist_ready"],
                "redist_missing": counts["redist_missing"],
                "app_local_count": counts["app_local"],
                "unresolved_count": counts["unresolved"],
                "ready_to_run": ready_to_run,
            },
            "dependencies": dependencies,
        }

    def auto_deploy(self, prefix_id: str, exe_path_str: str | None = None, deploy_redist: bool = True) -> dict[str, Any]:
        """
        Idempotently stages missing Raven runtime components into `build/native`,
        syncs container `system32`/`syswow64` utilities, and deploys cached
        redistributables into the container only when files are missing or changed.
        """
        prefix_path = self.cfg.path("prefixes_root") / prefix_id
        prefix_path.mkdir(parents=True, exist_ok=True)
        native_dir = self.cfg.path("native_dir")
        (native_dir / "x86").mkdir(parents=True, exist_ok=True)

        actions: list[str] = []

        # 1. Implicit runtime components
        for imp in self.cfg.data.get("implicit_runtime_components", []):
            src64 = Path(self.cfg.expand(imp["source64"]))
            dst64 = Path(self.cfg.expand(imp["target64"]))
            if sync_file_idempotent(src64, dst64):
                actions.append(f"Staged {dst64.name}")
            if "source32" in imp and "target32" in imp:
                src32 = Path(self.cfg.expand(imp["source32"]))
                dst32 = Path(self.cfg.expand(imp["target32"]))
                if sync_file_idempotent(src32, dst32):
                    actions.append(f"Staged x86/{dst32.name}")

        # 2. Explicit runtime components
        for info in self.cfg.data.get("runtime_components", {}).values():
            if "source64" in info and "component64" in info:
                src = Path(self.cfg.expand(info["source64"]))
                dst = native_dir / info["component64"]
                if sync_file_idempotent(src, dst):
                    actions.append(f"Staged {info['component64']}")
            if "source32" in info and "component32" in info:
                src = Path(self.cfg.expand(info["source32"]))
                dst = native_dir / info["component32"]
                if sync_file_idempotent(src, dst):
                    actions.append(f"Staged {info['component32']}")

        # 3. Root certificates
        roots_src = self.cfg.path("roots_dir")
        roots_dst = native_dir / "roots"
        if roots_src.exists():
            roots_dst.mkdir(parents=True, exist_ok=True)
            copied_certs = sum(
                1 for crt in roots_src.glob("*.crt") if sync_file_idempotent(crt, roots_dst / crt.name)
            )
            if copied_certs:
                actions.append(f"Staged {copied_certs} root certificates")

        # 4. Sync prefix system32/syswow64 utilities (idempotent SHA-256 check)
        synced = self.sync_prefix_system_utilities(prefix_path)
        if synced:
            actions.append(f"Updated {len(synced)} system utilities in container ({', '.join(synced[:4])}{'...' if len(synced) > 4 else ''})")

        # 5. Deploy redistributables from donor cache if enabled
        if deploy_redist:
            donors = self.find_donor_redist_dlls()
            deployed_redists = []
            for folder in ("system32", "syswow64"):
                target_dir = prefix_path / "drive_c" / "windows" / folder
                target_dir.mkdir(parents=True, exist_ok=True)
                for _, src_path in donors[folder].items():
                    dst_path = target_dir / src_path.name
                    if sync_file_idempotent(src_path, dst_path):
                        deployed_redists.append(f"{folder}/{src_path.name}")
            if deployed_redists:
                actions.append(f"Deployed {len(deployed_redists)} redistributables ({', '.join(deployed_redists[:5])}{'...' if len(deployed_redists) > 5 else ''})")

        return {
            "ok": True,
            "prefix_id": prefix_id,
            "idempotent_noop": len(actions) == 0,
            "actions": actions or ["Idempotent check complete: all runtime components and container libraries are already in sync."],
            "analysis": self.analyze_executable(prefix_id, exe_path_str) if exe_path_str else None,
        }
