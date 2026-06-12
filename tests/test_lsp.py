"""Tests for the minimal Flex language server."""

from __future__ import annotations

import io
import json

from flx.lsp import LanguageServer, diagnostics_for_source, formatting_edits


def _frame(payload: dict[str, object]) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def _messages(raw: bytes) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    stream = io.BytesIO(raw)
    while True:
        headers: dict[str, str] = {}
        while True:
            line = stream.readline()
            if not line:
                return out
            if line == b"\r\n":
                break
            key, _, value = line.decode("ascii").partition(":")
            headers[key.lower()] = value.strip()
        length = int(headers["content-length"])
        out.append(json.loads(stream.read(length).decode("utf-8")))


def test_lsp_diagnostics_from_checker() -> None:
    diags = diagnostics_for_source("file:///bad.flx", "fn main() -> I64 = { true }\n")
    assert [d["code"] for d in diags] == ["TYPE003"]
    assert diags[0]["source"] == "flx"
    assert "expected I64" in str(diags[0]["message"])


def test_lsp_formatting_edits_whole_document() -> None:
    edits = formatting_edits("file:///fmt.flx", "fn main()->I64={1}\n")
    assert len(edits) == 1
    assert "fn main() -> I64 = {" in edits[0]["newText"]


def test_lsp_initialize_over_stdio() -> None:
    incoming = io.BytesIO(
        b"".join(
            [
                _frame({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                _frame({"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": {}}),
                _frame({"jsonrpc": "2.0", "method": "exit", "params": {}}),
            ]
        )
    )
    outgoing = io.BytesIO()
    assert LanguageServer(incoming, outgoing).run() == 0
    messages = _messages(outgoing.getvalue())
    assert messages[0]["id"] == 1
    result = messages[0]["result"]
    assert isinstance(result, dict)
    assert result["capabilities"]["documentFormattingProvider"] is True
