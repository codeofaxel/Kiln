"""A fake Bambu port-6000 camera server for tests.

Speaks the printer's own LAN video protocol: TLS, then an 80-byte auth
packet (username + LAN access code), then a run of frames, each a 16-byte
little-endian header (payload size, itrack 0, flags 1, 0) followed by the
JPEG bytes.  Frames are written in chunks no larger than a real printer's
TLS records so the reader's reassembly is exercised, and a wrong access
code is answered the way the printer answers it: the connection closes
with no data at all.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import socket
import ssl
import struct
import tempfile
import threading
import time
from pathlib import Path

_USERNAME = b"bblp"
_CHUNK = 4096


def make_jpeg(index: int, size: int = 6000) -> bytes:
    """A minimal JPEG-shaped payload with a recognisable body."""
    body = (f"frame{index:04d}".encode("ascii") * (size // 9 + 1))[: size - 6]
    return b"\xff\xd8\xff\xe0" + body + b"\xff\xd9"


def _self_signed_cert(tmp: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-bambu")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=1))
        .not_valid_after(now + _dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp / "cert.pem"
    key_path = tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class FakeBambuCamera:
    """Serve ``frames`` at ``interval`` seconds to every authenticated viewer."""

    def __init__(
        self,
        access_code: str = "12345678",
        frames: list[bytes] | None = None,
        interval: float = 0.05,
        repeat: bool = True,
        video_enabled: bool = True,
    ) -> None:
        self.access_code = access_code
        self.frames = frames or [make_jpeg(i) for i in range(4)]
        self.interval = interval
        self.repeat = repeat
        #: With the printer's video toggle off the port does not answer.
        self.video_enabled = video_enabled
        self.connections = 0
        self.rejected = 0
        self.frames_sent = 0
        self._stop = threading.Event()
        self._tmp = Path(tempfile.mkdtemp(prefix="fake-bambu-"))
        cert, key = _self_signed_cert(self._tmp)
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(str(cert), str(key))
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._threads: list[threading.Thread] = []
        self._accept = threading.Thread(target=self._serve, daemon=True)
        self._accept.start()

    # -- lifecycle -------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._sock.close()
        self._accept.join(timeout=2)
        for t in self._threads:
            t.join(timeout=2)

    def __enter__(self) -> FakeBambuCamera:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- protocol --------------------------------------------------------

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self.video_enabled:
                # The port is closed on a printer with video off; the nearest
                # honest fake is an accepted-then-dropped TCP connection.
                raw.close()
                continue
            t = threading.Thread(target=self._handle, args=(raw,), daemon=True)
            t.start()
            self._threads.append(t)

    def _handle(self, raw: socket.socket) -> None:
        try:
            conn = self._ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        with conn:
            conn.settimeout(2.0)
            try:
                auth = b""
                while len(auth) < 80:
                    chunk = conn.recv(80 - len(auth))
                    if not chunk:
                        return
                    auth += chunk
            except (TimeoutError, OSError):
                return
            size, kind, flags, zero = struct.unpack("<IIII", auth[:16])
            user = auth[16:48].rstrip(b"\x00")
            code = auth[48:80].rstrip(b"\x00")
            if (size, kind, flags, zero) != (0x40, 0x3000, 0, 0):
                self.rejected += 1
                return
            if user != _USERNAME or code != self.access_code.encode("ascii"):
                self.rejected += 1
                return  # closes with no data, as the printer does
            self.connections += 1
            i = 0
            while not self._stop.is_set():
                if i >= len(self.frames):
                    if not self.repeat:
                        return
                    i = 0
                jpeg = self.frames[i]
                i += 1
                header = struct.pack("<IIII", len(jpeg), 0, 1, 0)
                try:
                    conn.sendall(header)
                    for off in range(0, len(jpeg), _CHUNK):
                        conn.sendall(jpeg[off : off + _CHUNK])
                        time.sleep(0.001)
                except OSError:
                    return
                self.frames_sent += 1
                if self._stop.wait(self.interval):
                    return
