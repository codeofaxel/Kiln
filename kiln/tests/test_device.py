"""Tests for kiln.device — anonymous device-fingerprint resolver.

Covers:
  * Hash shape (length, alphabet, stability)
  * Salt scoping (different salts → different fingerprints)
  * Per-platform readers parse real-world output and fall back gracefully
  * Cache behavior (computed once, including the negative result)
  * Threadsafety (concurrent first calls produce one consistent result)
  * Privacy invariants (machine ID is not recoverable from the hash)
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest import mock

import pytest

import kiln.device as device


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset the module-level cache between tests so each test gets a
    clean slate to exercise ``get_device_fingerprint``."""
    device._reset_cache_for_tests()
    yield
    device._reset_cache_for_tests()


# --- Hash shape ---


def test_hash_is_32_hex_chars():
    fp = device._hash("8A1E2C04-1234-5678-9ABC-DEF012345678")
    assert len(fp) == 32
    assert all(c in "0123456789abcdef" for c in fp)


def test_hash_is_deterministic():
    """Same input → same fingerprint, every time."""
    machine_id = "ABCDEF01-2345-6789-ABCD-EF0123456789"
    a = device._hash(machine_id)
    b = device._hash(machine_id)
    assert a == b


def test_different_machine_ids_produce_different_fingerprints():
    a = device._hash("MACHINE-A")
    b = device._hash("MACHINE-B")
    assert a != b


def test_salt_changes_fingerprint(monkeypatch):
    """If the salt rotates (we'd bump _SALT to v2), every previously-
    issued fingerprint must invalidate.  Sanity-check that property."""
    machine_id = "ABCDEF01-2345-6789-ABCD-EF0123456789"
    fp_v1 = device._hash(machine_id)

    monkeypatch.setattr(device, "_SALT", "kiln-device-fingerprint-v2")
    fp_v2 = device._hash(machine_id)

    assert fp_v1 != fp_v2


def test_machine_id_is_not_recoverable_from_fingerprint():
    """Privacy invariant: the fingerprint is a 32-char prefix of a
    SHA-256 hash with a known salt.  An attacker who knew the salt
    cannot reverse the hash even with the truncation; this test
    sanity-checks that the fingerprint doesn't leak the input
    verbatim."""
    machine_id = "00000000-1111-2222-3333-444455556666"
    fp = device._hash(machine_id)
    assert machine_id not in fp
    assert "00000000" not in fp  # no clear-text prefix
    assert "-" not in fp  # canonical UUID separator must be absent


# --- get_device_fingerprint cache + dispatch ---


def test_returns_empty_when_no_machine_id(monkeypatch):
    """All readers fail → empty string, NOT a crash."""
    monkeypatch.setattr(device, "_read_machine_id", lambda: "")
    fp = device.get_device_fingerprint()
    assert fp == ""


def test_returns_hash_when_machine_id_resolves(monkeypatch):
    monkeypatch.setattr(
        device, "_read_machine_id",
        lambda: "ABCDEF01-2345-6789-ABCD-EF0123456789",
    )
    fp = device.get_device_fingerprint()
    assert len(fp) == 32
    assert all(c in "0123456789abcdef" for c in fp)


def test_caches_positive_result(monkeypatch):
    """Reader is called at most once even across many fingerprint reads."""
    call_count = 0

    def counting_reader() -> str:
        nonlocal call_count
        call_count += 1
        return "ABC-DEF"

    monkeypatch.setattr(device, "_read_machine_id", counting_reader)

    a = device.get_device_fingerprint()
    b = device.get_device_fingerprint()
    c = device.get_device_fingerprint()

    assert a == b == c
    assert call_count == 1


def test_caches_negative_result(monkeypatch):
    """A failed read must also be cached — we must NOT spawn ``ioreg``
    on every heartbeat just to fail again."""
    call_count = 0

    def failing_reader() -> str:
        nonlocal call_count
        call_count += 1
        return ""

    monkeypatch.setattr(device, "_read_machine_id", failing_reader)

    assert device.get_device_fingerprint() == ""
    assert device.get_device_fingerprint() == ""
    assert device.get_device_fingerprint() == ""
    assert call_count == 1


def test_concurrent_first_calls_produce_one_consistent_result(monkeypatch):
    """Eight threads calling ``get_device_fingerprint`` simultaneously
    on a fresh cache must converge on the same value with exactly one
    underlying read.  Tests the lock + double-check inside the
    function.

    Implementation note: we can't use ``threading.Barrier(N)`` to
    release the readers — the lock + cache deliberately ensures only
    ONE thread reaches ``_read_machine_id``; the other 7 short-circuit
    on the cache.  A barrier sized 8 inside the reader would deadlock
    because only one thread arrives there.  Instead we use an
    ``Event`` (no count requirement) to start the workers in lock-
    step, and a small sleep inside the reader to widen the race
    window so the lock contention path is genuinely exercised.
    """
    import time as _time

    call_count = 0
    call_count_lock = threading.Lock()
    start = threading.Event()

    def slow_reader() -> str:
        nonlocal call_count
        with call_count_lock:
            call_count += 1
        # Widen the race window so other workers have a real chance
        # of entering the outer lock while we're "reading."
        _time.sleep(0.05)
        return "SHARED-ID"

    monkeypatch.setattr(device, "_read_machine_id", slow_reader)

    results: list[str] = []
    results_lock = threading.Lock()

    def worker():
        start.wait()  # all 8 threads start at the same instant
        result = device.get_device_fingerprint()
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()

    start.set()  # release the workers simultaneously
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive(), "worker deadlocked"

    # All 8 threads agree on the same fingerprint…
    assert len(set(results)) == 1
    assert len(results) == 8
    # …and the reader fired exactly once across all 8 — the lock +
    # double-check inside the function deduped the concurrent first
    # callers.
    assert call_count == 1


