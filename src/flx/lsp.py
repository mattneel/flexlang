"""Minimal Language Server Protocol support for Flex.

The server is intentionally small: stdio JSON-RPC, full document sync,
compiler diagnostics, and whole-document formatting through the existing
formatter. It is enough for editors to get real feedback while the richer IDE
surface settles.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import unquote, urlparse

from flx.diagnostics import Diagnostic, FlexError
from flx.macro import expand
from flx.sema.specialize import check_and_monomorphize
from flx.syntax.formatter import format_source
from flx.syntax.parser import parse

Json = dict[str, Any]


def _uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return unquote(parsed.path)
    return uri


def _range_for_source(source: str) -> Json:
    lines = source.splitlines()
    if not lines:
        return {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
    return {
        "start": {"line": 0, "character": 0},
        "end": {"line": len(lines) - 1, "character": len(lines[-1])},
    }


def _range_for_diag(diag: Diagnostic) -> Json:
    span = diag.span
    if span is None:
        return {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
    return {
        "start": {
            "line": max(0, span.start.line - 1),
            "character": max(0, span.start.col - 1),
        },
        "end": {
            "line": max(0, span.end.line - 1),
            "character": max(0, span.end.col - 1),
        },
    }


def _lsp_diag(diag: Diagnostic) -> Json:
    message = diag.message if diag.help is None else f"{diag.message}\n{diag.help}"
    return {
        "range": _range_for_diag(diag),
        "severity": 1,
        "code": diag.code,
        "source": "flx",
        "message": message,
    }


def diagnostics_for_source(uri: str, source: str) -> list[Json]:
    """Return LSP diagnostics for one in-memory Flex document.

    This path intentionally checks the current document as provided by the
    editor. Cross-file package loading is handled by the CLI; the LSP can grow
    an overlay-aware program loader later without changing the protocol surface.
    """
    path = _uri_to_path(uri)
    try:
        module = expand(parse(source, path))
        check_and_monomorphize(module)
    except FlexError as err:
        return [_lsp_diag(diag) for diag in err.diagnostics]
    except RecursionError:
        return [
            _lsp_diag(
                Diagnostic(
                    "PAR003",
                    "input is too deeply nested",
                    None,
                    help="simplify the expression or split it into intermediate `let`s",
                )
            )
        ]
    return []


def formatting_edits(uri: str, source: str) -> list[Json]:
    path = _uri_to_path(uri)
    formatted = format_source(source, path)
    if formatted == source:
        return []
    return [{"range": _range_for_source(source), "newText": formatted}]


@dataclass
class LanguageServer:
    stdin: BinaryIO
    stdout: BinaryIO
    documents: dict[str, str] = field(default_factory=dict)
    shutdown_requested: bool = False

    def run(self) -> int:
        while True:
            msg = self._read_message()
            if msg is None:
                return 0
            if self._handle(msg):
                return 0

    def _read_message(self) -> Json | None:
        headers: dict[str, str] = {}
        while True:
            line = self.stdin.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            key, _, value = line.decode("ascii").partition(":")
            headers[key.lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        if length <= 0:
            return None
        return cast(Json, json.loads(self.stdin.read(length).decode("utf-8")))

    def _write(self, payload: Json) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.stdout.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
        self.stdout.flush()

    def _respond(self, msg_id: object, result: object = None) -> None:
        self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _notify(self, method: str, params: Json) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _publish(self, uri: str) -> None:
        source = self.documents.get(uri)
        if source is None:
            return
        self._notify(
            "textDocument/publishDiagnostics",
            {"uri": uri, "diagnostics": diagnostics_for_source(uri, source)},
        )

    def _handle(self, msg: Json) -> bool:
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            self._respond(
                msg_id,
                {
                    "capabilities": {
                        "textDocumentSync": 1,
                        "documentFormattingProvider": True,
                    },
                    "serverInfo": {"name": "flx-lsp"},
                },
            )
            return False
        if method == "shutdown":
            self.shutdown_requested = True
            self._respond(msg_id, None)
            return False
        if method == "exit":
            return True
        if method == "textDocument/didOpen":
            doc = params["textDocument"]
            self.documents[doc["uri"]] = doc.get("text", "")
            self._publish(doc["uri"])
            return False
        if method == "textDocument/didChange":
            doc = params["textDocument"]
            changes = params.get("contentChanges") or []
            if changes:
                self.documents[doc["uri"]] = changes[-1].get("text", "")
            self._publish(doc["uri"])
            return False
        if method == "textDocument/didSave":
            doc = params["textDocument"]
            if "text" in params:
                self.documents[doc["uri"]] = params["text"]
            self._publish(doc["uri"])
            return False
        if method == "textDocument/formatting":
            doc = params["textDocument"]
            uri = doc["uri"]
            source = self.documents.get(uri)
            if source is None and urlparse(uri).scheme == "file":
                try:
                    source = Path(_uri_to_path(uri)).read_text(encoding="utf-8")
                except OSError:
                    source = ""
            try:
                result = formatting_edits(uri, source or "")
            except FlexError as err:
                result = []
                self._notify(
                    "textDocument/publishDiagnostics",
                    {"uri": uri, "diagnostics": [_lsp_diag(diag) for diag in err.diagnostics]},
                )
            self._respond(msg_id, result)
            return False
        if msg_id is not None:
            self._respond(msg_id, None)
        return False


def run() -> int:
    return LanguageServer(sys.stdin.buffer, sys.stdout.buffer).run()


if __name__ == "__main__":
    raise SystemExit(run())
