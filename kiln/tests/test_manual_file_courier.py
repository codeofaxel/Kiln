"""A manual check run from a plain install sends the manual itself, whole.

``verify_manual_authenticity`` and ``batch_verify_manuals`` run on Kiln's
servers, which cannot open a file on this computer.  The stub reads each file
the call names here and sends its bytes with the call
(``history_records["file_bytes"]``, :func:`kiln.history_courier.file_bytes_for_call`);
the path argument goes empty, and the answer names each file by a mark this
install turns back into its own path.

Only a PDF or a 3MF is read past its opening bytes, so an agent naming any
other file on this computer sends nothing of it.  The network is the only
thing stood in for.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import zipfile

import pytest

from kiln import history_courier as courier

PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ntrailer\n%%EOF\n"
OTHER_PDF = b"%PDF-1.4\n% a different manual\n%%EOF\n"
LIMITS = {"max_files": 4, "max_file_bytes": 4096, "max_total_bytes": 8192}


def _zip(parts: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return buf.getvalue()


THREE_MF = _zip({"[Content_Types].xml": b"<Types/>", "3D/3dmodel.model": b"<model/>", "Kiln/manual.pdf": PDF})
SECRET = b"aws_secret_access_key = do-not-send-me\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KILN_DESIGNS_DIR", raising=False)
    return tmp_path


def _file(home, name: str, data: bytes) -> str:
    path = home / "kit" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _sent(record: dict, sha: str) -> bytes:
    return base64.b64decode(record["contents_b64"][sha])


class TestWhatIsSent:
    def test_a_manual_goes_whole_under_the_fingerprint_of_its_bytes(self, home):
        path = _file(home, "manual.pdf", PDF)

        sent = courier.file_bytes_for_call(path, LIMITS)

        sha = hashlib.sha256(PDF).hexdigest()
        assert sent["arg"] == path
        assert sent["items"] == [{"path": path, "sha256": sha}]
        assert _sent(sent, sha) == PDF

    def test_a_3mf_goes_too(self, home):
        path = _file(home, "kit.3mf", THREE_MF)
        sent = courier.file_bytes_for_call(path, LIMITS)
        assert _sent(sent, sent["items"][0]["sha256"]) == THREE_MF

    def test_identical_copies_go_once(self, home):
        paths = [_file(home, f"copy{n}.pdf", PDF) for n in range(3)]
        paths.append(_file(home, "other.pdf", OTHER_PDF))

        sent = courier.file_bytes_for_call(paths, LIMITS)

        assert [i["path"] for i in sent["items"]] == paths
        assert len(sent["contents_b64"]) == 2
        assert {i["sha256"] for i in sent["items"][:3]} == {hashlib.sha256(PDF).hexdigest()}

    def test_any_other_file_this_computer_holds_sends_nothing_of_itself(self, home):
        path = _file(home, "credentials", SECRET * 4)

        sent = courier.file_bytes_for_call(path, LIMITS)

        assert sent["items"] == [{"path": path, "missing": "not_a_manual"}]
        assert sent["contents_b64"] == {}
        assert b"do-not-send-me" not in json.dumps(sent).encode()

    @pytest.mark.parametrize("name", ["notes.txt", "notes.pdf"])
    def test_a_file_that_only_mentions_a_pdf_header_is_not_sent(self, home, name):
        """A PDF starts with its header; a file that merely carries the
        five characters somewhere in its first kilobyte is not one."""
        path = _file(home, name, b"# my notes\n%PDF-1.7 is the format\n" + SECRET)
        sent = courier.file_bytes_for_call(path, LIMITS)
        assert sent["items"] == [{"path": path, "missing": "not_a_manual"}]
        assert b"do-not-send-me" not in json.dumps(sent).encode()

    def test_a_real_pdf_not_named_as_one_is_not_sent(self, home):
        path = _file(home, "manual.txt", PDF)
        assert courier.file_bytes_for_call(path, LIMITS)["contents_b64"] == {}

    def test_a_byte_order_mark_and_a_little_space_before_the_header_is_still_a_pdf(self, home):
        data = b"\xef\xbb\xbf\r\n  " + PDF
        sent = courier.file_bytes_for_call(_file(home, "bom.pdf", data), LIMITS)
        assert _sent(sent, sent["items"][0]["sha256"]) == data

    @pytest.mark.parametrize("name", ["backup.zip", "backup.3mf"])
    def test_a_plain_zip_is_not_sent_even_named_as_a_3mf(self, home, name):
        path = _file(home, name, _zip({"secrets.txt": SECRET, "Kiln/manual.pdf": PDF}))
        sent = courier.file_bytes_for_call(path, LIMITS)
        assert sent["items"] == [{"path": path, "missing": "not_a_manual"}]
        assert sent["contents_b64"] == {}

    def test_a_link_is_never_followed(self, home):
        real = _file(home, "manual.pdf", PDF)
        link = home / "kit" / "link.pdf"
        os.symlink(real, link)
        sent = courier.file_bytes_for_call(str(link), LIMITS)
        assert sent["items"] == [{"path": str(link), "missing": "a_link"}]
        assert sent["contents_b64"] == {}

    def test_a_missing_file_a_too_large_one_and_a_non_path_are_named_not_sent(self, home):
        big = _file(home, "big.pdf", PDF + b"x" * LIMITS["max_file_bytes"])
        sent = courier.file_bytes_for_call([str(home / "nowhere.pdf"), big, 7], LIMITS)
        assert [i.get("missing") for i in sent["items"]] == ["not_found", "too_large", "invalid"]
        assert sent["contents_b64"] == {}

    def test_more_files_than_one_call_carries_is_refused_here(self, home):
        paths = [_file(home, f"m{n}.pdf", PDF) for n in range(LIMITS["max_files"] + 1)]
        sent = courier.file_bytes_for_call(paths, LIMITS)
        assert sent["refused"] and sent["contents_b64"] == {}
        refusal = courier.served_refusal("batch_verify_manuals", {"file_bytes": sent})
        assert refusal["code"] == "FILES_TOO_LARGE" and "smaller batches" in refusal["error"]

    def test_more_bytes_together_than_one_call_carries_sends_nothing(self, home):
        limits = {**LIMITS, "max_total_bytes": len(PDF) + len(OTHER_PDF) - 1}
        paths = [_file(home, "a.pdf", PDF), _file(home, "b.pdf", OTHER_PDF)]
        sent = courier.file_bytes_for_call(paths, limits)
        assert sent["refused"] and sent["contents_b64"] == {} and sent["items"] == []

    def test_no_limits_from_the_manifest_sends_nothing(self, home):
        sent = courier.file_bytes_for_call(_file(home, "m.pdf", PDF), {})
        assert sent["refused"] and sent["refused_code"] == "UPDATE_KILN"
        assert sent["contents_b64"] == {}


def _register_stubs(monkeypatch, tmp_path, tools: list[dict], fake_call) -> dict:
    """The paid-tool stubs for *tools*, as a plain install registers them."""
    from kiln import server

    pkg = tmp_path / "pkg"
    pkg.mkdir(exist_ok=True)
    (pkg / "pro_tool_manifest.json").write_text(json.dumps({"tools": tools}), encoding="utf-8")
    monkeypatch.setattr(server, "Path", lambda _p: pkg / "kiln")
    monkeypatch.setattr(server, "_PRO_TOOL_NUDGES", {})
    monkeypatch.setattr(server, "_PRO_TOOL_TIERS", {})
    monkeypatch.setattr(server, "_PRO_TOOL_QUOTA", {})
    monkeypatch.setattr(server, "_pro_api_call", fake_call)
    registered: dict = {}

    class _MCP:
        def tool(self, **_kwargs):
            def deco(fn):
                registered[fn.__name__] = fn
                return fn
            return deco

    server._register_pro_tool_stubs(_MCP())
    return registered


def _manual_tool(name: str, param: str, many: bool) -> dict:
    """A manifest entry the way kiln-pro writes one for a manual check."""
    kind = {"type": "array", "items": {"type": "string"}} if many else {"type": "string"}
    return {
        "name": name, "description": "d", "tier": "pro",
        "history": {
            "schema_version": 1, "mode": "served", "kinds": ["file_bytes"],
            "file_param": param, "file_limits": LIMITS,
        },
        "parameters": {"type": "object", "properties": {param: kind}, "required": [param]},
    }


class TestTheStub:
    def test_the_manual_rides_the_call_and_the_path_names_nothing_there(self, home, tmp_path, monkeypatch):
        calls: list[dict] = []

        def fake_call(tool, **kwargs):
            calls.append({"tool": tool, **kwargs})
            return {"status": "error", "code": "RELEASE_NOT_FOUND", "error": "no release", "path": "kiln-file:0"}

        stubs = _register_stubs(
            monkeypatch, tmp_path, [_manual_tool("verify_manual_authenticity", "pdf_path", False)], fake_call,
        )
        path = _file(home, "manual.pdf", PDF)

        answer = stubs["verify_manual_authenticity"](pdf_path=path)

        [call] = calls
        assert call["pdf_path"] == ""
        sent = call["history_records"]["file_bytes"]
        assert _sent(sent, sent["items"][0]["sha256"]) == PDF
        # Whole files get the long wait, not the thirty seconds records get.
        assert call.get("_timeout", 0) >= 300
        assert answer["path"] == path

    def test_a_batch_sends_an_empty_list_and_gets_its_own_paths_back(self, home, tmp_path, monkeypatch):
        calls: list[dict] = []

        def fake_call(tool, **kwargs):
            calls.append(kwargs)
            items = kwargs["history_records"]["file_bytes"]["items"]
            return {"status": "success", "data": {"results": [
                {"path": f"kiln-file:{n}", "authentic": True} for n in range(len(items))
            ]}}

        stubs = _register_stubs(
            monkeypatch, tmp_path, [_manual_tool("batch_verify_manuals", "paths", True)], fake_call,
        )
        paths = [_file(home, "a.pdf", PDF), _file(home, "b.pdf", PDF)]

        answer = stubs["batch_verify_manuals"](paths=paths)

        assert calls[0]["paths"] == []
        assert len(calls[0]["history_records"]["file_bytes"]["contents_b64"]) == 1
        assert [r["path"] for r in answer["data"]["results"]] == paths

    def test_an_oversized_batch_is_refused_before_anything_is_sent(self, home, tmp_path, monkeypatch):
        calls: list = []
        stubs = _register_stubs(
            monkeypatch, tmp_path, [_manual_tool("batch_verify_manuals", "paths", True)],
            lambda tool, **kw: calls.append(tool) or {"status": "success"},
        )
        paths = [_file(home, f"m{n}.pdf", PDF) for n in range(LIMITS["max_files"] + 1)]

        answer = stubs["batch_verify_manuals"](paths=paths)

        assert answer["code"] == "FILES_TOO_LARGE" and calls == []
