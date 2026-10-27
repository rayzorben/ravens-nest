const state = {
  config: null,
  prefixes: [],
  trash: [],
  doctor: null,
  processes: [],
  upstream: null,
  sampleApps: [],
  selectedPrefixId: null,
  activeMainView: "container", // "container" | "upstream" | "doctor" | "processes" | "trash" | "config"
  activeTab: "tab-programs",
  inspectedData: null,
  pickerCallback: null,
  pickerCurrentDir: "",
  themeChoice: localStorage.getItem("raven-theme-choice") || null,
  configFormHydratedForPrefix: null,
};

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function showToast(message) {
  const container = document.getElementById("toast-container");
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

async function apiGet(url) {
  const res = await fetch(url);
  return res.json();
}

async function apiPost(url, body = {}) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

function applyTheme(choice, persist = false) {
  state.themeChoice = choice;
  localStorage.setItem("raven-theme-choice", choice);
  let effective = choice;
  if (choice === "system") {
    effective = window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  document.documentElement.setAttribute("data-theme", effective);
  document.querySelectorAll(".theme-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.themeChoice === choice);
  });
  if (persist) {
    apiPost("/api/config/update", { overrides: { ui: { default_theme: choice } } });
  }
}

function populateConfigSelects(cfg) {
  if (!cfg) return;

  // Blueprints in Create Container modal
  const bpSelect = document.getElementById("new-container-blueprint");
  if (bpSelect && bpSelect.options.length === 0) {
    for (const bp of cfg.blueprints || []) {
      const opt = document.createElement("option");
      opt.value = bp.id;
      opt.textContent = bp.name;
      bpSelect.appendChild(opt);
    }
  }

  // Desktop backends in Create Container, Runner Config, and Container Settings
  for (const selId of ["new-container-desktop", "runner-desktop", "cfg-container-desktop"]) {
    const sel = document.getElementById(selId);
    if (sel && sel.options.length === 0) {
      for (const b of cfg.desktop_backends || []) {
        const opt = document.createElement("option");
        opt.value = b.id;
        opt.textContent = b.label;
        sel.appendChild(opt);
      }
    }
  }
}

function getSelectedPrefix() {
  return state.prefixes.find((p) => p.id === state.selectedPrefixId) || state.prefixes[0] || null;
}

async function refreshState() {
  const data = await apiGet("/api/state");
  const firstLoad = !state.config;
  state.config = data.config || null;
  state.prefixes = data.prefixes || [];
  state.trash = data.trash || [];
  state.doctor = data.doctor || null;
  const prevRunning = state.processes.filter((p) => p.status === "running").length;
  state.processes = data.processes || [];
  state.sampleApps = data.sample_apps || [];
  if (state.upstream && state.processes.filter((p) => p.status === "running").length < prevRunning) {
    refreshUpstream();
  }

  if (firstLoad && state.config) {
    populateConfigSelects(state.config);
    const initialTheme = state.themeChoice || state.config.ui?.default_theme || "dark";
    applyTheme(initialTheme, false);
    state.pickerCurrentDir = state.config.paths?.prefixes_root || "/home";
  }

  if (
    (!state.selectedPrefixId || !state.prefixes.some((p) => p.id === state.selectedPrefixId)) &&
    state.prefixes.length > 0
  ) {
    const chief = state.prefixes.find((p) => p.id === "chief-architect");
    state.selectedPrefixId = chief ? chief.id : state.prefixes[0].id;
    state.configFormHydratedForPrefix = null;
  }

  renderSidebar();
  renderMainView();
}

function renderSidebar() {
  const list = document.getElementById("containers-list");
  list.innerHTML = "";

  for (const p of state.prefixes) {
    const appCount = p.apps.filter((a) => a.category !== "system_tool").length;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `nav-item ${state.activeMainView === "container" && state.selectedPrefixId === p.id ? "active" : ""}`;
    btn.innerHTML = `
      <span class="nav-icon">📦</span>
      <div class="nav-item-body">
        <span class="nav-title">${escapeHtml(p.display_name)}</span>
        <span class="nav-meta">${escapeHtml(p.current_version)} • ${appCount} app${appCount === 1 ? "" : "s"} • ${escapeHtml(p.desktop)}</span>
      </div>
    `;
    btn.addEventListener("click", () => {
      state.selectedPrefixId = p.id;
      state.activeMainView = "container";
      state.inspectedData = null;
      state.configFormHydratedForPrefix = null;
      renderSidebar();
      renderMainView();
    });
    list.appendChild(btn);
  }

  document.getElementById("nav-upstream").classList.toggle("active", state.activeMainView === "upstream");
  document.getElementById("nav-doctor").classList.toggle("active", state.activeMainView === "doctor");
  document.getElementById("nav-processes").classList.toggle("active", state.activeMainView === "processes");
  document.getElementById("nav-trash").classList.toggle("active", state.activeMainView === "trash");
  document.getElementById("nav-config").classList.toggle("active", state.activeMainView === "config");

  if (state.doctor) {
    const s = state.doctor.summary;
    document.getElementById("doctor-badge").textContent = `${s.ready_components}/${s.total_components} components ready`;
    document.getElementById("host-compositor-label").textContent =
      `Host: ${s.compositor} (${s.wayland_display || s.x11_display || "headless"})`;
  }

  const runningCount = state.processes.filter((p) => p.status === "running").length;
  document.getElementById("processes-badge").textContent =
    runningCount > 0 ? `${runningCount} running` : `${state.processes.length} recorded`;

  document.getElementById("trash-badge").textContent = `${state.trash.length} archived`;
}

function renderMainView() {
  const views = ["container", "upstream", "doctor", "processes", "trash", "config"];
  for (const v of views) {
    document.getElementById(`view-${v}`).classList.toggle("hidden", state.activeMainView !== v);
  }
  document.getElementById("container-top-actions").classList.toggle("hidden", state.activeMainView !== "container");
  const verBadge = document.getElementById("view-version-badge");
  verBadge.classList.toggle("hidden", state.activeMainView !== "container");

  const badge = document.getElementById("view-badge");

  if (state.activeMainView === "upstream") {
    document.getElementById("view-title").textContent = "Raven Upstream & Container Rebuilds";
    const up = state.upstream;
    badge.className = `badge ${up && !up.update_available ? "ok" : "warn"}`;
    badge.textContent = !up ? "Checking..." : up.update_available ? "Update Available" : "Up to Date";
    renderUpstreamView();
    return;
  }

  if (state.activeMainView === "doctor") {
    document.getElementById("view-title").textContent = "Deployment & Provisioning Doctor";
    const allOk = state.doctor?.summary?.all_ready;
    badge.className = `badge ${allOk ? "ok" : "warn"}`;
    badge.textContent = allOk ? "All D1–D13 Checks Passing" : "Action Needed";
    renderDoctorView();
    return;
  }

  if (state.activeMainView === "processes") {
    document.getElementById("view-title").textContent = "Process & Diagnostic Trace Console";
    badge.className = "badge";
    badge.textContent = `${state.processes.length} sessions`;
    renderProcessesView();
    return;
  }

  if (state.activeMainView === "trash") {
    document.getElementById("view-title").textContent = "Trash & Archived Containers";
    badge.className = "badge";
    badge.textContent = `${state.trash.length} archived`;
    renderTrashView();
    return;
  }

  if (state.activeMainView === "config") {
    document.getElementById("view-title").textContent = "Configuration (Everything as Config)";
    badge.className = "badge ok";
    badge.textContent = "DRY • KISS • SOLID • Idempotent";
    renderGlobalConfigView();
    return;
  }

  const prefix = getSelectedPrefix();
  if (!prefix) {
    document.getElementById("view-title").textContent = "No Active Containers";
    badge.className = "badge warn";
    badge.textContent = "Create or restore a container";
    return;
  }

  document.getElementById("view-title").textContent = prefix.display_name;
  verBadge.className = "badge ok";
  verBadge.textContent = `${prefix.current_version} (#${prefix.registry_hash.slice(0, 8)})`;
  badge.className = `badge ${prefix.initialized ? "ok" : "warn"}`;
  badge.textContent = `${prefix.profile} • ${prefix.desktop.toUpperCase()}`;

  renderContainerView(prefix);
}

function renderContainerView(prefix) {
  const userApps = prefix.apps.filter((a) => a.category !== "system_tool");
  const sysTools = prefix.apps.filter((a) => a.category === "system_tool");
  const installedRedists = prefix.redistributables.filter((r) => r.status === "installed").length;

  document.getElementById("container-summary-cards").innerHTML = `
    <div class="stat-card">
      <span class="stat-label">Container Prefix Path</span>
      <span class="stat-value mono" style="font-size:12.5px;word-break:break-all;">${escapeHtml(prefix.path)}</span>
      <span class="stat-sub">${escapeHtml(prefix.notes || "Isolated C: Drive & Registry")}</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">Active State &amp; Version</span>
      <span class="stat-value">${escapeHtml(prefix.current_version)}</span>
      <span class="stat-sub">Registry SHA-256: <code>${escapeHtml(prefix.registry_hash)}</code> (${prefix.registry_size_kb} KB)</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">System Libraries</span>
      <span class="stat-value">${prefix.sys32_count} x64 / ${prefix.syswow64_count} x86</span>
      <span class="stat-sub">COM Servers: ${prefix.com_registered ? "Registered ✓" : "Pending First Run"} • Redists: ${installedRedists}/${prefix.redistributables.length}</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">Version History</span>
      <span class="stat-value">${prefix.snapshots.length} Snapshots</span>
      <span class="stat-sub">${prefix.snapshots.filter((s) => s.pinned).length} pinned • Non-destructive rollback</span>
    </div>
  `;

  // Applications list
  const appsList = document.getElementById("apps-list");
  appsList.innerHTML = "";
  if (userApps.length === 0) {
    appsList.innerHTML = `
      <div class="empty-state">
        No applications installed in this container yet. Click <strong>"Run EXE / MSI Installer..."</strong> or <strong>"+ Add Executable"</strong> to add one.
      </div>
    `;
  } else {
    for (const app of userApps) {
      appsList.appendChild(createAppRow(prefix, app, false));
    }
  }

  // System tools + sample portable apps
  const sysList = document.getElementById("sys-tools-list");
  sysList.innerHTML = "";
  for (const sample of state.sampleApps) {
    if (sample.exists) {
      sysList.appendChild(
        createAppRow(
          prefix,
          {
            id: "sample-" + sample.name,
            name: sample.name,
            vendor: "Portable Test Suite",
            path: sample.path,
            dos_path: "Z:\\" + sample.path.replace(/\//g, "\\"),
            desktop: prefix.desktop,
            timeout_ms: state.config?.ui?.default_timeout_ms || 86400000,
          },
          true
        )
      );
    }
  }
  for (const tool of sysTools) {
    sysList.appendChild(createAppRow(prefix, tool, true));
  }

  // Inspector dropdown
  const select = document.getElementById("inspector-app-select");
  const prevVal = select.value;
  select.innerHTML = "";
  const allInspectable = [...userApps, ...state.sampleApps.filter((s) => s.exists), ...sysTools];
  for (const item of allInspectable) {
    const opt = document.createElement("option");
    opt.value = item.path;
    opt.textContent = `${item.name} (${item.path.split("/").pop()})`;
    select.appendChild(opt);
  }
  if (prevVal && [...select.options].some((o) => o.value === prevVal)) {
    select.value = prevVal;
  }

  // Versioning & Snapshots timeline
  renderSnapshotsTimeline(prefix);

  // Redistributables tab
  const redistList = document.getElementById("redist-list");
  redistList.innerHTML = "";
  for (const r of prefix.redistributables) {
    const badgeClass = r.status === "installed" ? "ok" : r.status === "partial" ? "warn" : "err";
    const statusText = r.status === "installed" ? "Installed" : r.status === "partial" ? "Partial" : "Not Installed";
    const card = document.createElement("div");
    card.className = "comp-card";
    card.innerHTML = `
      <div class="comp-card-top">
        <div>
          <strong>${escapeHtml(r.name)}</strong>
          <p class="muted">${r.present64.length} of ${r.total_expected} x64 DLLs in system32 (${r.present32.length} x86 in syswow64)</p>
        </div>
        <span class="badge ${badgeClass}">${statusText}</span>
      </div>
      <div class="comp-files">
        ${r.present64.slice(0, 10).map((f) => `<span class="file-chip">${escapeHtml(f)} ✓</span>`).join("")}
        ${r.missing64.slice(0, 6).map((f) => `<span class="file-chip missing">${escapeHtml(f)}</span>`).join("")}
      </div>
    `;
    redistList.appendChild(card);
  }

  // Hydrate Container Settings form once per prefix selection so typing isn't overwritten by poll
  if (state.configFormHydratedForPrefix !== prefix.id) {
    state.configFormHydratedForPrefix = prefix.id;
    document.getElementById("cfg-container-name").value = prefix.display_name;
    document.getElementById("cfg-container-profile").value = prefix.profile;
    document.getElementById("cfg-container-desktop").value = prefix.desktop;
    document.getElementById("cfg-container-notes").value = prefix.notes || "";
    const envLines = Object.entries(prefix.env || {}).map(([k, v]) => `${k}=${v}`);
    document.getElementById("cfg-container-env").value = envLines.join("\n");
  }
}

function renderSnapshotsTimeline(prefix) {
  const snapList = document.getElementById("snapshots-list");
  snapList.innerHTML = "";
  if (prefix.snapshots.length === 0) {
    snapList.innerHTML = `<div class="empty-state">No version snapshots recorded for this container yet. Click <strong>"+ Create Version Snapshot"</strong> to capture <code>v1</code>.</div>`;
    return;
  }

  for (const snap of prefix.snapshots) {
    const row = document.createElement("div");
    row.className = "app-row";
    const liveBadge = snap.matches_live ? `<span class="badge ok">● MATCHES LIVE STATE</span>` : "";
    const pinBadge = snap.pinned ? `<span class="badge warn">📌 Pinned</span>` : "";
    row.innerHTML = `
      <div class="app-row-left">
        <div class="app-avatar ${snap.matches_live ? "" : "sys"}">${escapeHtml(snap.version)}</div>
        <div class="app-info">
          <div class="app-name-line">
            <span class="app-name">${escapeHtml(snap.tag)}</span>
            ${liveBadge}
            ${pinBadge}
            <span class="badge mono">#${escapeHtml(snap.registry_hash)}</span>
          </div>
          <span class="muted">${escapeHtml(snap.notes || "Snapshot checkpoint")} • ${escapeHtml(snap.created_at)} • Registry ${snap.registry_size_kb} KB • ${snap.sys32_count} sys32 files</span>
          <span class="app-path">${escapeHtml(snap.name)}</span>
        </div>
      </div>
      <div class="app-actions">
        <button type="button" class="btn btn-sm btn-secondary btn-pin">${snap.pinned ? "Unpin" : "📌 Pin"}</button>
        <button type="button" class="btn btn-sm btn-secondary btn-fork">⧉ Fork Container</button>
        <button type="button" class="btn btn-sm btn-accent btn-rollback">⏪ Rollback to ${escapeHtml(snap.version)}</button>
        <button type="button" class="btn btn-sm btn-danger btn-del-snap" title="Delete Snapshot">🗑</button>
      </div>
    `;

    row.querySelector(".btn-pin").addEventListener("click", async () => {
      await apiPost("/api/prefix/snapshot", {
        prefix_id: prefix.id,
        action: "update_version",
        snapshot_name: snap.name,
        pinned: !snap.pinned,
      });
      showToast(`${snap.version} ${!snap.pinned ? "pinned" : "unpinned"}`);
      await refreshState();
    });

    row.querySelector(".btn-fork").addEventListener("click", () => {
      openCloneDialog(prefix, snap);
    });

    row.querySelector(".btn-rollback").addEventListener("click", async () => {
      showToast(`Rolling back '${prefix.display_name}' to ${snap.version} (${snap.tag})...`);
      const res = await apiPost("/api/prefix/snapshot", {
        prefix_id: prefix.id,
        action: "restore",
        snapshot_name: snap.name,
      });
      showToast(res.output || `Rolled back to ${snap.version}`);
      await refreshState();
    });

    row.querySelector(".btn-del-snap").addEventListener("click", async () => {
      await apiPost("/api/prefix/snapshot", {
        prefix_id: prefix.id,
        action: "delete_version",
        snapshot_name: snap.name,
      });
      showToast(`Deleted snapshot ${snap.version} (${snap.name})`);
      await refreshState();
    });

    snapList.appendChild(row);
  }
}

function createAppRow(prefix, app, isSystem) {
  const row = document.createElement("div");
  row.className = "app-row";
  const initials = app.name.slice(0, 2).toUpperCase();
  const unpinBtn = app.pinned
    ? `<button type="button" class="btn btn-sm btn-danger btn-unpin" title="Unpin from Container">✕</button>`
    : "";
  row.innerHTML = `
    <div class="app-row-left">
      <div class="app-avatar ${isSystem ? "sys" : ""}">${escapeHtml(initials)}</div>
      <div class="app-info">
        <div class="app-name-line">
          <span class="app-name">${escapeHtml(app.name)}</span>
          <span class="badge">${escapeHtml(app.vendor || "Application")}</span>
        </div>
        <span class="app-path" title="${escapeHtml(app.path)}">${escapeHtml(app.dos_path || app.path)}</span>
      </div>
    </div>
    <div class="app-actions">
      <button type="button" class="btn btn-sm btn-secondary btn-inspect">🔍 Inspect DLLs</button>
      <button type="button" class="btn btn-sm btn-secondary btn-config">⚙ Runner</button>
      <button type="button" class="btn btn-sm btn-accent btn-launch">▶ Run</button>
      ${unpinBtn}
    </div>
  `;

  row.querySelector(".btn-inspect").addEventListener("click", async () => {
    switchTab("tab-inspector");
    const select = document.getElementById("inspector-app-select");
    select.value = app.path;
    await inspectExecutable(prefix.id, app.path);
  });

  row.querySelector(".btn-config").addEventListener("click", () => {
    openRunnerDialog(prefix, app);
  });

  row.querySelector(".btn-launch").addEventListener("click", async () => {
    showToast(`Launching ${app.name} in container '${prefix.display_name}'...`);
    const res = await apiPost("/api/run", {
      prefix_id: prefix.id,
      path: app.path,
      args: app.args || "",
      desktop: app.desktop || prefix.desktop,
      timeout_ms: app.timeout_ms || state.config?.ui?.default_timeout_ms || 86400000,
      elevated: app.elevated || false,
      auto_deploy: true,
    });
    if (res.ok) {
      state.activeMainView = "processes";
      await refreshState();
    } else {
      showToast(`Error: ${res.error}`);
    }
  });

  const unpinEl = row.querySelector(".btn-unpin");
  if (unpinEl) {
    unpinEl.addEventListener("click", async () => {
      await apiPost("/api/prefix/remove-app", { prefix_id: prefix.id, app_id: app.id });
      showToast(`Removed pinned runner '${app.name}'`);
      await refreshState();
    });
  }

  return row;
}

async function inspectExecutable(prefixId, exePath) {
  const container = document.getElementById("inspector-results");
  container.innerHTML = `<div class="empty-state">Running <code>raven --inspect</code> and scanning transitive PE dependencies...</div>`;
  const data = await apiGet(`/api/inspect?prefix_id=${encodeURIComponent(prefixId)}&path=${encodeURIComponent(exePath)}`);
  state.inspectedData = data;
  renderInspectorResults(data);
}

function renderInspectorResults(data) {
  const container = document.getElementById("inspector-results");
  if (!data || data.error) {
    container.innerHTML = `<div class="diag-alert">Inspection Error: ${escapeHtml(data?.error || "Unknown error")}</div>`;
    return;
  }

  const s = data.summary;
  const readyBadge = s.ready_to_run
    ? `<span class="badge ok">✓ All Required Libraries Deployed &amp; Ready</span>`
    : `<span class="badge warn">⚠ ${s.runtime_components_missing + s.redist_missing + s.unresolved_count} Missing Libraries Detected</span>`;

  let msiBanner = "";
  if (data.is_msi && data.msi_readiness) {
    msiBanner = `
      <div class="cmake-banner" style="margin-bottom:12px;">
        <strong>📦 Automatic MSI Wrapper Active (Fixes D1 &amp; D9)</strong>
        <span class="muted">Detected OLE Compound Document (.msi). Raven Nest automatically routes execution through <code>msiexec.exe /i</code> with 32-bit SysWOW64 custom action support:</span>
        <div class="cmd-box">${escapeHtml(data.msi_readiness.auto_wrapped_command)}</div>
      </div>
    `;
  }

  const rowsHtml = (data.dependencies || [])
    .map((dep) => {
      const stClass = dep.status === "ready" ? "ok" : dep.status === "missing_deployable" ? "warn" : "err";
      const stLabel =
        dep.status === "ready"
          ? "Ready ✓"
          : dep.status === "missing_deployable"
          ? "Auto-Deployable ⚡"
          : "Missing ✕";
      return `
        <tr>
          <td class="mono"><strong>${escapeHtml(dep.dll)}</strong></td>
          <td><span class="badge">${escapeHtml(dep.category.replace("_", " "))}</span></td>
          <td><span class="badge ${stClass}">${stLabel}</span></td>
          <td>${dep.symbol_count || "—"}</td>
          <td class="muted">${escapeHtml(dep.provider)}</td>
        </tr>
      `;
    })
    .join("");

  container.innerHTML = `
    ${msiBanner}
    <div class="inspector-summary-bar">
      <span class="badge">${escapeHtml(data.arch)}</span>
      ${readyBadge}
      <span class="badge">Core Win32: ${s.core_count}</span>
      <span class="badge ${s.runtime_components_missing ? "warn" : "ok"}">Raven Components: ${s.runtime_components_ready} ready / ${s.runtime_components_missing} missing</span>
      <span class="badge ${s.redist_missing ? "warn" : "ok"}">Redistributables: ${s.redist_ready} ready / ${s.redist_missing} missing</span>
      <span class="badge">App-Local DLLs: ${s.app_local_count}</span>
    </div>
    <div style="overflow-x:auto; border:1px solid var(--border-subtle); border-radius:var(--radius-md);">
      <table class="dep-table">
        <thead>
          <tr>
            <th>Imported Module</th>
            <th>Layer</th>
            <th>Status</th>
            <th>Imports</th>
            <th>Resolution &amp; Deployment Provider</th>
          </tr>
        </thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Raven Upstream: GitHub commit tracking, clone/pull + build, container rebuilds
// ---------------------------------------------------------------------------

const UPSTREAM_DISMISS_KEY = "raven-upstream-dismissed";

function upstreamDismissKey(up) {
  return `${up.target_sha}:${up.outdated_containers_count}:${up.needs_runtime_build}`;
}

async function refreshUpstream(force = false) {
  try {
    state.upstream = await apiGet(`/api/upstream/status${force ? "?force=1" : ""}`);
  } catch (err) {
    return;
  }
  renderUpstreamBadge();
  renderUpstreamBanner();
  if (state.activeMainView === "upstream") renderMainView();
}

async function startUpstreamJob(path, body, message) {
  showToast(message);
  const res = await apiPost(path, body);
  if (!res.ok) {
    showToast(res.error || "Failed to start upstream job");
    return;
  }
  state.activeMainView = "processes";
  await refreshState();
}

function startUpstreamPipeline(rebuildTarget) {
  const cloned = state.upstream?.local?.cloned;
  const what = rebuildTarget === "none" ? "" : rebuildTarget === "all" ? " and rebuilding all containers" : ` and rebuilding '${rebuildTarget}'`;
  return startUpstreamJob(
    "/api/upstream/sync",
    { sync_mode: cloned ? "pull" : "clone", rebuild_target: rebuildTarget },
    `${cloned ? "Pulling" : "Cloning"} Raven from GitHub, building${what}...`,
  );
}

function rebuildContainers(target) {
  return startUpstreamJob(
    "/api/upstream/rebuild",
    { target },
    target === "all" ? "Rebuilding all containers with the current Raven build..." : `Rebuilding '${target}'...`,
  );
}

function renderUpstreamBadge() {
  const up = state.upstream;
  const el = document.getElementById("upstream-badge");
  if (!up) return;
  if (!up.local.cloned) el.textContent = "Not cloned";
  else if (up.has_remote_updates) el.textContent = `${up.remote.commits_behind} new commit${up.remote.commits_behind === 1 ? "" : "s"}`;
  else if (up.outdated_containers_count > 0) el.textContent = `${up.outdated_containers_count} container${up.outdated_containers_count === 1 ? "" : "s"} outdated`;
  else el.textContent = `#${up.build.short_sha || up.local.short_sha} • up to date`;
}

function renderUpstreamBanner() {
  const up = state.upstream;
  const banner = document.getElementById("upstream-update-banner");
  let dismissed = null;
  try {
    dismissed = localStorage.getItem(UPSTREAM_DISMISS_KEY);
  } catch (err) {
    dismissed = null;
  }
  const show = up && up.update_available && dismissed !== upstreamDismissKey(up);
  banner.classList.toggle("hidden", !show);
  if (!show) return;

  // A fresh pull/build is only needed when GitHub is ahead or the checkout is unbuilt;
  // otherwise only the containers lag behind the existing build.
  const needsBuild = !up.local.cloned || up.has_remote_updates || up.needs_runtime_build;
  const n = up.outdated_containers_count;
  banner.innerHTML = `
    <div class="update-banner-text">
      <strong>⟳ Raven update available</strong>
      <span class="muted">${escapeHtml(up.headline)}</span>
    </div>
    <div class="update-banner-actions">
      <button type="button" class="btn btn-sm btn-secondary" data-act="review">Review</button>
      ${needsBuild ? `<button type="button" class="btn btn-sm btn-secondary" data-act="build">${up.local.cloned ? "Pull" : "Clone"} &amp; Build</button>` : ""}
      ${n > 0 || needsBuild ? `<button type="button" class="btn btn-sm btn-accent" data-act="all">${needsBuild ? "Build &amp; " : ""}Rebuild All (${up.total_containers_count})</button>` : ""}
      <button type="button" class="btn btn-sm btn-secondary" data-act="dismiss" title="Hide until something new arrives">✕</button>
    </div>
  `;
  banner.querySelector('[data-act="review"]').addEventListener("click", () => {
    state.activeMainView = "upstream";
    renderSidebar();
    renderMainView();
  });
  banner.querySelector('[data-act="build"]')?.addEventListener("click", () => startUpstreamPipeline("none"));
  banner.querySelector('[data-act="all"]')?.addEventListener("click", () =>
    needsBuild ? startUpstreamPipeline("all") : rebuildContainers("all"),
  );
  banner.querySelector('[data-act="dismiss"]').addEventListener("click", () => {
    try {
      localStorage.setItem(UPSTREAM_DISMISS_KEY, upstreamDismissKey(up));
    } catch (err) {
      /* per-viewer convenience only */
    }
    banner.classList.add("hidden");
  });
}

function renderUpstreamView() {
  const up = state.upstream;
  if (!up) return;
  const { local, remote, build } = up;

  document.getElementById("upstream-remote-label").innerHTML =
    `Tracking <code>${escapeHtml(remote.remote_url)}</code> on branch <code>${escapeHtml(remote.branch)}</code>` +
    ` • last checked ${escapeHtml(remote.checked_at)}${remote.error ? ` • <span class="badge err">${escapeHtml(remote.error)}</span>` : ""}`;

  const cloneLabel = document.getElementById("btn-upstream-build-only");
  cloneLabel.textContent = local.cloned ? "Pull & Build Only" : "Clone & Build Only";

  document.getElementById("upstream-summary-cards").innerHTML = `
    <div class="stat-card">
      <span class="stat-label">Local Checkout</span>
      <span class="stat-value">${local.cloned ? `#${escapeHtml(local.short_sha)}` : "Not cloned"}</span>
      <span class="stat-sub mono" style="word-break:break-all;">${escapeHtml(local.repo_root)}${local.branch ? ` (${escapeHtml(local.branch)})` : ""}</span>
      <span class="stat-sub">${local.dirty_count ? `${local.dirty_count} uncommitted change(s) — stashed before pulling` : escapeHtml(local.subject)}</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">GitHub (${escapeHtml(remote.branch)})</span>
      <span class="stat-value">${remote.remote_short_sha ? `#${escapeHtml(remote.remote_short_sha)}` : "Unknown"}</span>
      <span class="stat-sub">${remote.commits_behind > 0 && local.cloned ? `${remote.commits_behind} commit(s) ahead of local` : remote.fetch_ok ? "Local checkout is current" : "Could not reach GitHub"}</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">Built Runtime</span>
      <span class="stat-value">${build.binary_exists ? `#${escapeHtml(build.short_sha || "?")}` : "Not built"}</span>
      <span class="stat-sub">${build.binary_exists ? `Built ${escapeHtml(build.built_at)} • SHA-256 <code>${escapeHtml(build.binary_sha256)}</code>` : escapeHtml(build.binary_path)}</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">Containers</span>
      <span class="stat-value">${up.total_containers_count - up.outdated_containers_count}/${up.total_containers_count} current</span>
      <span class="stat-sub">${up.outdated_containers_count} can be rebuilt</span>
    </div>
  `;

  const incoming = remote.incoming_commits || [];
  document.getElementById("upstream-incoming").innerHTML = `
    <strong>${escapeHtml(up.headline)}</strong>
    ${incoming.length
      ? `<ul class="commit-list">${incoming
          .map((c) => `<li><code>${escapeHtml(c.sha)}</code>${escapeHtml(c.subject)} <span class="muted">— ${escapeHtml(c.author)}, ${escapeHtml(c.rel_time)}</span></li>`)
          .join("")}</ul>`
      : `<p class="muted">No incoming commits.</p>`}
  `;

  const tbody = document.getElementById("upstream-containers-body");
  tbody.innerHTML = "";
  for (const c of up.containers) {
    const stClass = c.status === "up_to_date" ? "ok" : "warn";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${escapeHtml(c.display_name)}</strong><div class="muted">${escapeHtml(c.current_snapshot_version)}</div></td>
      <td><code>${escapeHtml(c.container_raven_short)}</code><div class="muted">${escapeHtml(c.container_raven_updated_at || "")}</div></td>
      <td><span class="badge ${stClass}">${escapeHtml(c.status.replace(/_/g, " "))}</span><div class="muted">${escapeHtml(c.reason)}</div></td>
      <td style="text-align:right;"><button type="button" class="btn btn-sm ${c.matches_latest ? "btn-secondary" : "btn-accent"}">Rebuild</button></td>
    `;
    tr.querySelector("button").addEventListener("click", () =>
      up.has_remote_updates || up.needs_runtime_build ? startUpstreamPipeline(c.id) : rebuildContainers(c.id),
    );
    tbody.appendChild(tr);
  }
}

function renderDoctorView() {
  if (!state.doctor) return;
  const doc = state.doctor;
  const banner = document.getElementById("doctor-cmake-banner");
  const cmakeOk = doc.cmake.source_dir_matches && doc.cmake.font_hashes_match;

  banner.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
      <div>
        <strong>CMake Build &amp; Liberation Font SHA-256 Pinning (D4 / D8)</strong>
        <p class="muted">Source Path Match: <strong>${doc.cmake.source_dir_matches ? "Yes ✓" : "MISMATCH ✕"}</strong> • Font Hashes Match Host: <strong>${doc.cmake.font_hashes_match ? "Yes ✓" : "MISMATCH (Auto-computed below)"}</strong> • Host MinGW/Build Tools: <strong>${doc.toolchain.ok ? "All Installed ✓" : "Missing: " + doc.toolchain.missing.join(", ")}</strong></p>
      </div>
      <span class="badge ${cmakeOk ? "ok" : "warn"}">${cmakeOk ? "CMake Cache Healthy" : "Cache Reconfigure Recommended"}</span>
    </div>
    <div class="cmd-box">${escapeHtml(doc.cmake.configure_command)}</div>
  `;

  const grid = document.getElementById("doctor-components-grid");
  grid.innerHTML = "";
  for (const comp of doc.components) {
    const stClass = comp.status === "ready" ? "ok" : comp.status === "partial" ? "warn" : "err";
    const card = document.createElement("div");
    card.className = "comp-card";
    card.innerHTML = `
      <div class="comp-card-top">
        <div>
          <span class="badge" style="margin-bottom:4px;">Issue ${escapeHtml(comp.code)}</span>
          <h4>${escapeHtml(comp.title)}</h4>
          <p class="muted">${escapeHtml(comp.description)}</p>
        </div>
        <span class="badge ${stClass}">${comp.present_count}/${comp.total_count} Ready</span>
      </div>
      <div class="comp-files">
        ${comp.present.map((f) => `<span class="file-chip">${escapeHtml(f.split("/").pop())} ✓</span>`).join("")}
        ${comp.missing.map((f) => `<span class="file-chip missing">${escapeHtml(f.split("/").pop())} ✕</span>`).join("")}
      </div>
      <div style="margin-top:auto; padding-top:6px; display:flex; justify-content:flex-end;">
        <button type="button" class="btn btn-sm btn-secondary btn-repair">⚙ Provision / Verify</button>
      </div>
    `;
    card.querySelector(".btn-repair").addEventListener("click", async () => {
      showToast(`Starting provisioning task: ${comp.repair_action}...`);
      const res = await apiPost("/api/doctor/repair", { action: comp.repair_action });
      if (res.ok) {
        state.activeMainView = "processes";
        await refreshState();
      }
    });
    grid.appendChild(card);
  }
}

function renderProcessesView() {
  const list = document.getElementById("processes-list");
  list.innerHTML = "";
  if (state.processes.length === 0) {
    list.innerHTML = `<div class="empty-state">No processes launched yet in this session.</div>`;
    return;
  }

  for (const proc of state.processes) {
    const stClass = proc.status === "running" ? "ok" : proc.status === "exited" ? "ok" : "err";
    const card = document.createElement("div");
    card.className = "proc-card";
    const diagHtml = (proc.diagnostics || [])
      .map(
        (d) => `
      <div class="diag-alert">
        <strong>⚕ ${escapeHtml(d.title)}</strong><br>
        ${escapeHtml(d.advice)}
      </div>
    `
      )
      .join("");

    card.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap;">
        <div>
          <strong>${escapeHtml(proc.name)}</strong>
          <span class="muted"> • PID ${proc.pid} • Scope: <code>${escapeHtml(proc.prefix_id)}</code> • Started ${escapeHtml(proc.started_at)}</span>
        </div>
        <div style="display:flex; align-items:center; gap:8px;">
          <span class="badge ${stClass}">${escapeHtml(proc.status.toUpperCase())}${proc.exit_code !== null ? ` (${proc.exit_code})` : ""}</span>
          ${proc.status === "running" ? `<button type="button" class="btn btn-sm btn-danger btn-kill">Stop</button>` : ""}
        </div>
      </div>
      ${diagHtml}
      <div class="proc-log">${escapeHtml((proc.logs || []).join("\n"))}</div>
    `;
    const killBtn = card.querySelector(".btn-kill");
    if (killBtn) {
      killBtn.addEventListener("click", async () => {
        await apiPost("/api/process/kill", { process_id: proc.id });
        await refreshState();
      });
    }
    list.appendChild(card);
  }
}

