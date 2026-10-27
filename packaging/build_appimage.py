#!/usr/bin/env python3
"""
Builds the Raven Nest AppImage from `config/default.json` → `packaging` / `paths`.

Idempotent: the AppDir is converged file-by-file (SHA-256 checked, stale files
pruned), appimagetool is downloaded once into `paths.appimage_tools_dir`, and
re-running with unchanged sources produces the same AppDir.

Usage: python3 packaging/build_appimage.py [--version X]   (or `make appimage`)
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(UI_DIR))

from services.config_service import ConfigService, sync_file_idempotent  # noqa: E402


def write_text_idempotent(path: Path, text: str, executable: bool = False) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    changed = not path.exists() or path.read_text(encoding="utf-8") != text
    if changed:
        path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return changed


def collect_sources(pkg: dict) -> dict[Path, Path]:
    """Maps each bundled source file to its path relative to the install subdir."""
    excluded_names = set(pkg["bundle_exclude_names"])
    excluded_suffixes = tuple(pkg["bundle_exclude_suffixes"])
    files: dict[Path, Path] = {}
    for name in pkg["bundle_files"]:
        src = UI_DIR / name
        if not src.is_file():
            raise SystemExit(f"build_appimage: missing bundle file {src} (run `make` first?)")
        files[Path(name)] = src
    for dirname in pkg["bundle_dirs"]:
        for src in sorted((UI_DIR / dirname).rglob("*")):
            rel = src.relative_to(UI_DIR)
            if src.is_file() and not excluded_names & set(rel.parts) and not src.name.endswith(excluded_suffixes):
                files[rel] = src
    return files


def desktop_entry(pkg: dict, version: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={pkg['app_name']}\n"
        f"Comment={pkg['comment']}\n"
        "Exec=AppRun\n"
        f"Icon={pkg['icon_name']}\n"
        f"Categories={pkg['categories']}\n"
        "Terminal=false\n"
        f"X-AppImage-Name={pkg['app_name']}\n"
        # appimagetool appends this line itself; writing it up front keeps re-runs a no-op
        f"X-AppImage-Version={version}\n"
    )


def assemble_appdir(cfg: ConfigService, version: str) -> Path:
    pkg = cfg.data["packaging"]
    appdir = cfg.path("appimage_appdir")
    install_dir = appdir / pkg["install_subdir"]
    icon_ext = Path(pkg["icon_source"]).suffix

    expected: set[Path] = set()
    changed = 0
    for rel, src in collect_sources(pkg).items():
        dst = install_dir / rel
        expected.add(dst)
        changed += sync_file_idempotent(src, dst)

    apprun = (UI_DIR / "packaging" / "AppRun").read_text(encoding="utf-8")
    apprun = apprun.replace("@INSTALL_SUBDIR@", pkg["install_subdir"]).replace("@BINARY_NAME@", pkg["binary_name"])
    generated = {
        appdir / "AppRun": (apprun, True),
        appdir / f"{pkg['app_id']}.desktop": (desktop_entry(pkg, version), False),
    }
    for path, (text, executable) in generated.items():
        expected.add(path)
        changed += write_text_idempotent(path, text, executable)

    icon_dst = appdir / f"{pkg['icon_name']}{icon_ext}"
    expected.add(icon_dst)
    changed += sync_file_idempotent(UI_DIR / pkg["icon_source"], icon_dst)
    dir_icon = appdir / ".DirIcon"
    expected.add(dir_icon)
    if not dir_icon.is_symlink() or dir_icon.readlink() != Path(icon_dst.name):
        dir_icon.unlink(missing_ok=True)
        dir_icon.symlink_to(icon_dst.name)
        changed += 1
    (install_dir / pkg["binary_name"]).chmod(0o755)

    # Prune files that are no longer part of the bundle so the AppDir converges
    for path in sorted(appdir.rglob("*"), reverse=True):
        if (path.is_file() or path.is_symlink()) and path not in expected:
            path.unlink()
            changed += 1
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()

    print(f"[appimage] AppDir {appdir} ({'updated ' + str(changed) + ' file(s)' if changed else 'already in sync'})")
    return appdir


def ensure_appimagetool(cfg: ConfigService) -> Path:
    pkg = cfg.data["packaging"]
    tool = cfg.path("appimage_tools_dir") / Path(pkg["appimagetool_url"]).name
    if not tool.exists():
        tool.parent.mkdir(parents=True, exist_ok=True)
        print(f"[appimage] Downloading {pkg['appimagetool_url']}")
        tmp = tool.with_suffix(".part")
        urllib.request.urlretrieve(pkg["appimagetool_url"], tmp)
        tmp.rename(tool)
    tool.chmod(0o755)
    return tool


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Raven Nest AppImage")
    parser.add_argument("--version", default=os.environ.get("RAVEN_NEST_VERSION", "dev"))
    args = parser.parse_args()

    cfg = ConfigService(UI_DIR)
    pkg = cfg.data["packaging"]
    appdir = assemble_appdir(cfg, args.version)
    tool = ensure_appimagetool(cfg)
    output = cfg.path("appimage_output_dir") / pkg["appimage_name"]

    env = os.environ.copy()
    env.update({
        "ARCH": pkg["appimage_arch"],
        "VERSION": args.version,
        # Runs appimagetool without FUSE (CI runners and containers lack it)
        "APPIMAGE_EXTRACT_AND_RUN": "1",
    })
    print(f"[appimage] Packing {output} (version {args.version})")
    rc = subprocess.call([str(tool), str(appdir), str(output)], env=env)
    if rc == 0:
        print(f"[appimage] Built {output}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
