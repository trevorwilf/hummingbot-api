import asyncio
import logging
import os
import secrets
import shutil
import socket
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

import docker
from docker.errors import DockerException, NotFound
from docker.types import LogConfig
from requests.exceptions import ConnectionError as RequestsConnectionError

from config import settings
from models import V2ControllerDeployment
from services.resume_service import (
    ResumeAbortReason,
    ResumeError,
    guard_target_available,
    resolve_deploy_target,
    seed_resume_state,
)
from utils.file_system import fs_util
from utils.gateway_certs import ensure_gateway_certs, gateway_certs_dir

# Create module-specific logger
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-instance deploy serialisation (CDX-001)
# ---------------------------------------------------------------------------
#
# Two concurrent deploys of the SAME instance name must not interleave their
# create/stage/seed/promote steps. Exclusive creation alone already makes the
# race safe (the loser's promote fails and it cleans up its own staging dir),
# but serialising makes the outcome deterministic — exactly one deploy proceeds
# and the other is refused DEST_EXISTS having done no work — instead of two
# deploys building full instances so one can be thrown away.
#
# Registry rather than one global lock: deploys of DIFFERENT names are
# independent and must stay concurrent. Refcounted so the registry cannot grow
# without bound on a long-lived API; every mutation below happens between
# awaits, which is atomic under a single event loop.
_deploy_locks: Dict[str, asyncio.Lock] = {}
_deploy_lock_users: Dict[str, int] = {}

# How many times to re-roll a staging suffix on the (vanishingly unlikely)
# chance the random name already exists.
_STAGING_NAME_ATTEMPTS = 5

# ---------------------------------------------------------------------------
# No-replace promote (CDX-R01)
# ---------------------------------------------------------------------------
#
# Windows ``os.rename`` is natively no-replace: MoveFileW without
# MOVEFILE_REPLACE_EXISTING fails when the destination exists at all. POSIX
# ``rename(2)`` is NOT — SUSv4 says that if the destination is an existing EMPTY
# directory it "shall be removed" and the source renamed onto it. An
# ``exists()``-then-``rename()`` promote therefore has a real window on the
# production platform: a target that appears after the check is silently
# absorbed rather than refused (CDX-R01).
#
# ``_rename`` is a seam so the POSIX branch — the production-Linux code path —
# can be exercised from a Windows dev box against a model of rename(2)'s
# documented directory semantics. Production always binds it to ``os.rename``.
_NATIVE_NOREPLACE_RENAME = os.name == "nt"
_rename = os.rename


def _rename_noreplace(src: str, dst: str) -> None:
    """Rename ``src`` onto ``dst``, never replacing an existing ``dst``.

    The promote primitive. Atomic (so an instance directory is never observable
    half-built) AND exclusive (so a target that appeared while this attempt was
    staging is refused, never merged into or absorbed).

    On POSIX the exclusivity comes from ``os.mkdir``, not from a preceding
    existence check: mkdir is itself the atomic no-replace primitive — it either
    creates ``dst``, giving this attempt exclusive ownership of the name, or
    raises ``FileExistsError``. There is no check-then-act window, which is
    exactly what a bare ``rename`` over an empty directory would exploit. Once
    the reservation is held, the only directory the rename can replace is our
    own empty one.

    Raises:
        FileExistsError: ``dst`` already exists. Nothing was moved or removed.
    """
    if _NATIVE_NOREPLACE_RENAME:
        _rename(src, dst)
        return
    os.mkdir(dst)  # atomic reservation — FileExistsError if the name is taken
    try:
        _rename(src, dst)
    except OSError:
        # Release the reservation so this name is not blocked by our own
        # leftover. ``os.rmdir`` refuses a non-empty directory, so this can
        # never destroy data: anything that landed inside is left for a human.
        try:
            os.rmdir(dst)
        except OSError as rm_exc:
            logger.error("Could not release promote reservation '%s': %s", dst, rm_exc)
        raise


# ---------------------------------------------------------------------------
# CLA-M02 — the bots/ path coupling
# ---------------------------------------------------------------------------
#
# Two code paths address the SAME bots tree through two different names, and
# nothing ever checked they agree:
#
#   * the copy-forward hook writes through a CWD-RELATIVE ``bots/`` — inside the
#     API container that is ``/hummingbot-api/bots`` (its WORKDIR);
#   * ``_run_instance_container`` builds the bot containers' bind-mount SOURCES
#     from ``$BOTS_PATH`` — a HOST path (``os.path.join(bots_path, instance_dir,
#     ...)``), because the daemon resolves mount sources on the host, not in our
#     namespace.
#
# The deployment is only correct because the compose file happens to bind
# ``${BOTS_PATH}/bots`` at ``/hummingbot-api/bots``. That is an undocumented
# invariant: break it and the hook seeds a data/ directory that the bot it was
# seeded for can never read — the bot starts clean and re-seeds from the wallet,
# which is a money event, and nothing logs a word.
#
# So: prove it at deploy time by asking the daemon what THIS container's mounts
# actually are.
_HOOK_BOTS_ROOT = "bots"  # the hook's write root, relative to CWD