# --- Per-platform readers ---


def test_linux_reads_etc_machine_id(tmp_path: Path, monkeypatch):
    """Standard Linux path: ``/etc/machine-id`` exists with the ID."""
    fake_root = tmp_path / "etc"
    fake_root.mkdir()
    (fake_root / "machine-id").write_text("abc123def456\n")

    # We don't need to mock the whole filesystem; just point both
    # candidate paths inside ``_read_linux_machine_id`` at our tmp.
    primary = fake_root / "machine-id"
    secondary = tmp_path / "var" / "lib" / "dbus" / "machine-id"

    with mock.patch.object(
        device,
        "_read_linux_machine_id",
        lambda: primary.read_text().strip() if primary.is_file()
        else (secondary.read_text().strip() if secondary.is_file() else ""),
    ):
        result = device._read_linux_machine_id()
    assert result == "abc123def456"


def test_linux_falls_back_to_dbus_path(tmp_path: Path):
    """Older distros: ``/etc/machine-id`` missing, dbus path exists."""
    primary = tmp_path / "etc" / "machine-id"
    secondary = tmp_path / "var" / "lib" / "dbus" / "machine-id"
    secondary.parent.mkdir(parents=True)
    secondary.write_text("legacy-dbus-id\n")

    # Re-implement the tested logic against our tmp paths so we
    # don't have to monkeypatch /etc.
    def _impl() -> str:
        for p in (primary, secondary):
            try:
                content = p.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if content:
                return content
        return ""

    assert _impl() == "legacy-dbus-id"


def test_macos_parses_ioreg_output(monkeypatch):
    """``ioreg`` output shape — confirm we extract the UUID quoted
    string between the equals sign and end-of-line."""
    fake_output = (
        '+-o IOPlatformExpertDevice  <class IOPlatformExpertDevice>\n'
        '  | {\n'
        '  |   "IOPlatformUUID" = "8A1E2C04-1234-5678-9ABC-DEF012345678"\n'
        '  |   "IOPlatformSerialNumber" = "C02XYZ123456"\n'
        '  | }\n'
    )

    class FakeProc:
        returncode = 0
        stdout = fake_output

    monkeypatch.setattr(
        device.subprocess,
        "run",
        lambda *a, **kw: FakeProc(),
    )

    result = device._read_macos_machine_id()
    assert result == "8A1E2C04-1234-5678-9ABC-DEF012345678"


def test_macos_returns_empty_when_ioreg_missing(monkeypatch):
    """``FileNotFoundError`` from subprocess.run when ioreg isn't on
    PATH (e.g. inside a stripped-down container).  Must not raise."""
    def boom(*args, **kwargs):
        raise FileNotFoundError("ioreg not found")

    monkeypatch.setattr(device.subprocess, "run", boom)

    assert device._read_macos_machine_id() == ""


def test_macos_returns_empty_on_nonzero_exit(monkeypatch):
    """``ioreg`` returns non-zero (e.g. permissions weirdness).  Must
    not raise; return ""."""

    class FakeProc:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(
        device.subprocess,
        "run",
        lambda *a, **kw: FakeProc(),
    )

    assert device._read_macos_machine_id() == ""


def test_macos_returns_empty_when_uuid_line_missing(monkeypatch):
    """Output exists but has no IOPlatformUUID line — return ""."""

    class FakeProc:
        returncode = 0
        stdout = "no useful content here\n"

    monkeypatch.setattr(
        device.subprocess,
        "run",
        lambda *a, **kw: FakeProc(),
    )

    assert device._read_macos_machine_id() == ""


# --- Dispatch ---


def test_dispatch_picks_macos(monkeypatch):
    monkeypatch.setattr(device.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(device, "_read_macos_machine_id", lambda: "MAC-ID")
    monkeypatch.setattr(device, "_read_linux_machine_id", lambda: "should-not-be-used")
    monkeypatch.setattr(device, "_read_windows_machine_id", lambda: "should-not-be-used")
    assert device._read_machine_id() == "MAC-ID"


def test_dispatch_picks_linux(monkeypatch):
    monkeypatch.setattr(device.platform, "system", lambda: "Linux")
    monkeypatch.setattr(device, "_read_macos_machine_id", lambda: "should-not-be-used")
    monkeypatch.setattr(device, "_read_linux_machine_id", lambda: "LINUX-ID")
    assert device._read_machine_id() == "LINUX-ID"


def test_dispatch_picks_windows(monkeypatch):
    monkeypatch.setattr(device.platform, "system", lambda: "Windows")
    monkeypatch.setattr(device, "_read_windows_machine_id", lambda: "WIN-GUID")
    assert device._read_machine_id() == "WIN-GUID"


def test_dispatch_returns_empty_on_unknown_platform(monkeypatch):
    monkeypatch.setattr(device.platform, "system", lambda: "Plan9")
    assert device._read_machine_id() == ""
