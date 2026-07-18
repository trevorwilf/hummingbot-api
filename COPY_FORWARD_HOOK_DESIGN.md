# Copy-Forward Hook — Design v2 (no implementation)

**Date:** 2026-07-15 · **Repos:** engine `E:\tradingsoftware\hummingbot` @ `47c714a19` (nonkyc), api `E:\tradingsoftware\hummingbot-api` @ `0639c7a` (nonkyc) · **Status:** design only — no code changes

**v2 changelog:** incorporates the Fable adversarial review (`copy_forward_hook_design_REVIEW.md`). Blocking fixes: stop-container step added to the flow (B1), attach point moved after config staging (B2), copy set is config-derived instead of glob-derived (B3). Refinements: per-controller resume semantics (R1), `latest` lineage via Postgres `bot_runs` (R2), archive-source limited to local-move mode + nesting handling (R3), verification no longer relies on a save-on-stop that does not exist (R4), `resume_strict:false` removed — always fail-closed (R5), dry-run as a separate preview path (R6). New operating constraints: fleet-wide unique controller `id:` (C1), expected post-resume reconcile warnings (C2), config-drift diff/warn (C3).

---

## 1. Objective & scope

**Goal:** after a graceful stop + image upgrade, a redeployed bot resumes its range-ladder inventory ledger (and, for SQLite deployments, its trade DB) instead of re-seeding from the wallet.

**Design pivot — the engine already does the resume.** `range_inventory_ladder.py:_load_state()` (`:1931`) loads its state file from `data/` on startup if present; `state_file_name` (`:396`) names it; writes are atomic (mkstemp + fsync + replace, `:2020-2029`). The only missing piece is that hummingbot-api hands the engine a **fresh empty `data/`** on every deploy (`services/docker_service.py:215-218`). The hook's entire job is therefore: **populate the new instance's `data/` with the prior run's state files before the container starts.**

This is uniquely race-free at that point: deploy is create+run in one step (`docker_service.py:350`, `detach=True`) and the engine auto-starts the strategy via `SCRIPT_CONFIG`, so any post-deploy copy loses to `_load_state()` and a fresh re-seed. (A manual Phase-0 copy-forward is possible only as deploy → immediately stop-bot + stop-container → replace fresh ledger → start-container, and carries a boot-race window in which the fresh bot may seed and place orders. That risk is what validates the pre-start attach point as the only clean design.)

**In scope:** the range-ladder ledger `.json` (+ `.owner`), and optionally the engine `*.sqlite` (+ `-journal` sidecar) for SQLite deployments.
**Out of scope (by design):** resuming live executors (the engine cannot — established), adopting orphaned exchange orders (the flow relies on graceful stop having cancelled them), MarketState in-flight-order restore (keyed by the rotating `config_file_path`; moot after a graceful stop), and S3-archived sources (see §5).

## 2. Design principles

1. **Fail-closed on resume — always.** If resume is requested and the source cannot be found and validated, **abort the deploy with a clear error** before any container starts. There is no best-effort mode: silent-fresh is the current bug (re-seed → insufficient-funds symptom), and a soft flag would reintroduce it. *(v2: the former `resume_strict:false` option is removed.)*
2. **Single-owner invariant, enforced by ordering.** The state file may have exactly one live owner. The `.owner` marker is warn-only (`range_inventory_ladder.py:1975-1997`), so safety comes from orchestration ordering: the source **container must be exited** before the copy, and the copy completes before the new container starts. PID checks are not used — PIDs are container-namespaced and meaningless to the API host; Docker container state is the only valid stopped-check. *(v2: B1.)*
3. **Preserve immutable instances.** Keep the API's new-timestamped-instance model. Carry the *data*, not the name — new instance identity, continuous state.
4. **Copy only into a not-yet-running container.** The copy happens between config staging and `containers.run`, so there is never a concurrent writer.
5. **Deterministic, explicit source.** No guessing among the many stranded ledgers. Resolve a single named source; if ambiguous, refuse and require `explicit`.
6. **Config-derived copy set.** What to copy is computed from the controller YAMLs being deployed — never from filename globs. *(v2: B3.)*

## 3. Architecture overview

```
stop-bot (MQTT)      stop-container        deploy w/ resume        hook: validate + copy       new bot boots
  cancels orders  ──►  container exits ──►  new instance dir,  ──►  config-derived state   ──►  _load_state()
  strategy stops       ledger quiesced      conf staged             files into empty data/      finds ledger;
                       on disk (atomic)                             + manifest                  Postgres re-attaches
                                                                                                by controller_id
```

