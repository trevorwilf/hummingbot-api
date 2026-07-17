# Copy-Forward Hook — Implementation Report

**Branch:** `dev` (merged → `nonkyc`, both pushed to origin)  
**Design doc:** `COPY_FORWARD_HOOK_DESIGN.md` (design v2)  
**Date completed:** 2026-07-15  
**Final test run:** 226 passed, 14 failed (all 14 pre-existing baseline), 0 collection errors — **GREEN**

---

## Phase-by-Phase Summary

### Phase 1 — Baseline capture + deploy-model config surface
**Branch:** `fix/copyforward-p1-models` → merged `dev`  
**Commits:** `b00787d` (baseline), `0c73221` (models), `03fbd3c` (merge)  
**Files changed:** `models/bot_orchestration.py`, `test_logs/copyforward_baseline_failures.txt`  
**Tests added:** `tests/test_copyforward_models.py` — **28 tests**

Added 5 optional resume fields to both `V2ControllerDeployment` and `V2ScriptDeployment`:
- `resume_mode: Literal["off","explicit","latest"] = "off"`
- `resume_from: Optional[str] = None` (validated via `_validate_safe_name`)
- `resume_from_archive: bool = False`
- `resume_extra_paths: Optional[List[str]] = None` (absolute paths and `..` segments rejected)
- `resume_accept_ungraceful: bool = False`

Cross-field validators: `resume_mode == "explicit"` requires `resume_from`; `resume_from` set while `resume_mode == "off"` is a validation error. Baseline frozen at 14 pre-existing failures.

---

### Phase 2 — Source resolution
**Branch:** `fix/copyforward-p2-source` → merged `dev`  
**Commits:** `3ef249c`, `b86be0c` (merge)  
**Files changed:** `services/resume_service.py` (created, 432 lines)  
**Tests added:** `tests/test_copyforward_source.py` — **18 tests**

Public surface: `ResumeAbortReason` enum, `ResumeError(Exception)` with `.reason`, `ResolvedSource` dataclass, `async resolve_source(...)`.

- **explicit mode:** resolves `bots/instances/<name>/data` or `bots/archived/<name>/data` (with `resume_from_archive`). Archive-nesting pathology handled: innermost plausible level selected; ambiguous levels → `ARCHIVE_NESTED`. S3-absent archives include explicit wording in `SOURCE_NOT_FOUND`.
- **latest mode:** strips only the final `-YYYYMMDD-HHMMSS` suffix (single anchored regex; never double-strips). Queries `bot_runs` table by lineage match; falls back to directory listing on DB unavailable. Timestamp-based ordering (not mtime). Tie/ambiguity → `LATEST_AMBIGUOUS`; zero candidates → `SOURCE_NOT_FOUND`. Self-exclusion enforced.

Notable deviation: real-world name `KRAKEN_LADDER_V1-20260712-2302-20260712-230254` correctly yields base `KRAKEN_LADDER_V1-20260712-2302` (the intermediate segment is a human tag, not a timestamp).

---

### Phase 3 — Config-derived copy set + per-controller semantics
**Branch:** `fix/copyforward-p3-copyset` → merged `dev`  
**Commits:** `8d79fa4`, `85b438e` (merge)  
**Files changed:** `services/resume_service.py` (+471 lines, total ~900)  
**Tests added:** `tests/test_copyforward_copyset.py` — **25 tests**

`compute_copy_plan(new_instance_dir, source, deployment) -> CopyPlan` reads staged controller YAMLs in `<new_instance>/conf/controllers/`, identifies range-ladder controllers, and for each:

- Copies ledger JSON + `.owner` sidecar (verified sidecar naming against engine `range_inventory_ladder.py`)
- Identity verified via `.owner` JSON `controller_id`; filename mismatch → `OWNER_MISMATCH`
- `.owner` missing: default-named ledger → warn + copy; custom `state_file_name` → `OWNER_MISMATCH` (fail-closed)
- Zero-length or unparseable JSON → `LEDGER_INVALID` abort
- Source ledger not in deploy → `skipped`; no source ledger → `fresh_seed` warn
- Absolute `state_file_name` → `absolute_skipped` + loud warning (escapes `data/`)
- SQLite mode: adds `*.sqlite`/`*.sqlite-journal` from source `data/`
- `resume_extra_paths`: validated within `source.data_dir` after `.resolve()` (symlink-safe); escape → `EXTRA_PATH_ESCAPE`; missing → `EXTRA_PATH_MISSING`
- Never copies: `*.diagnostic_*.jsonl`, `*.tmp`, logs, market data

