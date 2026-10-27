from __future__ import annotations

import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

from .config_service import ConfigService, sha256_file


class DoctorService:
    """
    Single-responsibility service for evaluating D1–D13 build/runtime health,
    CMake cache state, and Liberation font SHA-256 hashes from configuration.
    """

    def __init__(self, cfg: ConfigService) -> None:
        self.cfg = cfg

    def parse_cmake_cache(self) -> dict[str, str]:
        cache_file = self.cfg.path("build_dir") / "CMakeCache.txt"
        if not cache_file.exists():
            return {}
        values: dict[str, str] = {}
        for line in cache_file.read_text(errors="replace").splitlines():
            m = re.match(r"^([^/#][^:]*):[^=]+=(.*)$", line)
            if m:
                values[m.group(1)] = m.group(2)
        return values

    def get_host_font_hashes(self) -> dict[str, Any]:
        font_dir = self.cfg.path("fonts_dir")
        faces = self.cfg.data.get("font_qualification", {}).get("required_files", [])
        found = {}
        missing = []
        for face in faces:
            p = font_dir / face
            if p.exists():
                found[face] = sha256_file(p)
            else:
                missing.append(face)
        combined = ";".join(found[f] for f in faces) if not missing else ""
        return {
            "directory": str(font_dir),
            "exists": font_dir.exists() and not missing,
            "faces": found,
            "missing": missing,
            "cmake_font_hashes": combined,
        }

    def cmake_configure_args(self, font_hashes: str | None = None) -> list[str]:
        """Single authoritative CMake configure argv (shared by doctor, repair, and upstream builds)."""
        if font_hashes is None:
            font_hashes = self.get_host_font_hashes()["cmake_font_hashes"]
        p = self.cfg.path
        return [
            "cmake",
            "-S", str(p("repo_root")),
            "-B", str(p("build_dir")),
            f"-DRAVEN_INSTALLER_COMPONENT_DIR={p('installer64_dir')}",
            f"-DRAVEN_INSTALLER32_COMPONENT_DIR={p('installer32_dir')}",
            f"-DRAVEN_CONTROLS_COMPONENT_DIR={p('controls64_dir')}",
            f"-DRAVEN_CONTROLS32_COMPONENT_DIR={p('controls32_dir')}",
            f"-DRAVEN_RTL_COMPONENT_PATH={p('rtl_component_path')}",
            f"-DRAVEN_TEXT_COMPONENT_PATH={p('text_component_path')}",
            f"-DRAVEN_ROOT_CERTIFICATE_DIRECTORY={p('roots_dir')}",
            f"-DRAVEN_SKIA_SOURCE={p('skia_source_dir')}",
            f"-DRAVEN_SKIA_LIBRARY={p('skia_library_path')}",
            f"-DRAVEN_FONT_DIRECTORY={p('fonts_dir')}",
            f"-DFONT_HASHES={font_hashes}",
            f"-DRAVEN_DXVK_PE64_DIR={p('graphics_dir') / 'dxvk/pe64'}",
            f"-DRAVEN_DXVK_PE32_DIR={p('graphics_dir') / 'dxvk/pe32'}",
            f"-DRAVEN_VKD3D_PE64_DIR={p('graphics_dir') / 'vkd3d-proton/pe64'}",
        ]

    def cmake_build_args(self) -> list[str]:
        """CMake build argv for the configured Raven runtime targets (`upstream.cmake_build_targets`)."""
        up = self.cfg.data.get("upstream", {})
        jobs = int(up.get("build_jobs", 4))
        targets = up.get("cmake_build_targets", ["raven"])
        return ["cmake", "--build", str(self.cfg.path("build_dir")), f"-j{jobs}", "--target", *targets]

    def evaluate(self) -> dict[str, Any]:
        cache = self.parse_cmake_cache()
        fonts = self.get_host_font_hashes()
        cached_font_hashes = cache.get("FONT_HASHES", "")

        required_tools = self.cfg.data.get("toolchain", {}).get("required_binaries", [])
        tools_status = {t: shutil.which(t) is not None for t in required_tools}
        missing_tools = [t for t, ok in tools_status.items() if not ok]

        repo_root = self.cfg.path("repo_root")
        build_dir = self.cfg.path("build_dir")
        home_dir = cache.get("CMAKE_HOME_DIRECTORY", "")
        cache_valid_source = (Path(home_dir).resolve() == repo_root.resolve()) if home_dir else False

        evaluated_components = []
        ready_count = 0
        for check in self.cfg.data.get("doctor_checks", []):
            present = []
            missing = []
            for tmpl in check.get("files", []):
                f = Path(self.cfg.expand(tmpl))
                rel = str(f.relative_to(repo_root) if f.is_relative_to(repo_root) else f)
                if f.exists():
                    present.append(rel)
                else:
                    missing.append(rel)

            if "check_glob" in check:
                g = check["check_glob"]
                gdir = Path(self.cfg.expand(g["dir"]))
                count = len(list(gdir.glob(g["pattern"]))) if gdir.exists() else 0
                if count == 0:
                    missing.append(g["label"])
                else:
                    present.append(f"{g['label']} ({count} files)")

            status = "ready" if not missing else ("partial" if present else "missing")
            if status == "ready":
                ready_count += 1
            evaluated_components.append({
                "id": check["id"],
                "code": check["code"],
                "title": check["title"],
                "description": check["description"],
                "status": status,
                "present_count": len(present),
                "total_count": len(present) + len(missing),
                "present": present,
                "missing": missing,
                "repair_action": check["repair_action"],
            })

        font_hash_arg = fonts["cmake_font_hashes"] or cached_font_hashes
        cmake_configure_cmd = " ".join(shlex.quote(a) for a in self.cmake_configure_args(font_hash_arg))

        return {
            "summary": {
                "ready_components": ready_count,
                "total_components": len(evaluated_components),
                "all_ready": ready_count == len(evaluated_components) and not missing_tools and cache_valid_source,
                "wayland_display": os.environ.get("WAYLAND_DISPLAY", ""),
                "x11_display": os.environ.get("DISPLAY", ""),
                "compositor": os.environ.get("XDG_CURRENT_DESKTOP") or ("niri" if os.environ.get("NIRI_SOCKET") else "wayland"),
            },
            "toolchain": {
                "ok": len(missing_tools) == 0,
                "tools": tools_status,
                "missing": missing_tools,
            },
            "cmake": {
                "cache_exists": (build_dir / "CMakeCache.txt").exists(),
                "source_dir_matches": cache_valid_source,
                "recorded_source_dir": home_dir,
                "actual_source_dir": str(repo_root),
                "font_hashes_match": bool(fonts["cmake_font_hashes"] and cached_font_hashes == fonts["cmake_font_hashes"]),
                "host_font_hashes": fonts["cmake_font_hashes"],
                "cached_font_hashes": cached_font_hashes,
                "configure_command": cmake_configure_cmd,
            },
            "components": evaluated_components,
        }