def _self_container_id() -> Optional[str]:
    """This container's id/name, or None if it cannot be determined.

    Docker sets the container hostname to the short container id unless compose
    overrides it; ``HOSTNAME`` is the documented escape hatch for the override
    case. Outside a container this returns the host's name, which simply will
    not resolve as a container — handled by the caller as "unavailable".
    """
    try:
        return os.environ.get("HOSTNAME") or socket.gethostname() or None
    except OSError:
        return None


def _inspect_self_mounts(client) -> Optional[list]:
    """This API container's mount table, or ``None`` if self-inspection is
    genuinely unavailable.

    ``None`` means "we could not determine our own mounts" — NOT "we have none".
    An empty list is a real answer (a container with no bind mounts) and is
    returned as ``[]``, because that answer proves a mismatch.

    The distinction is the whole safety argument for CLA-M02, so the
    unavailable set is closed and enumerated here rather than being whatever a
    bare ``except Exception`` happens to swallow:

    * no client, or no determinable container id — not running under Docker;
    * ``NotFound`` — our id is not a container the daemon knows (dev/test, or
      the API running on the host);
    * ``DockerException`` / ``requests`` ``ConnectionError`` — daemon
      unreachable or API error, so we know nothing;
    * a payload that is not the shape the Docker SDK documents (``attrs`` dict,
      ``Mounts`` list of dicts) — we are not talking to a real daemon (this is
      what a ``MagicMock`` client in the test suite looks like). A real daemon
      always returns the documented shape, so a real MISMATCH can never arrive
      disguised as a malformed payload.

    Note what is deliberately NOT caught: everything after this function
    returns. Comparing the paths happens in the caller, outside any handler, so
    no bug in the comparison — and no genuine mismatch — can be laundered into
    "unavailable".
    """
    if client is None:
        return None
    container_id = _self_container_id()
    if not container_id:
        return None
    try:
        attrs = client.containers.get(container_id).attrs
    except NotFound:
        return None
    except (DockerException, RequestsConnectionError):
        return None
    if not isinstance(attrs, dict):
        return None
    mounts = attrs.get("Mounts")
    if not isinstance(mounts, list):
        return None
    if not all(isinstance(m, dict) for m in mounts):
        return None
    return mounts


def _mount_remainder(destination: str, target: str) -> Optional[str]:
    """The path of ``target`` relative to ``destination``, or ``None`` when
    ``destination`` does not contain ``target``.

    ``os.path.relpath`` rather than a string prefix test, because a prefix test
    reads ``/hummingbot-api/bots-backup`` as containing ``/hummingbot-api/bots``
    and would then compute a host path out of two unrelated mounts. A
    non-containing destination yields a remainder that escapes upward (or, on
    Windows, a different drive), and both are rejected here.
    """
    dest_n = os.path.normpath(destination)
    target_n = os.path.normpath(target)
    if dest_n == target_n:
        return ""
    try:
        rel = os.path.relpath(target_n, dest_n)
    except ValueError:
        return None  # different drives (Windows) — cannot contain
    if rel == os.curdir:
        return ""
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return rel


def _host_source_for(mounts: list, container_path: str) -> Optional[str]:
    """The HOST directory backing ``container_path``, per this container's mount
    table — or ``None`` if no mount backs it (the path lives in the container's
    own writable layer).

    Matches the LONGEST covering mount rather than requiring an exact
    destination: binding the parent (``/hummingbot-api``) backs
    ``/hummingbot-api/bots`` just as validly as binding it directly, and calling
    that "unmounted" would be a false alarm that aborts a correct deployment.
    """
    best_depth = -1
    best_host = None
    for mount in mounts:
        dest = mount.get("Destination")
        source = mount.get("Source")
        if not isinstance(dest, str) or not isinstance(source, str) or not dest:
            continue
        remainder = _mount_remainder(dest, container_path)
        if remainder is None:
            continue
        depth = len(os.path.normpath(dest).split(os.sep))
        if depth > best_depth:
            best_depth = depth
            best_host = os.path.join(source, remainder) if remainder else source
    return best_host


@asynccontextmanager
async def _instance_deploy_lock(instance_name: str):
    """Serialise deploys targeting ``instance_name`` (held create→promote)."""
    lock = _deploy_locks.setdefault(instance_name, asyncio.Lock())
    _deploy_lock_users[instance_name] = _deploy_lock_users.get(instance_name, 0) + 1
    try:
        async with lock:
            yield
    finally:
        _deploy_lock_users[instance_name] -= 1
        if _deploy_lock_users[instance_name] <= 0:
            _deploy_lock_users.pop(instance_name, None)
            _deploy_locks.pop(instance_name, None)