---

### Phase 4 — Guards (fail-closed)
**Branch:** `fix/copyforward-p4-guards` → merged `dev`  
**Commits:** `0b01297`, `4976d55` (merge)  
**Files changed:** `services/resume_service.py` (+311 lines)  
**Tests added:** `tests/test_copyforward_guards.py` — **19 tests**

`async run_guards(source, new_data_dir, deployment, docker_client, bot_run_repo) -> GuardReport`

1. **Source container state** (Docker SDK only): `running`/`restarting`/`paused` → `SOURCE_RUNNING`. `NotFound` → PASS. No PID/owner liveness checks.
2. **Destination clean**: any `*.json`, `*.sqlite`, or `.owner` present → `DEST_NOT_EMPTY`.
3. **Ungraceful source**: most recent `bot_runs` row with non-terminal status or missing end marker → `UNGRACEFUL_SOURCE`. DB row absent → treated as ungraceful (unknown history). `resume_accept_ungraceful=True` → PASS with loud WARNING in guard report.

---

### Phase 5 — Hook wiring + manifest/events/drift
**Branch:** `fix/copyforward-p5-hook` → merged `dev`  
**Commits:** `2a4eb55`, `9c86645` (merge)  
**Files changed:** `services/docker_service.py`, `services/resume_service.py` (+311 lines), `routers/bot_orchestration.py`, `main.py`  
**Tests added:** `tests/test_copyforward_hook.py` — **15 tests**

Attach point in `create_hummingbot_instance`: after controller config staging (loop copying YAMLs into `<instance>/conf/controllers/`), strictly before `client.containers.run`. Gated by single `if deployment_resume.resume_mode != "off":` — off-mode is completely untouched.

Hook sequence:
1. `resolve_source` → `run_guards` → `compute_copy_plan` → execute copies (`shutil.copy2`)
2. **Config-drift diff**: field-level diff of source vs newly-staged controller YAMLs; any difference → WARNING log + recorded in manifest (template wins, no carry-forward)
3. Write `data/resume.manifest.json`: `{source_instance, source_path, mode, files[{name, size, sha256}], decisions, drift, guard_report, created_at}`
4. Log `bot_resume_seeded` (structured: source, file count, decisions summary)

On `ResumeError`: `containers.run` never executes; log `bot_resume_failed` with reason enum; best-effort `rmtree` of instance dir (logged if cleanup fails); re-raise for loud deploy failure.

---

### Phase 6 — Router plumbing + resume-preview endpoint
**Branch:** `fix/copyforward-p6-router` → merged `dev`  
**Commits:** `aa762a4`, `8057eb7` (merge)  
**Files changed:** `routers/bot_orchestration.py` (+54 lines), `services/resume_service.py` (+140 lines)  
**Tests added:** `tests/test_copyforward_router.py` — **20 tests**

- Resume fields threaded from deploy endpoints through to `create_hummingbot_instance` via the existing `deployment_resume` parameter pattern.
- **Preview endpoint:** `POST /bot-orchestration/deploy-v2-controllers/resume-preview`
  - Read-only: runs resolution + guards + copy-plan; no directories created, no files copied, no docker writes
  - Copy set sourced from `bots/conf/controllers/<name>.yml` (template YAMLs) since no instance is staged at preview time
  - Returns `{resolved_source, files, decisions, guard_report, would_succeed, reason?}`
  - `ResumeError` → HTTP 409 `{reason: <enum>, detail: <message>}`; validation errors stay 422

---

### Phase 7 — End-to-end regression matrix
**Branch:** `fix/copyforward-p7-regression` → merged `dev`  
**Commits:** `3dcb410`, `eedf6a3` (merge)  
**Files changed:** `tests/test_copyforward_e2e.py`  
**Tests added:** `tests/test_copyforward_e2e.py` — **7 tests** (parametrized; covers all §11 matrix rows)

