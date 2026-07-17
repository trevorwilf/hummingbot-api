# hbtriage_api — Final Implementation Report

**Run:** hbtriage_api (API repo share, Batches 1–3 + CDX-013)
**Repo:** `E:/tradingsoftware/hummingbot-api` · **Branch:** `nonkyc`
**Phases completed:** 1–9 (9 implementation phases + finalization)
**Date finalized:** 2026-07-16
**Author family:** Claude (Opus 4.8) · **Reviewer family:** GPT-5.6 sol (codex)

---

> **Bias note (carry into every read of this document):** The reviewer family (codex) authored
> most of the `CDX-*` findings. When a codex reviewer grades a Claude-authored fix to a
> `CDX-*` finding, it may defend its own finding rather than judge the fix on its merits.
> Conversely, when a codex reviewer clears a `CLA-*` phase (the CLA findings were authored by
> Claude) with no findings, that verdict deserves an extra skeptical read.
> Every REJECTED `CDX-*` finding in this document is flagged for human arbitration on those grounds.

---

## 1. BASELINE REALITY

**Claimed baseline** (from `test_logs/copyforward_baseline_failures.txt`): 14 failed / 232 passed

**Real baseline** (captured in phase 1 step A, `batch_logs/api_baseline_initial.log`, BEFORE any edit,
on `nonkyc` at commit `b3aad36`):

| | Failed | Passed |
|---|---|---|
| **Real** | **14** | **246** |
| Claimed | 14 | 232 |

**Verdict:** The failure _count and identities_ matched exactly (zero discrepancy in the failure
set). The pass count in the claim was wrong: 232 vs 246 actual. The uncertainty the scope triage
flagged was real — no review phase had run pytest — but the failure set turned out to be correct
despite never having been checked.

**Phase 10 final suite** (`batch_logs/p10_final_pytest.log`, on `nonkyc` unmutated):
**14 failed / 748 passed** — the 14 failures are byte-identical to the baseline set.

**Pre-existing failing tests (NOT regressions; never touched):**

```
tests/test_auth.py::TestAuthDebugMode::test_auth_passes_correct_credentials
tests/test_auth.py::TestAuthDebugMode::test_auth_rejects_bad_credentials_when_not_debug
tests/test_auth.py::TestAuthDebugMode::test_debug_mode_logs_warning_on_bypass
tests/test_config.py::TestSecuritySettingsEnvPrefix::test_explicit_env_vars_still_work
tests/test_docker_service.py::TestDockerServiceVPNNetworkMode::test_create_instance_uses_dynamic_network_mode
tests/test_health_endpoint.py::TestHealthEndpoint::test_health_endpoint_exists
tests/test_health_endpoint.py::TestHealthEndpoint::test_health_no_auth_required
tests/test_health_endpoint.py::TestHealthEndpoint::test_health_returns_components
tests/test_nonkyc_connector_unit.py::TestNonKYCAuth::test_generate_auth_dict_get
tests/test_nonkyc_connector_unit.py::TestNonKYCAuth::test_generate_auth_dict_post
tests/test_phase3_hardening.py::TestFix1StatusPollingTask::test_status_polling_started
tests/test_unified_connector_service.py::TestStartConnectorNetwork::test_all_six_tasks_started
tests/test_unified_connector_service.py::TestStartConnectorNetwork::test_status_polling_task_is_started
tests/test_unified_connector_service.py::TestStartConnectorNetwork::test_stop_cleans_up_status_polling
```

Green criterion met: no test that passed at baseline now fails; every phase-added test passes;
zero pre-existing failures were masked or routed around.

---

## 2. REVIEW LEDGER

This ledger is compiled verbatim from `batch_reviews/ADJUDICATION_phase_0N_hbtriage_api.md`.
For each phase: reviewer findings → author decision → rationale summary → mutation evidence.

---

### Phase 1 — CDX-001 + CLA-008 P1: Exclusive target creation + real-path preview

**Findings raised:** 4 · **Accepted-fixed:** 4 · **Rejected:** 0 · **Deferred:** 0
**Phase branch:** `fix/hbtriage-p1-exclusive-target` → merged `--no-ff` into `nonkyc` as `1e06f47`
**Adjudication commit:** `85cc8f4`
**Post-merge suite:** 14 failed / 262 passed

---

#### CDX-R01 — Atomic promotion can replace a target that appears during the promote race
- **Category:** concurrency · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED**
- **Finding:** `docker_service.py:297-304` — POSIX `rename(2)` permits replacing an empty
  destination directory; the existing check-then-rename is not atomic and an operator `mkdir` or
  Docker auto-created bind-mount source could slip between them.
- **Adjudication rationale:** Confirmed against SUSv4 text. Author's comment in the pre-fix code
  explicitly acknowledged the overwrite and rationalized it away. Two reviewer supporting claims
  were overstated (narrow consequence and no realistic competing creator in deploy path), but the
  mechanism is real and the fix is cheap.
- **Fix:** New `_rename_noreplace` primitive using `os.mkdir(dst)` as the atomic no-replace
  reservation on POSIX; `_promote_staging` drops its `exists()` pre-check and maps
  `FileExistsError` → `DEST_EXISTS`.
- **Mutation evidence:** Dropped `os.mkdir(dst)` reservation → `test_rename_noreplace_refuses_an_empty_existing_target`
  and `test_promote_maps_a_racing_target_to_dest_exists` **FAILED**. Reverted; zero residue.
- **Disclosure:** Tests run on Windows (natively no-replace `os.rename`). CDX-R01 fix not executed
  against a real POSIX kernel; gap closes on the human's Linux rebuild.

---

#### CDX-R02 — Preview validates a disposable random target, not the target subsequently used by deploy
- **Category:** spec-conformance · **Severity:** Medium · **Confidence:** High
- **Decision: ACCEPTED-FIXED (in part) — PARTIALLY REJECTED CDX-* FINDING**
- **Finding (part 1, accepted):** `resolve_deploy_target` docstring claimed preview and deploy
  shared it; they did not — deploy re-joined its own path in `docker_service.py:332`.
- **Fix (part 1):** `docker_service.py:402` now calls `resolve_deploy_target`; docstring now true.
- **Finding (part 2, declined):** Reviewer proposed a preview/deploy name handshake to guarantee
  they check the same candidate.
- **Adjudication rationale (declined):** (1) Phase spec prohibits preview from creating or mutating
  anything — a handshake requires reserving the name, which is a mutation. (2) A non-reserving
  handshake cannot close its own TOCTOU window. (3) The collision probability after phase 1 step 3
  added sub-second + random components is negligible by construction.
