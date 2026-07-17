# Copy-Forward Hook — Post-Implementation Review

**Date:** 2026-07-15
**Reviewer:** Claude (Opus 4.8), with two parallel adversarial sub-reviewers (Opus 4.8, fresh context)
**Repos / commits at review time:**
- API (subject): `E:\tradingsoftware\hummingbot-api` — branch `nonkyc`, HEAD `b3aad36` (P8 merge), feature base `0639c7a`, `dev` at `eedf6a3` (P7).
- Engine (ground-truth reference, read-only): `E:\tradingsoftware\hummingbot` — `47c714a19` (nonkyc).
**Design spec:** `E:\tradingsoftware\hummingbot-api\COPY_FORWARD_HOOK_DESIGN.md` (v2).
**Implementation prompt / runner:** `hbapi_copyforward_claude_code_prompt.md`, `run_hbapi_copyforward_batch.ps1`.

---

## 0. TL;DR verdict

**Ship-ready for this operator's deployment.** The copy-forward hook is cleanly isolated, genuinely fail-closed, and off-by-default. The batch (8 phases, unattended) completed: all 7 impl phases merged to `dev`, `dev` merged to `nonkyc`, pushed; no halt files.

An adversarial pass found **12 latent bugs** (below). **None are triggerable in this operator's actual config** (engine-on-Postgres, string controller ids like `range_inventory_ladder_xmr_V1`, flat `state_file_name`s, no SQLite WAL, API-managed `data/`), and **all but one fail *closed*** (abort the resume with HTTP 409) rather than silently re-seeding. The single fail-*open* path (#3) requires a nonsensical `id: 0`/`id: False`. A small hardening patch is recommended but not blocking.

**The one thing that actually validates the untested surface is a live `resume-preview` dry-run against a real stopped instance** — all tests are mocked, so mock-vs-reality is the real residual risk, and it's closed by running it, not re-reading it.

---

## 1. Batch outcome + test-gate verification (independently re-run)

- **Gate design:** baseline-diff. Phase 1 froze the pre-existing failures to `test_logs/copyforward_baseline_failures.txt` (14). A phase is green iff no NEW failures + all its own tests pass + zero collection errors.
- **Independent re-run** (`C:/anaconda3/envs/hummingbot/python.exe -m pytest tests/ --ignore=tests/test_nonkyc_live_api.py -q`):
  - **14 failed / 232 passed.** The 14 are **byte-identical to the frozen baseline** (normalized diff empty). Zero new failures, zero collection errors.
  - Passing count rose 30 → 232: all 7 `test_copyforward_*` suites (~3,250 test lines) are green.
- The 14 pre-existing failures (health endpoint, unified-connector polling, config env-prefix, nonkyc auth unit, VPN network-mode) are unrelated and correctly left untouched.

## 2. Structural / safety verification (first pass, read directly)

| Property | Evidence | Status |
|---|---|---|
| **Off-mode = byte-identical path** | `docker_service.py:308` gates the whole hook behind `if config.resume_mode != "off":`. No resume fields → hook never entered. | PASS |
| **No container starts on failed/partial seed** | `seed_resume_state` (`resume_service.py:1462-1521`) wraps everything; on ANY exception → `_log_resume_failed` → `_cleanup_failed_instance` (`rmtree` the instance dir) → re-raise. Call site `docker_service.py:309` is **not** wrapped, so it propagates and `containers.run` (`:371`) is never reached. | PASS |
| **Attach point** | After config staging incl. the `conf_client.yml` mutation (`:299`), before `containers.run`. Staged YAMLs drive the copy set + sqlite detection. | PASS |
| **Guards fail-closed** | `run_guards` (`:1154`) raises on FIRST hard failure. `_guard_source_container`: `NotFound`=pass, active states=abort. `_guard_destination_empty`: any `*.json`/`*.sqlite`/`*.owner` → abort. `_guard_ungraceful_source`: unknown history = ungraceful (overridable via `resume_accept_ungraceful`). | PASS |
| **Router error mapping** | `routers/bot_orchestration.py`: `except ResumeError → 409 {reason, detail}` precedes generic `except Exception → 500`, on both deploy paths + preview. Model validation stays 422. | PASS |
| **Async conversion safe** | `create_hummingbot_instance` became `async`; both real callers (`:541`, `:604`) `await` it. No un-awaited stragglers. | PASS |
| **Tests touch no real infra** | Every "real" grep hit is a docstring or the `real_copy2 = shutil.copy2` save-before-patch idiom. No `from_env`, `create_engine`, `psycopg2`, or network. | PASS |
| **DI wired** | `main.py:237` `DockerService(db_manager=db_manager)`. | PASS |
| **No scope creep** | `services/bots_orchestrator.py` (+28) and `services/unified_connector_service.py` (+19) came from production hotfixes `9dc0de7` / `25fb268` on `origin/nonkyc`, pulled by the P8 pre-push merge `b3aad36` (parents `58c0b6f` + `9dc0de7`). `dev`'s history for both files stops at the base commit — the batch never touched them. | CONFIRMED |

