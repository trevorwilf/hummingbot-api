# hbtriage_api — batch prompt (Run B: api repo)

Repo: `E:/tradingsoftware/hummingbot-api` · Base branch: **`nonkyc`** · 9 implementation phases + finalization (phase 10).
Scope authority: `E:/claude/advesarial_reviews/reports/scope_triage.md` (**SOLE authority** — 14 IN SCOPE / 9 OUT OF SCOPE; where any other report disagrees, the triage wins).
Evidence (read-only detail): `REPORT_hb_predeploy.md` (synthesis), `CODEX_FINDINGS_*`, `CLAUDE_FINDINGS_*`, `CODEX_VS_CLAUDE_*`, `CLAUDE_VS_CODEX_*` in the same folder.
This run implements the API-repo share of the triage: Batches 1–3 and CDX-013. The engine-repo share (Run A: CDX-002, engine halves of C1/C2, CDX-009, CDX-010, doc corrections) already ran; a human docker-rebuild gate (CDX-002 runtime==image verification) sits between the runs by design.

## Batch execution mode

- Each step is a separate headless invocation with a FRESH context. You are told ONE step of ONE phase; do ONLY that.
- Re-read this prompt and `scope_triage.md` from disk at the start of every step — prior conversation is gone.
- Implementation phases commit on their own branch; they do NOT push. Merging happens only in step C (adjudication), `--no-ff` into `nonkyc`.
- The final phase (10) is finalization: test the base branch, write the report. No review step.
- On unfixable failure (after the 5-attempt protocol) your FINAL MESSAGE must begin `BATCH_HALT phase=<N> reason=` and you must stop. Do not write halt files yourself; the runner owns them. Do not "pause to notify".
- Runner-owned artifacts are NOT project files: `batch_logs/`, `batch_reviews/`, `BATCH_HALT_*.md`, this prompt, the runner `.ps1`, and `REPORT_hbtriage_api.md` are gitignored and must NEVER be committed. Never use `git add -A` or `git add .` — stage exactly the files your phase changed.

## Absolute prohibitions (binding on every step)

1. **NO DOCKER, in any form** — no `docker`/`docker compose` `build|config|run|pull|exec|stop|rm|up|down|images|inspect` or any docker-adjacent command. Live trading system. All docker work is the human's.
2. **Do NOT implement any OUT OF SCOPE finding**: CDX-003, CDX-011/CDX-M01, CDX-012, CDX-014/CLA-005, CLA-006, CDX-004, CLA-001, CLA-003, CLA-007. If a phase notices one, record it in your final message; never fix it.
3. **Do NOT apply the TODO's P1 fix `str(owner_id) != str(controller_id)`** — both review engines independently called it unsound; the triage prohibits it. It is a trap left in the doc.
4. **Do NOT touch `E:/tradingsoftware/dockerscripts/`** or any path outside `E:/tradingsoftware/hummingbot-api` (writes) and `E:/tradingsoftware/hummingbot` + `E:/claude/advesarial_reviews/reports/` (reads only).
5. **This run's repo is `E:/tradingsoftware/hummingbot-api` ONLY.** You may READ the engine repo for contract mirroring; you may never write to it.
6. Never cancel, modify, or place an order. Never touch a running container. Never read or print secrets: `.env`, keys, tokens, connector credentials. `DOCKER_BOT_NETWORK_MODE` and `DEV_VAR` live-value reads are the human's.
7. **Fail closed on uncertainty.** Several in-scope fixes exist because code chose fail-open (CDX-007, CDX-008). Never weaken a fail-closed default to make a test pass. Existing `STOPPED` rows are UNVERIFIED (CDX-005) — never treat them as evidence of clean retirement.
8. **The live `resume-preview` dry-run against a real stopped instance is the HUMAN's, by hand.** It is the only thing that closes the mock/reality gap. Do not fake it, mock it, or claim it was done; it is listed as an open obligation in the final report.

## Shared cross-repo contracts (verbatim in both run prompts — implement EXACTLY this)

Two findings span both repos and cannot ship atomically. Each side must be independently fail-closed so whichever lands first is still safe.

### CONTRACT C1 — `state_file_name` path contract (CDX-007 / CLA-004)

