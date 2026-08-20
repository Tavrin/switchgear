"""Provider conversation stores bound to controller-minted session lineages."""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from . import identity, lease
from .errors import Refuse
from .paths import mkdir_exclusive, reject_symlinks
from .schema import validate
from .state import StateRoot, atomic_write_json, read_json


BINDING_VERSION = 1


def new_session_store_id() -> str:
    return str(uuid.uuid4())


def require_session_store_id(value: str) -> str:
    """Require the canonical uuid4 form used as a lineage directory name."""
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise Refuse(
            f"invalid session_store_id {value!r}; start a new job instead of "
            "selecting a session store by path"
        ) from exc
    if parsed.version != 4 or str(parsed) != value:
        raise Refuse(
            f"invalid session_store_id {value!r}; session lineages are canonical uuid4 values. "
            "Start a new job instead."
        )
    return value


def root_dir(root: StateRoot, *, create: bool = False) -> str:
    path = os.path.join(root.path, "sessions")
    if os.path.islink(path):
        raise Refuse("state sessions directory must not be a symlink")
    if create:
        os.makedirs(path, mode=0o700, exist_ok=True)
        reject_symlinks(path, "sessions")
    return path


def lineage_dir(root: StateRoot, session_store_id: str) -> str:
    return os.path.join(root_dir(root), require_session_store_id(session_store_id))


def worktree_facts(ident: identity.WorktreeIdentity) -> dict[str, Any]:
    return identity.identity_core(ident)


def _binding_record(
    session_store_id: str,
    harness: str,
    created_by_job: str,
    ident: identity.WorktreeIdentity,
) -> dict[str, Any]:
    return {
        "binding_version": BINDING_VERSION,
        "session_store_id": require_session_store_id(session_store_id),
        "harness": harness,
        "created_at": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "created_by_job": created_by_job,
        "worktree": worktree_facts(ident),
    }


def _write_binding(path: str, binding: dict[str, Any]) -> None:
    # A malformed controller record must never become durable and then be
    # trusted by resume or gc. Validation is adjacent to the write so later
    # edits cannot accidentally insert an unvalidated transformation between.
    validate(binding, "session-binding.schema.json")
    atomic_write_json(path, binding)


def create_lineage(
    root: StateRoot,
    session_store_id: str,
    harness: str,
    created_by_job: str,
    ident: identity.WorktreeIdentity,
) -> str:
    sessions = root_dir(root, create=True)
    path = os.path.join(sessions, require_session_store_id(session_store_id))
    # Exclusive creation is the collision guard. Silently opening an existing
    # uuid would turn an astronomically unlikely collision into silent adoption
    # of another conversation, the exact privacy defect lineages remove.
    mkdir_exclusive(path)
    binding = _binding_record(session_store_id, harness, created_by_job, ident)
    _write_binding(os.path.join(path, "binding.json"), binding)
    return path


def read_binding(path: str) -> dict[str, Any]:
    """Read a binding only after its complete schema has accepted it."""
    binding = read_json(path)
    validate(binding, "session-binding.schema.json")
    return binding


def _resume_refusal(detail: str) -> Refuse:
    return Refuse(
        "cannot resume: the workspace this conversation was bound to is gone or "
        f"is not the same one ({detail}). Continuing would risk mounting an "
        "unrelated conversation. Start a new job instead."
    )


def verify_lineage(
    root: StateRoot,
    session_store_id: str,
    harness: str,
    ident: identity.WorktreeIdentity,
) -> str:
    path = lineage_dir(root, session_store_id)
    marker = os.path.join(path, "binding.json")
    try:
        binding = read_binding(marker)
    except (OSError, Refuse, ValueError) as exc:
        raise _resume_refusal(f"binding.json is absent or invalid: {exc}") from exc
    if binding.get("binding_version") != BINDING_VERSION:
        raise _resume_refusal(
            f"binding version {binding.get('binding_version')!r} is not supported"
        )
    if binding.get("session_store_id") != session_store_id:
        raise _resume_refusal("binding.json names a different session lineage")
    if binding.get("harness") != harness:
        raise _resume_refusal(
            f"binding harness {binding.get('harness')!r} is not {harness!r}"
        )
    expected = worktree_facts(ident)
    if binding.get("worktree") != expected:
        differing = sorted(
            key for key in expected
            if (binding.get("worktree") or {}).get(key) != expected[key]
        )
        raise _resume_refusal(
            "worktree binding differs in " + (", ".join(differing) or "recorded facts")
        )
    return path


