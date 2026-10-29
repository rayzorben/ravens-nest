from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .config_service import ConfigService
from .container_service import ContainerService

VARIABLE_NAME = re.compile(r"^RAVEN_[A-Z0-9_]+$")
EXPORTED_STRING = re.compile(r'^"((?:[^"\\]|\\.)*)"="((?:[^"\\]|\\.)*)"$')


class RuntimeSettingsService:
    """
    Single-responsibility service for the Raven runtime options a prefix keeps in its own
    registry (`HKCU\\Software\\Raven\\Environment`), the same values Raven's built-in control
    panel edits and the runtime applies to every process it starts in that prefix.
    All registry access goes through the prefix's `reg.exe`, never by parsing `registry.dat`.
    """

    def __init__(self, cfg: ConfigService, container_service: ContainerService) -> None:
        self.cfg = cfg
        self.containers = container_service

    @property
    def settings(self) -> dict[str, Any]:
        return self.cfg.data["runtime_settings"]

    def is_registry_variable(self, name: str) -> bool:
        """Variables the control panel manages; container env copies of these would override it."""
        return name in self.settings.get("registry_variables", [])

    def _run_reg(self, prefix_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        timeout_ms = int(self.settings.get("reg_timeout_ms", 30000))
        cmd = [
            str(self.cfg.path("raven_bin")),
            "--run-process",
            "--execution=native",
            f"--timeout-ms={timeout_ms}",
            str(self.cfg.path("reg_exe")),
            "--",
            *args,
        ]
        env = os.environ.copy()
        env["RAVEN_PREFIX"] = str(prefix_path)
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout_ms / 1000 + 30)

    def _prefix_path(self, prefix_id: str) -> Path | None:
        prefix_path = self.cfg.path("prefixes_root") / prefix_id
        return prefix_path if prefix_id and prefix_path.is_dir() else None

    def _check_ready(self, prefix_id: str) -> tuple[Path | None, str | None]:
        prefix_path = self._prefix_path(prefix_id)
        if not prefix_path:
            return None, f"Container '{prefix_id}' not found"
        if not self.cfg.path("reg_exe").exists():
            return None, f"reg.exe not found at {self.cfg.path('reg_exe')}; rebuild the Raven runtime"
        return prefix_path, None

    def read(self, prefix_id: str) -> dict[str, Any]:
        """Returns every RAVEN_* string value under the registry key (exported with `reg export`)."""
        prefix_path, error = self._check_ready(prefix_id)
        if error:
            return {"ok": False, "error": error}
        values: dict[str, str] = {}
        with tempfile.TemporaryDirectory(prefix="raven-nest-reg-") as tmp:
            export_file = Path(tmp) / "environment.reg"
            win_path = "Z:" + str(export_file).replace("/", "\\")
            self._run_reg(prefix_path, ["export", self.settings["registry_key"], win_path, "/y"])
            # A missing key exports nothing: the prefix simply has no runtime options yet.
            if export_file.exists():
                for line in export_file.read_text(encoding="utf-16", errors="replace").splitlines():
                    m = EXPORTED_STRING.match(line.strip())
                    if m:
                        name = m.group(1).replace('\\"', '"').replace("\\\\", "\\")
                        if VARIABLE_NAME.match(name):
                            values[name] = m.group(2).replace('\\"', '"').replace("\\\\", "\\")
        return {"ok": True, "registry_key": self.settings["registry_key"], "values": values}

    def write(self, prefix_id: str, values: dict[str, Any], replace: bool = False) -> dict[str, Any]:
        """
        Idempotently sets each value (an empty value removes it) and only touches values that differ.
        With `replace`, RAVEN_* values not named in `values` are removed too.
        """
        prefix_path, error = self._check_ready(prefix_id)
        if error:
            return {"ok": False, "error": error}
        wanted = {str(k).strip(): ("" if v is None else str(v).strip()) for k, v in values.items()}
        for name, value in wanted.items():
            if not VARIABLE_NAME.match(name):
                return {"ok": False, "error": f"Invalid runtime option name: {name} (expected RAVEN_*)"}
            if '"' in value:
                return {"ok": False, "error": f"Runtime option {name} may not contain double quotes"}

        current = self.read(prefix_id)
        if not current["ok"]:
            return current
        live: dict[str, str] = current["values"]
        if replace:
            wanted.update({name: "" for name in live if name not in wanted})

        key = self.settings["registry_key"]
        changed: list[str] = []
        for name, value in sorted(wanted.items()):
            if value == live.get(name, ""):
                continue
            args = ["add", key, "/v", name, "/t", "REG_SZ", "/d", value, "/f"] if value else ["delete", key, "/v", name, "/f"]
            proc = self._run_reg(prefix_path, args)
            if proc.returncode != 0:
                return {"ok": False, "error": f"reg.exe {args[0]} {name} failed", "output": (proc.stdout + proc.stderr).strip()[-2000:], "changed": changed}
            changed.append(name)

        result = self.read(prefix_id)
        result["changed"] = changed
        return result

    def migrate_env(self, prefix_id: str) -> dict[str, Any]:
        """
        Idempotently moves control-panel-managed variables out of the container's `nest.json` env
        into the prefix registry, so the env no longer silently overrides the control panel.
        """
        prefix_path = self._prefix_path(prefix_id)
        if not prefix_path:
            return {"ok": False, "error": f"Container '{prefix_id}' not found"}
        meta = self.containers.load_meta(prefix_path)
        env = meta.get("env", {})
        moving = {k: v for k, v in env.items() if self.is_registry_variable(k)}
        if not moving:
            return {"ok": True, "migrated": []}
        res = self.write(prefix_id, moving)
        if not res["ok"]:
            return res
        meta["env"] = {k: v for k, v in env.items() if k not in moving}
        self.containers.save_meta(prefix_path, meta)
        return {"ok": True, "migrated": sorted(moving)}