One new component ("resume seeding") runs inside `create_hummingbot_instance`, **after config staging and before the container run**.

## 4. Attachment point *(v2: B2)*

| Where | File:line | Why |
|---|---|---|
| **Primary hook** | `services/docker_service.py` — after the script/controller config copy completes (`:234-269`) and the `data/` dir exists (`:217`), before `containers.run` (`:350`) | The new instance's controller YAMLs are on disk (needed for the config-derived copy set and id validation), the container is not yet up, and the bind-mount (`:311`) has not taken effect |
| Copy primitive | `utils/file_system.py:101` `copy_folder` (path-guarded `copytree`), or a file-level variant for the derived set | Existing, path-safety-guarded |
| Field plumbing | `models/bot_orchestration.py:119,146` (deploy models; `_validate_safe_name` present) → threaded through `routers/bot_orchestration.py:537,593` | Where deploy inputs are declared and passed |
| Archive-sourced variant | `utils/bot_archiver.py` (the missing inverse of its move) | Local-move archives only (§5) |

Note: the hook may alternatively read controller ids from the deploy's source YAMLs (`bots/conf/controllers/<name>.yml` via `deployment.controllers_config`) — either source is acceptable; the instance-staged copies are preferred because they are exactly what the new bot will run.

## 5. Source resolution — "which one to grab" *(v2: R2, R3)*

`resume_mode` selects the strategy:

| `resume_mode` | Behavior | Source dir |
|---|---|---|
| `off` (default) | Current behavior — fresh empty `data/` | none |
| `explicit` | Operator names the exact prior instance | `bots/instances/<resume_from>/data`, or `bots/archived/<resume_from>/data` if `resume_from_archive` |
| `latest` | Auto-resolve the newest prior run of the same logical bot | see rules below |