def _legacy_marker_matches(
    path: str,
    ident: identity.WorktreeIdentity,
    recorded_git_identity_after: str | None,
) -> bool:
    """Corroborate a legacy path marker with the prior job's repository fact.

    A legacy marker recorded only path/device/inode, all of which are reused
    when a worktree is deleted and recreated. Migration is therefore a strict
    one-time convenience: even one commit after the prior job changes the full
    git identity digest and refuses migration. That cost is intentional; the
    alternative can hand an unrelated repository another job's conversation.
    """
    try:
        marker = read_json(path)
        current_git_identity = (
            identity.git_identity_digest(ident)
            if (
                isinstance(marker, dict)
                and marker.get("worktree") == ident.realpath
                and marker.get("st_dev") == ident.st_dev
                and marker.get("st_ino") == ident.st_ino
            ) else None
        )
    except (OSError, Refuse, ValueError):
        return False
    return (
        isinstance(marker, dict)
        and marker.get("worktree") == ident.realpath
        and marker.get("st_dev") == ident.st_dev
        and marker.get("st_ino") == ident.st_ino
        and isinstance(recorded_git_identity_after, str)
        and recorded_git_identity_after == current_git_identity
    )


def _quarantine_legacy(root: StateRoot, legacy: str, key: str) -> str | None:
    if not os.path.lexists(legacy):
        return None
    quarantine = os.path.join(root_dir(root, create=True), ".quarantine")
    if os.path.islink(quarantine):
        raise Refuse("session quarantine must not be a symlink")
    os.makedirs(quarantine, mode=0o700, exist_ok=True)
    target = os.path.join(quarantine, f"{key}-{uuid.uuid4()}")
    os.rename(legacy, target)
    return target


def migrate_legacy(
    root: StateRoot,
    session_store_id: str,
    harness: str,
    created_by_job: str,
    ident: identity.WorktreeIdentity,
    recorded_git_identity_after: str | None,
) -> str:
    """Move one harness only when legacy and prior-job evidence both agree.

    This deliberately refuses after any commit: legacy migration is a one-time
    convenience, while mounting an unrelated conversation is a privacy breach.
    """
    sessions = root_dir(root, create=True)
    key = lease.identity_key(ident)
    legacy = os.path.join(sessions, key)
    marker = os.path.join(legacy, "worktree.json")
    harness_src = os.path.join(legacy, harness)
    if (
        not _legacy_marker_matches(marker, ident, recorded_git_identity_after)
        or not os.path.isdir(harness_src)
    ):
        quarantined = _quarantine_legacy(root, legacy, key)
        if quarantined:
            detail = f"it was set aside at {quarantined}"
        else:
            detail = f"no legacy store existed at {legacy}, so nothing could be set aside"
        raise Refuse(
            "the legacy conversation could not be safely bound; " + detail + ". "
            "Its path marker and the prior job's full git identity must both "
            "match the current worktree; even one later commit intentionally "
            "refuses this one-time migration rather than risk handing an "
            "unrelated repository the conversation. It was not mounted into "
            "the worker. Start a new job instead."
        )

    target = os.path.join(sessions, require_session_store_id(session_store_id))
    mkdir_exclusive(target)
    harness_dst = os.path.join(target, harness)
    binding = _binding_record(session_store_id, harness, created_by_job, ident)
    # Validate before moving anything. If the later atomic write itself fails,
    # put the harness back so an I/O error cannot strand the only conversation
    # copy in a lineage that has no trustworthy binding.
    validate(binding, "session-binding.schema.json")
    try:
        os.rename(harness_src, harness_dst)
    except Exception as move_exc:
        # mkdir_exclusive runs before the move. Without this cleanup, a failed
        # first rename reserved the new lineage forever even though no
        # conversation or valid binding ever reached it.
        try:
            os.rmdir(target)
        except OSError as cleanup_exc:
            raise Refuse(
                f"legacy migration could not move {harness_src} to "
                f"{harness_dst}, and could not remove the empty target "
                f"{target}: {cleanup_exc}. Remove that target and start a new "
                "job; the conversation remains at its legacy path."
            ) from move_exc
        raise
    try:
        _write_binding(os.path.join(target, "binding.json"), binding)
    except Exception as binding_exc:
        try:
            os.rename(harness_dst, harness_src)
        except OSError as rollback_exc:
            raise Refuse(
                f"legacy migration could not write a valid binding and could "
                f"not roll the conversation back. The only conversation is at "
                f"{harness_dst}, under a lineage with no valid binding. Move it "
                f"back to {harness_src} and remove {target}, then start a new "
                "job; do not resume this unbound lineage."
            ) from rollback_exc
        try:
            os.rmdir(target)
        except OSError:
            pass
        raise binding_exc
    return target