**Files changed vs base `0639c7a` (feature only):** `services/resume_service.py` (+1661, new), `models/bot_orchestration.py` (+76), `routers/bot_orchestration.py` (+58), `services/docker_service.py` (+25), `main.py` (+1), plus `tests/test_copyforward_{models,source,copyset,guards,hook,router,e2e}.py` and `test_logs/copyforward_baseline_failures.txt`.

## 3. Line-by-line: `latest` lineage (`resume_service.py:108-438`) — correct

- `_API_SUFFIX_RE = re.compile(r"-(\d{8}-\d{6})$")` (`:108`) — end-anchored, exactly 8+6 digits (matches the API's `%Y%m%d-%H%M%S` suffix, `bot_orchestration.py:~501`).
- `_strip_api_suffix` (`:112`) uses `sub(count=1)`; under the `$` anchor there is only one match anyway. Against the real operator name **`KRAKEN_LADDER_V1-20260712-2302-20260712-230254`** it strips only the final `-20260712-230254` → base `KRAKEN_LADDER_V1-20260712-2302`. The interior `-2302` (4 digits) can't satisfy `\d{8}`, so **no double-strip** (design R2 requirement).
- `_parse_api_timestamp` (`:118`) ranks by `strptime`, never mtime.
- `_collect_latest_candidates` (`:342`): DB lineage preferred; directory fallback fires **only on a DB exception**, NOT on a successful-but-empty result (an empty working DB correctly yields "no prior run"). Correct, fail-closed choice.
- `_match_candidates` (`:377`): matches `_strip_api_suffix(name) == target_base` byte-for-byte, excludes self, deliberately preserves duplicates for the tie check.
- `_pick_newest` (`:401`): two names sharing a base must differ in timestamp, so a tie on `max_ts` means literally-duplicated lineage rows → `LATEST_AMBIGUOUS`. Unparseable suffixes dropped w/ warning; all-unparseable → `SOURCE_NOT_FOUND`.
- **Note (not a bug):** `get_bot_runs(limit=1000)` could theoretically miss lineage on a >1000-run fleet, but the newest-first ordering makes this a non-issue for the "resume a bot you just stopped" use case; it fails closed.
- **Design-vs-impl note:** R2's "disagreeing controller ids → refuse at resolution" is enforced *downstream* as `OWNER_MISMATCH` in the copy plan, not at resolution. Net outcome (abort) is equivalent.

## 4. Line-by-line: `.owner` identity (`resume_service.py:648-742`) — correct, cross-checked vs engine

| Hook assumption | Engine ground truth | Match |
|---|---|---|
| ledger = `state_file_name or range_inventory_ladder_<id>.json` | `state_path` `range_inventory_ladder.py:1471` | ✓ |
| owner sidecar = `<ledger>.owner` | `Path(f"{state_path}.owner")` `:1984`; `:1490` requires `.json`/`.json.owner` names stay byte-identical across restarts | ✓ |
| owner JSON key `controller_id` = YAML `id:` | `{"controller_id": self.config.id, "pid":…, "started_at":…}` `:2008-2012` (`sort_keys=True`) | ✓ |
| mismatch semantics | engine's own contention check compares `existing.get("controller_id") != self.config.id` `:1990` | ✓ |

Edge-case ladder, all fail-closed-correct:
- ledger + `.owner` id ≠ deploy id → **OWNER_MISMATCH**.
- ledger, no `.owner`, **custom** `state_file_name` → **OWNER_MISMATCH** (id unverifiable).
- ledger, no `.owner`, **default** name → warn + copy (id embedded in filename). Rare: engine writes the marker *before* the ledger (`_save_state` calls `_ensure_state_owner_marker()` at `:2022`).
- no ledger → per-controller **fresh_seed** + warn.

Carrying `.owner` forward is safe: resumed controller reads the copied marker, sees `existing_id == self.config.id`, so **no false contention warning**, then rewrites pid. Guard 1 (source container exited) means no live writer during copy.

Exclusion patterns line up with real artifacts: `*.tmp` catches `mkstemp(suffix=".tmp")` remnants (`:2023`); `*.diagnostic_*.jsonl` catches stamped diagnostics (`:1492-1498`), which are `.jsonl` and thus also invisible to the `*.json` orphan glob.

---

## 5. Adversarial review — findings (recalibrated)

Two fresh-context Opus reviewers, each told to *break* it, verified with executable probes against the real module (mocked Docker/DB). Reviewer severity is their rating; "My sev" is recalibrated for THIS operator's config + failure direction.

### 5.1 Copy-plan pipeline (`compute_copy_plan` + helpers)

| # | Finding | file:line | Trigger | Fail direction | Reviewer / My sev |
|---|---|---|---|---|---|
| 1 | Numeric YAML `id:` → false `OWNER_MISMATCH`: `owner_id` is `str` (engine coerces `id:str` before writing `.owner`), hook reads raw YAML `int` via `config.get("id")`; `"123" != 123` always True | `resume_service.py:714` | `id: 123` unquoted + a legit `.owner` | **Safe (aborts)** | HIGH / LOW-MED |
| 2 | `resume_extra_paths: ["."]` or `[""]` copies the **entire** source `data/` (`data_dir / "." == data_dir`, `is_relative_to` self True) and double-copies every ledger; pulls in excluded/diagnostic/other-controller files — defeats the "config-derived, never glob" invariant | `_plan_extra_paths:818-833` + `_execute_copy_plan:1275-1278` | operator sets that advanced field to `.`/`""` | Open-ish (over-copy) | HIGH / **MED** |
| 3 | `id: 0` / `id: False` → `if not controller_id:` True → controller silently dropped → engine re-seeds from wallet (the insufficient-funds bug) | `compute_copy_plan:874` | falsy id | **Open (silent fresh-seed)** | MED / LOW\* |
| 4 | Falsy-but-present `controller_id` (`0`/`""`/`false`) in `.owner` → `if not owner_id:` → false `OWNER_MISMATCH` | `_read_owner_controller_id:664` | hand-corrupted `.owner` (engine never writes falsy) | Safe (aborts) | MED / LOW |
| 5 | Subdir `state_file_name` (`sub/ledger.json`) misplaces owner dst: `new_data_dir / src_owner.name` flattens the subdir → owner lands in `data/`, not `data/sub/`; orphaned | `_plan_controller:721` | subdir in `state_file_name` | Cosmetic (resume still works) | MED / LOW |
| 6 | Traversal `state_file_name` (`../../evil.json`, not absolute so `_is_absolute_state_file` passes it) → owner (flattened, in-tree) copied before ledger; ledger dst escapes → `EXTRA_PATH_ESCAPE` abort leaves orphan owner. **Mitigated:** `seed_resume_state`'s `_cleanup_failed_instance` rmtrees the whole instance dir. Content NOT exfiltrated. | plan ordering + `_execute_copy_plan:1267` | `../` in `state_file_name` | Safe (aborts + cleanup) | MED / LOW |
| 7 | Case/whitespace `db_engine` (`SQLite`, ` sqlite`) fails exact `== "sqlite"` → sqlite deploy's trade DB silently not carried forward | `_is_sqlite_deployment:600` | non-exact `db_engine` on a **sqlite** deploy | Open (for sqlite users) | MED / MED (general) |
| 8 | SQLite WAL sidecars (`.sqlite-wal`/`.sqlite-shm`) never copied; only `.sqlite` + `.sqlite-journal` | `_plan_sqlite:799-804` | sqlite in WAL mode | loses un-checkpointed txns | LOW / LOW (general) |
| 9 | Symlinked source ledger (default-named) bypasses read-containment (`read_bytes` follows link, copies external content in) — asymmetric vs `extra_paths` which uses `resolve()` | `_validate_ledger:628` / `_plan_controller:696` | attacker write to source `data/` | — | LOW / NIT |
| 10 | Duplicate controller `id:` across two staged YAMLs → duplicate `(src,dst)` copy items; `decisions` second clobbers first | `compute_copy_plan:870-881` | dup ids in one deploy (violates C1) | Idempotent | LOW / NIT |

\* #3 is the only fail-*open* path but needs a nonsensical id no real controller uses.

**Copy-plan vectors tried and SAFE:** absolute `state_file_name` (POSIX slash, backslash, drive-letter — correct on both platforms) → `absolute_skipped`; absolute `resume_extra_paths` → `EXTRA_PATH_ESCAPE`; `../..` / symlink-out / missing extra_paths → fail-closed; empty-JSON ledger (`{}`/`[]`/`null`) → copied, judged acceptable-by-design (engine quarantine handles structural validity; rejecting `{}` would false-positive on legit fresh states); orphan-scan regex `range_inventory_ladder_(.+)\.json` → no misclassification.

### 5.2 Preview endpoint (`preview_resume` + router)

| # | Finding | file:line | Trigger | Fail direction | Reviewer / My sev |
|---|---|---|---|---|---|
| P1 | `would_succeed` can't detect `DEST_NOT_EMPTY`: preview points the guard at a fresh temp `data/` (never non-empty); real deploy checks the real `bots/instances/<name>/data/`, which is reused when `instance_dir` already exists (`if not os.path.exists`) — e.g. same-second redeploy collides on the `%Y%m%d-%H%M%S` suffix, or a leftover dir. So preview can say `would_succeed:true` while the real deploy 409s. | `resume_service.py:1620-1626`; `docker_service.py:220-223`; `bot_orchestration.py:503-506` | same-second redeploy / leftover instance dir | Preview optimistic; **real deploy still fails closed** | MED / LOW-MED |
| P2 | Docker daemon `APIError` (not `NotFound`) propagates past `run_guards` → router `except Exception` → opaque HTTP 500 instead of actionable 409. Shared with the real deploy path (so preview is faithful). | `_guard_source_container:1018-1029` | Docker daemon down/unreachable | Cosmetic | LOW / LOW |

**Preview vectors tried and SAFE:** strict read-only (every write under `tempfile.TemporaryDirectory`; source only `read_bytes()`; `_execute_copy_plan` called only at `:1417`, never in preview; only Docker call is `containers.get()`); temp-dir cleaned on happy path + `ResumeError` + unexpected exception (no leak); copy-set source parity (both derive from `deployment.controllers_config`; missing template → same `skipped` classification both paths); error-mapping order (`except ResumeError` precedes `except Exception`); concurrency (distinct temp dirs, no shared mutable state); `db_manager=None` degradation (latest→dir fallback, history→ungraceful, identical to real path); `latest` exclude-self; `extra_paths` dst-containment moot (model blocks `..`/absolute at 422; `_plan_extra_paths:821` enforces src-side containment).

---

## 6. Recommended hardening patch (small, well-specified — not blocking)

Bundle on a `fix/copyforward-hardening` branch, tests + baseline-diff gate. ~30 lines + regression tests (each finding above has a concrete trigger to encode). Sonnet/Opus-tier mechanical work against this spec.

- **Type/falsy cluster (#1, #3, #4):** compare ids as `str(...)` on both sides; replace `if not controller_id:` / `if not owner_id:` with `is None` (plus explicit empty-string handling); normalize `decisions` / `deployed_ids` keys to `str` so int and str ids can't split a controller into two buckets.
- **#2:** in `_plan_extra_paths` (and/or the P1 model validator) reject `""` and `.`, and require each extra path to resolve to a **strict subpath** of source `data/` (reject `resolved == data_root` and directory-valued root).
- **#7:** `str(engine).strip().lower() == "sqlite"`.
- **#5:** derive owner dst as `new_data_dir / f"{ledger_name}.owner"` (not `src_owner.name`) so a subdir'd `state_file_name` keeps its owner beside its ledger.
- **Optional #6:** enforce `dst.resolve().is_relative_to(new_data_root)` at plan time in `_plan_controller` (fail before any copy), and make owner/ledger dst derivation consistent.
- **Optional P2:** catch `docker.errors.APIError` in `_guard_source_container` → `ResumeError` so "can't verify source container" maps to 409 with a clear detail.
- **Optional #10:** abort on duplicate staged controller `id:` (ambiguous deploy).
- **Optional #8:** include `.sqlite-wal` / `.sqlite-shm` in `_plan_sqlite` (only matters if the engine ever runs SQLite in WAL mode; this operator is Postgres).

## 7. Residual operational caveats (not bugs)

1. **Never exercised against real infra** — all tests mocked. Before flipping `resume_mode` on in production, run `POST /bot-orchestration/deploy-v2-controllers/resume-preview` against a real stopped instance first (read-only; Docker touched only for the container-state read). This is the real validation of the mock-vs-reality gap.
2. **psycopg2 preflight is a runbook step, not code** (design §10.1) — the hook does not verify the new image ships the driver.
3. **14 pre-existing test failures remain** — orthogonal to this work, but real; worth a separate cleanup pass.
4. **Controller `id:` must be fleet-wide unique** under the shared engine-Postgres (design C1) — the resume design deepens reliance on id stability.
5. **Expected post-resume reconcile/understatement warnings** after a gap (design C2) — healthy behavior, do not roll back on these alone.

## 8. Upgrade + resume runbook (from design §10, for reference)

1. Pull the new image (`/docker/pull-image`); confirm it contains `psycopg2`.
2. `/stop-bot` (MQTT stop — cancels orders; does NOT exit the container).
3. `/docker/stop-container/{name}` and confirm exited (only now is the ledger quiesced, single-owner).
4. (Recommended) verify the exchange is flat; cancel any resting orders.
5. Deploy new instance: new `image:`, same `controllers_config` (same `id:`), `resume_mode: explicit`, `resume_from: <stopped instance name>`.
6. Hook runs inside `create_hummingbot_instance`: resolve → guards → config-derived copy → drift diff → manifest → container starts.
7. New engine boots → `_load_state()` finds the ledger → ladder resumes; Postgres re-attaches positions/PnL by `controller_id`.
8. (Optional) archive the old instance only after the new bot is confirmed healthy.

## 9. Key file:line index (for future chats)

- Hook gate / attach point: `services/docker_service.py:308` (gate), `:309` (call), `:371` (`containers.run`).
- Core module: `services/resume_service.py` — `resolve_source:188`, `_resolve_latest:299`, `_pick_newest:401`, `compute_copy_plan:841`, `_plan_controller:673`, `_read_owner_controller_id:648`, `_validate_ledger:621`, `_is_sqlite_deployment:580`, `_plan_extra_paths:807`, `run_guards:1154`, `_guard_source_container:1008`, `_execute_copy_plan:1248`, `_seed:1401`, `seed_resume_state:1462`, `preview_resume:1537`.
- Router: `routers/bot_orchestration.py` — deploy paths await at `:541`/`:604`, `ResumeError→409`, `resume-preview` endpoint.
- Models: `models/bot_orchestration.py` — resume fields on `V2ControllerDeployment` / `V2ScriptDeployment`.
- Engine ground truth: `controllers/market_making/range_inventory_ladder.py` — `state_path:1469`, `.owner` writer `:1975-2018` (`controller_id` at `:2009`), atomic `_save_state:2020`.
- Test command: `C:/anaconda3/envs/hummingbot/python.exe -m pytest tests/ --ignore=tests/test_nonkyc_live_api.py -q --tb=short`
- Baseline: `test_logs/copyforward_baseline_failures.txt` (14 frozen pre-existing failures).