class DockerService:
    # Class-level configuration for cleanup
    PULL_STATUS_MAX_AGE_SECONDS = 3600  # Keep status for 1 hour
    PULL_STATUS_MAX_ENTRIES = 100  # Maximum number of entries to keep
    CLEANUP_INTERVAL_SECONDS = 300  # Run cleanup every 5 minutes

    def __init__(self, db_manager=None):
        self.SOURCE_PATH = os.getcwd()
        # AsyncDatabaseManager (optional) — used by the copy-forward resume hook
        # for bot_runs lineage/guard queries. Deploys with resume off never touch it.
        self.db_manager = db_manager
        self._pull_status: Dict[str, Dict] = {}
        self._cleanup_thread = None
        self._stop_cleanup = threading.Event()

        try:
            self.client = docker.from_env()
            # Start background cleanup thread
            self._start_cleanup_thread()
        except DockerException as e:
            logger.error(f"It was not possible to connect to Docker. Please make sure Docker is running. Error: {e}")

    @staticmethod
    def _get_bot_network_mode() -> str:
        """Return the network_mode for spawned bot containers.

        Reads DOCKER_BOT_NETWORK_MODE from env (default: 'host').
        In the VPN stack this should be set to 'container:<gluetun_container_name>'
        so all bot traffic is routed through the VPN tunnel.
        """
        return os.environ.get("DOCKER_BOT_NETWORK_MODE", "host")

    @staticmethod
    def _get_compose_labels(instance_name: str) -> dict:
        """Return Docker labels that graft the bot into the Compose project.

        This makes spawned bots appear in `docker compose ps`, Dozzle,
        and enables autoheal restart monitoring.
        """
        project = os.environ.get("COMPOSE_PROJECT_NAME", "")
        service = os.environ.get("COMPOSE_SERVICE_PREFIX", "hummingbot-bot")
        labels = {"autoheal": "true"}
        if project:
            labels.update({
                "com.docker.compose.project": project,
                "com.docker.compose.service": f"{service}-{instance_name}",
                "com.docker.compose.container-number": "1",
                "com.docker.compose.oneoff": "False",
            })
        return labels

    def get_active_containers(self, name_filter: str = None):
        try:
            all_containers = self.client.containers.list(filters={"status": "running"})
            if name_filter:
                containers_info = [
                    {
                        "id": container.id,
                        "name": container.name,
                        "status": container.status,
                        "image": container.image.tags[0] if container.image.tags else container.image.id[:12]
                    }
                    for container in all_containers if name_filter.lower() in container.name.lower()
                ]
            else:
                containers_info = [
                    {
                        "id": container.id,
                        "name": container.name,
                        "status": container.status,
                        "image": container.image.tags[0] if container.image.tags else container.image.id[:12]
                    }
                    for container in all_containers
                ]
            return containers_info
        except DockerException as e:
            return str(e)

    def get_available_images(self):
        try:
            images = self.client.images.list()
            return {"images": images}
        except DockerException as e:
            return str(e)

    def pull_image(self, image_name):
        try:
            return self.client.images.pull(image_name)
        except DockerException as e:
            return str(e)

    def pull_image_sync(self, image_name):
        """Synchronous pull operation for background tasks"""
        try:
            result = self.client.images.pull(image_name)
            return {"success": True, "image": image_name, "result": str(result)}
        except DockerException as e:
            return {"success": False, "error": str(e)}

    def get_exited_containers(self, name_filter: str = None):
        try:
            all_containers = self.client.containers.list(filters={"status": "exited"}, all=True)
            if name_filter:
                containers_info = [
                    {
                        "id": container.id,
                        "name": container.name,
                        "status": container.status,
                        "image": container.image.tags[0] if container.image.tags else container.image.id[:12]
                    }
                    for container in all_containers if name_filter.lower() in container.name.lower()
                ]
            else:
                containers_info = [
                    {
                        "id": container.id,
                        "name": container.name,
                        "status": container.status,
                        "image": container.image.tags[0] if container.image.tags else container.image.id[:12]
                    }
                    for container in all_containers
                ]
            return containers_info
        except DockerException as e:
            return str(e)

    def clean_exited_containers(self):
        try:
            self.client.containers.prune()
        except DockerException as e:
            return str(e)

    def is_docker_running(self):
        try:
            self.client.ping()
            return True
        except DockerException:
            return False

    def stop_container(self, container_name):
        try:
            container = self.client.containers.get(container_name)
            container.stop()
        except DockerException as e:
            return str(e)

    def start_container(self, container_name):
        try:
            container = self.client.containers.get(container_name)
            container.start()
        except DockerException as e:
            return str(e)

    def get_container_status(self, container_name):
        """Get the status of a container"""
        try:
            container = self.client.containers.get(container_name)
            return {
                "success": True,
                "state": {
                    "status": container.status,
                    "running": container.status == "running",
                    "exit_code": getattr(container.attrs.get("State", {}), "ExitCode", None)
                }
            }
        except DockerException as e:
            return {"success": False, "message": str(e)}

    def remove_container(self, container_name, force=True):
        try:
            container = self.client.containers.get(container_name)
            container.remove(force=force)
            return {"success": True, "message": f"Container {container_name} removed successfully."}
        except DockerException as e:
            return {"success": False, "message": str(e)}

    @staticmethod
    def _ensure_contained(path: str, base_dir: str, label: str):
        """
        Defense in depth: verify that `path` stays inside `base_dir` after resolving symlinks and
        traversal sequences. Raises ValueError if it escapes the allowed base directory.
        """
        resolved_base = os.path.realpath(base_dir)
        resolved_path = os.path.realpath(path)
        if os.path.commonpath([resolved_base, resolved_path]) != resolved_base:
            raise ValueError(f"Invalid {label}: '{path}' resolves outside of '{base_dir}'.")
        return resolved_path

    def _check_bots_path_coupling(self) -> Optional[dict]:
        """Prove the hook's write root and the bot containers' mount source are
        the same directory (CLA-M02).

        The two paths are computed exactly as the code that uses them computes
        them — ``os.path.abspath("bots")`` is what the hook writes through, and
        ``abspath(join($BOTS_PATH, "bots"))`` is the root of every mount source
        ``_run_instance_container`` builds — so this checks the real coupling,
        not a restatement of it.

        Returns:
            A structured warning dict when the coupling could not be verified,
            or ``None`` when it was verified to hold.

        Raises:
            ResumeError: ``BOTS_PATH_MISCONFIGURED``, mapped to 409. Raised only
                on PROOF: a successful self-inspection whose answer disagrees.
                Unverifiable is not proof — outside a container there is nothing
                to disagree with, and aborting there would brick every dev and
                test deploy over assertion machinery for a mount that does not
                exist. Proven danger fails closed; unknown warns loudly.
        """
        bots_path = os.environ.get("BOTS_PATH", self.SOURCE_PATH)
        mount_root = os.path.abspath(os.path.join(bots_path, "bots"))
        write_root = os.path.abspath(_HOOK_BOTS_ROOT)

        mounts = _inspect_self_mounts(self.client)
        if mounts is None:
            message = (
                f"Could not verify the bots/ path coupling: this process could "
                f"not inspect its own container mounts. The resume hook writes "
                f"through '{write_root}' while bot containers will bind-mount "
                f"host path '{mount_root}' — if those are not the same "
                f"directory, a resumed bot silently starts with an empty data/ "
                f"and re-seeds from the wallet. Unverified, not refused."
            )
            logger.warning(message, extra={"event": "bots_path_coupling_unverified"})
            return {
                "code": "BOTS_PATH_COUPLING_UNVERIFIED",
                "message": message,
                "hook_write_root": write_root,
                "bot_mount_root": mount_root,
            }

        # Everything below is outside the unavailable handler on purpose: once
        # the daemon has answered, any disagreement is a finding, not a doubt.
        actual_mount_root = _host_source_for(mounts, write_root)
        if actual_mount_root is None:
            raise ResumeError(
                ResumeAbortReason.BOTS_PATH_MISCONFIGURED,
                f"The resume hook's write root '{write_root}' is not backed by "
                f"any bind mount of this API container, so it lives in the "
                f"container's own writable layer — but bot containers bind-mount "
                f"host path '{mount_root}' (from BOTS_PATH='{bots_path}'). "
                f"Nothing written by the hook could ever be read by the bot. "
                f"Refusing to deploy.",
            )
        if os.path.normpath(actual_mount_root) != os.path.normpath(mount_root):
            raise ResumeError(
                ResumeAbortReason.BOTS_PATH_MISCONFIGURED,
                f"bots/ path coupling violated: the resume hook writes through "
                f"'{write_root}', which this container mounts from host path "
                f"'{actual_mount_root}' — but bot containers would bind-mount "
                f"host path '{mount_root}' (from BOTS_PATH='{bots_path}'). Those "
                f"are different directories: a resumed bot would start with an "
                f"empty data/ and re-seed from the wallet. Refusing to deploy. "
                f"Set BOTS_PATH so that BOTS_PATH + '/bots' == "
                f"'{actual_mount_root}', or fix the bind mount.",
            )
        logger.debug(
            "bots/ path coupling verified: hook write root '%s' and bot mount "
            "root '%s' are the same host directory.", write_root, mount_root,
        )
        return None

    def _create_staging_dir(self, instance_name: str) -> str:
        """Create the exclusive staging sibling this attempt builds the instance in.

        The instance is assembled at ``<target>.staging-<random>`` and promoted
        onto the target with a single rename once it is complete (CDX-001). Two
        properties matter:

        * ``exist_ok=False`` — the directory is ours only if WE created it, which
          is what makes it safe for the failure path to delete it.
        * a sibling of the target, so the promote is a same-filesystem rename
          (atomic) rather than a copy; and the ``.`` in the name can never
          collide with a real instance name (the model's SAFE_NAME_PATTERN
          allows no dots), nor be mistaken for lineage by ``latest`` resolution.
        """
        for _ in range(_STAGING_NAME_ATTEMPTS):
            staging_name = f"{instance_name}.staging-{secrets.token_hex(4)}"
            staging_dir = os.path.join("bots", "instances", staging_name)
            self._ensure_contained(staging_dir, os.path.join("bots", "instances"), "staging_dir")
            try:
                os.makedirs(staging_dir, exist_ok=False)
            except FileExistsError:
                continue
            # The root is exclusively ours from here. If building it out fails,
            # remove it right here: the caller's cleanup only arms once this
            # returns, so an exception escaping now would leak a
            # <target>.staging-* tree per failed attempt (CDX-R03).
            try:
                os.makedirs(os.path.join(staging_dir, "data"))
                os.makedirs(os.path.join(staging_dir, "logs"))
            except BaseException:
                self._remove_staging_dir(staging_dir)
                raise
            return staging_dir
        raise ResumeError(
            ResumeAbortReason.DEST_EXISTS,
            f"Could not create a unique staging directory for '{instance_name}' "
            f"after {_STAGING_NAME_ATTEMPTS} attempts.",
        )

    @staticmethod
    def _promote_staging(staging_dir: str, instance_dir: str) -> None:
        """Publish the fully-built staging dir at its final path, atomically.

        The rename is atomic between siblings, so an instance directory is never
        observable half-built: it appears complete or not at all. It is also
        no-replace (:func:`_rename_noreplace`), so a target that appeared while
        this attempt was staging is refused rather than merged into.

        There is deliberately NO ``exists()`` pre-check here. A check-then-rename
        is not an exclusive promote on POSIX — ``rename(2)`` completes over an
        empty destination directory that appeared in the window, which is the
        CDX-R01 defect. The primitive is the gate; correctness must not rest on
        an assumption about which other code paths can create the target.
        """
        try:
            _rename_noreplace(staging_dir, instance_dir)
        except FileExistsError as exc:
            raise ResumeError(
                ResumeAbortReason.DEST_EXISTS,
                f"Target instance directory '{instance_dir}' appeared while this "
                f"deploy was staging — refusing to overwrite it.",
            ) from exc
        except OSError as exc:
            # POSIX refuses a non-empty destination with ENOTEMPTY/EEXIST; any
            # other OSError that leaves a target behind is the same refusal.
            if os.path.exists(instance_dir):
                raise ResumeError(
                    ResumeAbortReason.DEST_EXISTS,
                    f"Target instance directory '{instance_dir}' appeared while "
                    f"this deploy was staging — refusing to overwrite it "
                    f"({exc}).",
                ) from exc
            raise

    @staticmethod
    def _remove_staging_dir(staging_dir: str) -> None:
        """Drop this attempt's staging dir. Safe by construction: the only path
        passed here is one ``_create_staging_dir`` exclusively created."""
        try:
            if os.path.exists(staging_dir):
                shutil.rmtree(staging_dir)
                logger.info("Removed staging dir '%s' after a failed deploy.", staging_dir)
        except OSError as exc:
            logger.error("Could not remove staging dir '%s': %s", staging_dir, exc)

    async def create_hummingbot_instance(self, config: V2ControllerDeployment):
        bots_path = os.environ.get('BOTS_PATH', self.SOURCE_PATH)  # Default to 'SOURCE_PATH' if BOTS_PATH is not set
        instance_name = config.instance_name

        # CLA-M02 — before ANY filesystem mutation: a proven-broken coupling
        # means everything staged below would be written somewhere the bot
        # cannot read it, so there is nothing to gain by building it first.
        coupling_warning = self._check_bots_path_coupling()
        # The one resolver, shared with preview_resume (CDX-R02): if preview and
        # deploy each joined the path themselves they could drift, and preview's
        # whole job is to grade the path deploy will actually build.
        instance_dir = str(resolve_deploy_target("bots", instance_name))
        # Defense in depth: ensure the resolved paths stay within their allowed base directories
        # before any filesystem mutation (makedirs/copytree) takes place.
        self._ensure_contained(instance_dir, os.path.join("bots", "instances"), "instance_name")
        source_credentials_dir = os.path.join("bots", 'credentials', config.credentials_profile)
        self._ensure_contained(source_credentials_dir, os.path.join("bots", "credentials"), "credentials_profile")

        # CDX-001 — exclusive creation, staged build, atomic promote. Held across
        # the whole window so a concurrent deploy of this name cannot slip
        # between the existence check and the promote.
        async with _instance_deploy_lock(instance_name):
            # An existing target instance dir is never reused and never deleted:
            # its data/ may be the operator's only copy of a live ledger. This
            # runs for EVERY deploy, resume or not — the old code reused the
            # directory (and, on a failed resume, deleted it).
            guard_target_available(instance_dir)

            staging_dir = self._create_staging_dir(instance_name)
            try:
                gateway_certs_host_dir, resume_manifest = await self._stage_instance(
                    config, staging_dir, instance_name, source_credentials_dir
                )
                self._promote_staging(staging_dir, instance_dir)
            except BaseException:
                # Only ever this attempt's own staging dir — the target, whether
                # it pre-existed or appeared mid-flight, is never touched.
                self._remove_staging_dir(staging_dir)
                raise

        response = self._run_instance_container(
            config, bots_path, instance_name, instance_dir, gateway_certs_host_dir
        )

        # Surface the resume hook's structured warnings (CONTRACT C1's opt-out skip
        # is the first of them; CLA-M01's sizing-critical drift rides here too) on
        # the deploy response. A skipped controller means a bot came up without its
        # ledger, and sizing drift means it came up trading a different amount of
        # money than the bot it replaced; the operator who asked for that is
        # entitled to see it in the reply rather than find it in a log later.
        resume_warnings = (resume_manifest or {}).get("warnings") or []
        if resume_warnings:
            response["resume_warnings"] = resume_warnings
        # CLA-M02's unverified-coupling warning is deploy-wide, not resume-scoped:
        # it is raised for every deploy, including resume_mode="off".
        if coupling_warning:
            response["warnings"] = [coupling_warning]
        return response

    async def _stage_instance(
        self,
        config: V2ControllerDeployment,
        staging_dir: str,
        instance_name: str,
        source_credentials_dir: str,
    ):
        """Build the complete instance inside ``staging_dir``: conf, client
        config, and the seeded ``data/``. Nothing here touches the target path,
        so a failure at any point leaves the bot tree exactly as it was.

        Returns:
            ``(gateway_certs_host_dir, resume_manifest)``. ``resume_manifest`` is
            ``None`` when the resume hook did not run (``resume_mode == "off"``);
            the caller reads its ``warnings`` onto the deploy response.
        """
        # ``fs_util`` paths are relative to its base ("bots"); the instance is
        # still under its staging name at this point.
        staging_fs_rel = f"instances/{os.path.basename(staging_dir)}"

        # Copy credentials into the staging instance. The staging dir is fresh,
        # so conf/ cannot already exist — the old rmtree-then-recopy of an
        # existing conf/ (which destroyed a live instance's connector configs on
        # a same-name redeploy) is gone with the reuse path that needed it.
        destination_credentials_dir = os.path.join(staging_dir, 'conf')
        shutil.copytree(source_credentials_dir, destination_credentials_dir)

        # Copy specific script config and referenced controllers if provided
        if config.script_config:
            script_config_dir = os.path.join("bots", 'conf', 'scripts')
            controllers_config_dir = os.path.join("bots", 'conf', 'controllers')
            destination_scripts_config_dir = os.path.join(staging_dir, 'conf', 'scripts')
            destination_controllers_config_dir = os.path.join(staging_dir, 'conf', 'controllers')

            os.makedirs(destination_scripts_config_dir, exist_ok=True)

            # Copy the specific script config file
            source_script_config_file = os.path.join(script_config_dir, config.script_config)
            destination_script_config_file = os.path.join(destination_scripts_config_dir, config.script_config)

            if os.path.exists(source_script_config_file):
                shutil.copy2(source_script_config_file, destination_script_config_file)

                # Load the script config to find referenced controllers
                try:
                    # Path relative to fs_util base_path (which is "bots")
                    script_config_relative_path = f"conf/scripts/{config.script_config}"
                    script_config_content = fs_util.read_yaml_file(script_config_relative_path)
                    controllers_list = script_config_content.get('controllers_config', [])

                    # If there are controllers referenced, copy them
                    if controllers_list:
                        os.makedirs(destination_controllers_config_dir, exist_ok=True)

                        for controller_file in controllers_list:
                            source_controller_file = os.path.join(controllers_config_dir, controller_file)
                            destination_controller_file = os.path.join(
                                destination_controllers_config_dir, controller_file
                            )

                            if os.path.exists(source_controller_file):
                                shutil.copy2(source_controller_file, destination_controller_file)
                                logger.info(f"Copied controller config: {controller_file}")
                            else:
                                logger.warning(
                                    f"Controller config file {controller_file} not found in {controllers_config_dir}"
                                )

                except Exception as e:
                    logger.error(f"Error reading script config file {config.script_config}: {e}")
            else:
                logger.warning(f"Script config file {config.script_config} not found in {script_config_dir}")
        # Path relative to fs_util base_path (which is "bots"). Read/written
        # under the staging name, but the instance_id written INTO it is the
        # final instance name — that is the bot's identity, not its build
        # location.
        conf_file_path = f"{staging_fs_rel}/conf/conf_client.yml"
        client_config = fs_util.read_yaml_file(conf_file_path)
        client_config['instance_id'] = instance_name

        # SEC-048: point the instance at the secured (mTLS) Gateway and give it the shared
        # cert set. Cert keys are decrypted inside the container with CONFIG_PASSWORD, so this
        # is only enabled when a config password is set. Generation is idempotent: the instance
        # reuses (or seeds) the same CA the Gateway uses.
        gateway_certs_host_dir = None
        if settings.security.config_password:
            # Generate/locate the shared cert set (written to the local-base dir) and resolve the
            # matching HOST path for the bind-mount source.
            ensure_gateway_certs(settings.security.config_password)
            gateway_certs_host_dir = gateway_certs_dir(host=True)
            gateway_section = client_config.get('gateway') or {}
            gateway_section['gateway_use_ssl'] = True
            client_config['gateway'] = gateway_section

        fs_util.dump_dict_to_yaml(conf_file_path, client_config)

        # Copy-forward resume hook (COPY_FORWARD_HOOK_DESIGN.md §3/§4): seed the new
        # instance's data/ from the prior run's state files — after config staging
        # (the staged controller YAMLs drive the copy set) and strictly before
        # containers.run. With resume_mode "off" the hook is not invoked and the
        # deploy path is identical to pre-hook behavior. On any ResumeError the hook
        # logs bot_resume_failed, removes the staging dir it was told this attempt
        # created, and re-raises — the container never starts on a failed or
        # partial seed, and the promote never happens.
        resume_manifest = None
        if config.resume_mode != "off":
            resume_manifest = await seed_resume_state(
                deployment=config,
                new_instance_dir=Path(staging_dir),
                bots_path=Path("bots"),
                docker_client=self.client,
                db_manager=self.db_manager,
                # Identity is the final instance name — never the staging dir's
                # name, which would strip to the wrong base and break `latest`.
                new_instance_name=instance_name,
                created_by_this_attempt=True,
            )

        return gateway_certs_host_dir, resume_manifest

    def _run_instance_container(
        self,
        config: V2ControllerDeployment,
        bots_path: str,
        instance_name: str,
        instance_dir: str,
        gateway_certs_host_dir,
    ):
        """Launch the container for an instance that is fully built and promoted."""
        # Set up Docker volumes
        instance_conf = os.path.abspath(os.path.join(bots_path, instance_dir, 'conf'))
        instance_connectors = os.path.abspath(os.path.join(bots_path, instance_dir, 'conf', 'connectors'))
        instance_scripts = os.path.abspath(os.path.join(bots_path, instance_dir, 'conf', 'scripts'))
        instance_controllers = os.path.abspath(os.path.join(bots_path, instance_dir, 'conf', 'controllers'))
        instance_data = os.path.abspath(os.path.join(bots_path, instance_dir, 'data'))
        instance_logs = os.path.abspath(os.path.join(bots_path, instance_dir, 'logs'))
        shared_scripts = os.path.abspath(os.path.join(bots_path, "bots", 'scripts'))
        shared_controllers = os.path.abspath(os.path.join(bots_path, "bots", 'controllers'))

        volumes = {
            instance_conf: {'bind': '/home/hummingbot/conf', 'mode': 'rw'},
            instance_connectors: {'bind': '/home/hummingbot/conf/connectors', 'mode': 'rw'},
            instance_scripts: {'bind': '/home/hummingbot/conf/scripts', 'mode': 'rw'},
            instance_controllers: {'bind': '/home/hummingbot/conf/controllers', 'mode': 'rw'},
            instance_data: {'bind': '/home/hummingbot/data', 'mode': 'rw'},
            instance_logs: {'bind': '/home/hummingbot/logs', 'mode': 'rw'},
            shared_scripts: {'bind': '/home/hummingbot/scripts', 'mode': 'rw'},
            shared_controllers: {'bind': '/home/hummingbot/controllers', 'mode': 'rw'},
        }

        # SEC-048: mount the shared mTLS certs read-only where hummingbot reads them
        # (root_path()/certs == /home/hummingbot/certs inside the instance container).
        if gateway_certs_host_dir:
            volumes[gateway_certs_host_dir] = {'bind': '/home/hummingbot/certs', 'mode': 'ro'}

        # Set up environment variables
        environment = {}
        password = settings.security.config_password
        if password:
            environment["CONFIG_PASSWORD"] = password

        if config.script_config:
            if password:
                environment['SCRIPT_CONFIG'] = config.script_config
            else:
                return {"success": False, "message": "Password not provided. We cannot start the bot without a password."}

        if config.headless:
            environment["HEADLESS_MODE"] = "true"

        log_config = LogConfig(
            type="json-file",
            config={
                'max-size': '10m',
                'max-file': "5",
            })
        network_mode = self._get_bot_network_mode()
        labels = self._get_compose_labels(instance_name)
        logger.info(
            f"Launching bot '{instance_name}' with "
            f"network_mode={network_mode}, labels={labels}"
        )
        try:
            self.client.containers.run(
                image=config.image,
                name=instance_name,
                volumes=volumes,
                environment=environment,
                network_mode=network_mode,
                labels=labels,
                detach=True,
                tty=True,
                stdin_open=True,
                log_config=log_config,
            )
            return {"success": True, "message": f"Instance {instance_name} created successfully."}
        except docker.errors.DockerException as e:
            return {"success": False, "message": str(e)}

    def _start_cleanup_thread(self):
        """Start the background cleanup thread"""
        if self._cleanup_thread is None or not self._cleanup_thread.is_alive():
            self._cleanup_thread = threading.Thread(target=self._periodic_cleanup, daemon=True)
            self._cleanup_thread.start()
            logger.info("Started Docker pull status cleanup thread")

    def _periodic_cleanup(self):
        """Periodically clean up old pull status entries"""
        while not self._stop_cleanup.is_set():
            try:
                self._cleanup_old_pull_status()
            except Exception as e:
                logger.error(f"Error in cleanup thread: {e}")

            # Wait for the next cleanup interval
            self._stop_cleanup.wait(self.CLEANUP_INTERVAL_SECONDS)

    def _cleanup_old_pull_status(self):
        """Remove old entries to prevent memory growth"""
        current_time = time.time()
        to_remove = []

        # Find entries older than max age
        for image_name, status_info in self._pull_status.items():
            # Skip ongoing pulls
            if status_info["status"] == "pulling":
                continue

            # Check age of completed/failed operations
            end_time = status_info.get("completed_at") or status_info.get("failed_at")
            if end_time and (current_time - end_time > self.PULL_STATUS_MAX_AGE_SECONDS):
                to_remove.append(image_name)

        # Remove old entries
        for image_name in to_remove:
            del self._pull_status[image_name]
            logger.info(f"Cleaned up old pull status for {image_name}")

        # If still over limit, remove oldest completed/failed entries
        if len(self._pull_status) > self.PULL_STATUS_MAX_ENTRIES:
            completed_entries = [
                (name, info) for name, info in self._pull_status.items()
                if info["status"] in ["completed", "failed"]
            ]
            # Sort by end time (oldest first)
            completed_entries.sort(
                key=lambda x: x[1].get("completed_at") or x[1].get("failed_at") or 0
            )

            # Remove oldest entries to get under limit
            excess_count = len(self._pull_status) - self.PULL_STATUS_MAX_ENTRIES
            for i in range(min(excess_count, len(completed_entries))):
                del self._pull_status[completed_entries[i][0]]
                logger.info(f"Cleaned up excess pull status for {completed_entries[i][0]}")

    def pull_image_async(self, image_name: str):
        """Start pulling a Docker image asynchronously with status tracking"""
        # Check if pull is already in progress
        if image_name in self._pull_status:
            current_status = self._pull_status[image_name]
            if current_status["status"] == "pulling":
                return {
                    "message": f"Pull already in progress for {image_name}",
                    "status": "in_progress",
                    "started_at": current_status["started_at"],
                    "image_name": image_name
                }

        # Start the pull in a background thread
        threading.Thread(target=self._pull_image_with_tracking, args=(image_name,), daemon=True).start()

        return {
            "message": f"Pull started for {image_name}",
            "status": "started",
            "image_name": image_name
        }

    def _pull_image_with_tracking(self, image_name: str):
        """Background task to pull Docker image with status tracking"""
        try:
            self._pull_status[image_name] = {
                "status": "pulling",
                "started_at": time.time(),
                "progress": "Starting pull..."
            }

            # Use the synchronous pull method
            result = self.pull_image_sync(image_name)

            if result.get("success"):
                self._pull_status[image_name] = {
                    "status": "completed",
                    "started_at": self._pull_status[image_name]["started_at"],
                    "completed_at": time.time(),
                    "result": result
                }
            else:
                self._pull_status[image_name] = {
                    "status": "failed",
                    "started_at": self._pull_status[image_name]["started_at"],
                    "failed_at": time.time(),
                    "error": result.get("error", "Unknown error")
                }
        except Exception as e:
            self._pull_status[image_name] = {
                "status": "failed",
                "started_at": self._pull_status[image_name].get("started_at", time.time()),
                "failed_at": time.time(),
                "error": str(e)
            }

    def get_all_pull_status(self):
        """Get status of all pull operations"""
        operations = {}
        for image_name, status_info in self._pull_status.items():
            status_copy = status_info.copy()

            # Add duration for each operation
            start_time = status_copy.get("started_at")
            if start_time:
                if status_copy["status"] == "pulling":
                    status_copy["duration_seconds"] = round(time.time() - start_time, 2)
                elif "completed_at" in status_copy:
                    status_copy["duration_seconds"] = round(status_copy["completed_at"] - start_time, 2)
                elif "failed_at" in status_copy:
                    status_copy["duration_seconds"] = round(status_copy["failed_at"] - start_time, 2)

            operations[image_name] = status_copy

        return {
            "pull_operations": operations,
            "total_operations": len(operations)
        }

    def cleanup(self):
        """Clean up resources when shutting down"""
        self._stop_cleanup.set()
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=1)
