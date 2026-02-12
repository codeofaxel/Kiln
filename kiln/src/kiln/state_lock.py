"""Optimistic locking / state versioning for concurrent printer access.

Prevents multiple agents from corrupting printer state by enforcing
monotonically increasing version numbers on every state mutation.
Agents must acquire a version before modifying state and release it
when done.  If another agent has incremented the version in the
meantime, the stale agent receives a :class:`StaleStateError`.

Thread safety is guaranteed via :class:`threading.Lock` -- learned
the hard way from the design_cache bug fix.

Example::

    lock = get_state_lock()
    with lock_printer("ender3-lab", owner="agent-1") as version:
        # version.version is guaranteed current
        do_work_on_printer("ender3-lab")
    # automatically released on exit
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class StateVersion:
    """Snapshot of a printer's current lock version.

    :param printer_id: Unique printer identifier.
    :param version: Monotonically increasing version counter.
    :param updated_at: Unix timestamp of last version bump.
    :param updated_by: Agent or session that owns this version.
    """

    printer_id: str
    version: int
    updated_at: float = field(default_factory=time.time)
    updated_by: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "printer_id": self.printer_id,
            "version": self.version,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class StaleStateError(Exception):
    """Raised when a state update conflicts with a newer version.

    :param printer_id: The printer whose state is stale.
    :param expected_version: The version the caller thought was current.
    :param actual_version: The version actually stored.
    """

    def __init__(
        self,
        printer_id: str,
        *,
        expected_version: int,
        actual_version: int,
    ) -> None:
        self.printer_id = printer_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Stale state for printer {printer_id!r}: "
            f"expected version {expected_version}, "
            f"actual version {actual_version}"
        )


# ---------------------------------------------------------------------------
# Printer state lock
# ---------------------------------------------------------------------------

class PrinterStateLock:
    """Thread-safe optimistic lock manager for printer state versions.

    Stores a :class:`StateVersion` per printer.  Every :meth:`acquire`
    bumps the version and records the owner; :meth:`release` only
    succeeds if the caller's version still matches.

    :param persistence: Optional :class:`~kiln.persistence.KilnPersistence`
        instance for crash-recovery persistence.  When ``None``, versions
        are stored in-memory only.
    """

    def __init__(
        self,
        *,
        persistence: Optional[Any] = None,
    ) -> None:
        self._versions: Dict[str, StateVersion] = {}
        self._lock = threading.Lock()
        self._persistence = persistence

        if self._persistence is not None:
            self._load_from_persistence()

    # -- public API --------------------------------------------------------

    def acquire(self, printer_id: str, *, owner: str = "unknown") -> StateVersion:
        """Increment the version for *printer_id* and record *owner*.

        :param printer_id: Printer to lock.
        :param owner: Agent or session identifier claiming the lock.
        :returns: The newly created :class:`StateVersion`.
        """
        with self._lock:
            current = self._versions.get(printer_id)
            new_version = (current.version + 1) if current else 1
            sv = StateVersion(
                printer_id=printer_id,
                version=new_version,
                updated_at=time.time(),
                updated_by=owner,
            )
            self._versions[printer_id] = sv
            self._persist(sv)
            logger.debug(
                "Acquired lock for %r v%d by %s",
                printer_id, new_version, owner,
            )
            return sv

    def release(self, printer_id: str, version: int) -> bool:
        """Release the lock if *version* matches the current version.

        :param printer_id: Printer to release.
        :param version: The version the caller holds.
        :returns: ``True`` if released successfully, ``False`` if stale.
        """
        with self._lock:
            current = self._versions.get(printer_id)
            if current is None:
                return False
            if current.version != version:
                logger.warning(
                    "Stale release for %r: caller has v%d, current is v%d",
                    printer_id, version, current.version,
                )
                return False
            del self._versions[printer_id]
            self._persist_delete(printer_id)
            logger.debug("Released lock for %r v%d", printer_id, version)
            return True

    def check(self, printer_id: str, version: int) -> bool:
        """Check whether *version* is still current for *printer_id*.

        :param printer_id: Printer to check.
        :param version: The version to validate.
        :returns: ``True`` if *version* matches the stored version.
        """
        with self._lock:
            current = self._versions.get(printer_id)
            if current is None:
                return False
            return current.version == version

    def get_version(self, printer_id: str) -> Optional[StateVersion]:
        """Return the current :class:`StateVersion` for *printer_id*.

        :returns: The version info, or ``None`` if no lock is held.
        """
        with self._lock:
            return self._versions.get(printer_id)

    def force_release(self, printer_id: str) -> None:
        """Unconditionally release the lock for *printer_id*.

        Use this as an admin override for stuck locks.

        :param printer_id: Printer to force-release.
        """
        with self._lock:
            if printer_id in self._versions:
                old = self._versions.pop(printer_id)
                self._persist_delete(printer_id)
                logger.warning(
                    "Force-released lock for %r (was v%d by %s)",
                    printer_id, old.version, old.updated_by,
                )

    def list_locks(self) -> List[StateVersion]:
        """Return all currently held locks.

        :returns: A list of :class:`StateVersion` entries, sorted by
            printer_id for deterministic output.
        """
        with self._lock:
            return sorted(
                self._versions.values(),
                key=lambda sv: sv.printer_id,
            )

    # -- persistence helpers -----------------------------------------------

    def _persist(self, sv: StateVersion) -> None:
        """Write a version record to SQLite if persistence is configured."""
        if self._persistence is None:
            return
        try:
            self._persistence.save_agent_memory(
                f"_state_lock:{sv.printer_id}",
                sv.to_dict(),
                device_type=None,
            )
        except Exception as exc:
            logger.warning(
                "Failed to persist state lock for %r: %s",
                sv.printer_id, exc,
            )

    def _persist_delete(self, printer_id: str) -> None:
        """Remove a version record from SQLite if persistence is configured."""
        if self._persistence is None:
            return
        try:
            self._persistence.delete_agent_memory(
                f"_state_lock:{printer_id}",
                device_type=None,
            )
        except Exception as exc:
            logger.warning(
                "Failed to delete persisted lock for %r: %s",
                printer_id, exc,
            )

    def _load_from_persistence(self) -> None:
        """Restore lock state from SQLite on startup."""
        if self._persistence is None:
            return
        try:
            memories = self._persistence.list_agent_memories()
            for mem in memories:
                key = mem.get("key", "")
                if not key.startswith("_state_lock:"):
                    continue
                value = mem.get("value")
                if not isinstance(value, dict):
                    continue
                printer_id = value.get("printer_id", "")
                if printer_id:
                    self._versions[printer_id] = StateVersion(
                        printer_id=printer_id,
                        version=value.get("version", 0),
                        updated_at=value.get("updated_at", 0.0),
                        updated_by=value.get("updated_by", "unknown"),
                    )
            if self._versions:
                logger.info(
                    "Restored %d state lock(s) from persistence",
                    len(self._versions),
                )
        except Exception as exc:
            logger.warning("Failed to load state locks from persistence: %s", exc)


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------

class PrinterLockContext:
    """Context manager for scoped printer lock acquire / release.

    Acquires the lock on entry and releases it on exit.  If the lock
    is stale by exit time (another agent acquired it), a warning is
    logged but no exception is raised -- the damage is already done.

    Usage::

        with PrinterLockContext(lock, "ender3-lab", owner="agent-1") as version:
            # work with printer while holding the lock
            pass
    """

    def __init__(
        self,
        lock: PrinterStateLock,
        printer_id: str,
        *,
        owner: str = "unknown",
    ) -> None:
        self._lock = lock
        self._printer_id = printer_id
        self._owner = owner
        self._version: Optional[StateVersion] = None

    def __enter__(self) -> StateVersion:
        self._version = self._lock.acquire(self._printer_id, owner=self._owner)
        return self._version

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._version is not None:
            released = self._lock.release(
                self._printer_id, self._version.version,
            )
            if not released:
                logger.warning(
                    "Lock for %r v%d was stale on context exit "
                    "(owner=%s, exc=%s)",
                    self._printer_id,
                    self._version.version,
                    self._owner,
                    exc_type.__name__ if exc_type else None,
                )


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

_state_lock: Optional[PrinterStateLock] = None


def get_state_lock() -> PrinterStateLock:
    """Return the module-level :class:`PrinterStateLock` singleton.

    The instance is lazily created on first call.
    """
    global _state_lock
    if _state_lock is None:
        _state_lock = PrinterStateLock()
    return _state_lock


def lock_printer(printer_id: str, *, owner: str = "unknown") -> PrinterLockContext:
    """Return a :class:`PrinterLockContext` using the global singleton.

    :param printer_id: Printer to lock.
    :param owner: Agent or session identifier.
    :returns: A context manager that acquires on entry and releases on exit.
    """
    return PrinterLockContext(get_state_lock(), printer_id, owner=owner)