- **Mutation evidence:** Restored `instance_dir = os.path.join(...)` (skipping shared resolver) →
  `test_deploy_builds_where_resolve_deploy_target_says` **FAILED**. Reverted; zero residue.
- **Flag for human:** The declined portion (non-reserving preview) is a partially-rejected
  `CDX-*` finding. See REQUIRES HUMAN ARBITRATION section.

---

#### CDX-R03 — Failure while creating staging subdirectories leaks the attempt's staging directory
- **Category:** resource-leak · **Severity:** Medium · **Confidence:** High
- **Decision: ACCEPTED-FIXED**
- **Finding:** `_create_staging_dir` created staging root exclusively then `data/` and `logs/`
  outside any try; caller invoked it one line above the `try:` block that would clean up.
  Exceptions from child-dir creation bypassed all cleanup paths.
- **Fix:** Child `makedirs` wrapped in `try/except BaseException` removing the exclusively-created
  root and re-raising.
- **Mutation evidence:** Removed the `try/except BaseException` → leaked staging directory
  detected `[WindowsPath('.../bots/instances/LADDER_BOT-20260714-121212.staging-afa6c061')]`.
  Reverted; zero residue.

---

#### CDX-R04 — Concurrency tests do not prove deploy actually uses the per-instance lock
- **Category:** test-theater · **Severity:** Medium · **Confidence:** High
- **Decision: ACCEPTED-FIXED — REVIEWER PROVEN RIGHT BY EXPERIMENT**
- **Finding:** Pre-existing concurrency tests proved exclusive-create + atomic promote, NOT the
  deploy lock. They passed even with the lock completely disabled.
- **Mutation evidence (mandatory, run first):** `_instance_deploy_lock(instance_name)` →
  `_instance_deploy_lock(f"{instance_name}-{secrets.token_hex(4)}")` (same-name serialization
  disabled) → **4 passed**. Theater confirmed; rejection not permitted.
- **Fix:** New `TestDeployLockIntegration::test_second_same_name_deploy_cannot_enter_staging_while_first_holds_lock`
  parks deploy A inside staging and asserts B never enters staging while A holds the lock.
- **Post-fix mutation:** Same mutation → B entered staging while A held lock, **1 FAILED**.
  Reverted; zero residue.

---

### Phase 2 — CDX-007/CLA-004 API half: C1 path contract, absolute-skip removed

**Findings raised:** 3 · **Accepted-fixed:** 3 · **Rejected:** 0 · **Deferred:** 0
**Phase branch:** `fix/hbtriage-p2-path-contract` → merged `--no-ff` into `nonkyc` as `3cbf68b`
**Adjudication commit:** `2c081de`
**Post-merge suite:** 14 failed / 357 passed

---

#### CDX-R01 — Non-boolean request values can activate the absolute-path opt-out
- **Category:** security · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED**
- **Finding:** `allow_absolute_state_file_name` field used plain `bool`; Pydantic coerces `"true"`,
  `"yes"`, `1`, `"on"` → `True`, allowing stringly-typed clients to disarm a fail-closed money
  guard.
- **Fix:** Field changed to `StrictBool` on both `V2ScriptDeployment` and `V2ControllerDeployment`.
- **Mutation evidence:** `StrictBool` → `bool` on both models → **24 of 30** parametrized
  coercion tests FAILED. Reverted; zero residue.

---

#### CDX-R02 — Containment resolution errors fall back to a lexical path instead of failing closed
- **Category:** security · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED**
- **Finding:** `_assert_contained` caught `OSError` and fell back to `Path(candidate).absolute()`
  — a lexical path proves only where the string points, not where the filesystem points. A
  symlink-based traversal attempt could pass containment as "clean" (lexically), then be classified
  `fresh_seed`, deploying a bot with no ledger.
- **Fix:** Root and candidate resolved in one loop; any `OSError` / `RuntimeError` / `ValueError`
  → `ResumeError(STATE_FILE_PATH_INVALID)`. No lexical fallback.
- **Mutation evidence:** Restored lexical fallback → **4 FAILED** (candidate `OSError`,
  `RuntimeError`, `ValueError`, root `OSError`). Reverted; zero residue.

---

#### CDX-R03 — Both runtime-containment tests can silently skip the security invariant
- **Category:** test-theater · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED (runner has symlink privilege — reviewer proven by substance, not literal theater)**
- **Finding:** `pytest.skip` on no-symlink-privilege meant the security invariant could disappear
  silently on un-elevated Windows or CI.
- **Mutation evidence:** Applied containment guard → `if False:` → **2 FAILED** on this runner
  (has symlink privilege). Tests are real here; literal "test-theater" label does not hold on the
  batch runner. Accepted on substance: a test that opts itself out on the target platform is a
  latent hole.
- **Fix:** `link_dir_out_of_tree` helper in `tests/test_copyforward_copyset.py:42-75` tries
  `os.symlink`; falls back to `_winapi.CreateJunction` (no privilege required); calls
  `pytest.fail` with diagnostic if neither available.
- **Observation from this phase (not fixed here — scope):**
  `tests/test_copyforward_copyset.py:685` (`test_extra_path_escape_via_symlink`) has the same
  skip-on-no-privilege pattern, guarding the `EXTRA_PATH_ESCAPE` invariant. File symlink, not
  directory, so the junction helper does not directly apply. Real, unaddressed — see REQUIRES
  HUMAN ARBITRATION.

---

### Phase 3 — CDX-008/CLA-002 API half: C2 id contract, abort not skip

**Findings raised:** 1 · **Accepted-fixed:** 1 · **Rejected:** 0 · **Deferred:** 0
**Phase branch:** `fix/hbtriage-p3-controller-id` → merged `--no-ff` into `nonkyc` as `6cb78a3`
**Adjudication commit:** test-only commit on phase branch
**Post-merge suite:** 14 failed / 402 passed

---

#### CDX-R01 — Owner-mismatch test cannot detect the prohibited string-coercion regression
- **Category:** test-theater · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED — REVIEWER PROVEN RIGHT BY EXPERIMENT**
- **Finding:** `tests/test_copyforward_p3_controller_id.py:372` claimed to guard against
  the prohibited `str(owner_id) != str(controller_id)` comparison, but compared two strings
  (`"somebody_else"` vs `"ctrl_pad"`) that are unequal under coercion just as under direct
  comparison. Structurally incapable of detecting the regression.
- **Mutation evidence (step 1):** `if owner_id != controller_id:` → `if str(owner_id) != str(controller_id):`
  → named test **1 PASSED**; full suite **14 failed, 401 passed** — not one test caught the
  prohibited coercion.
