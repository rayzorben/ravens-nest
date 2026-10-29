from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .config_service import ConfigService, sha256_file, write_json_idempotent
from .container_service import ContainerService
from .dependency_service import DependencyService
from .doctor_service import DoctorService
from .process_service import ProcessService
from .runtime_settings_service import RuntimeSettingsService
from .version_snapshot_service import VersionSnapshotService


SYNC_MODE_LABELS: dict[str, str] = {
    "pull": "GitHub Pull & Build Raven ({branch})",
    "clone": "GitHub Clone & Build Raven ({branch})",
    "build_local": "Build Local Raven Checkout",
    "rebuild_only": "Rebuild Containers with Current Raven Build",
}


class UpstreamService:
    """
    Single-responsibility service for:
    1. Auto-detecting new commits and pushes on the Raven GitHub repository.
    2. Cloning, pulling, configuring, and building the latest Raven runtime.
    3. Tracking the Raven commit/binary version inside each container and
       offering idempotent rebuilds of an individual container or all containers.
    """

    def __init__(
        self,
        cfg: ConfigService,
        container_service: ContainerService,
        dep_service: DependencyService,
        ver_service: VersionSnapshotService,
        doctor_service: DoctorService,
        proc_service: ProcessService,
    ) -> None:
        self.cfg = cfg
        self.containers = container_service
        self.dep = dep_service
        self.ver = ver_service
        self.doctor = doctor_service
        self.proc = proc_service
        self._lock = threading.Lock()
        self._last_remote_check: float = 0.0
        self._cached_remote: dict[str, Any] = {}

    def _git(self, args: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
        repo_root = self.cfg.path("repo_root")
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _parse_commit_lines(self, raw: str) -> list[dict[str, str]]:
        commits: list[dict[str, str]] = []
        for line in raw.strip().splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({
                    "sha": parts[0].strip(),
                    "subject": parts[1].strip(),
                    "rel_time": parts[2].strip(),
                    "author": parts[3].strip(),
                })
        return commits

    def get_local_git_info(self) -> dict[str, Any]:
        repo_root = self.cfg.path("repo_root")
        if not (repo_root / ".git").exists():
            return {
                "cloned": False,
                "repo_root": str(repo_root),
                "branch": "",
                "head_sha": "",
                "short_sha": "",
                "subject": "",
                "commit_date": "",
                "dirty_count": 0,
                "recent_commits": [],
            }

        try:
            head_sha = self._git(["rev-parse", "HEAD"]).stdout.strip()
            branch = self._git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
            subject = self._git(["log", "-1", "--format=%s"]).stdout.strip()
            commit_date = self._git(["log", "-1", "--format=%ci"]).stdout.strip()
            dirty_out = self._git(["status", "--porcelain", "--untracked-files=no"]).stdout.strip()
            dirty_count = len(dirty_out.splitlines()) if dirty_out else 0
            recent_raw = self._git(["log", "-n", "8", "--format=%h|%s|%cr|%an"]).stdout
            recent_commits = self._parse_commit_lines(recent_raw)
        except Exception:
            head_sha = ""
            branch = ""
            subject = ""
            commit_date = ""
            dirty_count = 0
            recent_commits = []

        return {
            "cloned": bool(head_sha),
            "repo_root": str(repo_root),
            "branch": branch,
            "head_sha": head_sha,
            "short_sha": head_sha[:7] if head_sha else "",
            "subject": subject,
            "commit_date": commit_date,
            "dirty_count": dirty_count,
            "recent_commits": recent_commits,
        }

    def get_build_stamp(self, local_info: dict[str, Any] | None = None) -> dict[str, Any]:
        if local_info is None:
            local_info = self.get_local_git_info()
        raven_bin = self.cfg.path("raven_bin")
        stamp_file = self.cfg.path("build_stamp_file")

        bin_exists = raven_bin.exists() and raven_bin.is_file()
        bin_sha256 = sha256_file(raven_bin)[:16] if bin_exists else ""
        bin_mtime = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(raven_bin.stat().st_mtime))
            if bin_exists
            else ""
        )

        stored: dict[str, Any] = {}
        if stamp_file.exists():
            try:
                stored = json.loads(stamp_file.read_text(encoding="utf-8"))
            except Exception:
                stored = {}

        commit_sha = stored.get("commit_sha") or (local_info["head_sha"] if bin_exists else "")
        short_sha = commit_sha[:7] if commit_sha else ""
        subject = stored.get("subject") or (local_info["subject"] if bin_exists else "")
        built_at = stored.get("built_at") or bin_mtime

        return {
            "binary_exists": bin_exists,
            "binary_path": str(raven_bin),
            "binary_sha256": bin_sha256,
            "commit_sha": commit_sha,
            "short_sha": short_sha,
            "subject": subject,
            "built_at": built_at,
            "stamp_recorded": stamp_file.exists(),
        }

    def write_build_stamp(self, local_info: dict[str, Any] | None = None) -> dict[str, Any]:
        """Idempotently records the built Raven commit & binary SHA-256."""
        if local_info is None:
            local_info = self.get_local_git_info()
        raven_bin = self.cfg.path("raven_bin")
        stamp_file = self.cfg.path("build_stamp_file")
        bin_sha256 = sha256_file(raven_bin)[:16] if raven_bin.exists() else ""
        stamp = {
            "commit_sha": local_info.get("head_sha", ""),
            "short_sha": local_info.get("short_sha", ""),
            "branch": local_info.get("branch", ""),
            "subject": local_info.get("subject", ""),
            "binary_sha256": bin_sha256,
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        write_json_idempotent(stamp_file, stamp)
        return self.get_build_stamp(local_info)

    def _check_remote_git(self, force: bool = False) -> dict[str, Any]:
        up_cfg = self.cfg.data.get("upstream", {})
        remote_url = up_cfg["remote_url"]
        branch = up_cfg["branch"]
        interval = int(up_cfg.get("check_interval_sec", 60))
        timeout = int(up_cfg.get("fetch_timeout_sec", 15))
        auto_enabled = bool(up_cfg.get("auto_check_enabled", True))

        now = time.time()
        with self._lock:
            if not force and self._cached_remote and (now - self._last_remote_check < interval):
                return self._cached_remote
            if not force and not auto_enabled and self._cached_remote:
                return self._cached_remote

        repo_root = self.cfg.path("repo_root")
        cloned = (repo_root / ".git").exists()
        remote_sha = ""
        remote_subject = ""
        remote_date = ""
        commits_behind = 0
        commits_ahead = 0
        incoming_commits: list[dict[str, str]] = []
        fetch_ok = False
        error_msg = ""

        try:
            if cloned:
                # Fetch latest remote branch refs so we can inspect incoming commits
                fetch_proc = self._git(["fetch", "origin", branch, "--quiet"], timeout=timeout)
                fetch_ok = fetch_proc.returncode == 0
                ref = f"origin/{branch}"
                rev_proc = self._git(["rev-parse", ref])
                if rev_proc.returncode == 0:
                    remote_sha = rev_proc.stdout.strip()
                    remote_subject = self._git(["log", "-1", "--format=%s", ref]).stdout.strip()
                    remote_date = self._git(["log", "-1", "--format=%ci", ref]).stdout.strip()
                    behind_str = self._git(["rev-list", "--count", f"HEAD..{ref}"]).stdout.strip()
                    ahead_str = self._git(["rev-list", "--count", f"{ref}..HEAD"]).stdout.strip()
                    commits_behind = int(behind_str) if behind_str.isdigit() else 0
                    commits_ahead = int(ahead_str) if ahead_str.isdigit() else 0
                    if commits_behind > 0:
                        inc_raw = self._git(
                            ["log", f"HEAD..{ref}", "-n", "15", "--format=%h|%s|%cr|%an"]
                        ).stdout
                        incoming_commits = self._parse_commit_lines(inc_raw)
            else:
                ls_proc = subprocess.run(
                    ["git", "ls-remote", remote_url, f"refs/heads/{branch}"],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if ls_proc.returncode == 0 and ls_proc.stdout.strip():
                    remote_sha = ls_proc.stdout.strip().split()[0]
                    fetch_ok = True
                    commits_behind = 1
        except Exception as exc:
            error_msg = str(exc)

        remote_info = {
            "remote_url": remote_url,
            "branch": branch,
            "remote_sha": remote_sha,
            "remote_short_sha": remote_sha[:7] if remote_sha else "",
            "remote_subject": remote_subject,
            "remote_date": remote_date,
            "commits_behind": commits_behind,
            "commits_ahead": commits_ahead,
            "incoming_commits": incoming_commits,
            "fetch_ok": fetch_ok,
            "error": error_msg,
            "checked_at": time.strftime("%H:%M:%S"),
        }
        with self._lock:
            self._cached_remote = remote_info
            self._last_remote_check = now
        return remote_info

    def get_status(self, force_remote: bool = False) -> dict[str, Any]:
        local = self.get_local_git_info()
        remote = self._check_remote_git(force=force_remote)
        stamp = self.get_build_stamp(local)

        commits_behind = remote["commits_behind"]
        needs_runtime_build = (
            not stamp["binary_exists"]
            or (bool(local["head_sha"]) and stamp["commit_sha"] != local["head_sha"])
        )

        # Target commit is the latest remote commit if behind, else local HEAD
        target_sha = remote["remote_sha"] if (commits_behind > 0 and remote["remote_sha"]) else local["head_sha"]
        target_short_sha = target_sha[:7] if target_sha else ""
        target_subject = (
            remote["remote_subject"]
            if (commits_behind > 0 and remote["remote_subject"])
            else local["subject"]
        )

        containers = self.containers.list_containers()
        container_statuses: list[dict[str, Any]] = []
        outdated_ids: list[str] = []

        for c in containers:
            c_ver = c.get("raven_version") or {}
            c_sha = c_ver.get("commit_sha", "")
            c_short = c_ver.get("short_sha") or (c_sha[:7] if c_sha else "untracked")
            c_bin = c_ver.get("binary_sha256", "")

            matches_built = bool(
                stamp["binary_exists"]
                and c_sha
                and c_sha == stamp["commit_sha"]
                and (not c_bin or c_bin == stamp["binary_sha256"])
            )
            matches_latest = bool(matches_built and commits_behind == 0 and not needs_runtime_build)

            if not c_sha:
                reason = f"Container not yet stamped with Raven {stamp['short_sha'] or target_short_sha}"
                status = "needs_rebuild"
            elif commits_behind > 0:
                reason = f"{commits_behind} new commit(s) pushed to GitHub ({remote['remote_short_sha']})"
                status = "outdated_remote"
            elif not matches_built:
                reason = f"Container is on #{c_short}, built runtime is #{stamp['short_sha']}"
                status = "outdated_build"
            elif needs_runtime_build:
                reason = f"Local git HEAD #{local['short_sha']} not yet built"
                status = "outdated_build"
            else:
                reason = f"Up to date with Raven #{c_short}"
                status = "up_to_date"

            if not matches_latest:
                outdated_ids.append(c["id"])

            container_statuses.append({
                "id": c["id"],
                "display_name": c["display_name"],
                "current_snapshot_version": c["current_version"],
                "container_raven_sha": c_sha,
                "container_raven_short": c_short,
                "container_raven_subject": c_ver.get("subject", ""),
                "container_raven_updated_at": c_ver.get("updated_at", ""),
                "matches_built": matches_built,
                "matches_latest": matches_latest,
                "status": status,
                "reason": reason,
            })

        update_available = (
            not local["cloned"]
            or commits_behind > 0
            or needs_runtime_build
            or len(outdated_ids) > 0
        )

        headline = "All containers and Raven runtime are up to date."
        if not local["cloned"]:
            headline = f"Raven source repository not cloned at {local['repo_root']}."
        elif commits_behind > 0:
            headline = (
                f"{commits_behind} new commit{'s' if commits_behind != 1 else ''} pushed to GitHub "
                f"({remote['remote_short_sha']}: {remote['remote_subject']})"
            )
        elif needs_runtime_build:
            headline = f"Local commit #{local['short_sha']} is ready to be compiled into build/native/raven."
        elif outdated_ids:
            headline = (
                f"{len(outdated_ids)} container{'s' if len(outdated_ids) != 1 else ''} "
                f"can be rebuilt with Raven #{stamp['short_sha']}."
            )

        return {
            "update_available": update_available,
            "has_remote_updates": commits_behind > 0,
            "needs_runtime_build": needs_runtime_build,
            "headline": headline,
            "target_sha": target_sha,
            "target_short_sha": target_short_sha,
            "target_subject": target_subject,
            "local": local,
            "remote": remote,
            "build": stamp,
            "containers": container_statuses,
            "outdated_container_ids": outdated_ids,
            "outdated_containers_count": len(outdated_ids),
            "total_containers_count": len(containers),
        }

    def rebuild_single_container(
        self,
        prefix_id: str,
        stamp: dict[str, Any] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        Idempotently upgrades/rebuilds a single sandbox container with the
        current built Raven runtime:
        1. Takes a pre-rebuild snapshot (if configured and state changed).
        2. Syncs all system32/syswow64 binaries, runtime DLLs, and redistributables.
        3. Runs `raven --reboot` to migrate/verify container registry & directories.
        4. Records the new `raven_version` stamp in `nest.json`.
        5. Takes a post-rebuild version snapshot.
        """
        prefix_path = self.cfg.path("prefixes_root") / prefix_id
        if not prefix_path.exists():
            return {"ok": False, "prefix_id": prefix_id, "error": f"Container '{prefix_id}' not found"}

        if stamp is None:
            stamp = self.write_build_stamp()

        meta = self.containers.load_meta(prefix_path)
        prev_ver = meta.get("raven_version") or {}
        short_sha = stamp.get("short_sha") or "latest"
        commit_sha = stamp.get("commit_sha") or ""
        bin_sha256 = stamp.get("binary_sha256") or ""
        subject = stamp.get("subject") or "Raven runtime build"

        already_stamped = (
            prev_ver.get("commit_sha") == commit_sha
            and prev_ver.get("binary_sha256") == bin_sha256
            and bool(commit_sha)
        )

        up_cfg = self.cfg.data.get("upstream", {})
        snap_before = bool(up_cfg.get("auto_snapshot_before_container_rebuild", True))
        snap_after = bool(up_cfg.get("auto_snapshot_after_container_rebuild", True))

        steps: list[str] = []

        # 1. Pre-rebuild safety snapshot if state differs from latest snapshot
        if snap_before and not already_stamped:
            versions = self.ver.list_versions(prefix_path)
            if versions and not versions[0].get("matches_live"):
                pre = self.ver.create_snapshot(
                    prefix_path,
                    tag=f"before-raven-{short_sha}",
                    notes=f"Safety checkpoint before upgrading container to Raven #{short_sha}",
                    pinned=False,
                )
                if pre.get("ok"):
                    steps.append(f"Captured safety snapshot {pre.get('version')} ({pre.get('snapshot_name')})")

        # 2. Idempotent deployment of all Raven runtime components & system32/syswow64 utilities
        deploy_res = self.dep.auto_deploy(prefix_id, deploy_redist=True)
        steps.extend(deploy_res.get("actions", []))

        # 3. Run `raven --reboot` to refresh registry hives & system directories under latest binary
        raven_bin = self.cfg.path("raven_bin")
        if raven_bin.exists() and (not already_stamped or force or not deploy_res.get("idempotent_noop")):
            env = os.environ.copy()
            env["RAVEN_PREFIX"] = str(prefix_path)
            proc = subprocess.run(
                [str(raven_bin), "--reboot"],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode == 0:
                steps.append(f"Executed raven --reboot with Raven #{short_sha}")

        # 4. Update container nest.json with the Raven version stamp
        meta = self.containers.load_meta(prefix_path)
        meta["raven_version"] = {
            "commit_sha": commit_sha,
            "short_sha": short_sha,
            "subject": subject,
            "binary_sha256": bin_sha256,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.containers.save_meta(prefix_path, meta)

        # 5. Post-rebuild version snapshot if updated
        if snap_after and (not already_stamped or not deploy_res.get("idempotent_noop")):
            post = self.ver.create_snapshot(
                prefix_path,
                tag=f"raven-{short_sha}",
                notes=f"Rebuilt container with Raven #{short_sha}: {subject}",
                pinned=False,
            )
            if post.get("ok"):
                steps.append(f"Created version snapshot {post.get('version')} (raven-{short_sha})")

        return {
            "ok": True,
            "prefix_id": prefix_id,
            "display_name": meta.get("display_name", prefix_id),
            "raven_version": meta["raven_version"],
            "idempotent_noop": already_stamped and bool(deploy_res.get("idempotent_noop")),
            "steps": steps,
            "prefix": self.containers.inspect_container(prefix_path),
        }

    def rebuild_containers(self, target: str = "all", force: bool = False) -> dict[str, Any]:
        """
        Rebuilds either a specific container (`target == prefix_id`) or all
        active containers (`target == "all"`) using the current built Raven runtime.
        """
        stamp = self.write_build_stamp()
        if target == "all":
            target_ids = [c["id"] for c in self.containers.list_containers()]
        else:
            target_ids = [target]

        results = []
        for cid in target_ids:
            results.append(self.rebuild_single_container(cid, stamp=stamp, force=force))

        return {
            "ok": all(r.get("ok") for r in results),
            "target": target,
            "rebuilt_count": len(results),
            "raven_version": stamp,
            "results": results,
            "upstream": self.get_status(force_remote=False),
        }

    def start_pipeline_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Launches a tracked background process that clones/pulls Raven from GitHub,
        provisions & builds CMake targets, records the build stamp, and optionally
        rebuilds a specific container or all containers.
        """
        sync_mode = payload.get("sync_mode", "pull")  # key of SYNC_MODE_LABELS
        rebuild_target = payload.get("rebuild_target", "all")  # "none" | "all" | "<prefix_id>"
        remote_url = payload.get("remote_url") or self.cfg.data["upstream"]["remote_url"]
        branch = payload.get("branch") or self.cfg.data["upstream"]["branch"]

        cmd = [
            sys.executable,
            "-u",
            "-m",
            "services.upstream_service",
            "--ui-dir",
            str(self.cfg.ui_dir),
            "--sync-mode",
            str(sync_mode),
            "--rebuild-target",
            str(rebuild_target),
            "--remote-url",
            str(remote_url),
            "--branch",
            str(branch),
        ]
        if sync_mode not in SYNC_MODE_LABELS:
            return {"ok": False, "error": f"Unknown sync mode: {sync_mode}"}
        job_name = SYNC_MODE_LABELS[sync_mode].format(branch=branch)
        if rebuild_target == "all":
            job_name += " + Rebuild All Containers"
        elif rebuild_target and rebuild_target != "none":
            job_name += f" + Rebuild '{rebuild_target}'"
        if sync_mode == "rebuild_only" and rebuild_target in ("", "none"):
            return {"ok": False, "error": "rebuild_only requires a rebuild target"}

        return self.proc._spawn_tracked(
            cmd=cmd,
            cwd=self.cfg.ui_dir,
            env=os.environ.copy(),
            prefix_id=rebuild_target if rebuild_target not in ("all", "none") else "system",
            name=job_name,
            path_str=str(self.cfg.path("repo_root")),
        )


def _run_logged(cmd: list[str], cwd: Path | None = None) -> int:
    print(f"[raven-updater] $ {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line.rstrip("\n"), flush=True)
    return proc.wait()


def _git_out(repo_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args], capture_output=True, text=True
    ).stdout.strip()


def _sync_source(repo_root: Path, remote_url: str, branch: str, sync_mode: str) -> int:
    """Step 1: idempotently clones or fast-forwards the Raven checkout, never discarding local work."""
    print(f"=== [Step 1/4] Synchronizing Raven Source ({repo_root}) ===", flush=True)
    git = ["git", "-C", str(repo_root)]
    if not (repo_root / ".git").exists():
        repo_root.parent.mkdir(parents=True, exist_ok=True)
        print(f"[raven-updater] Cloning {remote_url} (branch: {branch}) into {repo_root}...", flush=True)
        rc = _run_logged(["git", "clone", "--branch", branch, remote_url, str(repo_root)])
        if rc != 0:
            print(f"[raven-updater] ERROR: git clone failed with exit code {rc}", flush=True)
        return rc

    if sync_mode == "build_local":
        print("[raven-updater] Skipping git pull (building current local checkout)...", flush=True)
        return 0

    rc = _run_logged([*git, "fetch", "origin", branch])
    if rc != 0:
        print(f"[raven-updater] ERROR: git fetch failed with exit code {rc}", flush=True)
        return rc

    # Safely preserve any uncommitted tracked modifications before switching/merging
    if _git_out(repo_root, "status", "--porcelain", "--untracked-files=no"):
        stash_msg = f"raven-nest-auto-backup-{time.strftime('%Y%m%d-%H%M%S')}"
        print(f"[raven-updater] Stashing local tracked changes as '{stash_msg}'...", flush=True)
        _run_logged([*git, "stash", "push", "-m", stash_msg])

    cur_branch = _git_out(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if cur_branch != branch:
        print(f"[raven-updater] Switching from branch '{cur_branch}' (preserved in git refs) to '{branch}'...", flush=True)
        return _run_logged([*git, "checkout", "-B", branch, f"origin/{branch}"])

    if _run_logged([*git, "merge", "--ff-only", f"origin/{branch}"]) == 0:
        return 0
    # Local commits diverged from GitHub: keep them on a backup branch, then converge on origin
    backup = f"raven-nest-backup-{time.strftime('%Y%m%d-%H%M%S')}"
    print(f"[raven-updater] Fast-forward not possible; saving local '{branch}' as '{backup}'...", flush=True)
    rc = _run_logged([*git, "branch", backup])
    if rc != 0:
        print(f"[raven-updater] ERROR: could not create backup branch (exit {rc}); aborting.", flush=True)
        return rc
    return _run_logged([*git, "reset", "--hard", f"origin/{branch}"])


def _build_runtime(doctor_svc: DoctorService, proc_svc: ProcessService, repo_root: Path) -> int:
    """Steps 2-3: provisions missing doctor components, (re)configures CMake if stale, builds targets."""
    print("=== [Step 2/4] Provisioning Components & Verifying CMake Configuration ===", flush=True)
    repair_cmds = proc_svc.repair_commands()
    doc_eval = doctor_svc.evaluate()
    for comp in doc_eval["components"]:
        action = comp["repair_action"]
        if comp["status"] == "ready" or action in ("reconfigure_cmake", "build_runtime") or action not in repair_cmds:
            continue
        print(f"[raven-updater] {comp['code']} {comp['title']} is {comp['status']}; running {action}...", flush=True)
        rc = _run_logged(repair_cmds[action], cwd=repo_root)
        if rc != 0:
            print(f"[raven-updater] ERROR: {action} failed with exit code {rc}", flush=True)
            return rc

    cmake = doctor_svc.evaluate()["cmake"]
    if not (cmake["cache_exists"] and cmake["source_dir_matches"] and cmake["font_hashes_match"]):
        rc = _run_logged(repair_cmds["reconfigure_cmake"], cwd=repo_root)
        if rc != 0:
            print(f"[raven-updater] ERROR: CMake configure failed with exit code {rc}", flush=True)
            return rc
    else:
        print("[raven-updater] CMake cache and Liberation font SHA-256 hashes are already in sync.", flush=True)

    build_cmd = repair_cmds["build_runtime"]
    print(f"=== [Step 3/4] Building Raven Targets ({' '.join(build_cmd[build_cmd.index('--target') + 1:])}) ===", flush=True)
    rc = _run_logged(build_cmd, cwd=repo_root)
    if rc != 0:
        print(f"[raven-updater] ERROR: CMake build failed with exit code {rc}", flush=True)
    return rc


def run_pipeline_cli() -> int:
    parser = argparse.ArgumentParser(description="Raven Nest Upstream Clone/Pull, Build & Container Rebuild Pipeline")
    parser.add_argument("--ui-dir", required=True)
    parser.add_argument("--sync-mode", default="pull", choices=list(SYNC_MODE_LABELS))
    parser.add_argument("--rebuild-target", default="all")
    parser.add_argument("--remote-url", default="")
    parser.add_argument("--branch", default="")
    args = parser.parse_args()

    ui_dir = Path(args.ui_dir).resolve()
    cfg = ConfigService(ui_dir)
    ver_svc = VersionSnapshotService(cfg)
    dep_svc = DependencyService(cfg)
    container_svc = ContainerService(cfg, dep_svc, ver_svc)
    doctor_svc = DoctorService(cfg)
    proc_svc = ProcessService(cfg, container_svc, dep_svc, doctor_svc, RuntimeSettingsService(cfg, container_svc))
    up_svc = UpstreamService(cfg, container_svc, dep_svc, ver_svc, doctor_svc, proc_svc)

    repo_root = cfg.path("repo_root")
    up_cfg = cfg.data["upstream"]

    if args.sync_mode == "rebuild_only":
        print("=== [Steps 1-3/4] Skipped: rebuilding containers with the existing Raven build ===", flush=True)
    else:
        rc = _sync_source(repo_root, args.remote_url or up_cfg["remote_url"], args.branch or up_cfg["branch"], args.sync_mode)
        if rc == 0:
            rc = _build_runtime(doctor_svc, proc_svc, repo_root)
        if rc != 0:
            return rc
        stamp = up_svc.write_build_stamp()
        print(
            f"[raven-updater] Built Raven #{stamp['short_sha']} ({stamp['subject']}) • binary SHA-256: {stamp['binary_sha256']}",
            flush=True,
        )

    # Step 4: Rebuild requested container(s)
    if args.rebuild_target and args.rebuild_target != "none":
        if not cfg.path("raven_bin").exists():
            print(f"[raven-updater] ERROR: no Raven binary at {cfg.path('raven_bin')}; build it first.", flush=True)
            return 1
        print(f"=== [Step 4/4] Rebuilding Containers (target: {args.rebuild_target}) ===", flush=True)
        res = up_svc.rebuild_containers(target=args.rebuild_target, force=True)
        for item in res.get("results", []):
            print(f"[raven-updater] Container '{item.get('display_name', item.get('prefix_id'))}':", flush=True)
            for step in item.get("steps", []) or [item.get("error", "")]:
                print(f"  - {step}", flush=True)
        if not res["ok"]:
            return 1
    else:
        print("=== [Step 4/4] Container rebuild skipped (runtime build complete) ===", flush=True)

    print("[raven-updater] Pipeline finished successfully.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_pipeline_cli())
