"""Where a generation job is kept between the call that starts it and the
calls that read it back.

A provider's :meth:`~kiln.generation.base.GenerationProvider.generate` starts
a job; :meth:`get_job_status` and :meth:`download_result` are separate calls
that name it by id, sometimes minutes later.  Everything a provider needs to
answer them -- the job's state, which of a service's endpoints holds it, the
request's own words, the finished file, and the part the job was made from --
is one :class:`JobRecord` in a :class:`JobRecordStore`.

Two stores, one contract:

* :class:`MemoryJobRecords` -- this process's memory.  Every provider has one,
  and on a plain install it is the only one: one process, one person, the
  records living as long as the process does, exactly as before.
* A store a deployment supplies.  When ``kiln_pro`` is installed,
  :func:`supplied_store_for` asks
  ``kiln_pro.hosted_generation_jobs.job_record_store()`` on every call, for
  the providers the server shares between calls (:func:`share_records`).
  ``None`` means "use this process's memory"; a store means "keep this
  caller's records there".

THE CONTRACT EVERY STORE KEEPS
------------------------------
``put(record)``
    Keep a new record as the calling person's own.
``get(provider, job_id)``
    That person's record, or ``None``.  A job id somebody else started
    answers ``None`` exactly as an id nobody started does.
``update(provider, job_id, **fields)``
    Change :data:`UPDATABLE` fields of that person's own record; returns it,
    or ``None`` when they have none.
``expire()``
    Forget records past the store's keep; returns how many.
``keep_result(record, path)``
    Note where the record's finished file is, before the record is put.
``result_file(record, output_dir)``
    A readable path to that file on this machine, or ``None``.
``strict``
    ``True``: an id with no record is unknown, even where the service that
    ran the job would still answer for it.  ``False`` (memory): a service's
    id this process never saw is asked about, as it always was.
``keeps_prompts``
    Whether ``get`` gives back the request's own words.

A store that cannot answer raises :class:`JobStoreUnavailable`, which the
tools return as a retryable refusal (``JOB_STORE_UNAVAILABLE``).  A provider
never falls back to its own memory when a supplied store fails: a record
kept only in this process would answer "not found" to the call that needs it.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from kiln.generation.base import GenerationError, GenerationJob, GenerationStatus

logger = logging.getLogger(__name__)

#: Which of a service's endpoints holds a job, for a provider with more than
#: one.  ``""`` for a provider with one.
ROUTES = frozenset({"", "text", "image", "multi_image", "refine", "retexture", "remesh"})

#: What a ``made_from`` reference may name: the part a job started from.
#: ``mesh_sha256`` is the SHA-256 of the exact bytes the job was handed; the
#: rest name where those bytes sit in the person's design history.
MADE_FROM_KEYS = frozenset({"design_id", "version_id", "version", "mesh_sha256", "brief_id"})

#: The fields :meth:`JobRecordStore.update` may change.  The job's identity
#: (provider, id, route) and its request (prompt, format, style) never change.
UPDATABLE = frozenset({"status", "progress", "error", "result", "made_from"})


class JobStoreUnavailable(GenerationError):
    """The store that keeps this caller's jobs could not answer.

    Retryable: nothing about the request was wrong, and the same call can
    succeed a moment later.
    """

    def __init__(self, message: str = "", *, code: str = "JOB_STORE_UNAVAILABLE") -> None:
        super().__init__(
            message
            or "Kiln couldn't reach the place it keeps your generation jobs, so "
            "it didn't start or read this one. Nothing was changed; try again "
            "in a moment.",
            code=code,
        )


@dataclass
class JobRecord:
    """Everything a provider must know to answer for one job later."""

    provider: str
    job_id: str
    status: str = GenerationStatus.PENDING.value
    progress: int = 0
    created_at: float = 0.0
    format: str = "stl"
    style: str | None = None
    error: str | None = None
    #: The request's own words.  A store that does not keep them gives back "".
    prompt: str = ""
    #: Which of the service's endpoints holds the job (:data:`ROUTES`).
    route: str = ""
    #: Where the finished file is; written and read only by the store
    #: (:meth:`JobRecordStore.keep_result` / :meth:`~JobRecordStore.result_file`).
    result: dict[str, Any] | None = None
    #: The part this job was made from, if it was made from one
    #: (:data:`MADE_FROM_KEYS`).
    made_from: dict[str, Any] | None = None

    @classmethod
    def from_job(cls, job: GenerationJob, **extra: Any) -> JobRecord:
        """The record of *job*, plus fields a job does not carry (``route``,
        ``made_from``)."""
        return cls(
            provider=job.provider,
            job_id=job.id,
            status=job.status.value,
            progress=int(job.progress or 0),
            created_at=float(job.created_at or 0.0),
            format=job.format,
            style=job.style,
            error=job.error,
            prompt=job.prompt,
            **extra,
        )

    def job(self) -> GenerationJob:
        """The job as a status call answers it."""
        return GenerationJob(
            id=self.job_id,
            provider=self.provider,
            prompt=self.prompt,
            status=GenerationStatus(self.status),
            progress=self.progress,
            created_at=self.created_at,
            format=self.format,
            style=self.style,
            error=self.error,
        )


def made_from_part(mesh_bytes: bytes, **where: Any) -> dict[str, Any]:
    """A ``made_from`` reference for a job started on *mesh_bytes*: their
    SHA-256, plus whichever of the history keys the caller knows."""
    ref: dict[str, Any] = {"mesh_sha256": hashlib.sha256(mesh_bytes).hexdigest()}
    for key, value in where.items():
        if key in MADE_FROM_KEYS and value not in (None, ""):
            ref[key] = value
    return ref


class JobRecordStore(Protocol):
    """The contract in the module docstring."""

    strict: bool
    keeps_prompts: bool

    def put(self, record: JobRecord) -> None: ...

    def get(self, provider: str, job_id: str) -> JobRecord | None: ...

    def update(self, provider: str, job_id: str, **fields: Any) -> JobRecord | None: ...

    def expire(self) -> int: ...

    def keep_result(self, record: JobRecord, path: str) -> None: ...

    def result_file(self, record: JobRecord, output_dir: str) -> str | None: ...


def check_update(changes: dict[str, Any]) -> None:
    """Refuse a change :meth:`JobRecordStore.update` may not make: a field
    outside :data:`UPDATABLE`, or a ``made_from`` naming keys outside
    :data:`MADE_FROM_KEYS`.  Every store calls it."""
    unknown = set(changes) - UPDATABLE
    if unknown:
        raise ValueError(f"a job record cannot change {sorted(unknown)}")
    made_from = changes.get("made_from")
    if made_from is not None and (
        not isinstance(made_from, dict) or set(made_from) - MADE_FROM_KEYS
    ):
        raise ValueError(f"made_from may name only {sorted(MADE_FROM_KEYS)}")


class MemoryJobRecords:
    """One process's records, for as long as the process lives.

    Not shared between callers on purpose: a plain install is one person.
    A provider the server shares between callers keeps its records here
    only when no store was supplied (:func:`supplied_store_for`).
    """

    strict = False
    keeps_prompts = True

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], JobRecord] = {}
        self._lock = threading.Lock()

    def put(self, record: JobRecord) -> None:
        with self._lock:
            self._records[(record.provider, record.job_id)] = record

    def get(self, provider: str, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._records.get((provider, job_id))

    def update(self, provider: str, job_id: str, **changes: Any) -> JobRecord | None:
        check_update(changes)
        with self._lock:
            record = self._records.get((provider, job_id))
            if record is None:
                return None
            for name, value in changes.items():
                setattr(record, name, copy.deepcopy(value))
            return record

    def expire(self) -> int:
        """Nothing: a process's records last as long as the process."""
        return 0

    def keep_result(self, record: JobRecord, path: str) -> None:
        record.result = {"path": path}

    def result_file(self, record: JobRecord, output_dir: str) -> str | None:
        """Where the provider wrote the file.  ``output_dir`` is not used:
        the file is already on this machine, where it was made."""
        return (record.result or {}).get("path") or None