- **Mutation evidence (step 2, post-fix):** Same mutation → new test **FAILED** with
  `DID NOT RAISE ResumeError`. Reverted; zero residue confirmed by blob hash.
- **Fix:** New test `test_numerically_typed_owner_id_is_a_mismatch`: `.owner` sidecar carrying JSON
  number `123` against canonical controller id `"123"`. Direct comparison fails closed (non-`str`
  sidecar id is unverifiable identity).

---

### Phase 4 — CDX-M02: Ledger envelope validation

**Findings raised:** 3 · **Accepted-fixed:** 3 · **Rejected:** 0 · **Deferred:** 0
**Phase branch:** `fix/hbtriage-p4-ledger-envelope` → merged `--no-ff` into `nonkyc` as `8541e4f`
**Adjudication commit:** `eb10616`
**Post-merge suite:** 14 failed / 579 passed

---

#### CDX-R01 — Validator knowingly accepts envelopes the engine rejects (assets + future skew)
- **Category:** spec-conformance · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED**
- **Finding:** (a) Asset validation skipped despite being trivially mirrorable — and the
  author's parenthetical guess at `split_hb_trading_pair` semantics was wrong (`split("-", 1)` vs
  engine's `split("-")`). Engine compares assets at `:2025-2028` and quarantines → wallet re-seed.
  (b) Future-skew check omitted; `STATE_MAX_FUTURE_SKEW_SECONDS = 86400` — a day's tolerance means
  the API's clock would need to be >24h behind the bot's to cause a false abort.
- **Fix:** `services/ledger_envelope_contract.py` — mirrored `_split_hb_trading_pair` verbatim;
  assets derived from the pair the engine resolves; future-skew mirrored against caller-injected
  clock; unusable clock → `LEDGER_INVALID`.
- **Mutation evidence:** Both checks disabled via `if False:` → 3 and 1 tests FAILED respectively.
  Reverted; zero residue.

---

#### CDX-R02 — Missing/malformed staged identity fields become unchecked matches
- **Category:** logic · **Severity:** High · **Confidence:** High (most serious of phase 4)
- **Decision: ACCEPTED-FIXED**
- **Finding:** `if expected_value is None: continue` was fail-open — skipped comparison when
  the staged config omitted a field. Engine never skips; it resolves the default and compares.
  A staged YAML without `connector_name` means the engine compares the ledger against `"binance"`;
  a `"nonkyc"` ledger was blessed by the API and quarantined by the engine → wallet re-seed.
- **Fix:** `_resolve_expected_identity` + `ENGINE_IDENTITY_DEFAULTS` (cited to engine `:192-206`);
  absent → mirrored default; present non-blank `str` → verbatim; non-`str`/blank → `LEDGER_INVALID`.
  `test_unknown_expected_value_skips_that_comparison` DELETED (it asserted the nonconformance);
  replaced with `test_absent_staged_field_is_compared_against_the_engine_default`.
- **Collateral evidence:** After fix, 53 previously-passing copyforward tests failed — every one
  had a staged YAML omitting `connector_name`/`trading_pair` and had been silently skipping the
  comparison. Fixtures updated to carry the fields a real ladder YAML carries.
- **Mutation evidence:** Restoring skip `if field_name not in staged_config: continue` →
  4 FAILED (one per identity field). Reverted; zero residue.

---

#### CDX-R03 — Deeply nested JSON escapes the LEDGER_INVALID mapping
- **Category:** error-handling · **Severity:** Medium · **Confidence:** High
- **Decision: ACCEPTED-FIXED**
- **Finding:** `RecursionError` is a `RuntimeError`, not a `ValueError`, so it escaped
  `except (ValueError, UnicodeDecodeError)`. Reviewer's stated depth (~1,100) was wrong (first
  raises near 5× the recursion limit); finding is still real.
- **Fix:** `except RecursionError` → `ResumeError(LEDGER_INVALID)`. Test uses
  `sys.getrecursionlimit() * 5`.
- **Mutation evidence:** Deleted handler → test FAILED with raw `RecursionError`. Reverted;
  zero residue.

---

**Phase 4 additional obligation noted (not raised by reviewer):** `tests/ledger_fixtures.py`
split pair with `split("-", 1)` while engine uses `split("-")`. Corrected.

**Mirror-sync obligation is now broader than documented:** covers `SUPPORTED_LEDGER_SCHEMA_VERSIONS`
PLUS `ENGINE_IDENTITY_DEFAULTS` (`:192-206`) PLUS `STATE_MAX_FUTURE_SKEW_SECONDS` (`:1387`).

---

### Phase 5 — Three small guards: CDX-015, CLA-008 #7, CLA-008 P2

**Findings raised:** 0 · **Accepted-fixed:** 0 · **Rejected:** 0 · **Deferred:** 0
**Reviewer verdict:** "No findings above the bar."
**Phase branch:** `fix/hbtriage-p5-small-guards` → merged `--no-ff` into `nonkyc` as `78a1548`
**Author commit:** `020e8a6`
**Post-merge suite:** 14 failed / 610 passed

The reviewer passed all 12 added tests (PASS verdict per test, with named mutations). Author
independently ran 3 spot-check mutations to confirm:

| Mutation | Tests failed |
|---|---|
| Unknown engine stops aborting (`if True:`) | 8 FAILED (all unknown-engine abort params) |
| Restore CLA-008 #7 owner-path flatten | 2 FAILED (`test_owner_planned_at_the_ledgers_relative_path`, `test_owner_lands_adjacent_on_disk`) |
| Docker errors unmapped (`except () as exc:`) | 2 FAILED (both docker-failure refuse params) |

No `CDX-*` finding was raised or rejected. No human arbitration items from this phase.

---

### Phase 6 — CLA-M02 + CLA-M01: Path-coupling assertion + drift surfacing

**Findings raised:** 0 · **Accepted-fixed:** 0 · **Rejected:** 0 · **Deferred:** 0
**Reviewer verdict:** "No findings above the bar."
**Phase branch:** `fix/hbtriage-p6-coupling-drift` → merged `--no-ff` into `nonkyc` as `ec953f5`
**Post-merge suite:** 14 failed / 661 passed

Reviewer is the codex family, which authored the `CDX-*` findings; this phase implements
`CLA-*` findings. The author re-read the full branch diff before accepting the verdict.
Spec conformance confirmed:

- CLA-M02 coupling check fires at `services/docker_service.py:620` before any filesystem mutation;
  mismatch aborts before staging bytes.