- **ACCEPT:** unset/None; or a `str` whose stripped value is non-empty and, parsed as BOTH `PurePosixPath` and `PureWindowsPath`: `is_absolute()` is False, has no drive and no root/anchor, contains no `..` component, is not `.`, and its POSIX normalization remains a strict descendant of `data/` when joined (lexical check — no filesystem access needed at validation time).
- **REJECT (fail-closed):** absolute POSIX or Windows paths, drive letters, UNC paths, any `..` component, `.` — UNLESS an explicit opt-out boolean (`allow_absolute_state_file_name`, default False) is set, which permits ABSOLUTE paths only, never traversal.
- **Canonical value:** the stripped string. Empty-after-strip maps to unset/None (default behavior), which is not fail-open: None selects the default state file name.
- Engine half (Run A, done): enforced at controller-config validation in `range_inventory_ladder.py` plus a runtime containment assertion. API half (this run, phase 2): enforce the same accept/reject set in CFH planning and remove the absolute-skip success path.

### CONTRACT C2 — controller `id` contract (CDX-008 / CLA-002)

- **ACCEPT:** a `str` whose stripped length is >= 1. Canonical id = the stripped value; all identity derivations (ledger filename, `.owner` match) use the canonical id.
- **REJECT:** non-`str` (int, bool, None — Pydantic v2 already rejects these engine-side; preserve that), `""`, whitespace-only.
- Engine half (Run A, done): `ControllerConfigBase.id` has `min_length=1` plus a strip validator. API half (this run, phase 3): `resume_service.py` ABORTS the deploy (never `continue`s) on any staged range-ladder controller violating C2.
- **PROHIBITED on both sides:** the `str(owner_id) != str(controller_id)` comparison fix.

## Comprehensive testing (every phase; exact command)

Run from the repo root (`E:/tradingsoftware/hummingbot-api`):

```bash
mkdir -p batch_logs
C:/anaconda3/envs/hummingbot/python.exe -m pytest tests/ --ignore=tests/test_nonkyc_live_api.py -q > batch_logs/<phase>_<step>_pytest.log 2>&1
```

- **Baseline reality — the claimed baseline is UNCERTAIN.** `test_logs/copyforward_baseline_failures.txt` claims 14 failed / 232 passed, but NO review phase ever ran pytest; every test-count claim in the CFH docs is unverified. Phase 1 step A captures the REAL baseline (below) before touching anything. No phase may be judged green against a fabricated number.
- **Baseline protocol:** immediately after creating your phase branch and BEFORE any edit, run the suite once into `batch_logs/<phase>_pre.log`. "Green" for this phase = no test that passed pre-change now fails, and every test this phase added or deliberately changed passes. A pre-existing failure recorded in the pre-log is NOT your regression — do not "fix" it and never weaken anything to route around it. Exception: if your phase's spec deliberately changes behavior an existing test asserts (e.g. skip→abort semantics), update that test to the new spec and say so in your final message.
- **Token discipline (mandatory):** never stream full test output into the conversation. Redirect to `batch_logs/` and read back only `tail -30` plus `grep -E "FAILED|ERROR|passed|failed"`; pull individual tracebacks with targeted `grep -B2 -A15`.
- **Failure protocol:** on failure, ultrathink; up to 5 fix attempts. Iterate cheaply inside an attempt with `pytest --lf` or explicit node ids; an attempt is judged ONLY by re-running the full suite. After 5 failed attempts, stop: FINAL MESSAGE begins `BATCH_HALT phase=<N> reason=`.
- **Test authenticity:** every existing CFH hook test is mocked at the unit-under-test level (`tests/test_copyforward_e2e.py:176-182`, `tests/test_copyforward_hook.py:138-143`). Do NOT extend that pattern. New tests must exercise the real logic (real tmp_path filesystems, real in-memory DB sessions); mock only unavoidable externals (the docker daemon). Expected values come from the SPEC, never from running your own implementation. Every new test must fail when the fix it covers is reverted — the reviewer will name mutations and you will have to run them.

## Cross-family review (how a phase actually lands)

Every implementation phase runs three fresh-context calls:
- **Step A — AUTHOR (Claude family, write):** implement the phase on `fix/hbtriage-p<N>-<slug>` off `nonkyc`, tests green, COMMIT, do not merge, do not push.
- **Step B — REVIEWER (GPT-5.6 sol, codex family, STRICTLY READ-ONLY):** interrogates the branch diff (inlined by the runner) using the Review schema and TEST INTEGRITY AUDIT below. An opposing-family engine WILL interrogate your diff — write code and tests expecting hostile reading. Reviewer: you may not modify anything; a git tripwire aborts the run if the tree moves.
- **Step C — AUTHOR adjudicates (write):** answer EVERY finding with the Adjudication schema below; REJECT only with file:line evidence; run every named mutation experiment; re-run the full suite green on unmutated code; merge `--no-ff` into `nonkyc`.