Rules for `latest`:
- **Lineage comes from Postgres, not directory parsing.** Query `bot_runs.instance_name` history for entries whose base name matches. Base name = instance name with only the **final** `-\d{8}-\d{6}` suffix stripped (the API's own format, `routers/bot_orchestration.py:501`). Operator-supplied names may themselves embed timestamp-like tokens (real example: `KRAKEN_LADDER_V1-20260712-2302-20260712-230254`), so never strip more than the last suffix, and compare the remainder byte-for-byte. Directory listing is the fallback when Postgres is unavailable, with the same suffix rule.
- Exclude the instance being created. Pick the most recent by the parsed API timestamp (not mtime — archiving/backup perturbs mtime).
- Zero candidates → fail-closed. More than one plausible candidate with disagreeing controller ids → refuse and require `explicit`.
- **Archive fall-through with `instances/` precedence.** Archiving is the default on stop, so the winner may have been moved out of `bots/instances/`. If `bots/instances/<winner>/data` exists it wins outright (origin=`instances`; the archive is not consulted). Otherwise resolution falls through to the local-move archive `bots/archived/<winner>` (origin=`archived`), with the **same nested-archive handling** as the `explicit` + `resume_from_archive` path (shared resolver; an ambiguous nest still refuses with `ARCHIVE_NESTED`). If neither tree yields a `data/` dir → `SOURCE_NOT_FOUND`, with an error naming both searched roots and noting that compressed (`*_archive.tar.gz`) / S3 archives are not resumable. The DB-unavailable directory fallback likewise lists `bots/archived/` in addition to `bots/instances/`, deduped as a set-union — the same name in both trees is one instance in two places, not ambiguous lineage (DB lineage rows keep their duplicate-preserving tie check).
- Always log the resolved source.

Archive sources *(v2: R3)*:
- **Local-move archives only.** `BotArchiver`'s S3 mode tars and `rmtree`s the source (`bot_archiver.py:28-36`) and the repo has no download/extract path — S3-archived state is out of scope unless an S3 restore is added later. This applies equally to `explicit` + `resume_from_archive` and to the `latest` archive fall-through.
- **Handle the nesting pathology.** `bot_archiver` has no same-name collision handling (`:45-53`): archiving a name twice nests `bots/archived/<name>/<name>`. Resolution must detect nested dirs and either resolve the innermost complete instance or refuse with a clear error. Both archive-capable paths (`explicit` + `resume_from_archive`, and the `latest` fall-through) share one resolver, so the nesting rules cannot drift apart.

**Dry-run / preview** *(v2: R6)*: because deploy = create+run, preview cannot ride the deploy call. It is a separate read-only endpoint/flag that resolves the source and reports "would copy FROM `<instance>/data`: `<files>`" without calling `create_hummingbot_instance`.

### 5a. Controller-flag resolution — dashboard-driven resume *(CTRLRESUME)*

The stock dashboard cannot send request-level resume fields, and its instance names embed a timestamp (`KRAKEN_LADDER_V1-20260718-0101-<api-suffix>`), so request-level `latest` base-name lineage never matches across dashboard deploys. The one identity that IS stable is the controller `id`. A NEW resolution strategy keys off it, in front of the existing pipeline — guards, copy plan, C1/C2 contracts, manifest, preview and events are reused unchanged.

- **The flag.** An optional top-level `resume_mode` key in a controller's config yml, accepted values exactly `"latest"` / `"off"` (absent = off). Any other value aborts fail-closed (`RESUME_FLAG_INVALID`) — never guessed. Validated on the raw scalar spelling (`yaml.compose`, not `safe_load`) so YAML-1.1 false-aliases (`false`/`no`/`OFF`) that are NOT the documented `off` are rejected rather than coerced.
- **Strip at staging.** The engine's `ControllerConfigBase` sets `extra="forbid"` (`config_data_types.py:27`, `controller_base.py:58`), so an unknown `resume_mode` key in a STAGED yml makes the bot's config load FAIL. Flagged ymls are therefore copied through a line-level filter that removes ONLY the top-level `resume_mode:` line (trailing comment included); the staged file is byte-identical to the source except for that line, fail-closed-verified (`safe_load(staged) == safe_load(source)` minus the key). Comment/`[SET-ONCE]` preservation is a hard requirement. Non-flagged ymls keep today's plain byte-identical copy. The same shared parser reads flags for staging (deploy) and from the SOURCE ymls (preview), so the two paths cannot drift.
- **Precedence.** Request `resume_mode != "off"` wins outright — controller flags are IGNORED (logged), and the request `explicit`/`latest` paths behave byte-for-byte as before. Request `off` + ≥1 controller flagged `latest` runs the controller-identity resolver. Neither → the hook is not invoked, identical to today. `resume_accept_ungraceful` stays REQUEST-ONLY: a flag-driven resume that trips the ungraceful-source guard aborts unless the deploy request set the override — it is never a yml key (that would be a permanently disarmed money-guard).
- **Identity-keyed, instance-coherent.** Per flagged controller: canonical id (`classify_controller_id`, C2) + expected ledger (`classify_state_file_name`, C1, + `_expected_ledger_name`). Candidates = every dir under `bots/instances/` plus every name under `bots/archived/` (through `_resolve_archive_instance_dir`; `ARCHIVE_NESTED` propagates), excluding the instance being created; a candidate carries the controller iff `<candidate>/data/<expected_ledger>` exists as a file. Ordering is by the CONTAINING INSTANCE's parsed API timestamp — never mtime. Newest carrier per controller wins; identity is verified ON THE WINNER ONLY (`.owner` parse + canonical-id match, or the default-named-filename rule) and ANY failure aborts — NEVER a fall-back to the second-newest. A tie on newest → `LATEST_AMBIGUOUS`. All flagged controllers must agree on ONE physical source instance; disagreement → `CONTROLLER_SOURCES_DIVERGENT`, naming each controller and its winning instance.
- **First-run-only fallback.** ZERO candidates for ALL flagged controllers → a TRUE first run: the deploy proceeds WITHOUT resume, with a loud structured warning per flagged controller (`RESUME_FIRST_RUN_FRESH_SEED`) on the deploy AND preview responses; NO manifest is written. This is the ONLY fresh-seed in the feature — found-but-unusable history (unrankable carrier, corrupt `.owner`, ambiguous nest, divergent winners) is ALWAYS a refusal. Missing `instances/`/`archived/` dirs count as empty (fresh install); the CLA-M02 bots-path coupling check remains the guard against a wrongly-rooted bots path masquerading as empty.
- **Per-controller intent.** In a flag-driven resume, controllers WITHOUT the flag are NOT copied even if the resolved source holds their ledger — decision `skipped_not_flagged` plus a structured warning naming the controller and the ledger left behind (the operator's fix is adding the flag). The instance-level sqlite half still copies whenever the resume runs — the executor DB is whole-instance history, not per-controller.
- **Manifest + preview.** Both gain `"resolution": "controller_flag"` and the flagged controller ids; `ResolvedSource` itself is UNCHANGED, so everything downstream (guards → copy plan → copy → events) is untouched.

## 6. Copy set — config-derived, not globbed *(v2: B3, R1)*

The copy set is computed **per controller in the new deploy**, from the staged controller YAMLs:

1. For each controller of type `range_inventory_ladder`: expected state file = `state_file_name` if set, else `range_inventory_ladder_<id>.json` (mirrors `range_inventory_ladder.py:1471`). Copy that file + its `.owner` sidecar from the source `data/`.
2. If `state_file_name` is an **absolute path**, it escapes `data/` entirely — skip it and warn loudly (it is not the hook's to manage; the operator has opted into a shared-mount scheme).
3. For SQLite deployments only (engine `db_mode: sqlite`): copy `<config>.sqlite` and, if present, its `-journal` sidecar (default rollback journal — no WAL is configured). Postgres deployments (this operator) have no per-instance sqlite: skip.
4. `resume_extra_paths` (optional, advanced): additive relative paths, each validated to resolve **within** the source `data/` (containment check — `_validate_safe_name` covers names, not globs, so this needs its own guard).

Never copied: `*.diagnostic_*.jsonl` (session-stamped by design, `range_inventory_ladder.py:1487-1489`), `*.tmp` (interrupted atomic-write remnants), logs, market-data.

**Per-controller semantics** *(v2: R1 — replaces blanket abort)*:

| Case | Action |
|---|---|
| Controller in new deploy, ledger found + valid in source | Copy |
| Controller in new deploy, no ledger in source | **Warn + fresh seed for that controller** (legitimate first run — e.g. a newly added controller) |
| Controller in new deploy, ledger present but invalid (zero-length / unparseable) | **Abort** (never seed garbage; the engine would quarantine and re-seed) |
| Ledger in source for a controller **not** in the new deploy | Skip (log it) |

Identity is validated via the `.owner` JSON's `controller_id` field (`range_inventory_ladder.py:2009`), **not** the filename — a custom `state_file_name` carries no id. A `.owner`/deploy id mismatch on an expected ledger → abort.

## 7. Preconditions & guards (all must pass, else fail-closed)

1. **Source container has exited.** Checked via Docker container state (not `.owner` PID — PIDs are container-namespaced and unverifiable from the host). If the container object was already removed, the directory-on-disk is accepted as source provided no container of that name is running. *(v2: B1.)*
2. **Each expected ledger is valid.** Exists, non-zero length, parses as JSON. Because writes are atomic+fsync (`:2020-2029`), a ledger on disk after container exit is internally consistent and at most one mutation stale — there is **no save-on-stop** (verified: `_save_state()` fires only at load-repair `:1968`, first-init `:2186`, fill-booking `:2540`, reseed `:2749`, and per-tick `update_processed_data` `:4336`), so no mtime-based "flushed at stop" check is possible or needed. *(v2: R4.)*
3. **Controller-id alignment** per §6 (via `.owner`, per-controller semantics).
4. **Destination `data/` contains no state files.** It won't at this point in `create_hummingbot_instance`, but assert it — never overwrite an unexpected seed.
5. **(Advisory) source stopped gracefully.** If the source's last run shows a hard kill (engine `BotRun.ended_ts_ms` NULL / API `bot_runs.run_status != STOPPED`) or the exchange still reports open orders, **warn loudly and require an explicit override flag** (`resume_accept_ungraceful: true`) — the hook carries the ledger, not order cleanup; orphaned orders must be cancelled by the operator first.

## 8. `.owner` marker handling

Copy it forward (recommended): it is the identity-validation input (§6) and preserves lineage; the new process rewrites it with its own PID on first save. Because the source container has exited (guard 1), there is no live contention, and id alignment (guard 3) prevents the only warning case. Provenance beyond that lives in the manifest (§10).

## 9. Config surface (deploy models — description only) *(v2: R5 — `resume_strict` removed)*

New optional fields on `V2ControllerDeployment` / `V2ScriptDeployment` (`models/bot_orchestration.py`), all defaulting to today's behavior:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `resume_mode` | enum `off` / `explicit` / `latest` | `off` | Whether/how to seed `data/` |
| `resume_from` | optional string | none | Prior instance name (required when `explicit`; `_validate_safe_name`-checked) |
| `resume_from_archive` | bool | `false` | Allow sourcing from `bots/archived/` (local-move archives only) |
| `resume_extra_paths` | optional list of relative paths | none | Additive copy items, containment-validated within source `data/` |
| `resume_accept_ungraceful` | bool | `false` | Explicit override for guard 5's hard-kill/open-orders block |

There is **no** soft/best-effort mode: any guard failure aborts the deploy before a container exists. The instance name is still timestamped (no change to `routers/bot_orchestration.py:504,590`) — new identity, carried-forward state.

## 10. End-to-end "upgrade + resume" flow *(v2: B1 — stop-container step added)*

1. **Pull** the new image (`/docker/pull-image`); pre-flight that it contains `psycopg2` (engine-on-Postgres needs the driver; stock images lack it — the fork's build already probes imports in the final image).
2. **Graceful stop** the running bot (`/stop-bot`, MQTT `stop`): cancels orders, executors early-stop, final positions/PnL land in Postgres. Note: this **does not exit the container** — the headless process idles in its keep-alive loop.
3. **Stop the container** (`/docker/stop-container/{name}`) and confirm it exited. Only now is the ledger quiesced with a guaranteed single owner.
4. **(Recommended) verify the exchange is flat**: no resting orders for the account/pair; if any, cancel them before proceeding (guard 5 will otherwise block, or demand `resume_accept_ungraceful`).
5. **Deploy** the new instance: new `image:` tag, same `controllers_config` (same controller `id:` values), `resume_mode: explicit`, `resume_from: <the stopped instance name>`.
6. Inside `create_hummingbot_instance`: instance dir + empty `data/` created → conf staged → **hook runs** (resolve source → guards §7 → config-derived copy §6 → config-drift diff §12 → manifest) → container starts.
7. New engine boots on the new image → `_load_state()` finds the ledger → **ladder resumes**; engine-Postgres re-attaches positions/PnL by `controller_id`.
8. (Optional) archive the old instance afterward — never before the new bot is confirmed healthy.

## 11. Failure modes & handling (fail-closed table)

| Condition | Action |
|---|---|
| Source instance not found | Abort; error lists searched paths (instances + archived, noting the nesting check) |
| `latest` ambiguous (multiple lineages / conflicting ids) | Abort; require `explicit` |
| Source container still running (incl. idle-after-stop-bot) | Abort: "stop the container first" |
| Expected ledger missing for a deployed controller | Warn + fresh seed for that controller only (§6) |
| Expected ledger zero-length / invalid JSON | Abort (never seed garbage) |
| `.owner` controller-id mismatch on an expected ledger | Abort (wrong ledger) |
| `state_file_name` is absolute | Skip that file + loud warning (shared-mount scheme assumed) |
| Destination `data/` already has state files | Abort (unexpected state) |
| Source ended non-gracefully / open orders on exchange | Abort unless `resume_accept_ungraceful: true`; always warn |
| `resume_extra_paths` escapes source `data/` | Abort (containment violation) |
| Copy I/O error | Abort before container start (no half-seeded run) |
| Controller `resume_mode` value not `latest`/`off`, or a flagged yml that cannot be safely stripped (5a) | Abort `RESUME_FLAG_INVALID` (never guess; never let the engine-forbidden key through unstripped) |
| Flagged controllers resolve to DIFFERENT source instances (5a) | Abort `CONTROLLER_SOURCES_DIVERGENT`; message names each controller and its winning instance; require request-level `explicit` |

Every failure aborts **before** the container launches: a failed resume can never produce a silently-fresh, mis-seeded live bot. Two non-abort flag-driven outcomes (5a) also proceed to launch: a TRUE first run (no flagged controller's ledger exists anywhere → `RESUME_FIRST_RUN_FRESH_SEED` warning, no manifest) and a non-flagged controller in a flag-driven resume (`skipped_not_flagged` + warning, its ledger left behind).

## 12. Config-drift check *(v2: C3 — new)*

The fork supports runtime-updatable controller config (per-file reload). Live edits made while the old bot ran exist only in the **old instance's** `conf/controllers/`; a redeploy stages YAMLs from the shared template `bots/conf/controllers/` (`docker_service.py:234-269`). A resumed bot can therefore run different parameters than the bot that stopped — which would masquerade as "resume misbehaved."

The hook diffs, per controller, the **source instance's** staged YAML against the **new instance's** staged YAML and, on any difference, logs a field-level warning and records the diff in the manifest. (An opt-in `resume_carry_config: true` that copies the source instance's controller YAMLs forward instead is a possible later extension — default remains template-wins, warn-on-drift.)

## 13. Observability

- Write `data/resume.manifest.json` in the new instance: source instance + path, resolution mode, files copied (sizes + hashes), per-controller decisions (copied / fresh-seed / skipped), config-drift diff summary, guard results, timestamp. Audit trail + double-resume detector.
- Structured events: `bot_resume_seeded` / `bot_resume_failed` (with reason enum matching §11 rows).
- One-line log: `Resumed <new> from <source>: copied range_inventory_ladder_xmr_usdt.json (…); 1 controller fresh-seeded; config drift: none.`

## 14. Operating constraints *(v2: C1, C2 — must be documented for operators)*

1. **Controller `id:` is a fleet-wide unique key under shared engine-Postgres.** All bots sharing one `conf_client.yml` DBOtherMode target share one engine DB; startup loads `get_all_executors()` / `get_all_positions()` unfiltered and matches on controller-id membership alone (`executor_orchestrator.py:207-230`). Duplicate ids across bots bleed PnL/positions into each other — and this design deepens reliance on id stability. Rule: **one controller `id:` per logical bot, unique across every bot that shares the engine DB.**
2. **Expected post-resume warnings.** After a gap (fees drift, price moves, manual transfers), the ladder's ledger-vs-wallet reconcile/understatement checks may warn. This is expected behavior on a healthy resume — do not roll back on these warnings alone.
3. **Keep the join keys byte-identical across runs:** controller `id:` and (if set) `state_file_name`. They are the re-attach keys for both Postgres accounting and the ledger file.

## 15. Notes specific to this deployment (engine-on-Postgres + range-ladder)

- **Postgres half is already handled** — positions/PnL/fills re-attach by `controller_id` (external DB). The hook does not touch Postgres.
- **Only the ladder `.json` (+`.owner`) needs carrying** — no per-instance sqlite exists for Postgres deployments.
- **`config_file_path` still rotates** per deploy, so in-flight orders will not restore from `MarketState` — moot, because the flow's graceful stop cancelled them. The hook must not attempt to copy or rewrite MarketState.

## 16. Alternatives considered

| Alternative | Verdict |
|---|---|
| Shared absolute `state_file_name` on a shared mount | Simpler infra, but risks two bots on one ledger (`.owner` is warn-only), puts state in code dirs, doesn't cover sqlite |
| Reuse an un-timestamped instance name | Fights the immutable-instance model; container-name conflicts; `conf/` wipe semantics |
| `start_container` in-place restart | Cannot change the image — useless for upgrades |
| Restore-from-archive endpoint | Useful complement as an alternate *source*; the core remains the pre-start copy |
| Manual Phase-0 copy (no code) | Racy: deploy auto-starts and fresh-seeds before a human can intervene (§1); acceptable only as an emergency procedure |
| **Copy-forward at instance creation (this design)** | Immutable instances kept, per-run isolation, deterministic source, single-owner-safe by ordering, race-free by construction |

## 17. Limitations

- Resumes the **ladder ledger + Postgres accounting**; the ladder re-enters from that ledger. It does **not** reattach to the exact prior live orders/executors (engine limitation). This is "resume where it left off" in the achievable sense.
- Requires a graceful stop + container stop; a hard-killed source needs manual order cleanup and the explicit `resume_accept_ungraceful` override.
- Requires the new image to keep `psycopg2` for engine-on-Postgres.
- S3-archived sources are not restorable without adding a download/extract path (absent today).

## 18. Test plan outline (for whenever it is built — per repo test rules)

- **Unit — source resolution:** `off`/`explicit`/`latest`; final-suffix-only base-name parsing (incl. operator names embedding their own timestamps); Postgres-lineage vs directory fallback; ambiguity → abort; archived local-move source; nested-archive detection; S3-mode source → clear rejection.
- **Unit — copy set:** derived from staged YAMLs (`state_file_name` custom name; default name; absolute path → skip+warn); per-controller semantics (copy / fresh-seed-warn / skip / abort-on-invalid); `.owner`-based id validation; sqlite + `-journal` only when `db_mode: sqlite`; `resume_extra_paths` containment.
- **Unit — guards:** running-container rejection (incl. idle-after-stop-bot case); zero-length/invalid JSON abort; non-empty destination abort; ungraceful-source block + override flag.
- **Regression:** end-to-end mock-docker sequence stop-bot → stop-container → deploy-with-resume lands the ledger byte-identical in the new `data/` before container start; every §11 row aborts without launching a container; manifest contents; config-drift diff fires on a mutated template.
- **Integration (mock exchange):** resumed ladder loads the copied ledger (no re-seed event) and Postgres accounting continues under the same controller id.