- `_inspect_self_mounts` enumerates a closed "unavailable" set; empty mount table returns `[]`
  (evidence → aborts), not `None`.
- CLA-M01 changed no copy or template semantics; `_warn_sizing_critical_drift` only reports.

No mutation experiments run (none required — no test-theater/gap findings raised).
No `CDX-*` finding rejected; no human arbitration items from this phase.

---

### Phase 7 — CDX-005/CDX-M03: Acknowledged-retirement state machine

**Findings raised:** 9 · **Accepted-fixed:** 9 · **Rejected:** 0 · **Deferred:** 0
**Phase branch:** `fix/hbtriage-p7-retirement-fsm` → merged `--no-ff` into `nonkyc` as `4877139`
**Adjudication commit:** `77db01e`
**Post-merge suite:** 14 failed / 710 passed

---

#### CDX-R01 — Any MQTT response treated as proof of strategy quiescence
- **Category:** correctness · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED**
- **Finding:** With `async_backend=True`, `_on_cmd_stop` schedules `stop_loop()` and replies
  SUCCESS immediately — reply proves acceptance, not completion. Error replies (`status=400`)
  counted as acks via `is not None` check.
- **Fix:** Stop acks validated against engine reply envelope (`data.status == 200`). New required
  postcondition `quiescence_confirmed_at`: bounded poll of bot's `status` RPC until it replies
  `400/'No strategy is currently running!'` (the engine clears `trading_core.strategy` only at END
  of `stop_loop()`). Timeout → UNVERIFIED. New `RETIREMENT_QUIESCENCE_TIMEOUT_S` (default 120 s).
- **Mutation evidence:** Reverted validation to `response is not None` →
  `test_error_stop_reply_is_not_an_ack` and `test_malformed_stop_reply_is_not_an_ack` **FAILED**.
  Reverted.

---

#### CDX-R02 — Historical order rows promoted to exchange-confirmed zero / drain is a sleep
- **Category:** correctness · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED (with documented residual)**
- **Finding:** Zero-open-orders stamp was not machine-ordered after quiescence confirmation;
  a momentary DB zero before the bot's stop sequence completed proved nothing.
- **Fix:** Zero-open-orders stage now stamped AFTER quiescence confirmation.
  `zero_open_orders_basis="quiescence_unconfirmed"` used when confirmation not yet achieved.
- **Residual (recorded for human):** Live exchange reconciliation with sequence/watermark evidence
  is not implementable in this repo. `accounts_service.get_active_orders` reads only the API's
  own `connector.in_flight_orders`, never bot's exchange-side orders. This gap is exactly what the
  runbook's human live `resume-preview` dry-run obligation covers.
- **Mutation evidence:** Removed quiescence gate (`if True:`) →
  `test_no_quiescence_never_verifies` **FAILED** on zero-order assertions. Reverted.

---

#### CDX-R03 — Clean container exit fabricates state-flush evidence
- **Category:** data-loss · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED**
- **Finding:** Exit 0 proves termination only (SIGTERM-killed container can exit 0 without running
  shutdown path). `state_flushed_at` was stamped from exit code alone.
- **Fix:** `state_flushed_at` now requires quiescence confirmation AND exit 0, with its own
  timestamp and `state_flush_basis`.
- **Mutation evidence:** `if exit_code == 0:` alone → `test_no_quiescence_never_verifies`
  **FAILED** (`state_flushed_at` fabricated). Reverted.

---

#### CDX-R04 — CFH accepts a bare VERIFIED marker without evidence
- **Category:** spec-conformance · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED**
- **Finding:** Guard trusted an unconstrained string column; passing test codified marker-only trust.
- **Fix:** `_guard_ungraceful_source` now parses `retirement_evidence` and validates it with
  `missing_retirement_evidence` predicate. Null, malformed, or incomplete evidence → refusal.
  `test_verified_retirement_passes` rewritten to fully-evidenced row.
- **Mutation evidence:** Dropped `evidence_gaps == []` from guard → all three new refusal tests
  **FAILED**. Reverted.

---

#### CDX-R05 — Millisecond reply topics collide across concurrent retirements
- **Category:** concurrency · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED**
- **Finding:** `_pending_responses[topic] = future` silently replaces on same-millisecond collision.
- **Fix:** Reply topics now `{timestamp}-{uuid4().hex}`.
- **Mutation evidence:** Reverted to timestamp-only topic →
  `test_same_millisecond_reply_topics_do_not_collide` **FAILED**. Reverted.
- **Deferred observation (untouched code):** Pre-existing `publish_command_and_wait`
  (`utils/mqtt_manager.py:354-356`) has the identical timestamp-only flaw. Not fixed here per phase
  contract. See REQUIRES HUMAN ARBITRATION.

---

#### CDX-R06 — Evidence not persisted at stage boundaries
- **Category:** error-handling · **Severity:** Medium · **Confidence:** High
- **Decision: ACCEPTED-FIXED**
- **Finding:** A crash during the (up to 30 s) ack wait would lose `stop_requested_at` and
  `cancellation_requested_at`. `archived_at` was persisted after container removal, not before.
- **Fix:** (1) `publish_command_with_ack` gained `on_published` callback; orchestrator persists
  timestamps there. (2) `archived_at` persisted before container removal.
- **Mutation evidence:** Emptied callback → `test_crash_during_ack_wait_preserves_stop_requested`
  **FAILED**. Removed pre-removal persist → `test_crash_during_removal_preserves_archive_evidence`
  **FAILED**. Both reverted.

---

#### CDX-R07 — Gate tests derive their oracle from the implementation constant
- **Category:** test-theater · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED — REVIEWER PROVEN RIGHT BY EXPERIMENT**
- **Finding:** Tests imported `REQUIRED_RETIREMENT_EVIDENCE` from the production module — deleting
  a key from it would remove it from both the guard and the test simultaneously.
- **Mutation evidence (pre-fix):** Deleted `"fills_drained_at"` from
  `REQUIRED_RETIREMENT_EVIDENCE` → **10 PASSED** (theater confirmed). Cannot reject.
- **Fix:** Literal `SPEC_REQUIRED_EVIDENCE` tuple in the test file (10 keys); conformance test
  `test_required_evidence_matches_spec` asserts the production constant equals the literal.
- **Mutation evidence (post-fix):** Same mutation → **2 FAILED**
  (`test_required_evidence_matches_spec`, `test_each_postcondition_gate[fills_drained_at]`).
  Reverted.

---