**Bias note (applies to adjudication and the final report):** the reviewer family (codex) AUTHORED most of these findings (`CDX-*`). It may defend its own finding rather than judge the fix on its merits. Adjudicate on evidence, not deference — and every REJECTED `CDX-*` finding must be flagged for the human in the final report.

## Review schema (step B — use verbatim)

The reviewer judges ONLY this phase's branch diff plus read-only context. Each finding is a block:
- `ID` — `CDX-R01`, `CDX-R02`, ... (codex reviewer prefix, zero-padded).
- `Title` — one line.
- `Category` — correctness | concurrency | security | data-loss | api-contract | logic | resource-leak | performance | config | error-handling | edge-case | test-theater | test-gap | spec-conformance | other.
- `Severity` — Critical | High | Medium | Low | Info. · `Confidence` — High | Medium | Low.
- `Location` — `file:line` (+ symbol).
- `Symptoms / observable behavior` — what an engineer would SEE. Concrete.
- `Root-cause analysis` — mechanism + trigger conditions.
- `Validation / reproduction steps` — exact steps: inputs/state, command or code path, expected-vs-actual, reproducible by the author without you.
- `Proposed resolution` — fix, trade-offs, regression risk.
- `Evidence` — minimal quoted line(s) with `file:line`.
Then two sections: `Spec conformance` (does the diff implement phase N as specified here, including the shared contracts C1/C2 where relevant?) and `Test adequacy` (verdict per added/changed test — see audit below).
Rules: default to skepticism but do NOT invent findings — an empty report is valid ("no findings above the bar"). Medium+ only, unless a Low is a genuine latent bug. Never pure style. Every claim checkable at a cited `file:line`. You may NOT modify anything.

## TEST INTEGRITY AUDIT (step B — mandatory, highest-value job)

The phase arrived with passing tests. **Passing tests are evidence the tests ran, not that the code works.** For EVERY added or changed test, answer: *"If I broke the exact behavior this test claims to verify, would THIS test fail?"* If no — or you cannot tell — file a `test-theater` finding and name the **exact single-line mutation** to the IMPLEMENTATION (`file:line` — flip a comparison, delete a guard, return a constant, skip a persist) that the test should catch. You are read-only: reason statically and hand the author a precise, executable experiment. "Break it somehow" is useless.

Hunt these patterns explicitly: asserts nothing (passes if nothing raises) · tests the mock, not the code (unit under test patched; asserts only on `mock.return_value`/`assert_called_with`) · over-mocked so no real path executes · cannot-fail assertions (`is not None` on always-object, `len>=0`, self-comparison) · vacuous input (empty fixture never exercises the changed branch) · golden-output change-detector (expected values captured by RUNNING the implementation, not derived from the SPEC — check every expected value against the spec) · assertion weaker than the spec · disabled in place (skip/xfail/early return/commented asserts/`try/except: pass`) · wrong subject · accidental pass (ordering/timing/shared state) · happy path only (spec defines error/fail-closed behavior; only success tested — that is `test-gap`).

**Known context for this codebase:** `tests/test_copyforward_e2e.py:176-182` and `tests/test_copyforward_hook.py:138-143` mock the unit under test — the documented smoke-and-mirrors pattern in this repo. Treat any NEW test that repeats it as `test-theater` on sight. Severity follows the un-covered code: a vacuous test over money, data-loss, or fail-closed logic is High or Critical, never Low. In `Test adequacy`, give a verdict for EVERY added/changed test: PASS (would fail under the named mutation) or FAIL (test-theater), with the mutation stated.

## Adjudication schema (step C — answer every finding)

