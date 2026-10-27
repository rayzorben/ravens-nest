# Raven Nest UI — Architectural & Design Rules

All code in `ui/` follows five non-negotiable engineering principles:

1. **DRY (Don't Repeat Yourself)**: Shared utilities for file hashing, idempotent file syncing, API calls, and UI component rendering.
2. **KISS (Keep It Simple, Stupid)**: Clean modular Python backend services + zero-build-step modern HTML/CSS/JS frontend + lightweight C++20 GTK/WebKit2GTK native window host.
3. **SOLID**:
   - `services/config_service.py`: Configuration loading, path templating, and user override persistence.
   - `services/container_service.py`: Container CRUD, cloning, environment management, soft-trash, and deletion.
   - `services/version_snapshot_service.py`: Versioned snapshots, registry fingerprinting, rollback with pre-rollback safety checkpoints, and snapshot forking.
   - `services/dependency_service.py`: PE64/PE32 & MSI inspection and idempotent library deployment.
   - `services/doctor_service.py`: D1–D13 build/runtime health checks and provisioning orchestration.
   - `services/process_service.py`: Process execution, log streaming, diagnostic translation, and desktop shortcut generation.
4. **Idempotence**: All file deployments, container creations, snapshot restorations, and config updates verify state before mutating and can be re-executed safely any number of times.
5. **Everything as Config**: All options, paths, timeouts, blueprints, component maps, redistributable definitions, diagnostic patterns, and UI theme defaults reside in `ui/config/default.json` and `~/.config/raven-nest/config.json`.
