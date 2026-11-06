# Repository Cleanup

## Scope

Prepare source for future publication without a commit, push, upload or history
rewrite. Preserve the existing uncommitted DSH packaging implementation.

The user subsequently decided to remove the root `archive/`, `Dedraft/` and
`DeFeature/` directories and retain `Defillet/`. References below to the old
pipeline or archives describe the earlier cleanup, not current entry points.

## Removed Scripts

Removed obsolete scripts with no source callers or registered runtime use:

The cleanup removed 43 obsolete scripts from the worktree.

- Twelve obsolete `Legacy/*.py` journals and probes. The documented
  `Legacy/run_nx_py.bat` wrapper is retained and now uses configured NX paths.
- `Defillet/DebugAutoFillet` and `Defillet/ProbeScripts` debugging experiments.
- Two `Defillet/OCC_Imple/Legacy` superseded edge-discovery implementations.
- The misspelled `ReversibleDefillet/*_buckup.py` source backup.
- The abandoned .NET remoting bridge and GUI dispatcher implementation scripts.
  Their archive notices were retained at that cleanup stage.
- Two case-specific `DeFeature/debug_*.py` scripts.
- Two unused `Dedraft/OCC_Imple` demos and `OccUtils/probe_face_type_occ.py`.

At that cleanup stage, the backend, plugin, journals, tests, packaged skills,
standalone Web app, pipeline, Boolean service, restoration and geometry utilities
were retained. The former pipeline imported
`Defillet/Legacy/nx_2_occ_detect_sharp_edge_group_inference.py`; that historical
dependency is not a reason to retain the retired pipeline.

## Configuration

Removed author-machine Python/OpenCode fallbacks from the backend and launchers.
OpenCode reads now require explicit `OPENCODE_CONFIG_PATH`. Tests use synthetic
provider data rather than the author's model credentials and user identity.
Standalone CAD input/output arguments now require explicit paths instead of
silently reading or overwriting author-machine files. NX defaults derive from
`ProgramFiles`, with supported NX environment and command-line overrides retained.
Updated active setup and command examples to portable paths.

## Locally Preserved Files

Removed ignored generated files from the Git index without deleting local copies:
CAD models, NX outputs/logs, benchmark outputs, cached bytecode and generated
restore recipes. Also excluded locally copied `NXOpen`, `NXAPI_my_doc`,
`Midea-Feature-Tool`, `acoustic.pdf` and `paper2.pdf`. These are not release inputs;
SDK, company binary and paper redistribution requires separate permission.

Index removals are staged because `git rm --cached` operates on the index. Source
edits and script deletions remain unstaged. Nothing was committed.
There were 548 staged index removals. Their local originals were preserved at
that stage; the user later explicitly requested deletion of all files inside
`DeFeature/`, `Dedraft/` and `archive/`, including ignored local data.

## Repository Inventory

Directory names alone do not establish whether contents are project inputs:

| Path | Classification | Current disposition |
| --- | --- | --- |
| `.agents/`, `.aider-desk/` | Ignored local AI-tool skills and memory; not plugin release inputs | Keep locally until their owner decides whether to move or remove them. The directories may share linked content. |
| `.dsh/skills/` | Tracked project skills used by the DSH plugin release | Keep: `release-files.json` and `package-release.mjs` select these sources. |
| `dsh-plugins/`, `webapp/`, `cad/nx_persistent_worker.py` | Plugin, independent Web app and persistent NX runtime | Keep; generated clients, dependencies and build outputs remain ignored. |
| `DeFeature/`, `Dedraft/` | Retired standalone pipeline and draft workflows | Files deleted at the user's request; empty directories may remain locally. |
| `cad/Defillet/`, `cad/refine_step.py`, `cad/OccUtils/` | Retained geometry and runtime code | Keep; the current workbench uses code in `cad/Defillet/`. |
| `NXBooleanService/` | Independent Boolean service and local test data | Keep locally, excluded from Git and the plugin release. |
| `cad/Defillet/nx_reversible_defillet_cli.py`, `cad/run_nx_py.bat` | Reversible workflow and journal launcher | Keep. |
| `data/`, `webapp/jobs/`, `benchmark-results/`, `local-artifacts/` | Local inputs, jobs, experiments and research | Preserve locally; generated or private data are not publication inputs. The case coloring script now lives in `cad/Defillet/`. |
| `NXOpen/`, `NXAPI_my_doc/`, `Midea-Feature-Tool/` | Local SDK, documentation and company assets | Excluded from index, preserved locally; rights and any continuing use need review. |
| `.pytest_cache/`, `__pycache__/`, `node_modules/`, `dist/` | Caches, dependencies and build products | Ignored, but not deleted: some must be rebuilt or downloaded, and `dist/` contains the local release artifact. |
| `archive/` | Historical documents for retired runtimes | Files deleted at the user's request; empty directories may remain locally. |