Per finding: `Ref` · `Decision` (exactly one):
- `ACCEPTED-FIXED` — you agree; fixed properly (not a band-aid), with covering test. State commit + change.
- `REJECTED` — evidence-based rationale with `file:line` PROVING the reviewer wrong. "I prefer my version" is not a rationale. Do not cave to confident tone; do not dismiss what you cannot refute.
- `DEFERRED-OUT-OF-SCOPE` — real, but in code this phase did not touch. Do NOT fix here; record for the final report.
Plus `Rationale`, `Change` (for fixes), `Mutation evidence`, `Test evidence`.
**Mutation evidence is REQUIRED for every `test-theater`/`test-gap` finding — run the experiment, never argue:** apply the reviewer's named single-line mutation, run that test, record the outcome, REVERT. Test FAILED under mutation → the test is real; you may REJECT quoting the failure line. Test PASSED under mutation → reviewer proven right; you may NOT reject; fix the test so it fails under that mutation, revert, confirm green. A mutation is temporary scaffolding: revert it, confirm `git diff` shows zero residue; the FULL suite must be green on UNMUTATED code before merging. Never commit a mutation.
End with `Summary counts` (accepted/rejected/deferred by severity) and `Merge statement` (full suite green, merged `--no-ff` into `nonkyc`).

---

# PHASES

Ordering rationale (binding): phases 1–6 are the CFH fail-closed core and small guards; 7–8 (lifecycle, accounting) may add schema and therefore precede 9 (migrations), which baselines the FINAL schema. CDX-001 + CLA-008 P1 ship together in phase 1 — Codex called them entangled, not a preview nit; P1 is what makes `resume-preview` a genuine pre-deploy check.

## Phase 1 — CDX-001 + CLA-008 P1: exclusive target creation + real-path preview (High; entangled) — branch `fix/hbtriage-p1-exclusive-target`

**Files:** `services/resume_service.py` (cleanup :1368-1379, preview synthetic destination :1616-1627, guards :1046-1071), `services/docker_service.py` (dir reuse :220, conf rmtree/recopy :229-233, hook attach :308-315), `models/bot_orchestration.py` (second-granular names :503-506). Evidence: REPORT §4.3, §5.5.

