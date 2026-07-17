"""Phase 3 tests: config-derived copy set + per-controller semantics (design §6).

Covers ``services.resume_service.compute_copy_plan``:

  * default state-file name / custom ``state_file_name``
  * absolute ``state_file_name`` -> skip + loud warning (never copied)
  * ``.owner`` identity: mismatch -> abort; missing x2 (default-named warn+copy;
    custom-named -> OWNER_MISMATCH, identity unverifiable)
  * zero-length / unparseable ledger -> LEDGER_INVALID abort
  * no ledger in source -> warn + fresh_seed for that controller only
  * source ledger for a controller not in the deploy -> skipped (logged)
  * sqlite half on/off via a fixture ``conf_client.yml``
  * ``resume_extra_paths``: ok / escape (``..`` and a symlink pointing outside)
    / missing
  * never-copy exclusion list honored

All filesystem via ``tmp_path``. No Docker, no DB, no network.
"""

import json
import os
from types import SimpleNamespace

import pytest
import yaml

from ledger_fixtures import valid_ledger_payload
from services.resume_service import (
    CopyItem,
    CopyPlan,
    ResolvedSource,
    ResumeAbortReason,
    ResumeError,
    _is_excluded,
    compute_copy_plan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def link_dir_out_of_tree(link: "os.PathLike", target: "os.PathLike") -> None:
    """Create ``link`` as a directory link to ``target``, or FAIL the test.

    The C1 runtime-containment tests are the only coverage of the symlink escape,
    so they must never silently vanish. A ``pytest.skip`` here would mean the
    security invariant is unproven on exactly the platform whose path semantics
    make it interesting — a green suite that verified nothing.

    Symlink creation on Windows needs SeCreateSymbolicLinkPrivilege (Developer
    Mode or admin). Directory JUNCTIONS need no privilege and are resolved by
    ``Path.resolve`` identically, so they exercise the same code path. We try a
    symlink, fall back to a junction, and fail loudly if neither is available
    rather than skipping.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError) as symlink_exc:
        if os.name != "nt":
            pytest.fail(
                f"Could not create the symlink this containment test requires "
                f"({symlink_exc}). Refusing to skip: that would leave CONTRACT C1's "
                f"runtime half unverified."
            )
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        except Exception as junction_exc:
            pytest.fail(
                f"Could not create a symlink ({symlink_exc}) or a junction "
                f"({junction_exc}) for this containment test. Refusing to skip: that "
                f"would leave CONTRACT C1's runtime half unverified on this host."
            )


def make_dep(extra_paths=None, allow_absolute_state_file_name=False):
    """A minimal deploy stand-in — compute_copy_plan reads resume_extra_paths and
    the CONTRACT C1 opt-out.

    A SimpleNamespace (not the pydantic model) is used deliberately so the
    escape test can pass a ``..`` path that the model layer would reject — the
    point is to prove the *service* guard fails closed independently.
    """
    return SimpleNamespace(
        resume_extra_paths=extra_paths,
        allow_absolute_state_file_name=allow_absolute_state_file_name,
    )


def make_source(tmp_path, name="SRC"):
    inst = tmp_path / "instances" / name
    data = inst / "data"
    data.mkdir(parents=True, exist_ok=True)
    return ResolvedSource(
        instance_name=name, data_dir=data, instance_dir=inst, origin="instances"
    )


def new_instance(tmp_path, name="NEW"):
    return tmp_path / "instances" / name


def write_controller(
    new_instance_dir,
    filename,
    *,
    controller_id,
    state_file_name=None,
    controller_name="range_inventory_ladder",
    extra=None,
):
    cdir = new_instance_dir / "conf" / "controllers"
    cdir.mkdir(parents=True, exist_ok=True)
    doc = {
        "id": controller_id,
        "controller_name": controller_name,
        "controller_type": "market_making",
        # Match the ledger fixture's identity. The engine compares these against
        # its RESOLVED config, falling back to its model defaults (binance /
        # ETH-USDT) for anything the YAML omits — so a staged config without them
        # would rightly abort the copy (CDX-M02/CDX-R02) and mask what these
        # tests are actually about.
        "connector_name": "nonkyc",
        "trading_pair": "XMR-USDT",
    }
    if state_file_name is not None:
        doc["state_file_name"] = state_file_name
    if extra:
        doc.update(extra)
    (cdir / filename).write_text(yaml.safe_dump(doc), encoding="utf-8")


def write_conf_client(new_instance_dir, db_engine="sqlite"):
    cdir = new_instance_dir / "conf"
    cdir.mkdir(parents=True, exist_ok=True)
    doc = {} if db_engine is None else {"db_mode": {"db_engine": db_engine}}
    (cdir / "conf_client.yml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def write_ledger(
    source, filename, content=None, owner_id=..., ledger_controller_id="ctrl_a"
):
    """Write a ledger file in the source data/. ``owner_id=...`` (sentinel)
    means "no .owner sidecar"; ``owner_id=None`` writes a sidecar with no id.

    The body defaults to a VALID engine envelope (CDX-M02). It used to be
    ``{"seed_value_quote": 100}``, which the old length+JSON validator accepted —
    but it was never a ledger the engine would load, so every "copied" assertion
    below was really asserting that a file the bot quarantines got copied. The
    envelope validator makes that fixture a LEDGER_INVALID abort, so it is now
    built from the engine's contract (see ``tests/ledger_fixtures``).

    ``ledger_controller_id`` is the ledger's INTERNAL controller_id and must equal
    the canonical id of the controller under test — it is deliberately separate
    from ``owner_id`` so the owner-mismatch tests still isolate the SIDECAR as the
    only thing that disagrees.

    ``content`` (an explicit string) still writes bytes verbatim, which is what
    the zero-length and bad-JSON syntax tests need.
    """
    p = source.data_dir / filename
    if content is None:
        content = json.dumps(valid_ledger_payload(ledger_controller_id))
    p.write_text(content, encoding="utf-8")
    if owner_id is not ...:
        marker = {"pid": 1, "started_at": 1.0}
        if owner_id is not None:
            marker["controller_id"] = owner_id
        (source.data_dir / f"{filename}.owner").write_text(
            json.dumps(marker, sort_keys=True), encoding="utf-8"
        )
    return p


def kinds(plan, kind):
    return [it for it in plan.items if it.kind == kind]


# ---------------------------------------------------------------------------
# Small unit helpers
# ---------------------------------------------------------------------------

class TestUnitHelpers:
    def test_exclusions(self):
        assert _is_excluded("range_inventory_ladder_x.diagnostic_20260101-000000.jsonl")
        assert _is_excluded("something.tmp")
        assert _is_excluded("hummingbot_logs.log")
        assert not _is_excluded("range_inventory_ladder_x.json")
        assert not _is_excluded("trades.sqlite")


# ---------------------------------------------------------------------------
# Default / custom names
# ---------------------------------------------------------------------------

class TestNames:
    def test_default_name_copies_ledger_and_owner(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        plan = compute_copy_plan(new, src, make_dep())

        assert plan.decisions == {"ctrl_a": "copied"}
        assert {it.kind for it in plan.files_to_copy} == {"ledger", "owner"}
        ledger = kinds(plan, "ledger")[0]
        assert ledger.src == src.data_dir / "range_inventory_ladder_ctrl_a.json"
        assert ledger.dst == new / "data" / "range_inventory_ladder_ctrl_a.json"
        assert ledger.controller_id == "ctrl_a"

    def test_custom_state_file_name(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "my_ledger.json", owner_id="ctrl_b", ledger_controller_id="ctrl_b")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_b", state_file_name="my_ledger.json")

        plan = compute_copy_plan(new, src, make_dep())

        assert plan.decisions == {"ctrl_b": "copied"}
        ledger = kinds(plan, "ledger")[0]
        assert ledger.dst == new / "data" / "my_ledger.json"

    def test_non_ladder_controller_ignored(self, tmp_path):
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="pmm_1", controller_name="pmm_simple")

        plan = compute_copy_plan(new, src, make_dep())
        assert plan.decisions == {}
        assert plan.files_to_copy == []


# ---------------------------------------------------------------------------
# CONTRACT C1 — state_file_name path contract (CDX-007 / CLA-004)
# ---------------------------------------------------------------------------
#
# SPEC CHANGE (this phase): an absolute ``state_file_name`` used to be SKIPPED and
# the deploy allowed to succeed — the fail-open CDX-007 names. It now aborts, and
# the skip is reachable only behind the explicit ``allow_absolute_state_file_name``
# opt-out, which permits ABSOLUTE paths and never traversal.
#
# Every expected value below is derived from CONTRACT C1 as written in the batch
# prompt (and mirrored in services/state_file_contract.py), not from running the
# implementation.

class TestC1PathContract:
    """The C1 reject set: every one of these must ABORT the deploy."""

    def test_posix_absolute_aborts(self, tmp_path):
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_c", state_file_name="/tmp/x.json")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.STATE_FILE_PATH_INVALID

    def test_windows_drive_absolute_aborts(self, tmp_path):
        """``C:\\x.json`` is a single RELATIVE component to PurePosixPath and
        normalizes to a strict descendant of data/ — a POSIX-only check accepts it.
        C1 requires both flavors, so it is refused on the Linux host too."""
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_c", state_file_name="C:\\x.json")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.STATE_FILE_PATH_INVALID

    def test_unc_path_aborts(self, tmp_path):
        """Same blindness as the drive case: ``\\\\share\\x`` is one relative
        component under PurePosixPath."""
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_c", state_file_name="\\\\share\\x")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.STATE_FILE_PATH_INVALID

    def test_traversal_aborts(self, tmp_path):
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_c", state_file_name="../conf/x.yml")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.STATE_FILE_PATH_INVALID

    def test_traversal_aborts_even_with_optout(self, tmp_path):
        """C1: the opt-out permits ABSOLUTE paths only, never traversal."""
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_c", state_file_name="../conf/x.yml")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep(allow_absolute_state_file_name=True))
        assert exc.value.reason is ResumeAbortReason.STATE_FILE_PATH_INVALID

    def test_absolute_traversal_aborts_even_with_optout(self, tmp_path):
        """``/tmp/../etc/x.json`` is absolute AND traversing. C1 checks traversal
        FIRST, so the opt-out must not rescue it."""
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(
            new, "c.yml", controller_id="ctrl_c", state_file_name="/tmp/../etc/x.json"
        )

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep(allow_absolute_state_file_name=True))
        assert exc.value.reason is ResumeAbortReason.STATE_FILE_PATH_INVALID

    def test_backslash_traversal_aborts(self, tmp_path):
        """``sub\\..\\..\\x.json`` hides its '..' from PurePosixPath entirely (one
        opaque component). Only the Windows flavor sees the traversal."""
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(
            new, "c.yml", controller_id="ctrl_c", state_file_name="sub\\..\\..\\x.json"
        )

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.STATE_FILE_PATH_INVALID

    def test_dot_aborts(self, tmp_path):
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_c", state_file_name=".")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.STATE_FILE_PATH_INVALID

    def test_non_string_aborts(self, tmp_path):
        """C1 accepts unset or a str. A YAML ``state_file_name: 5`` is neither, and
        the old code would have str()'d it into a filename."""
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_c", state_file_name=5)

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.STATE_FILE_PATH_INVALID

    def test_drive_relative_aborts_even_with_optout(self, tmp_path):
        """``C:`` has a drive but is_absolute() is False, so it is NOT an absolute
        path and the opt-out (absolute paths only) must not admit it. Mirrors the
        engine rejecting it after its own opt-out early-return."""
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_c", state_file_name="C:")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep(allow_absolute_state_file_name=True))
        assert exc.value.reason is ResumeAbortReason.STATE_FILE_PATH_INVALID

    def test_root_relative_aborts_even_with_optout(self, tmp_path):
        """``\\x.json`` is rooted under the Windows flavor but not absolute (no
        drive) — same reasoning as the drive-relative case."""
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_c", state_file_name="\\x.json")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep(allow_absolute_state_file_name=True))
        assert exc.value.reason is ResumeAbortReason.STATE_FILE_PATH_INVALID

    def test_nothing_planned_when_c1_aborts(self, tmp_path):
        """The abort must leave no plan behind: a controller with a valid ledger
        earlier in the same deploy must not end up half-planned."""
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        new = new_instance(tmp_path)
        write_controller(new, "a.yml", controller_id="ctrl_a")
        write_controller(new, "b.yml", controller_id="ctrl_b", state_file_name="/tmp/x.json")

        with pytest.raises(ResumeError):
            compute_copy_plan(new, src, make_dep())
        # Nothing was copied: compute_copy_plan is read-only and the deploy aborts
        # before _execute_copy_plan ever runs.
        assert not (new / "data").exists()


class TestC1Accepted:
    """The C1 accept set."""

    def test_relative_subdir_planned_normally(self, tmp_path):
        src = make_source(tmp_path)
        (src.data_dir / "sub").mkdir()
        write_ledger(src, "sub/x.json", owner_id="ctrl_c", ledger_controller_id="ctrl_c")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_c", state_file_name="sub/x.json")

        plan = compute_copy_plan(new, src, make_dep())

        assert plan.decisions == {"ctrl_c": "copied"}
        ledger = kinds(plan, "ledger")[0]
        assert ledger.src == src.data_dir / "sub" / "x.json"
        assert ledger.dst == new / "data" / "sub" / "x.json"

    def test_whitespace_is_stripped_to_canonical(self, tmp_path):
        """C1: the canonical value is the STRIPPED string."""
        src = make_source(tmp_path)
        write_ledger(src, "my_ledger.json", owner_id="ctrl_c", ledger_controller_id="ctrl_c")
        new = new_instance(tmp_path)
        write_controller(
            new, "c.yml", controller_id="ctrl_c", state_file_name="  my_ledger.json  "
        )

        plan = compute_copy_plan(new, src, make_dep())

        assert plan.decisions == {"ctrl_c": "copied"}
        assert kinds(plan, "ledger")[0].dst == new / "data" / "my_ledger.json"

    def test_empty_after_strip_uses_default_name(self, tmp_path):
        """C1: empty-after-strip maps to unset, which selects the engine's DEFAULT
        state file name. Not fail-open — the controller still resumes."""
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_c.json", owner_id="ctrl_c", ledger_controller_id="ctrl_c")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_c", state_file_name="   ")

        plan = compute_copy_plan(new, src, make_dep())

        assert plan.decisions == {"ctrl_c": "copied"}
        assert kinds(plan, "ledger")[0].dst == (
            new / "data" / "range_inventory_ladder_ctrl_c.json"
        )


