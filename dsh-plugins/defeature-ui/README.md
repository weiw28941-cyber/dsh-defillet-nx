# Defeature DSH Plugin

NX STEP defeaturing tools, three skills, production and full development agent
presets, a managed Python/OCC backend, and the browser CAD workbench.

## Install

Requires DSH Web (tested against 0.1.1-rc.2), Node >=22.19, Windows x64,
locally installed Siemens NX (tested with NX2406), and a valid NX license.
NX, NX binaries, proprietary SDK files and license credentials are not bundled.

Install the generated package through DSH's custom plugin installer or:

```powershell
dsh plugin --profile web add "D:\path\defeature-dsh-ui-0.1.0.tgz"
```

Restart DSH Web, create a new `defeature-agent` or `defeature-agent-dev` session,
then open the CAD workbench. On first use confirm the independent Python/OCC
installation, or select an existing compatible Python. The installer downloads
Miniforge from conda-forge, verifies its published SHA-256, and creates an
isolated conda-forge environment without changing system Python or PATH.
It does not install anything without the explicit workbench confirmation.

## NX Interface

Choose the NX installation directory in the workbench. Settings can also be saved
while the backend is running; finish current CAD operations and restart DSH before
using the new configuration. Advanced overrides
are stored in `%DSH_HOME%\defeature\config.json` (default `%USERPROFILE%\.dsh`):

```json
{
  "nxBase": "C:\\Program Files\\Siemens\\NX2406",
  "runJournal": "C:\\Program Files\\Siemens\\NX2406\\NXBIN\\run_journal.exe",
  "stepSettings": "C:\\Program Files\\Siemens\\NX2406\\STEP214UG\\ugstep214.def",
  "pythonExecutable": "C:\\path\\python.exe"
}
```

`UGII_BASE_DIR`, `NX_RUN_JOURNAL`, `NX_STEP214_EXPORT_DEF` and `DEFEATURE_PYTHON`
are also supported. Configure the NX license through NX's own supported setup.
An explicitly configured Python executable is authoritative; an invalid path is
reported as setup required rather than silently selecting another environment.
Restart DSH after changing configuration while the backend is running.
Jobs, models, environment and logs stay outside the installed package and survive
plugin upgrades. Missing licenses and unavailable NX are infrastructure errors.

## Agent Presets

On activation, missing presets are installed into the user preset directory.
Existing presets are never overwritten, including previous locally authored ones.
Users with older path-bound presets should create a new preset ID from the shipped
templates or replace them manually after reviewing their customizations.
The plugin never changes the default preset or global sandbox/approval policy.

Production registers only the CAD allowlist. Development explicitly adds shell,
source editing, jobs, planning, delegation, workflows and arbitrary persistent
NXOpen execution. These are high-privilege capabilities, not a CAD sandbox.
Do not choose development mode for untrusted prompts or files. Unknown rollback
state quarantines the worker and blocks Candidate publication in either mode.

## Source And Release

`release-files.json` is the explicit runtime allowlist. The package excludes
repository history, old journals not imported by current services, test data,
NX installations, papers, credentials, models, jobs, caches and node_modules.
CAD runtime files retain their repository paths under `runtime/cad/`, alongside
the backend under `runtime/webapp/`. `cad/Defillet/nx_1_defillet.py` is
intentionally included despite its historical name:
the live detection/deletion journals import its shared NX functions.
`release-manifest.json` records every shipped file's size and SHA-256.
`python test/runtime-closure.py` checks packaged local imports, statically named
NX journals, and the tarball inventory and hashes after the release is built.

```powershell
npm install
npm run build
npm run release
npm run test:package
```

The release is generated from this repository, but runs without a source checkout.
For development export a fresh editable workspace:

```powershell
node lib/export-source.js "D:\\work\\defeature-editable"
```

Set `DEFEATURE_REPO_ROOT` to that workspace before restarting DSH to use its backend.
The export refuses to overwrite existing files. Edit a source checkout for full
client build scripts/tests; package exports include the runtime and client source,
not the repository's historical experiments. Do not edit node_modules to retain
changes across upgrades. Full upstream tests and development scripts are available
in the source repository, not included in the installed runtime package.

## Publishing

The generated tarball is locally installable; it is not automatically uploaded.
Before public npm/marketplace publication, choose a package owner, version,
repository URL and project license, then review dependency licenses. No project
license is asserted by this packaging step. A marketplace listing is separate from
building the installable bundle and requires the marketplace's submission process.