1. **Happy path:** source fabricated with ledger + `.owner` + excluded files (diagnostic, tmp); docker reports exited; ledger arrives byte-identical (sha256 verified) in new `data/` before `containers.run`; manifest validated.
2. **§11 fail-closed matrix:** parametrized over all `ResumeAbortReason` members — each aborts with correct reason, `containers.run` never called.
3. **Drift:** mutated template YAML → drift warning + manifest `drift` entry.
4. **Off-mode equivalence:** no resume fields → hook not invoked, container run args identical to pre-change golden.
5. **`latest` lineage:** multiple fake `bot_runs` rows including operator-timestamp-embedded name; newest resolved; timestamp tie → `LATEST_AMBIGUOUS`.

---

### Phase 8 — Finalization
**Branch:** `dev` (no new branch)

- Comprehensive test run: **226 passed, 14 failed (all baseline), 0 collection errors — GREEN**
- Merged `dev` → `nonkyc` (`--no-ff`)
- Merged remote `nonkyc` fixes (2 commits: bot-discovery scope, gateway guard) into local `nonkyc`
- Pushed `origin/dev` (new branch) and `origin/nonkyc`

---

## Test Tally

| File | Tests | Phase |
|---|---|---|
| `test_copyforward_models.py` | 28 | P1 |
| `test_copyforward_source.py` | 18 | P2 |
| `test_copyforward_copyset.py` | 25 | P3 |
| `test_copyforward_guards.py` | 19 | P4 |
| `test_copyforward_hook.py` | 15 | P5 |
| `test_copyforward_router.py` | 20 | P6 |
| `test_copyforward_e2e.py` | 7 | P7 |
| **Total new** | **132** | |
| Pre-existing baseline failures | 14 | (frozen, unchanged) |
| Final passing total | 226 | |

---

## Implementation Files

| File | Role | Size |
|---|---|---|
| `services/resume_service.py` | All resume logic: resolution, copy set, guards, hook orchestration, manifest | 1661 lines |
| `models/bot_orchestration.py` | 5 new optional resume fields on both deploy models + validators | +76 lines |
| `services/docker_service.py` | Hook attach point in `create_hummingbot_instance` | +25 lines |
| `routers/bot_orchestration.py` | Field threading + preview endpoint | +58 lines |
| `main.py` | Preview router registration | +2 lines |

---

## Operator Notes (Before First Use)

### Runbook pointer
See design doc §10 for the full operator runbook. Key flows: §10.1 (explicit resume), §10.2 (latest resume), §10.6 (pre-deploy checklist).

### `resume_accept_ungraceful` semantics
Default `false` — a source instance whose most recent `bot_runs` row is non-terminal (or absent) blocks the deploy with `UNGRACEFUL_SOURCE`. Set `resume_accept_ungraceful: true` only when you have externally verified the source stopped cleanly (e.g., manual stop via exchange + confirmed no open orders). The guard report records the override as a loud WARNING in the manifest.

### Preview endpoint usage
Before a production resume deploy, call `POST /bot-orchestration/deploy-v2-controllers/resume-preview` with the same payload. It performs full resolution, guard checks, and copy-plan computation without touching the filesystem or starting containers. Review `would_succeed`, `decisions`, and `guard_report` before committing.

### Off-mode guarantee
Deployments without resume fields (or `resume_mode: "off"`) follow the identical pre-change code path. The hook function is not invoked; no additional Docker queries, filesystem reads, or DB lookups occur. Zero behavior change for existing workflows.

### Fail-closed invariant
Every guard failure and every copy error aborts the deploy **before** `containers.run`. A partially-seeded instance directory is cleaned up (best-effort `rmtree`). No half-seeded instance will ever start. There is no soft/best-effort mode.

### Non-empty destination policy
If `bots/instances/<new_name>/data/` already contains `*.json`, `*.sqlite`, or `.owner` files, the deploy aborts (`DEST_NOT_EMPTY`). This prevents accidental state clobber on retried deploys with the same instance name.