class TestC1OptOut:
    def test_absolute_skipped_with_structured_warning(self, tmp_path):
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(
            new, "c.yml", controller_id="ctrl_c", state_file_name="/mnt/shared/ledger.json"
        )

        plan = compute_copy_plan(new, src, make_dep(allow_absolute_state_file_name=True))

        assert plan.decisions == {"ctrl_c": "skipped"}
        assert plan.files_to_copy == []  # nothing physically copied
        markers = kinds(plan, "absolute_skipped")
        assert len(markers) == 1 and markers[0].dst is None
        # Structured, not just prose: this is what reaches the response body.
        entries = [
            w for w in plan.structured_warnings
            if w["code"] == "STATE_FILE_ABSOLUTE_SKIPPED"
        ]
        assert len(entries) == 1
        assert entries[0]["controller_id"] == "ctrl_c"
        assert entries[0]["state_file_name"] == "/mnt/shared/ledger.json"

    def test_optout_default_is_off(self, tmp_path):
        """A deploy stand-in with NO opt-out attribute at all must still abort —
        the getattr default is the fail-closed one."""
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(
            new, "c.yml", controller_id="ctrl_c", state_file_name="/mnt/shared/ledger.json"
        )

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, SimpleNamespace(resume_extra_paths=None))
        assert exc.value.reason is ResumeAbortReason.STATE_FILE_PATH_INVALID