**Step A:**
0. **REAL BASELINE FIRST (before any edit, on the fresh branch):** run the suite into `batch_logs/api_baseline_initial.log`; extract the failure list to `batch_logs/api_baseline_failures.txt`; diff against the claimed `test_logs/copyforward_baseline_failures.txt` (14F/232P) and write the discrepancy summary to `batch_logs/baseline_discrepancy.md`. State the real numbers in your final message — finalization must carry them into the report. Later phases use their own `<phase>_pre.log`; this initial capture is the canonical record that the claimed baseline was checked.
1. **Exclusive creation:** an existing target instance directory is never reused and never deleted. Replace the `:220` reuse path with exclusive semantics (`os.makedirs(..., exist_ok=False)` or equivalent); an existing dir → `ResumeError`/409 `DEST_EXISTS`. The `:229-233` rmtree/recopy of an existing `conf/` goes away with it.
2. **Stage + atomic promote (the triage's preferred design):** build the instance in a unique temp sibling (`<target>.staging-<random>`) created exclusively by this attempt; run conf staging and `seed_resume_state` against the staging dir; on success promote atomically (`Path.replace`/`os.rename`) — promote failure because target appeared meanwhile = 409, staging cleaned. Track `created_by_this_attempt`: `_cleanup_failed_instance` (:1368-1379) may remove ONLY the staging path this attempt created — never a pre-existing target, never anything it did not create.
3. **Name uniqueness:** add a sub-second + random component to generated instance names (:503-506) so two deploys of the same base name within one second cannot collide. First grep for anything that parses the name/timestamp format back; preserve prefix conventions.
4. **Per-instance deploy lock:** a module-level asyncio lock registry keyed by target instance name, held across create+seed+promote, so concurrent deploys of the same name serialize.
5. **CLA-008 P1 — preview validates the REAL target path:** preview (:1616-1627) must compute the same candidate target the deploy path would use and run the SAME guard set against real filesystem state (existence/collision, C1 containment where applicable, source guards) WITHOUT creating or mutating anything, and include the resolved target path + per-guard results in the preview response.
6. Tests (spec-derived, real tmp_path filesystems; mock only the docker client): (a) pre-existing target with `data/sentinel.json` + `logs/sentinel.log`, deploy same name → 409 AND both sentinels survive byte-identical; (b) forced `ResumeError` mid-seed → staging dir removed, pre-existing sibling dirs untouched; (c) frozen clock, two generated names differ; (d) preview of a colliding name reports the collision and mutates nothing; (e) lock: two concurrent deploys of one name — exactly one proceeds.

**Step B — review focus:** any residual path where `_cleanup_failed_instance` can reach a directory this attempt did not create; promote-time race (target appears between check and rename); whether preview truly shares the deploy guard code or reimplements a lookalike; sentinel-survival test integrity (mutation: restore the `if new_instance_dir.exists(): shutil.rmtree(...)` body).

## Phase 2 — CDX-007/CLA-004 api half: C1 path contract, absolute-skip removed (High) — branch `fix/hbtriage-p2-path-contract`

**Files:** `services/resume_service.py:684-693` (the skip-and-proceed), plus wherever state-file paths are planned/copied. Evidence: REPORT §4.6.

**Step A:** implement CONTRACT C1 exactly, in one shared api-side predicate (e.g. a small module function used by planning and any model validation). Remove the absolute-skip success path: a `state_file_name` violating C1 → `ResumeError` (409-mapped) ABORTING the deploy — fail-closed — unless the deploy request carries `allow_absolute_state_file_name: true`, in which case absolute (never traversal) paths take the old skip path WITH a structured warning surfaced in the response. At plan/copy time, resolve the concrete destination and assert containment in the instance `data/` dir (runtime check) — violation aborts. Tests (spec-derived): `/tmp/x.json` → abort 409; `../conf/x.yml` → abort even with opt-out; `C:\\x.json`, `\\\\share\\x` → abort; `sub/x.json` → planned normally; opt-out + absolute → skipped with warning present in response. Update any existing test asserting the old skip semantics (spec change — say so).

**Step B — review focus:** C1 conformance both directions; that the opt-out cannot be reached by default; that the runtime containment check fires on symlink-style escapes; mutation for the abort test: restore the `:684-693` skip body.

## Phase 3 — CDX-008/CLA-002 api half: C2 id contract, abort not skip (High) — branch `fix/hbtriage-p3-controller-id`

**File:** `services/resume_service.py:874` (`if not controller_id: ... continue`). Evidence: REPORT §4.7.

**Step A:** implement CONTRACT C2 exactly: for every staged range-ladder controller, `id` must be a `str` with stripped length >= 1; violation (non-str — note the current falsy check silently drops `id: 0` too — empty, whitespace-only) → `ResumeError` ABORTING the whole deploy, never `continue`. Canonical id = stripped value, used for all downstream identity (ledger filename, `.owner` match, `deployed_ids`). **PROHIBITED:** `str(owner_id) != str(controller_id)`. Tests (spec-derived): staged config with `id: ""` → deploy aborts (assert the raise/409 and that NO partial plan was produced); `id: "   "` → aborts; `id: 0` → aborts; `id: " abc "` → planned under `"abc"`. Update existing tests that assumed skip semantics (spec change — say so). Runbook note for your final message: configs with empty/whitespace ids are now rejected at BOTH layers (engine rejected them in Run A; the API now aborts) — deliberate, desirable break.

**Step B — review focus:** abort actually aborts (no partial `deployed_ids` mutation before the raise); strip-canonicalization consistent with the engine half (C2); mutation: restore the `continue`.

## Phase 4 — CDX-M02: ledger envelope validation (High) — branch `fix/hbtriage-p4-ledger-envelope`

**File:** `services/resume_service.py:621-645` (`_validate_ledger` — currently length + UTF-8 + `json.loads` only). Evidence: REPORT §6 CDX-M02.

**Step A:** replace syntax-only validation with a versioned envelope validator enforcing the ENGINE's ledger contract. Derive the required header by READING the engine repo (read-only): `E:/tradingsoftware/hummingbot/controllers/market_making/range_inventory_ladder.py` — the load-time validation around :1796-1826, `STATE_SCHEMA_VERSION`/`SUPPORTED_STATE_SCHEMA_VERSIONS` at :1232-1233 (currently {6..10}). Requirements: top-level must be a dict; `schema_version` must be an int in a mirrored `SUPPORTED_LEDGER_SCHEMA_VERSIONS` constant (comment citing the engine source line; keeping it in sync is a recorded report obligation); required header keys per the engine contract (controller name/type/id, connector, pair, initialized flag, the numeric fields the engine requires); the ledger's INTERNAL controller id must equal the staged controller's canonical id. Any missing key, wrong type, unsupported version, identity mismatch, or other uncertainty → `LEDGER_INVALID` (existing fail-closed semantics). Tests (fixtures derived from the ENGINE's validation code — the spec — never from running the hook): `{}`, `[]`, `null`, missing `schema_version`, version 5, version 11, internal id mismatch → `LEDGER_INVALID`; a minimal valid v10 fixture → passes. Mutation the reviewer should consider: delete the version-membership check.