#### CDX-R08 — Skip-cancellation test never finalizes the skip=True case
- **Category:** test-theater · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED — REVIEWER PROVEN RIGHT BY EXPERIMENT**
- **Finding:** Test never verified VERIFIED can be reached on the skip=True path.
- **Mutation evidence (pre-fix):** `… and evidence.get("skip_order_cancellation") is False`
  → test **PASSED** (theater confirmed).
- **Fix:** Test now seeds both paths and finalizes BOTH through the repository.
- **Mutation evidence (post-fix):** Same mutation → **FAILED**
  (`assert 'UNVERIFIED' == 'VERIFIED'`). Reverted.

---

#### CDX-R09 — Legacy-default test never executes the ALTER migration
- **Category:** test-theater · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED — REVIEWER PROVEN RIGHT BY EXPERIMENT**
- **Finding:** `test_new_rows_default_unverified` was broken against the production ALTER; the test
  could not see a wrong default.
- **Mutation evidence (pre-fix):** Changed ALTER to `DEFAULT 'VERIFIED'` →
  `test_new_rows_default_unverified` **PASSED** (theater confirmed).
- **Fix:** New `test_legacy_rows_migrate_to_unverified` builds a pre-migration `bot_runs` table
  with a legacy STOPPED row, runs the real `AsyncDatabaseManager._run_migrations` routine, and
  asserts the row lands UNVERIFIED with null evidence.
- **Mutation evidence (post-fix):** Same mutation → **FAILED**
  (`assert 'VERIFIED' == 'UNVERIFIED'`). Reverted.

---

### Phase 8 — CDX-006: Fills accounting — insert-first + VWAP

**Findings raised:** 3 · **Accepted-fixed:** 3 · **Rejected:** 0 · **Deferred:** 0
**Phase branch:** `fix/hbtriage-p8-fill-accounting` → merged `--no-ff` into `nonkyc` as `5643806`
**Adjudication commit:** `95b159c`
**Post-merge suite:** 14 failed / 724 passed

---

#### CDX-R01 — Concurrent distinct fills can lose an aggregate update
- **Category:** concurrency · **Severity:** High · **Confidence:** Medium
- **Decision: ACCEPTED-FIXED**
- **Finding:** `services/orders_recorder.py` spawned one asyncio task per fill event, each with
  its own session. Two distinct fills for one order can both read the same pre-fill aggregates;
  last writer erases the other fill's contribution. Insert-first dedup protects only against
  duplicate deliveries, not distinct-fill interleaving.
- **Fix:** Per-order-id serialization: refcounted module-level `asyncio.Lock` keyed by
  `client_order_id` around insert+aggregate transaction. Order row read with `with_for_update()`
  for cross-process safety (postgres: `FOR UPDATE`; sqlite: no-op, correct for single-writer).
- **Mutation evidence (pre-fix):** Inserted `asyncio.sleep(0.1)` between order read and aggregate
  write → **11 PASSED** (gap confirmed). Reverted.
- **Mutation evidence (post-fix):** Removed per-order lock → `test_concurrent_distinct_fills_serialize_and_both_count`
  **FAILED** (`assert 2 == 1`). Reverted.
- **Residual:** Postgres `SELECT ... FOR UPDATE` path untestable in this environment (no postgres
  driver). In-process lock is the mutation-proven serialization for the deployed single-process API.

---

#### CDX-R02 — Connector component of the scoped fill identity untested
- **Category:** test-gap · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED**
- **Finding:** No test varied the connector axis; a two-column `(account_name, exchange_trade_id)`
  constraint would have passed everything — wrongly discarding a fill when one account sees the
  same exchange trade id on two connectors.
- **Mutation evidence (pre-fix):** Narrowed startup index to `(account_name, exchange_trade_id)` →
  **11 PASSED** (gap confirmed). Reverted.
- **Fix:** Two new tests: app-path connector-axis dedup and legacy-migration connector-axis insert.
- **Mutation evidence (post-fix):** Narrowing model constraint → new app test FAILED; narrowing
  startup SQL → extended legacy test FAILED. Both reverted.

---

#### CDX-R03 — Migration test bypasses the production startup wiring
- **Category:** test-theater · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED — REVIEWER PROVEN RIGHT BY EXPERIMENT**
- **Finding:** Test invoked `_run_migrations`/`_create_unique_indexes` directly; never proved
  `create_tables` calls them.
- **Mutation evidence (pre-fix):** Replaced `await self._create_unique_indexes(conn)` with `pass`
  → test **PASSED** (theater confirmed).
- **Fix:** New `test_real_startup_path_installs_scoped_index_on_legacy_db` drives real
  `AsyncDatabaseManager.create_tables()` against a legacy pre-phase `trades` table; proves the
  scoped index rejects a duplicate triple.
- **Mutation evidence (post-fix):** Same `pass` mutation → **FAILED** (`DID NOT RAISE
  IntegrityError`). Reverted.

---

### Phase 9 — CDX-013: Versioned migrations; fatal migration state; dead DROPs removed

**Findings raised:** 5 · **Accepted-fixed:** 5 · **Rejected:** 0 · **Deferred:** 0
**Phase branch:** `fix/hbtriage-p9-migrations` → merged `--no-ff` into `nonkyc` as `f07cedd`
**Adjudication commit:** `6cac728`
**Post-merge suite:** 14 failed / 748 passed

---

#### CDX-R01 — Migration scaffold absent from runtime image; compatibility test inspects only source tree
- **Category:** test-theater · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED**
- **Finding:** `Dockerfile` never copied `alembic.ini` or `alembic/`. The migrate service takes its
  silent skip branch when `alembic.ini` is absent. `alembic` not in `environment.yml` — a rebuilt
  image would lack the CLI/import entirely. Original `test_alembic_ini_is_at_the_invocation_root`
  checked only the host checkout, not the image.
- **Fix:** `Dockerfile`: `COPY main.py config.py deps.py alembic.ini ./` + `COPY alembic ./alembic`.
  `environment.yml`: added `alembic>=1.18.0` to pip section. New tests
  `test_runtime_image_packages_the_scaffold` and `test_alembic_is_a_declared_runtime_dependency`.
- **Mutation evidence:** Reverted Dockerfile copy (dropped alembic.ini + dir) →
  `test_runtime_image_packages_the_scaffold` **FAILED**. Reverted.

---

#### CDX-R02 — "Real startup path" tests pass when migration verification is made unreachable
- **Category:** test-theater · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED**
- **Finding:** `test_startup_path_calls_the_verification` was a substring grep of `main.py`.
  Neither original test entered `main.lifespan`, so neither proved startup actually awaits the check.