class TestC1RuntimeContainment:
    """The runtime half: lexically-clean names that escape via a symlink."""

    def test_symlink_escape_in_destination_aborts(self, tmp_path):
        """``sub/x.json`` is C1-clean lexically, but if the new instance's
        ``data/sub`` is a symlink out of data/, the ledger would be WRITTEN
        outside. Only resolving catches this."""
        src = make_source(tmp_path)
        (src.data_dir / "sub").mkdir()
        write_ledger(src, "sub/x.json", owner_id="ctrl_c", ledger_controller_id="ctrl_c")

        new = new_instance(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        new_data = new / "data"
        new_data.mkdir(parents=True)
        link_dir_out_of_tree(new_data / "sub", outside)

        write_controller(new, "c.yml", controller_id="ctrl_c", state_file_name="sub/x.json")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.STATE_FILE_PATH_INVALID

    def test_symlink_escape_in_source_aborts(self, tmp_path):
        """The mirror case: reading the ledger THROUGH a symlink that leaves the
        source data/ would copy an arbitrary file forward as this controller's state."""
        src = make_source(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "x.json").write_text('{"seed_value_quote": 1}', encoding="utf-8")
        link_dir_out_of_tree(src.data_dir / "sub", outside)

        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_c", state_file_name="sub/x.json")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.STATE_FILE_PATH_INVALID


# ---------------------------------------------------------------------------
# .owner identity
# ---------------------------------------------------------------------------

class TestOwnerIdentity:
    def test_owner_mismatch_aborts(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="SOMEONE_ELSE")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason == ResumeAbortReason.OWNER_MISMATCH

    def test_owner_missing_default_named_warns_and_copies(self, tmp_path):
        # Default-named ledger embeds the id in the filename -> identity OK.
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id=...)  # no sidecar
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        plan = compute_copy_plan(new, src, make_dep())

        assert plan.decisions == {"ctrl_a": "copied"}
        assert {it.kind for it in plan.files_to_copy} == {"ledger"}  # no owner item
        assert any("no '.owner'" in w or "no `.owner`" in w or ".owner" in w for w in plan.warnings)

    def test_owner_missing_custom_named_aborts(self, tmp_path):
        # Custom name carries no id -> identity unverifiable -> fail closed.
        src = make_source(tmp_path)
        write_ledger(src, "custom.json", owner_id=...)  # no sidecar
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a", state_file_name="custom.json")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason == ResumeAbortReason.OWNER_MISMATCH

    def test_owner_unparseable_aborts(self, tmp_path):
        src = make_source(tmp_path)
        p = write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id=...)
        (src.data_dir / f"{p.name}.owner").write_text("{ not json", encoding="utf-8")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason == ResumeAbortReason.OWNER_MISMATCH


