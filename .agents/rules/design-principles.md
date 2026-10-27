---
trigger: always_on
description: Core software engineering and architectural design principles for this workspace (DRY, KISS, SOLID, Idempotence, Everything as Config).
---

# Core Design Principles

All code, scripts, configurations, and UI components in this workspace MUST strictly adhere to the following five principles:

1. **DRY (Don't Repeat Yourself)**
   - Every piece of knowledge, schema, component mapping, and UI template helper must have a single, unambiguous, authoritative representation.
   - Share common logic through reusable helpers/modules rather than duplicating code across files or architectures.

2. **KISS (Keep It Simple, Stupid)**
   - Favor straightforward, readable, and maintainable implementations over unnecessary complexity or premature abstraction.
   - Keep control flow explicit and diagnostics human-readable.

3. **SOLID**
   - **Single Responsibility**: Separate configuration management, container lifecycle, versioning/snapshots, PE dependency analysis, deployment doctor checks, and process execution into distinct, focused modules.
   - **Open/Closed**: Extend behavior (new redistributables, new runtime components, new container blueprints, new diagnostic rules) via configuration rather than modifying core engine logic.
   - **Interface Segregation & Dependency Inversion**: Services depend on injected configuration and clear data contracts rather than concrete global state.

4. **Idempotence**
   - Every operation—creating a container, deploying runtime DLLs or redistributables, registering COM servers, taking/restoring snapshots, configuring CMake, or exporting desktop runners—must be safe to run repeatedly.
   - Check file existence, SHA-256 digests, or size+mtime stamps before mutating state so repeated runs converge on the exact same state without side effects or duplicate artifacts.

5. **Everything as Config (No Hard-Coded Options)**
   - Never hard-code file paths, network ports, timeouts, environment variables, container blueprints, DLL mappings, theme palettes, or CLI flags inside application logic.
   - Store all defaults in a structured configuration file (`config/default.json`) with user-level overrides (`~/.config/raven-nest/config.json`) and environment variable fallbacks.