**Step B — review focus:** completeness of the mirrored contract vs the engine's actual checks (read the engine file yourself, read-only); that the valid-v10 fixture was derived from the engine spec, not captured from the implementation; identity comparison uses canonical (stripped) ids consistent with C2.

## Phase 5 — Three small guards: CDX-015, CLA-008#7, CLA-008 P2 (Medium) — branch `fix/hbtriage-p5-small-guards`

**Files:** `services/resume_service.py` :599-600, :695-739, :1008-1029. Evidence: REPORT §4.12, §5.5.

**Step A:**
1. **CDX-015 (db_engine):** normalize `str(engine).strip().casefold()`. `None` → sqlite (preserve the documented default); `"sqlite"` → sqlite; `startswith("postgres")` → non-sqlite; ANY other value → `ResumeError` abort — never silently non-sqlite. Tests: `"SQLite"`, `" sqlite "` → sqlite; `"postgresql+asyncpg"` → non-sqlite; `"mysql"`, `"garbage"` → abort. Mutation: revert to exact `== "sqlite"`.
2. **CLA-008 #7 (nested owner sidecar):** in :695-739, the `.owner` currently flattens to `new_data_dir / src_owner.name` while the ledger preserves its relative path (:738-739). Preserve the SAME relative path for the owner. Test: nested `sub/dir/ledger.json` + `.owner` → both land adjacent at the same relative destination. Mutation: restore the flatten.
3. **CLA-008 P2 (`_guard_source_container`):** :1008-1029 catches only `NotFound`; any other Docker error becomes an opaque 500. Map Docker API/connection errors to a clean `ResumeError`/409 "source container state could not be verified" — fail-closed refuse, never proceed-on-error. `NotFound` keeps its current meaning. Test: mocked docker client raising `APIError` → 409 with that reason (mocking the docker client is legitimate here — the unit under test is the error mapping).

**Step B — review focus:** the unknown-engine abort (fail-closed, not fail-open); owner/ledger destination parity; that the P2 mapping cannot swallow programming errors (only docker-client exceptions map to 409).

## Phase 6 — CLA-M02 + CLA-M01: path-coupling assertion + drift surfacing (Med) — branch `fix/hbtriage-p6-coupling-drift`

**Files:** `services/docker_service.py` (:212-214, :311-312, :322 — CWD-relative `bots/` vs `BOTS_PATH`-derived mount source), `services/resume_service.py:1291-1347` (`_diff_controller_configs` warns in logs only). Evidence: REPORT §6.