#: Set on a provider the server keeps for every later call (``kiln.server``'s
#: provider registry).  Only such a provider asks for a supplied store: a
#: provider built for one call reads its own jobs back in that same call.
_SHARED_MARK = "_serves_later_calls"


def share_records(provider: Any) -> Any:
    """Mark *provider* as one whose jobs a LATER call reads back."""
    setattr(provider, _SHARED_MARK, True)
    return provider


def supplied_store_for(provider: Any) -> JobRecordStore | None:
    """The store a deployment supplies for *provider*'s jobs on this call,
    or ``None`` -- the provider then keeps them in its own memory
    (:meth:`~kiln.generation.base.GenerationProvider._job_records`)."""
    if not getattr(provider, _SHARED_MARK, False):
        return None
    try:
        from kiln_pro.hosted_generation_jobs import job_record_store
    except ImportError:
        return None
    try:
        return job_record_store()
    except GenerationError:
        raise
    except Exception as exc:  # noqa: BLE001 -- a store that cannot say is a store that cannot answer
        logger.warning("job record store could not be resolved", exc_info=True)
        raise JobStoreUnavailable() from exc


__all__ = [
    "MADE_FROM_KEYS",
    "ROUTES",
    "UPDATABLE",
    "JobRecord",
    "JobRecordStore",
    "JobStoreUnavailable",
    "MemoryJobRecords",
    "check_update",
    "made_from_part",
    "share_records",
    "supplied_store_for",
]
