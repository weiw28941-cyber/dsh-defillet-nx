# Defeature

[中文](README.zh-CN.md) | English

Local STEP defeaturing with Siemens NX, Python/OCC and a DeepSeek Harness (DSH)
CAD workbench. Geometry results are proposed as Candidates for user review.

## What It Does

- Inspect STEP geometry in a browser, select faces and review fillet components.
- Use NX to attempt fillet removal and inspect the resulting Candidate before
  accepting or rejecting it.
- Run a persistent NX worker to avoid restarting NX for each supported operation.
- Use the DSH plugin's CAD tools and skills, or run the standalone Web application.

The production agent exposes a restricted CAD tool set. The explicitly enabled
development preset includes shell, source editing and arbitrary NXOpen execution;
it is not a sandbox for untrusted prompts or files. If rollback cannot be verified,
the worker is quarantined and Candidate publication is blocked.

## Requirements

- Windows x64, Siemens NX and a valid NX license for NX operations; NX2406 has
  been used during development.
- Node.js 22.19 or later and DSH Web for the plugin; the plugin guide lists the
  tested DSH version.
- A compatible Python environment with Python/OCC and backend dependencies.
  The plugin can offer an isolated environment installation after confirmation.

NX, proprietary SDK files and license credentials are not included. This project
does not claim compatibility with every NX or DSH version.

## Documentation

- DSH plugin: [installation and release guide](dsh-plugins/defeature-ui/README.md).
- Independent Web application: [setup guide](webapp/README.md).
- Persistent NX runtime: [protocol](docs/persistent_nx_runtime.md).
- Controlled CAD tools: [contracts](docs/nx_controlled_cad_tools.md).

## Repository Layout

The source layout groups related CAD scripts together:

```text
.
|-- cad/
|   |-- Defillet/              # Fillet detection and NX journals
|   |-- OccUtils/              # OCC geometry and STEP utilities
|   |-- nx_persistent_worker.py # Persistent NX journal worker
|   |-- refine_step.py         # STEP refinement
|   `-- run_nx_py.bat          # NX journal launcher
|-- dsh-plugins/defeature-ui/  # DSH plugin, presets and release packaging
|-- webapp/                    # Independent Web frontend and backend
|-- docs/                      # Runtime contracts, plans and project records
|-- scripts/                   # Repository and benchmark scripts
`-- data/                      # Local model data
```

The former standalone `DeFeature` pipeline and `Dedraft` workflows are retired.
The independent Boolean service is not part of the plugin release.

## Build Locally

From `dsh-plugins/defeature-ui`:

```powershell
npm ci
npm run build
npm run release
npm run test:package
npm run test:tools
npm run test:mapping
```

The output is `dist/defeature-dsh-ui-0.1.0.tgz`. Building does not publish or
upload it. `release-files.json` defines the runtime and skill dependency closure.
NX, license credentials and proprietary SDK copies are not bundled. First-use
Python/OCC installation requires explicit user confirmation.

Activate a compatible Python/OCC environment for backend regression tests:

```powershell
python -m pytest webapp/backend/tests -q
```

Install the resulting archive using the [plugin guide](dsh-plugins/defeature-ui/README.md).
For the standalone application, see the [Web setup guide](webapp/README.md).

## Local Configuration

Set NX paths through the workbench or `UGII_BASE_DIR`, `NX_RUN_JOURNAL` and
`NX_STEP214_EXPORT_DEF`. Set `DEFEATURE_PYTHON` to select a Python interpreter.
The independent Web application only reads OpenCode provider configuration when
`OPENCODE_CONFIG_PATH` is explicitly supplied. Never commit real API keys.

Local models, jobs, logs, caches, research papers, local SDKs and company executables
are excluded from version control; the explicitly included test STEP is an exception.
Retain personal material under
`local-artifacts/`; `.gitignore` does not erase previously committed content.

## Publication Status

This checkout is being prepared for publication; nothing is automatically pushed.
Existing history contains local CAD artifacts, SDK copies, company binaries and
papers. Removing them from the current index does not remove that history.
Review [the cleanup report](docs/repository_cleanup.md) before any publication.
No project license or redistribution permission for third-party material is
asserted. Do not publish the existing history without a separate approved cleanup
or an approved clean-history export.