function renderTrashView() {
  const list = document.getElementById("trash-list");
  list.innerHTML = "";
  if (state.trash.length === 0) {
    list.innerHTML = `<div class="empty-state">Trash is empty. Soft-deleted containers appear here for safe recovery.</div>`;
    return;
  }

  for (const item of state.trash) {
    const row = document.createElement("div");
    row.className = "app-row";
    row.innerHTML = `
      <div class="app-row-left">
        <div class="app-avatar sys">🗑</div>
        <div class="app-info">
          <div class="app-name-line">
            <span class="app-name">${escapeHtml(item.display_name)}</span>
            <span class="badge">${escapeHtml(item.profile)}</span>
          </div>
          <span class="muted">Archived ${escapeHtml(item.deleted_at)} • ID: <code>${escapeHtml(item.trash_id)}</code></span>
        </div>
      </div>
      <div class="app-actions">
        <button type="button" class="btn btn-sm btn-accent btn-restore-trash">↩ Restore Container</button>
        <button type="button" class="btn btn-sm btn-danger btn-purge-trash">Purge Permanently</button>
      </div>
    `;

    row.querySelector(".btn-restore-trash").addEventListener("click", async () => {
      const res = await apiPost("/api/trash/restore", { trash_id: item.trash_id });
      if (res.ok && res.prefix) {
        state.selectedPrefixId = res.prefix.id;
        state.activeMainView = "container";
        showToast(`Restored container '${res.prefix.display_name}'!`);
        await refreshState();
      }
    });

    row.querySelector(".btn-purge-trash").addEventListener("click", async () => {
      await apiPost("/api/trash/purge", { trash_id: item.trash_id });
      showToast(`Permanently purged '${item.display_name}'`);
      await refreshState();
    });

    list.appendChild(row);
  }
}