**Step A:**
1. **CLA-M02:** make the undocumented coupling explicit and checked. At deploy time, assert that the host path derived from `os.environ['BOTS_PATH']` (+`/bots`) and the hook's write root (`/hummingbot-api/bots`, i.e. CWD-relative `bots/`) refer to the same directory: inspect the API's OWN container mounts via the docker client (destination `/hummingbot-api/bots` → its source must equal `BOTS_PATH + '/bots'`). Proven mismatch → abort the deploy with a clear error naming both paths (fail-closed on proven danger). Self-inspection unavailable (not in a container / test env) → structured warning in the deploy response naming both paths (do not brick dev/test environments over assertion machinery). Tests: mocked self-inspection showing mismatch → abort; match → proceeds; unavailable → warning surfaced.
2. **CLA-M01 (louder warning ONLY — template-wins stays):** in `_diff_controller_configs`, classify drift in sizing-critical fields — `total_amount_quote`, `max_fund_value_quote`, the range bound fields, `claimed_base_*` (enumerate the exact field names by reading the engine's ladder config, read-only) — and surface those as structured warning entries (field, source value, template value) in BOTH the preview and deploy responses, not only logs. No behavior change to template-wins. Tests: sizing-critical drift → warning entry present in the response object; unrelated-field drift → not flagged sizing-critical. Mutation: drop the response surfacing, keep the log line.

**Step B — review focus:** the mismatch→abort vs unavailable→warn boundary (is "unavailable" tightly defined, or can a real mismatch masquerade as unavailable?); that CLA-M01 changed no copy/template semantics; response-surfacing test integrity.

## Phase 7 — CDX-005 / CDX-M03: acknowledged-retirement state machine (High; FULL fix) — branch `fix/hbtriage-p7-retirement-fsm`

**Files:** `services/bots_orchestrator.py:641-665` (STOPPED written at :643 BEFORE the MQTT stop at :652 and the fixed 15s sleep at :665), `database/repositories/bot_run_repository.py:64-65`, `services/resume_service.py:1108-1134` (CFH graceful-source predicate :1124-1126), `models/bot_orchestration.py:104` (`skip_order_cancellation` default True), `services/mqtt_manager.py:426-465` (publish success ≠ acknowledgement). Evidence: REPORT §4.4. Schema additions are permitted (phase 9 baselines the final schema afterward).

**Step A — the spec (design freedom inside these hard requirements):**
1. Replace trust-in-STOPPED with an acknowledged retirement state machine persisting evidence per stage: strategy quiescence → cancellation requested → exchange-confirmed zero open orders → final-fill drain → state flush/checkpoint → process/container exit → archive. Persist a verified-STOPPED terminal state ONLY after all postconditions hold, each with its own persisted timestamp/evidence field.
2. Evidence must come from confirmed data (the orchestrator's own order/bot state, bot response messages) — never from MQTT publish success (:426-465 proves only broker publication). Where confirmation is impossible (bot dead, MQTT silent, timeout), persist UNVERIFIED — never fabricate, never default to verified. Replace the fixed 15s sleep with bounded polling + configurable timeout; timeout → UNVERIFIED (archival still allowed, but the run is not verified-retired).
3. `skip_order_cancellation` default stays (out of the triage's named change), but its use is recorded in the evidence; a run stopped with it set cannot reach exchange-confirmed-zero unless confirmation was obtained independently.
4. CFH predicate: `resume_service.py:1124-1126` must require the verified-retirement evidence, not the legacy two fields. Existing STOPPED rows (predating the evidence schema) are UNVERIFIED → CFH refuses with a clear reason; if an explicit human-override field already exists in the request flow it may extend to this refusal, default refuse.
5. Tests (spec-derived; real in-memory DB where persistence is involved): each postcondition gate (missing evidence X → not verified); legacy STOPPED row → CFH refuses; fully-evidenced row → CFH proceeds; timeout path → UNVERIFIED; ordering (verified-STOPPED cannot be persisted before exchange-confirmed-zero). Reviewer will name mutations like "skip persisting the zero-open-orders confirmation" — write tests that catch them.

**Step B — review focus (this is a money phase — maximum skepticism):** any path that still writes STOPPED early (:643's callers); evidence fabricated from publish success; the legacy-row refusal actually reachable in the CFH flow; schema changes coherent for phase 9's baseline; full TEST INTEGRITY AUDIT with named mutations per gate.

## Phase 8 — CDX-006: fills accounting — insert-first + VWAP (High) — branch `fix/hbtriage-p8-fill-accounting`

**Files:** `database/repositories/order_repository.py:49-68` (aggregates mutated, `average_fill_price = latest` at :52-54, FILLED flip :67-68), `services/orders_recorder.py:245-285` (aggregate mutation before trade dedup), `database/repositories/trade_repository.py:22-27` (duplicate → `None`, no rollback). Evidence: REPORT §4.5. Schema additions permitted (before phase 9).

**Step A:**
1. **Insert the immutable fill/trade FIRST** with a DB unique constraint scoped (account, connector, exchange_trade_id); use dialect-appropriate `INSERT ... ON CONFLICT DO NOTHING RETURNING` (SQLAlchemy pg + sqlite dialects both support it) or an equivalent IntegrityError-safe insert — IN THE SAME transaction/session as the aggregate update.
2. Mutate order aggregates ONLY when the insert actually inserted (returned a row). Duplicate delivery (same scoped `exchange_trade_id`) → no aggregate mutation, log the dedup. On any uncertainty whether the trade is a duplicate → do NOT mutate aggregates (fail-closed for money).
3. **VWAP:** replace latest-price with incremental VWAP: `new_avg = (prev_avg*prev_filled + fill_price*fill_amount) / (prev_filled + fill_amount)`, Decimal arithmetic, stored per current schema types. Status flips to FILLED only from post-insert aggregates.
4. Tests (real in-memory sqlite session — NOT mocked repositories): (a) replay the same scoped `exchange_trade_id` twice → exactly one trade row, aggregates counted once (mutation: drop the conflict guard); (b) VWAP: fills 1@100 then 1@200 → `average_fill_price == 150` (spec-derived — the OLD code gives 200, so this test fails on revert by construction); (c) same `exchange_trade_id`, different account → both recorded (scoping); (d) forced failure between insert and aggregate update → transaction leaves no partial state.

**Step B — review focus:** transactionality (same session? can a crash leave trade-without-aggregate or vice versa?); the uniqueness scope exactly (account, connector, exchange_trade_id); Decimal/float boundary handling; test (a)/(b) mutation-provability.

## Phase 9 — CDX-013: versioned migrations; fatal migration state; dead DROPs removed (Med-High; FULL) — branch `fix/hbtriage-p9-migrations`

**Files:** `database/connection.py` (`create_all` + ad-hoc `ALTER` :61-97 + `_drop_hummingbot_tables` :99-112), new `alembic.ini` + `alembic/` scaffold. Alembic 1.18.4 is already in the env. Evidence: REPORT §5.3; triage correction: the three dropped table names (`hummingbot_orders`/`hummingbot_trade_fills`/`hummingbot_order_status`) match NOTHING in either repo — they are no-ops that don't do what they claim.

**Step A:**
1. Scaffold alembic (ini + `env.py` wired to the app's metadata + async engine as appropriate) with ONE baseline revision capturing the CURRENT models (including phases 7–8 schema) and able to build the full schema from empty.
2. `connection.py`: remove `create_all`, the hand-written ALTERs, and `_drop_hummingbot_tables` from ordinary startup. Startup instead verifies `alembic current == head`; version table absent, behind head, or check failure → FATAL: raise and refuse to serve, with an error message naming the exact runbook commands (`alembic stamp <baseline-rev>` for pre-existing production DBs; `alembic upgrade head` otherwise).
3. READ-ONLY cross-check: the engine repo's stacks skip migration when `alembic.ini` is absent (`no vpn:320-326` / `vpn:441-446`) — verify your scaffold's paths/commands are compatible with that migrate service's invocation so it starts running real migrations after the next image build. If incompatible, note the exact mismatch in your final message for the engine-side report; do NOT edit the engine repo.
4. **RUNBOOK (loud, in your final message):** existing deployed DBs have no `alembic_version` table — after this change the API will REFUSE to start until the one-time `alembic stamp` is run. Deliberate fail-closed. Exact commands included.
5. Tests: empty sqlite → `upgrade head` builds the schema; startup check raises on absent version table; passes at head; `alembic autogenerate` diff against models is empty (schema parity); `_drop_hummingbot_tables` gone and unreferenced. Mutation: make the startup check pass when the version table is absent.

**Step B — review focus:** does the baseline revision REALLY match the final models (parity test integrity); is the fatal check enforced on the actual startup path (not a helper nobody calls); async-engine handling in `env.py`; the stamp-vs-upgrade runbook correctness.

## Phase 10 — Finalization (no review step)

Run the full suite on `nonkyc`; judge green against `batch_logs/api_baseline_initial.log` (phase 1's real baseline). If green, write `REPORT_hbtriage_api.md` in the repo root (do NOT commit it), containing:
- **Review ledger** from every record in `batch_reviews/`: per phase — findings raised / accepted+fixed / rejected (verbatim rationale) / deferred.
- **REQUIRES HUMAN ARBITRATION:** every REJECTED Critical/High, every DEFERRED finding, any test-theater finding rejected without mutation evidence (protocol violation), and every REJECTED `CDX-*` finding flagged per the bias note.
- **Test integrity:** every test-theater finding and its mutation-experiment outcome.
- **Baseline reality:** the real initial pytest numbers vs the claimed 14F/232P, from `batch_logs/baseline_discrepancy.md`.
- **Runbook notes:** (1) CDX-008: configs with empty/whitespace `id` are now rejected at both layers — check live conf trees; (2) CDX-013: one-time `alembic stamp` REQUIRED before the next API boot on existing DBs — exact commands; (3) CDX-005: legacy STOPPED rows are UNVERIFIED and CFH now refuses them by default — expected operational impact; (4) cross-repo contract status: C1/C2 now live on BOTH sides (engine half landed in Run A).
- **Open obligations:** the live `resume-preview` dry-run against a real stopped instance (HUMAN, by hand — the only thing that closes the mock/reality gap; never claim it was done); docker rebuild + CDX-002 runtime==image verification (human); keeping the mirrored `SUPPORTED_LEDGER_SCHEMA_VERSIONS` constant in sync with the engine.
- Bias note verbatim: the reviewer family (codex) authored the `CDX-*` findings and may have defended them; rejected `CDX-*` findings deserve extra human scrutiny.
No integration merge. No push. Stay on `nonkyc`.
