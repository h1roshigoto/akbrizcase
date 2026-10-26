"""Conservative allowlist, decoded bytes first; filenames are never storage paths."""

import os
import re

from app.modules.kb.files.process import FileRejected
from app.modules.kb.files.sandbox import run_parser, sealed_snapshot

MIMES = {"pdf": "application/pdf", "txt": "text/plain", "md": "text/markdown"}
# Reject raw HTML entirely in text/Markdown. Rendering in MVP-021+ must also
# disable raw HTML and unsafe URL schemes; this check is not a rendering engine.
HTML = re.compile(r"<\s*(?:!doctype|!--|/?[a-z][a-z0-9]*(?=[\s/>]))", re.I)
EXECUTABLE = re.compile(
    r"\A\s*(?:#!|<\?php|@echo\s+off|(?:import|from)\s+\w+|"
    r"(?:powershell|cmd\.exe|bash|sh)\s|(?:function|def)\s+\w+\s*\(|"
    r"(?:const|let|var)\s+\w+\s*=)",
    re.I,
)
MAGIC = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"\x1f\x8b",
    b"BZh",
    b"\xfd7zXZ",
    b"7z\xbc\xaf\x27\x1c",
    b"Rar!",
    b"MZ",
    b"\x7fELF",
    b"\x00asm",
    b"\xca\xfe\xba\xbe",
)


def validate(data: bytes, name: str, claimed_mime: str) -> str:
    if (
        not name
        or len(name) > 255
        or "/" in name
        or "\\" in name
        or any(ord(c) < 32 for c in name)
        or ":" in name
    ):
        raise FileRejected("FILE_NAME")
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    mime = MIMES.get(extension)
    if mime is None:
        raise FileRejected("FILE_EXTENSION")
    if claimed_mime != mime:
        raise FileRejected("FILE_MIME")
    if data.startswith(MAGIC) or data[257:262] == b"ustar":
        raise FileRejected("FILE_FORMAT")
    if extension != "pdf" and data.lstrip().startswith(b"%PDF-"):
        raise FileRejected("FILE_FORMAT")
    if extension == "pdf":
        if not re.match(rb"%PDF-(?:1\.[0-7]|2\.0)[\r\n]", data):
            raise FileRejected("PDF_SIGNATURE")
        if not data.rstrip().endswith(b"%%EOF") or b"startxref" not in data:
            raise FileRejected("PDF_STRUCTURE")
        descriptor = sealed_snapshot(data)
        try:
            run_parser(descriptor, "pdf")
        except FileRejected:
            raise FileRejected("PDF_STRUCTURE") from None
        finally:
            os.close(descriptor)
        return mime
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        raise FileRejected("TEXT_ENCODING") from None
    if any(ord(c) < 32 and c not in "\n\r\t" for c in text) or "\x7f" in text:
        raise FileRejected("TEXT_BINARY")
    if HTML.search(text) or EXECUTABLE.search(text):
        raise FileRejected("TEXT_ACTIVE")
    if re.search(r"(?:javascript|vbscript|data)\s*:", text, re.I):
        raise FileRejected("TEXT_ACTIVE")
    return mime