function renderGlobalConfigView() {
  if (!state.config) return;
  const cfg = state.config;
  const activeEl = document.activeElement;
  if (activeEl && activeEl.id === "global-cfg-json") return;

  document.getElementById("global-cfg-theme").value = cfg.ui?.default_theme || "dark";
  document.getElementById("global-cfg-max-snaps").value = cfg.snapshots?.max_retained_auto ?? 10;
  document.getElementById("global-cfg-timeout").value = cfg.ui?.default_timeout_ms ?? 86400000;
  document.getElementById("global-cfg-inspect-limit").value = cfg.server?.inspect_transitive_limit ?? 12;
  document.getElementById("global-cfg-json").value = JSON.stringify(cfg.user_overrides || {}, null, 2);
  document.getElementById("global-cfg-paths").textContent = JSON.stringify(cfg.paths || {}, null, 2);
}

function switchTab(tabId) {
  state.activeTab = tabId;
  document.querySelectorAll(".tab-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === tabId);
  });
  document.querySelectorAll(".tab-panel").forEach((p) => {
    p.classList.toggle("active", p.id === tabId);
  });
}

function openCloneDialog(prefix, snap = null) {
  const dlg = document.getElementById("dialog-clone-container");
  document.getElementById("clone-source-snapshot").value = snap ? snap.name : "";
  document.getElementById("clone-source-desc").textContent = snap
    ? `Forking a new sandbox container from '${prefix.display_name}' @ ${snap.version} (${snap.tag})`
    : `Cloning from live container '${prefix.display_name}' (${prefix.current_version})`;
  document.getElementById("clone-new-name").value = snap
    ? `${prefix.id}-${snap.version}`
    : `${prefix.id}-copy`;
  dlg.showModal();
}