- **Mutation evidence (pre-fix):** `main.py:145` → `if False: await db_manager.verify_schema_at_head()`
  → original tests **PASSED** (theater confirmed).
- **Fix:** New `test_lifespan_aborts_when_schema_not_at_head` enters the real `main.lifespan`
  context (with all side effects neutralized) and asserts it aborts when `verify_schema_at_head`
  raises `MigrationStateError`.
- **Mutation evidence (post-fix):** Same mutation → **FAILED**. Reverted.

---

#### CDX-R03 — DATABASE_URL compatibility test never executes Alembic's URL resolver
- **Category:** test-theater · **Severity:** High · **Confidence:** High
- **Decision: ACCEPTED-FIXED**
- **Finding:** Original test constructed `DatabaseSettings()` and asserted `alembic_config()` has
  no `sqlalchemy.url`. Never ran `alembic/env.py::_database_url`.
- **Mutation evidence (pre-fix):** `_database_url` → `return "sqlite:///wrong.db"` →
  original test **PASSED** (theater confirmed).
- **Fix:** `test_env_resolves_target_db_from_settings` runs real `command.upgrade(..., "head")`
  against the target DB and asserts it received `alembic_version` and full model schema.
- **Mutation evidence (post-fix):** Same mutation → **FAILED** (target DB never migrated). Reverted.

---

#### CDX-R04 — "Exact" schema-parity test ignores server-default drift
- **Category:** test-theater · **Severity:** Medium · **Confidence:** High
- **Decision: ACCEPTED-FIXED**
- **Finding:** `test_schema_at_head_matches_models_exactly` used only `compare_type=True`; alembic
  does not compare server defaults unless `compare_server_default=True`. env.py comment lied.
- **Mutation evidence (pre-fix):** `server_default=sa.func.now()` → `sa.text("'1970-01-01'")` →
  parity test **PASSED** (theater confirmed).
- **Fix:** Enabled `compare_server_default=True` in both `alembic/env.py::_configure` and the
  parity test. Verified zero diff entries on correct code.
- **Mutation evidence (post-fix):** Same mutation → parity test **FAILED**. Reverted.

---

#### CDX-R05 — Production async migration branch has no falsification coverage
- **Category:** test-gap · **Severity:** Medium · **Confidence:** High
- **Decision: ACCEPTED-FIXED**
- **Finding:** Every migration test used a sync sqlite URL. The `postgresql+asyncpg` routing
  (`_is_async_url` / `create_async_engine` / `run_sync`) could be broken with no test failing.
- **Mutation evidence (pre-fix):** Disabled async branch (`if False and is_async_url(url):`) →
  all added/changed tests **PASSED** (gap confirmed).
- **Fix:** Extracted `database/migration_runner.py` with injectable seams; new tests
  `test_async_url_routes_to_async_runner_never_sync_engine` and
  `test_sync_url_routes_to_sync_engine_never_async_runner`.
- **Mutation evidence (post-fix):** Same mutation → `test_async_url_routes_to_async_runner_never_sync_engine`
  **FAILED**. Reverted.

---

#### Phase 9 spec note (no finding)
The reviewer observed two revisions vs one baseline; this is deliberate. `0001` reproduces the
*actually deployed* pre-phase-7/8 schema so that a production `alembic stamp 0001_baseline` leaves
the DB genuinely behind head, forcing `0002` to add retirement-evidence and fill-identity columns.
A single baseline carrying those columns would let a stamped production DB report "at head" while
the columns are missing. Reviewer explicitly conceded this has "a defensible production-upgrade
rationale."

---

## 3. TEST INTEGRITY SUBSECTION

Every test-theater finding from the batch, in order of phase, with mutation-experiment outcome:

| Phase | Finding | Named mutation | Pre-fix outcome | Post-fix outcome | Theater? |
|---|---|---|---|---|---|
| P1 / CDX-R04 | Concurrency tests don't prove deploy uses the lock | `_instance_deploy_lock(name)` → `_instance_deploy_lock(f"{name}-{token}")` | 4 PASSED (theater) | 1 FAILED (lock test catches it) | YES → FIXED |
| P2 / CDX-R03 | Containment skip-on-no-privilege | containment guard → `if False:` | 2 FAILED on this runner (symlink privilege present) | N/A (accepted on substance, not literal theater) | CONDITIONAL → FIXED |
| P3 / CDX-R01 | Owner-mismatch test doesn't catch prohibited coercion | `owner_id != controller_id` → `str(owner_id) != str(controller_id)` | Named test 1 PASSED; full suite 401 PASSED (theater) | 1 FAILED (new test catches it) | YES → FIXED |
| P7 / CDX-R07 | Gate tests import oracle from production constant | Delete `"fills_drained_at"` from `REQUIRED_RETIREMENT_EVIDENCE` | 10 PASSED (theater) | 2 FAILED (literal spec tuple + conformance test) | YES → FIXED |
| P7 / CDX-R08 | Skip-cancellation test never finalizes skip=True case | `… and evidence.get("skip_order_cancellation") is False` | 1 PASSED (theater) | 1 FAILED | YES → FIXED |
| P7 / CDX-R09 | Legacy-default test never executes the ALTER migration | Production ALTER `DEFAULT 'VERIFIED'` | 1 PASSED (theater) | 1 FAILED | YES → FIXED |
| P8 / CDX-R03 | Migration test bypasses production startup wiring | Replace `await self._create_unique_indexes(conn)` with `pass` | 1 PASSED (theater) | 1 FAILED (lifespan test catches it) | YES → FIXED |
| P9 / CDX-R01 | Migration scaffold absent from runtime image | Revert Dockerfile COPY (drop alembic.ini + alembic/) | 1 FAILED (test checks source tree, not image) | 1 FAILED (new image-content test) | YES → FIXED |
| P9 / CDX-R02 | Startup-path test passes when verification unreachable | `if False: await db_manager.verify_schema_at_head()` | 2 PASSED (theater) | 1 FAILED (lifespan test) | YES → FIXED |
| P9 / CDX-R03 | URL compatibility test never executes Alembic resolver | `_database_url` → `return "sqlite:///wrong.db"` | 1 PASSED (theater) | 1 FAILED | YES → FIXED |
| P9 / CDX-R04 | Parity test ignores server-default drift | `server_default=sa.func.now()` → `sa.text("'1970-01-01'")` | 1 PASSED (theater) | 1 FAILED | YES → FIXED |
| P9 / CDX-R05 | Async migration branch has no falsification coverage | Disable async branch via `if False and is_async_url(url):` | All added tests PASSED (gap) | 1 FAILED | YES → FIXED |