# ---------------------------------------------------------------------------
# Ledger validity
# ---------------------------------------------------------------------------

class TestLedgerValidity:
    def test_zero_length_aborts(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", content="", owner_id="ctrl_a")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason == ResumeAbortReason.LEDGER_INVALID

    def test_bad_json_aborts(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(
            src, "range_inventory_ladder_ctrl_a.json", content="{ not: valid", owner_id="ctrl_a"
        )
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason == ResumeAbortReason.LEDGER_INVALID


# ---------------------------------------------------------------------------
# Fresh-seed / skip
# ---------------------------------------------------------------------------

class TestFreshSeedAndSkip:
    def test_no_ledger_fresh_seeds_with_warning(self, tmp_path):
        src = make_source(tmp_path)  # empty source data/
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_new")

        plan = compute_copy_plan(new, src, make_dep())

        assert plan.decisions == {"ctrl_new": "fresh_seed"}
        assert plan.files_to_copy == []
        assert any("fresh" in w.lower() for w in plan.warnings)

    def test_source_ledger_not_deployed_is_skipped(self, tmp_path):
        src = make_source(tmp_path)
        # Deployed controller with a valid ledger.
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        # Orphan ledger for a controller NOT in the deploy.
        write_ledger(src, "range_inventory_ladder_ctrl_z.json", owner_id="ctrl_z", ledger_controller_id="ctrl_z")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        plan = compute_copy_plan(new, src, make_dep())

        assert plan.decisions["ctrl_a"] == "copied"
        assert plan.decisions["ctrl_z"] == "skipped"
        # The orphan is never queued for copy.
        copied_srcs = {it.src.name for it in plan.files_to_copy}
        assert "range_inventory_ladder_ctrl_z.json" not in copied_srcs


# ---------------------------------------------------------------------------
# SQLite half
# ---------------------------------------------------------------------------

class TestSqlite:
    def test_sqlite_deployment_copies_db_and_journal(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        (src.data_dir / "trades.sqlite").write_text("db", encoding="utf-8")
        (src.data_dir / "trades.sqlite-journal").write_text("j", encoding="utf-8")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")
        write_conf_client(new, db_engine="sqlite")

        plan = compute_copy_plan(new, src, make_dep())

        names = {it.src.name for it in plan.files_to_copy}
        assert "trades.sqlite" in names
        assert "trades.sqlite-journal" in names
        assert kinds(plan, "sqlite") and kinds(plan, "sqlite_journal")

    def test_postgres_deployment_copies_no_sqlite(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        (src.data_dir / "trades.sqlite").write_text("db", encoding="utf-8")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")
        write_conf_client(new, db_engine="postgresql")

        plan = compute_copy_plan(new, src, make_dep())

        names = {it.src.name for it in plan.files_to_copy}
        assert "trades.sqlite" not in names
        assert kinds(plan, "sqlite") == []

    def test_missing_conf_client_defaults_to_sqlite(self, tmp_path):
        # Engine default is DBSqliteMode -> a missing conf_client.yml is sqlite.
        src = make_source(tmp_path)
        (src.data_dir / "trades.sqlite").write_text("db", encoding="utf-8")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")  # no conf_client.yml

        plan = compute_copy_plan(new, src, make_dep())
        assert "trades.sqlite" in {it.src.name for it in plan.files_to_copy}


# ---------------------------------------------------------------------------
# resume_extra_paths
# ---------------------------------------------------------------------------

class TestExtraPaths:
    def test_extra_path_ok(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        (src.data_dir / "sub").mkdir()
        (src.data_dir / "sub" / "extra.bin").write_text("x", encoding="utf-8")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        plan = compute_copy_plan(new, src, make_dep(extra_paths=["sub/extra.bin"]))

        extras = kinds(plan, "extra")
        assert len(extras) == 1
        assert extras[0].dst == new / "data" / "sub" / "extra.bin"

    def test_extra_path_escape_via_dotdot(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep(extra_paths=["../../outside.txt"]))
        assert exc.value.reason == ResumeAbortReason.EXTRA_PATH_ESCAPE

    def test_extra_path_escape_via_symlink(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        outside = tmp_path / "outside_dir"
        outside.mkdir()
        (outside / "target.txt").write_text("secret", encoding="utf-8")
        link = src.data_dir / "link.txt"
        try:
            os.symlink(outside / "target.txt", link)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not supported on this platform/privilege level")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep(extra_paths=["link.txt"]))
        assert exc.value.reason == ResumeAbortReason.EXTRA_PATH_ESCAPE

    def test_extra_path_missing(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep(extra_paths=["nope.bin"]))
        assert exc.value.reason == ResumeAbortReason.EXTRA_PATH_MISSING


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------

class TestExclusions:
    def test_never_copy_list_honored(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        # Junk that must never be copied, even with sqlite on.
        (src.data_dir / "range_inventory_ladder_ctrl_a.diagnostic_20260101-000000.jsonl").write_text(
            "{}", encoding="utf-8"
        )
        (src.data_dir / "leftover.tmp").write_text("x", encoding="utf-8")
        (src.data_dir / "hb.log").write_text("x", encoding="utf-8")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")
        write_conf_client(new, db_engine="sqlite")

        plan = compute_copy_plan(new, src, make_dep())

        copied = {it.src.name for it in plan.files_to_copy}
        assert not any(_is_excluded(n) for n in copied)
        assert "leftover.tmp" not in copied
        assert "hb.log" not in copied


# ---------------------------------------------------------------------------
# Dataclass sanity
# ---------------------------------------------------------------------------

class TestDataclasses:
    def test_copyitem_copyable(self, tmp_path):
        a = CopyItem(src=tmp_path / "x", dst=tmp_path / "y", controller_id="c", kind="ledger")
        b = CopyItem(src=tmp_path / "x", dst=None, controller_id="c", kind="absolute_skipped")
        assert a.copyable and not b.copyable
        plan = CopyPlan(items=[a, b])
        assert plan.files_to_copy == [a]