The former root `圆角删除技巧总结_耦合分组与删除顺序.md` is restored as
`.dsh/skills/fillet-removal-strategy/references/strategy-background.md` and is
included in the skill release inputs. Neither local
AI directories nor ignored experiment data are automatically safe to delete.

## History Rewrite Preparation

Do not rewrite history during source cleanup. The future history audit must cover
all refs and historical paths, not just today's ignore rules or staged removals.
Review SDK and company assets, CAD/job/benchmark outputs, papers, personal paths,
credentials and third-party permissions. Make and verify a full backup including
the current staged, unstaged and untracked work before any rewrite; test filtering
on an isolated copy first. Decide how to preserve the current working state and
obtain separate approval before applying a rewrite or removing old Git objects.

## Validation

After removing the directory contents, backend and Boolean configuration tests
passed (111 tests), package tests passed (5 tests), runtime closure and tarball
hashes passed (26 runtime inputs, 59 archive files), and the heuristic source
audit reported no findings across 163 text files. Empty directory shells remain.

Follow-up validation on 2026-11-05:

- Backend regressions: 111 passed, with one Starlette/httpx deprecation warning.
- Added configuration tests for active-interpreter defaults, authoritative explicit
  OCC Python settings and both aliases of the required inference model argument.
- The shared inference parser now rejects an omitted model before geometry work.
- At the time, `scripts/audit_publication_source.py` checked tracked and nonignored untracked
  text, ignored index entries, release runtime/skill inputs and pipeline script
  paths. It reported match locations without printing suspected credential values.
- Source audit: 176 prospective text files checked, zero heuristic findings;
  no ignored tracked files. Corrected two remaining machine-specific Boolean
  service documentation commands discovered by the audit.
- Follow-up package tests: five passed; runtime closure and tarball hashes passed
  for 26 runtime inputs and 59 archive files. Python syntax and staged/unstaged
  whitespace checks passed, with Git LF/CRLF conversion warnings only.
- Prior packaging validation passed the build/release, five package tests, tool
  separation, mapping, 35 preset entries, 26 runtime files and the 59-file tarball
  inventory/hashes. Standalone-only follow-up changes do not alter that artifact.
- The packaged backend smoke rerun reached health/configuration verification.
  Its final startup-disposal success marker was not observed; completion of that
  scenario remains unconfirmed and must not be reported as a passed check.

The source audit is deliberately heuristic, not a comprehensive secret scanner
or an audit of all private endpoints. It does not certify Git history. Fresh-machine
installation, full preset service mounting, browser rendering, real NX/license
operations and full standalone CAD execution were not verified by those checks.

## History Blocker

Read-only history inspection confirmed excluded material in existing commits,
including `2648b85`, `f072c27`, `d4bcd09` and `46091a1`. A push of the existing
history would still distribute it. This cleanup does not authorize history
rewriting, deleting local research data, selecting a license or publication.

A recognizable-credential assignment scan of history found no matching
`sk-`, `ghp_`, `github_pat_`, `AKIA` or `eyJ` assignments. This is a limited
heuristic, not proof that no secrets exist. Before publication, use a dedicated
secret scanner on the approved publication tree and all history to be shared;
rotate any real credentials found. Do not print secret values in reports.

The intended direction is to clean existing history after repository cleanup;
confirm the detailed rewrite plan, project license and third-party rights separately.