**No test-theater finding was rejected without mutation evidence. All findings in this table
were confirmed by experiment; the protocol requires acceptance in every case. Zero protocol
violations.**

---

## 4. REQUIRES HUMAN ARBITRATION

### 4.1 Partially-rejected CDX-* findings

#### P1 / CDX-R02 (partially rejected) — Preview/deploy name handshake

**Severity of declined portion:** Medium (spec-conformance)
**What the reviewer asked for:** A handshake token generated by preview, reused by deploy,
guaranteeing they check the same target candidate. Deploy must validate the supplied candidate and
never regenerate.
**What the author declined:** The handshake itself (the "return-a-token, deploy-must-reuse-it" mechanism).
**Author's grounds (in full):**
1. Phase spec requires preview to run WITHOUT creating or mutating anything; a handshake that
   guarantees the candidate requires reserving the name.
2. A non-reserving handshake cannot close its own TOCTOU window — the target can still be taken
   between the two HTTP calls.
3. Collision probability is negligible after phase 1 step 3 added sub-second + entropy components.

**What WAS fixed:** Deploy now uses the shared `resolve_deploy_target` helper (previously
it re-joined its own path in `docker_service.py:332`), closing the docstring drift.

**For the human to decide:** Is the non-reserving preview contract acceptable — i.e., is it
acceptable that preview's `DEST_EXISTS` check on the actual candidate it computes is near-vacuous
by construction (unique names), with collision information carried only via the base-name
`_preview_base_name_collision` check? Or does the runbook require that preview's answer be a
stronger pre-deploy guarantee?

---

### 4.2 Deferred observations (real, but in code no phase touched)

#### P2 observation — `test_extra_path_escape_via_symlink` has the skip-on-no-privilege pattern

**Severity:** High (same class as CDX-R03 that was fixed in P2)
**Location:** `tests/test_copyforward_copyset.py:685`
**What it is:** A file symlink (not directory) testing the `EXTRA_PATH_ESCAPE` security invariant.
Same `@pytest.mark.skipif(not os.access(...))` skip clause that CDX-R03 objected to. The junction
fallback added in P2 applies to directories only.
**What's needed:** A privilege-free file-symlink equivalent (e.g., Windows hardlink, or
restructured as a unit test against the validator directly). Not fixed per phase contract.

---

#### P7 observation — Pre-existing `publish_command_and_wait` has the same timestamp-only reply topic

**Severity:** High (same class as CDX-R05 that was fixed in P7 for `publish_command_with_ack`)
**Location:** `utils/mqtt_manager.py:354-356` (`publish_command_and_wait`)
**What it is:** Millisecond-granularity topic `{timestamp}` without UUID entropy; concurrent
callers on the same bot within one millisecond collide silently. Phase 7 fixed `publish_command_with_ack`
but not this pre-existing helper.
**What's needed:** Same fix applied to `publish_command_and_wait` — topic becomes
`{timestamp}-{uuid4().hex}`. Not fixed per phase contract.

---

#### P8 observation — Postgres `SELECT ... FOR UPDATE` path is untestable in this environment

**Severity:** Medium (coverage gap, not a code defect)
**Location:** `database/repositories/order_repository.py:50`
**What it is:** The `with_for_update()` cross-process lock renders a no-op on sqlite and cannot be
tested without a real postgres + asyncpg environment. The in-process per-order lock (tested and
mutation-proven) fully covers the deployed single-process API.
**What's needed:** A postgres integration test environment.

---

### 4.3 Protocol violation check — test-theater rejected without mutation evidence

**Result: NONE.** Every test-theater finding in the entire batch was either confirmed by
experiment (and fixed) or explicitly confirmed to have the tested behavior present on the batch
runner before accepting on substance (P2/CDX-R03). No test-theater finding was rejected without
running the named mutation. No protocol violations.

---

## 5. RUNBOOK NOTES

### 5.1 CDX-008 / Contract C2 — Controller id validation now aborts on both sides

Controller configs with an empty, whitespace-only, or non-`str` `id` are now rejected at BOTH
layers:
- **Engine side (Run A, done):** `ControllerConfigBase.id` has `min_length=1` plus a strip
  validator (Pydantic v2).
- **API side (this run, Phase 3):** `resume_service.py` aborts the whole deploy with
  `CONTROLLER_ID_INVALID` (HTTP 409) instead of silently `continue`ing past the controller.

Old behavior: a controller with `id: ""` was silently skipped; the bot deployed without it.
New behavior: the entire deploy aborts. **Deliberate, desirable break.**

Additionally: a `.owner` sidecar whose `controller_id` is not a JSON string is now treated as
an unverifiable identity and fails closed with `OWNER_MISMATCH`.

**Action before next deploy:** Check live `conf/` trees for controllers with empty, whitespace-only,
or non-string `id` fields and correct them before deploying on the new API.

---

### 5.2 CDX-013 — One-time `alembic stamp` REQUIRED on all existing deployed databases

**CRITICAL: The API will refuse to start if the `alembic_version` table is absent.** This is
an intentional fail-closed; an un-migrated database is indistinguishable from a broken one.

**Exact commands:**

For a **pre-existing production database** (deployed before this release, no `alembic_version` table):

```bash
# From the API container, after the new image is deployed:
cd /hummingbot-api
alembic stamp 0001_baseline   # marks DB as "at the pre-phase-7/8 baseline"
alembic upgrade head           # applies the phase-7/8 columns (retirement evidence + fill identity)
```

For an **empty database** (fresh deploy, no prior data):

```bash
cd /hummingbot-api
alembic upgrade head           # builds the full schema from scratch
```

**Additional requirement (CDX-R01):** The next image build must pick up the Dockerfile changes
that package `alembic.ini` and `alembic/` into the image, and the `alembic` dependency in
`environment.yml`. Until the image is rebuilt, the migrate service (which checks for
`alembic.ini`) continues to take its silent-skip branch, meaning migrations do NOT run
automatically on container start. The human docker rebuild is the gate.

**Mirror-sync obligation:** `SUPPORTED_LEDGER_SCHEMA_VERSIONS`, `ENGINE_IDENTITY_DEFAULTS`
(engine `:192-206`), and `STATE_MAX_FUTURE_SKEW_SECONDS` (engine `:1387`) are all mirrored
constants pinned by tests. When the engine updates any of these, the API mirror must be updated.
Versions {6..10} are the current supported set. A narrowed engine set not mirrored here costs a
wallet re-seed; a widened one costs a false abort.

---

### 5.3 CDX-005 / CDX-M03 — Legacy STOPPED rows are UNVERIFIED; CFH now refuses them by default

