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

from services.resume_service import (
    CopyItem,
    CopyPlan,
    ResolvedSource,
    ResumeAbortReason,
    ResumeError,
    _is_absolute_state_file,
    _is_excluded,
    compute_copy_plan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_dep(extra_paths=None):
    """A minimal deploy stand-in — compute_copy_plan only reads resume_extra_paths.

    A SimpleNamespace (not the pydantic model) is used deliberately so the
    escape test can pass a ``..`` path that the model layer would reject — the
    point is to prove the *service* guard fails closed independently.
    """
    return SimpleNamespace(resume_extra_paths=extra_paths)


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


def write_ledger(source, filename, content='{"seed_value_quote": 100}', owner_id=...):
    """Write a ledger file in the source data/. ``owner_id=...`` (sentinel)
    means "no .owner sidecar"; ``owner_id=None`` writes a sidecar with no id."""
    p = source.data_dir / filename
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
    def test_is_absolute_posix(self):
        assert _is_absolute_state_file("/abs/ledger.json")

    def test_is_absolute_windows_drive(self):
        assert _is_absolute_state_file("C:/abs/ledger.json")
        assert _is_absolute_state_file("C:\\abs\\ledger.json")

    def test_is_absolute_relative_is_false(self):
        assert not _is_absolute_state_file("ledger.json")
        assert not _is_absolute_state_file("sub/ledger.json")

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
        write_ledger(src, "my_ledger.json", owner_id="ctrl_b")
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
# Absolute state_file_name
# ---------------------------------------------------------------------------

class TestAbsolute:
    def test_absolute_skipped_with_warning(self, tmp_path):
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(
            new, "c.yml", controller_id="ctrl_c", state_file_name="/mnt/shared/ledger.json"
        )

        plan = compute_copy_plan(new, src, make_dep())

        assert plan.decisions == {"ctrl_c": "skipped"}
        assert plan.files_to_copy == []  # nothing physically copied
        markers = kinds(plan, "absolute_skipped")
        assert len(markers) == 1 and markers[0].dst is None
        assert any("absolute" in w.lower() for w in plan.warnings)


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
        write_ledger(src, "range_inventory_ladder_ctrl_z.json", owner_id="ctrl_z")
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
