from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any


def deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Pure, idempotent recursive dictionary merge (DRY)."""
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def sha256_file(path: Path) -> str:
    """Computes SHA-256 digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            h.update(chunk)
    return h.hexdigest()


def sync_file_idempotent(src: Path, dst: Path) -> bool:
    """
    Idempotently copies `src` to `dst` ONLY if `dst` is missing or differs
    in size/digest. Returns True if a copy occurred, False if already in sync.
    """
    if not src.exists() or not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.is_file():
        if dst.stat().st_size == src.stat().st_size and sha256_file(dst) == sha256_file(src):
            return False
    shutil.copy2(src, dst)
    return True


def write_json_idempotent(path: Path, data: Any) -> bool:
    """
    Idempotently writes formatted JSON only when serialized content changes.
    """
    text = json.dumps(data, indent=2, sort_keys=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8", errors="replace") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


class ConfigService:
    """
    Single-responsibility configuration service ("Everything as Config").
    Loads defaults from `ui/config/default.json`, merges user overrides from
    `~/.config/raven-nest/config.json`, and resolves all template variables.
    """

    def __init__(self, ui_dir: Path) -> None:
        self.ui_dir = ui_dir.resolve()
        if os.environ.get("RAVEN_ROOT"):
            self.repo_root = Path(os.environ["RAVEN_ROOT"]).resolve()
        elif (self.ui_dir.parent / "CMakeLists.txt").exists():
            self.repo_root = self.ui_dir.parent
        else:
            self.repo_root = Path.home() / "src" / "raven"
        self.default_config_path = self.ui_dir / "config" / "default.json"
        self._raw_defaults: dict[str, Any] = {}
        self._user_overrides: dict[str, Any] = {}
        self._merged: dict[str, Any] = {}
        self._resolved_paths: dict[str, Path] = {}
        self.reload()

    def reload(self) -> None:
        self._raw_defaults = json.loads(self.default_config_path.read_text(encoding="utf-8"))
        user_cfg_template = self._raw_defaults.get("paths", {}).get(
            "user_config_file", "{home}/.config/raven-nest/config.json"
        )
        user_cfg_path = Path(user_cfg_template.format(home=str(Path.home())))
        self.user_config_path = user_cfg_path
        if user_cfg_path.exists():
            try:
                self._user_overrides = json.loads(user_cfg_path.read_text(encoding="utf-8"))
            except Exception:
                self._user_overrides = {}
        else:
            self._user_overrides = {}

        self._merged = deep_merge(self._raw_defaults, self._user_overrides)
        self._resolve_paths()

    def _resolve_paths(self) -> None:
        tokens: dict[str, str] = {
            "auto_repo_root": str(self.repo_root),
            "home": str(Path.home()),
        }
        raw_paths = self._merged.get("paths", {})
        # Multi-pass resolution so path entries can reference earlier path keys
        for _ in range(4):
            for key, pattern in raw_paths.items():
                try:
                    tokens[key] = str(pattern).format(**tokens)
                except KeyError:
                    pass
        self._tokens = tokens
        self._resolved_paths = {k: Path(v) for k, v in tokens.items() if k != "auto_repo_root"}

    def expand(self, template: str) -> str:
        """Expands `{repo_root}`, `{build_dir}`, `{native_dir}`, `{home}`, etc."""
        try:
            return str(template).format(**self._tokens)
        except KeyError:
            return str(template)

    def path(self, key: str) -> Path:
        return self._resolved_paths[key]

    @property
    def data(self) -> dict[str, Any]:
        return self._merged

    @property
    def overrides(self) -> dict[str, Any]:
        return self._user_overrides

    def update_overrides(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Idempotently updates user configuration overrides and reloads."""
        self._user_overrides = deep_merge(self._user_overrides, patch)
        write_json_idempotent(self.user_config_path, self._user_overrides)
        self.reload()
        return self.export_client_config()

    def reset_overrides(self) -> dict[str, Any]:
        """Resets user configuration overrides back to default.json."""
        self._user_overrides = {}
        if self.user_config_path.exists():
            self.user_config_path.unlink()
        self.reload()
        return self.export_client_config()

    def export_client_config(self) -> dict[str, Any]:
        """Returns the resolved configuration for the UI & API consumers."""
        return {
            "server": self._merged["server"],
            "ui": self._merged["ui"],
            "paths": {k: str(v) for k, v in self._resolved_paths.items()},
            "desktop_backends": self._merged["desktop_backends"],
            "blueprints": self._merged["blueprints"],
            "snapshots": self._merged["snapshots"],
            "user_config_path": str(self.user_config_path),
            "default_config_path": str(self.default_config_path),
            "user_overrides": self._user_overrides,
        }