**What changed:** The retirement state machine now requires multi-stage evidence
(`quiescence_confirmed_at`, `zero_open_orders_confirmed_at`, `state_flushed_at`,
`fills_drained_at`, `archived_at`, etc.). STOPPED rows written before this change have `null`
in the `retirement_evidence` column.

**Operational impact:** CFH (`_guard_ungraceful_source`) now parses `retirement_evidence` and
validates it using `missing_retirement_evidence`. A legacy STOPPED row — evidence null, malformed,
or incomplete — results in refusal with a "marker alone is never trusted" detail.

**Expected behavior:** Any attempt to use a pre-change STOPPED instance as a graceful CFH source
will fail with a refusal message naming the missing evidence. This is correct: those instances
were never confirmed retired; they were only marked stopped.

**Human options:**
- Supply the `human_override_ungraceful_source` field (if that path exists in the request flow)
  to bypass the refusal — accepting that the source is unverified.
- Or perform a fresh retirement of the instance under the new state machine.

---

### 5.4 Cross-repo contract status (C1 and C2)

Both shared contracts now live on BOTH sides:

| Contract | Engine side (Run A) | API side (this run) |
|---|---|---|
| C1 — `state_file_name` path validation | Enforced at controller-config validation in `range_inventory_ladder.py` + runtime containment assertion | Enforced in `resume_service.py` planning; absolute-skip path removed; opt-out requires `allow_absolute_state_file_name: true` (StrictBool) |
| C2 — controller `id` validation | `ControllerConfigBase.id` `min_length=1` + strip validator | `resume_service.py` aborts on empty/whitespace/non-str id; canonical stripped id used for all downstream identity |

Configs that violated either contract on the engine side were already rejected in Run A. The API
layer now independently fails closed, so whichever side lands first in production is still safe.

---

### 5.5 Phase 4 additional operational note

**Staged ladder YAMLs that omit `connector_name` or `trading_pair` are now compared against the
engine's defaults** (`binance` / `ETH-USDT` from `ControllerConfigBase` `:192-206`). A live conf
tree that relies on omitting these fields while its ledger says otherwise will now abort the deploy
with `LEDGER_INVALID`. The engine would have quarantined and wallet-re-seeded in that case;
the API now aborts earlier. **Check live conf trees for explicit `connector_name` and `trading_pair`.**

---

## 6. OPEN OBLIGATIONS (human-owned; not closeable by code)

### 6.1 Live `resume-preview` dry-run against a real stopped instance (HUMAN, by hand)

**This is the only thing that closes the mock/reality gap.** It has never been done;
every test in this batch that touches CFH planning uses mocked or in-memory data.

- Boot the new API against a test environment where a legitimately retired bot instance exists.
- Run `POST /api/v1/resume/preview` against that stopped instance.
- Verify: preview reports the correct target path, correct per-guard results, and mutates nothing.
- Then run the real resume deploy; verify the bot comes up against the staged state.

Do not claim this obligation is closed. It is not.

### 6.2 Docker rebuild + CDX-002 runtime==image verification (HUMAN)

The next docker image build must:
1. Pick up the `Dockerfile` changes from Phase 9 (`alembic.ini` + `alembic/` + `environment.yml`).
2. Include the CDX-002 engine-side fix from Run A (runtime image verification).
3. After the rebuild, run `alembic upgrade head` (or the stamp + upgrade sequence for existing DBs).

The CDX-R01 fix (scaffold in image) is inert until the image is rebuilt.

### 6.3 Keeping mirrored constants in sync with the engine

Every time the hummingbot engine is updated, check:
- `SUPPORTED_STATE_SCHEMA_VERSIONS` (currently `{6..10}`, engine `:1386`) → update `SUPPORTED_LEDGER_SCHEMA_VERSIONS` in the API
- `ENGINE_IDENTITY_DEFAULTS` (engine `ControllerConfigBase` `:192-206`) → update API mirror
- `STATE_MAX_FUTURE_SKEW_SECONDS` (engine `:1387`) → update API mirror

Mirror drift in the direction of "engine supports more versions" causes false aborts.
Mirror drift in the direction of "engine supports fewer versions" causes a wallet re-seed.

### 6.4 CDX-R02 Phase 7 — Exchange-side order reconciliation gap

Live exchange reconciliation with sequence/watermark evidence is not implementable from API-side
data. `accounts_service.get_active_orders` reads only the API's own `connector.in_flight_orders`,
never the bot's exchange-side orders. An order the bot itself lost track of is unobservable.
This gap is documented but unclosed.

---

## 7. TEST SUITE EVOLUTION SUMMARY

| Phase | Tests passing at end | Net new |
|---|---|---|
| Baseline | 246 | — |
| Phase 1 | 262 | +16 |
| Phase 2 | 357 | +95 |
| Phase 3 | 402 | +45 |
| Phase 4 | 579 | +177 |
| Phase 5 | 610 | +31 |
| Phase 6 | 661 | +51 |
| Phase 7 | 710 | +49 |
| Phase 8 | 724 | +14 |
| Phase 9 | 748 | +24 |
| **Phase 10 (final)** | **748** | **0 (no phase-10 tests)** |

Total new tests across all phases: +502. The 14 pre-existing failures are unchanged throughout.

---

## 8. SCOPE COMPLIANCE

No OUT OF SCOPE finding was implemented at any point in this run. The absolute prohibitions were
observed in every phase:
- No Docker commands were run (absolute prohibition 1).
- No out-of-scope findings were touched (CDX-003, CDX-011/M01, CDX-012, CDX-014/CLA-005, CLA-006,
  CDX-004, CLA-001, CLA-003, CLA-007) (prohibition 2).
- The prohibited `str(owner_id) != str(controller_id)` comparison was not introduced in any phase;
  Phase 3 explicitly guards against it and Phase 3 CDX-R01 added a mutation-proven test for it
  (prohibition 3).
- No writes outside `E:/tradingsoftware/hummingbot-api` (prohibition 4/5).
- No orders placed, containers modified, secrets read (prohibition 6).
- No fail-closed default was weakened to make a test pass (prohibition 7).
- The live `resume-preview` dry-run was not claimed or simulated (prohibition 8).

---

## 9. STATUS

**Branch:** `nonkyc` · **Not pushed** (ahead of `origin/nonkyc` by 18 commits)
**Suite:** 14 failed / 748 passed — green against the canonical baseline
**Integration merge:** NOT performed (no merge requested; staying on `nonkyc` per spec)
**Push:** NOT performed per batch contract

<!-- BATCH_PHASE_10_FINALIZATION_COMPLETE name=hbtriage_api -->