function openRunnerDialog(prefix, app = null) {
  const dlg = document.getElementById("dialog-runner-config");
  document.getElementById("runner-app-name").value = app ? app.name : "";
  document.getElementById("runner-path-display").value = app ? app.path : "";
  document.getElementById("runner-args").value = app ? app.args || "" : "";
  document.getElementById("runner-desktop").value = app ? app.desktop || prefix.desktop : prefix.desktop;
  document.getElementById("runner-timeout").value =
    app ? app.timeout_ms || state.config?.ui?.default_timeout_ms || 86400000 : state.config?.ui?.default_timeout_ms || 86400000;
  document.getElementById("runner-elevated").checked = Boolean(app?.elevated);
  dlg.showModal();
}

async function openFilePicker(initialDir, onSelect) {
  state.pickerCallback = onSelect;
  const dlg = document.getElementById("dialog-file-picker");
  dlg.showModal();
  await loadPickerDirectory(initialDir || state.pickerCurrentDir);
}

async function loadPickerDirectory(dirPath) {
  const data = await apiGet(`/api/fs?dir=${encodeURIComponent(dirPath)}`);
  state.pickerCurrentDir = data.current;
  document.getElementById("picker-current-path").textContent = data.current;
  const container = document.getElementById("picker-entries");
  container.innerHTML = "";

  if (data.entries.length === 0) {
    container.innerHTML = `<div class="empty-state">No subdirectories or .exe/.msi files in this folder.</div>`;
    return;
  }

  for (const entry of data.entries) {
    const item = document.createElement("div");
    item.className = "picker-item";
    const icon = entry.type === "dir" ? "📁" : entry.name.toLowerCase().endsWith(".msi") ? "📦" : "⚙️";
    item.innerHTML = `
      <span>${icon} ${escapeHtml(entry.name)}</span>
      <span class="muted">${entry.type === "dir" ? "Directory" : `${entry.size_mb} MB`}</span>
    `;
    item.addEventListener("click", () => {
      if (entry.type === "dir") {
        loadPickerDirectory(entry.path);
      } else {
        document.getElementById("dialog-file-picker").close();
        if (state.pickerCallback) state.pickerCallback(entry.path);
      }
    });
    container.appendChild(item);
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  // Generic dialog close handler (DRY)
  document.querySelectorAll("[data-close-dialog]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.getElementById(btn.dataset.closeDialog)?.close();
    });
  });

  // Theme switcher buttons
  document.querySelectorAll(".theme-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      applyTheme(btn.dataset.themeChoice, true);
    });
  });

  // Tab switching
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      switchTab(btn.dataset.tab);
      if (btn.dataset.tab === "tab-inspector" && !state.inspectedData) {
        const prefix = getSelectedPrefix();
        const select = document.getElementById("inspector-app-select");
        if (prefix && select.value) {
          await inspectExecutable(prefix.id, select.value);
        }
      }
    });
  });

  // Sidebar navigation
  for (const [id, view] of [
    ["nav-upstream", "upstream"],
    ["nav-doctor", "doctor"],
    ["nav-processes", "processes"],
    ["nav-trash", "trash"],
    ["nav-config", "config"],
  ]) {
    document.getElementById(id).addEventListener("click", () => {
      state.activeMainView = view;
      renderSidebar();
      renderMainView();
    });
  }

  // Create New Container
  const dlgNew = document.getElementById("dialog-new-container");
  document.getElementById("btn-new-container").addEventListener("click", () => dlgNew.showModal());

  document.getElementById("form-new-container").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = document.getElementById("new-container-name").value.trim();
    const blueprint_id = document.getElementById("new-container-blueprint").value;
    const desktop = document.getElementById("new-container-desktop").value;
    const notes = document.getElementById("new-container-notes").value.trim();
    const add_notepad = document.getElementById("new-container-npp").checked;
    if (!name) return;

    dlgNew.close();
    showToast(`Creating and provisioning sandbox '${name}'...`);
    const res = await apiPost("/api/prefix/create", { name, blueprint_id, desktop, notes, add_notepad });
    if (res.ok && res.prefix) {
      state.selectedPrefixId = res.prefix.id;
      state.activeMainView = "container";
      state.configFormHydratedForPrefix = null;
      showToast(`Container '${res.prefix.display_name}' ready (${res.prefix.current_version})!`);
      await refreshState();
    }
  });

  // Clone Container
  document.getElementById("btn-clone-container").addEventListener("click", () => {
    const prefix = getSelectedPrefix();
    if (prefix) openCloneDialog(prefix, null);
  });

  document.getElementById("form-clone-container").addEventListener("submit", async (e) => {
    e.preventDefault();
    const prefix = getSelectedPrefix();
    if (!prefix) return;
    const newName = document.getElementById("clone-new-name").value.trim();
    const snapName = document.getElementById("clone-source-snapshot").value || null;
    document.getElementById("dialog-clone-container").close();
    showToast(`Cloning '${prefix.display_name}' -> '${newName}'...`);
    const res = await apiPost("/api/prefix/clone", {
      source_id: prefix.id,
      new_name: newName,
      snapshot_name: snapName,
    });
    if (res.ok && res.prefix) {
      state.selectedPrefixId = res.prefix.id;
      state.activeMainView = "container";
      state.configFormHydratedForPrefix = null;
      showToast(`Cloned container '${res.prefix.display_name}'!`);
      await refreshState();
    } else {
      showToast(res.error || "Clone failed");
    }
  });

  // Delete / Archive Container
  const dlgDelete = document.getElementById("dialog-delete-container");
  document.getElementById("btn-delete-container").addEventListener("click", () => {
    const prefix = getSelectedPrefix();
    if (!prefix) return;
    document.getElementById("delete-container-prompt").textContent =
      `Remove container '${prefix.display_name}' (${prefix.path})?`;
    document.getElementById("delete-container-mode").value = "trash";
    document.getElementById("delete-container-snapshots").checked = false;
    dlgDelete.showModal();
  });

  document.getElementById("form-delete-container").addEventListener("submit", async (e) => {
    e.preventDefault();
    const prefix = getSelectedPrefix();
    if (!prefix) return;
    const mode = document.getElementById("delete-container-mode").value;
    const delete_snapshots = document.getElementById("delete-container-snapshots").checked;
    dlgDelete.close();
    const res = await apiPost("/api/prefix/delete", {
      prefix_id: prefix.id,
      mode,
      delete_snapshots,
    });
    if (res.ok) {
      state.selectedPrefixId = null;
      state.configFormHydratedForPrefix = null;
      showToast(res.message || "Container removed");
      await refreshState();
    }
  });

  // Container Settings & Env save
  document.getElementById("form-container-settings").addEventListener("submit", async (e) => {
    e.preventDefault();
    const prefix = getSelectedPrefix();
    if (!prefix) return;
    const envObj = {};
    for (const line of document.getElementById("cfg-container-env").value.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const idx = trimmed.indexOf("=");
      if (idx > 0) {
        envObj[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1).trim();
      }
    }
    const res = await apiPost("/api/prefix/update", {
      prefix_id: prefix.id,
      display_name: document.getElementById("cfg-container-name").value.trim(),
      profile: document.getElementById("cfg-container-profile").value.trim(),
      desktop: document.getElementById("cfg-container-desktop").value,
      notes: document.getElementById("cfg-container-notes").value.trim(),
      env: envObj,
    });
    if (res.ok) {
      state.configFormHydratedForPrefix = null;
      showToast("Saved container configuration & environment variables!");
      await refreshState();
    }
  });

  // Versioned Snapshot Modal
  const dlgSnap = document.getElementById("dialog-new-snapshot");
  document.getElementById("btn-open-snapshot-modal").addEventListener("click", () => {
    document.getElementById("snap-tag").value = "checkpoint";
    document.getElementById("snap-notes").value = "";
    document.getElementById("snap-pinned").checked = true;
    dlgSnap.showModal();
  });

  document.getElementById("form-new-snapshot").addEventListener("submit", async (e) => {
    e.preventDefault();
    const prefix = getSelectedPrefix();
    if (!prefix) return;
    dlgSnap.close();
    const res = await apiPost("/api/prefix/snapshot", {
      prefix_id: prefix.id,
      action: "take",
      label: document.getElementById("snap-tag").value.trim(),
      notes: document.getElementById("snap-notes").value.trim(),
      pinned: document.getElementById("snap-pinned").checked,
    });
    showToast(`Captured version snapshot: ${res.output}`);
    await refreshState();
  });

  document.getElementById("btn-prune-snapshots").addEventListener("click", async () => {
    const prefix = getSelectedPrefix();
    if (!prefix) return;
    const res = await apiPost("/api/prefix/snapshot", {
      prefix_id: prefix.id,
      action: "prune",
    });
    const count = res.removed?.length || 0;
    showToast(count > 0 ? `Pruned ${count} old unpinned snapshots` : "No unpinned snapshots exceeded retention limit");
    await refreshState();
  });

  document.getElementById("btn-reboot-prefix").addEventListener("click", async () => {
    const prefix = getSelectedPrefix();
    if (!prefix) return;
    showToast("Simulating Windows reboot (--reboot)...");
    const res = await apiPost("/api/prefix/snapshot", { prefix_id: prefix.id, action: "reboot" });
    showToast(res.output || "Reboot complete");
    await refreshState();
  });

  document.getElementById("btn-restore-latest").addEventListener("click", async () => {
    const prefix = getSelectedPrefix();
    if (!prefix) return;
    showToast("Rolling back to latest snapshot...");
    const res = await apiPost("/api/prefix/snapshot", { prefix_id: prefix.id, action: "restore" });
    showToast(res.output || "Rollback complete");
    await refreshState();
  });

  // Auto-Deploy buttons
  const runAutoDeploy = async (targetPath = null) => {
    const prefix = getSelectedPrefix();
    if (!prefix) return;
    showToast(`Running idempotent library deployment for '${prefix.display_name}'...`);
    const res = await apiPost("/api/prefix/deploy", { prefix_id: prefix.id, path: targetPath });
    if (res.ok) {
      showToast((res.actions && res.actions[0]) || "Deployment complete!");
      if (res.analysis) {
        state.inspectedData = res.analysis;
        renderInspectorResults(res.analysis);
      }
      await refreshState();
    }
  };

  document.getElementById("btn-auto-deploy-container").addEventListener("click", () => runAutoDeploy(null));
  document.getElementById("btn-sync-redist").addEventListener("click", () => runAutoDeploy(null));
  document.getElementById("btn-deploy-missing-inspector").addEventListener("click", () => {
    const targetPath = state.inspectedData?.file || document.getElementById("inspector-app-select").value;
    runAutoDeploy(targetPath);
  });

  // Inspector controls
  document.getElementById("inspector-app-select").addEventListener("change", async (e) => {
    const prefix = getSelectedPrefix();
    if (prefix && e.target.value) {
      await inspectExecutable(prefix.id, e.target.value);
    }
  });

  document.getElementById("btn-inspect-pick").addEventListener("click", () => {
    const prefix = getSelectedPrefix();
    openFilePicker(prefix ? `${prefix.path}/drive_c` : state.pickerCurrentDir, async (pickedPath) => {
      if (prefix) await inspectExecutable(prefix.id, pickedPath);
    });
  });

  // Run Installer / Add App
  document.getElementById("btn-browse-installer").addEventListener("click", () => {
    const prefix = getSelectedPrefix();
    if (!prefix) return;
    openFilePicker(state.pickerCurrentDir, (pickedPath) => {
      openRunnerDialog(prefix, {
        name: pickedPath.split("/").pop(),
        path: pickedPath,
        args: "",
        desktop: prefix.desktop,
        timeout_ms: state.config?.ui?.installer_timeout_ms || 1800000,
        elevated: true,
      });
    });
  });

  document.getElementById("btn-add-app").addEventListener("click", () => {
    const prefix = getSelectedPrefix();
    if (prefix) openRunnerDialog(prefix, null);
  });

  // Runner modal controls
  const dlgRunner = document.getElementById("dialog-runner-config");
  document.getElementById("btn-runner-pick-file").addEventListener("click", () => {
    const prefix = getSelectedPrefix();
    openFilePicker(prefix ? `${prefix.path}/drive_c` : state.pickerCurrentDir, (pickedPath) => {
      document.getElementById("runner-path-display").value = pickedPath;
      if (!document.getElementById("runner-app-name").value) {
        document.getElementById("runner-app-name").value = pickedPath.split("/").pop().replace(/\.(exe|msi)$/i, "");
      }
    });
  });

  document.getElementById("btn-save-runner").addEventListener("click", async () => {
    const prefix = getSelectedPrefix();
    if (!prefix) return;
    const res = await apiPost("/api/prefix/add-app", {
      prefix_id: prefix.id,
      name: document.getElementById("runner-app-name").value.trim(),
      path: document.getElementById("runner-path-display").value.trim(),
      args: document.getElementById("runner-args").value.trim(),
      desktop: document.getElementById("runner-desktop").value,
      timeout_ms: Number(document.getElementById("runner-timeout").value || 86400000),
      elevated: document.getElementById("runner-elevated").checked,
    });
    if (res.ok) {
      dlgRunner.close();
      showToast("Application runner pinned to container!");
      await refreshState();
    } else {
      showToast(res.error || "Failed to save runner");
    }
  });

  document.getElementById("btn-export-desktop").addEventListener("click", async () => {
    const prefix = getSelectedPrefix();
    if (!prefix) return;
    const res = await apiPost("/api/runner/export", {
      prefix_id: prefix.id,
      name: document.getElementById("runner-app-name").value.trim() || "App",
      path: document.getElementById("runner-path-display").value.trim(),
      desktop: document.getElementById("runner-desktop").value,
      args: document.getElementById("runner-args").value.trim(),
    });
    if (res.ok) showToast(`Created runner: ${res.runner_script}`);
  });

  document.getElementById("form-runner-config").addEventListener("submit", async (e) => {
    e.preventDefault();
    const prefix = getSelectedPrefix();
    if (!prefix) return;
    dlgRunner.close();
    const res = await apiPost("/api/run", {
      prefix_id: prefix.id,
      path: document.getElementById("runner-path-display").value.trim(),
      args: document.getElementById("runner-args").value.trim(),
      desktop: document.getElementById("runner-desktop").value,
      timeout_ms: Number(document.getElementById("runner-timeout").value || 86400000),
      elevated: document.getElementById("runner-elevated").checked,
      auto_deploy: document.getElementById("runner-autodeploy").checked,
    });
    if (res.ok) {
      state.activeMainView = "processes";
      await refreshState();
    } else {
      showToast(res.error || "Failed to launch");
    }
  });

  // File Picker navigation
  document.getElementById("btn-picker-up").addEventListener("click", () => {
    const parent = state.pickerCurrentDir.split("/").slice(0, -1).join("/") || "/";
    loadPickerDirectory(parent);
  });
  document.getElementById("btn-picker-home").addEventListener("click", () => {
    loadPickerDirectory(state.config?.paths?.prefixes_root?.split("/.local")[0] || "/home");
  });
  document.getElementById("btn-picker-prefix").addEventListener("click", () => {
    const prefix = getSelectedPrefix();
    if (prefix) loadPickerDirectory(`${prefix.path}/drive_c`);
  });
  document.getElementById("btn-picker-testdata").addEventListener("click", () => {
    loadPickerDirectory(state.config?.paths?.testdata_dir || "/");
  });

  // Doctor global buttons
  document.getElementById("btn-doctor-reconfigure").addEventListener("click", async () => {
    showToast("Reconfiguring CMake with all D1–D13 component paths & host font hashes...");
    const res = await apiPost("/api/doctor/repair", { action: "reconfigure_cmake" });
    if (res.ok) {
      state.activeMainView = "processes";
      await refreshState();
    }
  });

  document.getElementById("btn-doctor-build").addEventListener("click", async () => {
    showToast("Building all Raven runtime & component targets...");
    const res = await apiPost("/api/doctor/repair", { action: "build_runtime" });
    if (res.ok) {
      state.activeMainView = "processes";
      await refreshState();
    }
  });

  // Global Config ("Everything as Config") save & reset
  document.getElementById("btn-save-global-config").addEventListener("click", async () => {
    let customJson = {};
    try {
      customJson = JSON.parse(document.getElementById("global-cfg-json").value || "{}");
    } catch (err) {
      showToast("Invalid JSON in User Overrides editor");
      return;
    }
    const themeVal = document.getElementById("global-cfg-theme").value;
    const patch = {
      ...customJson,
      ui: {
        ...(customJson.ui || {}),
        default_theme: themeVal,
        default_timeout_ms: Number(document.getElementById("global-cfg-timeout").value || 86400000),
      },
      snapshots: {
        ...(customJson.snapshots || {}),
        max_retained_auto: Number(document.getElementById("global-cfg-max-snaps").value || 10),
      },
      server: {
        ...(customJson.server || {}),
        inspect_transitive_limit: Number(document.getElementById("global-cfg-inspect-limit").value || 12),
      },
    };
    const res = await apiPost("/api/config/update", { overrides: patch });
    if (res.ok) {
      applyTheme(themeVal, false);
      showToast("Saved user configuration overrides to ~/.config/raven-nest/config.json");
      await refreshState();
    }
  });

  document.getElementById("btn-reset-global-config").addEventListener("click", async () => {
    const res = await apiPost("/api/config/reset", {});
    if (res.ok) {
      applyTheme(res.config?.ui?.default_theme || "dark", false);
      showToast("Reset configuration overrides to default.json");
      await refreshState();
    }
  });

  // Raven Upstream actions
  document.getElementById("btn-upstream-check").addEventListener("click", async () => {
    showToast("Checking GitHub for new Raven commits...");
    await refreshUpstream(true);
  });
  document.getElementById("btn-upstream-build-only").addEventListener("click", () => startUpstreamPipeline("none"));
  document.getElementById("btn-upstream-build-all").addEventListener("click", () => startUpstreamPipeline("all"));
  document.getElementById("btn-upstream-rebuild-all").addEventListener("click", () => rebuildContainers("all"));

  await refreshState();
  const interval = state.config?.server?.poll_interval_ms || 2500;
  setInterval(refreshState, interval);

  await refreshUpstream();
  const upCfg = state.config?.upstream || {};
  if (upCfg.auto_check_enabled !== false) {
    setInterval(() => refreshUpstream(), (upCfg.check_interval_sec || 60) * 1000);
  }
});
