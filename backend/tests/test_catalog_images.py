"""Synthetic images, real sealed descriptors, Pillow sandbox and local ClamAV."""

import hashlib
import io
import os
import secrets
import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin
from pytest import MonkeyPatch
from test_files_acceptance import INFECTED, SyntheticClamAV

from app.modules.catalog.images import pipeline as image_module
from app.modules.catalog.images.pipeline import IMAGE_LIMITS, MAX_BYTES, ImagePipeline
from app.modules.kb.files.process import FileRejected
from app.modules.kb.files.sandbox import run_parser, sealed_snapshot
from app.modules.kb.files.scanner import ClamAV

pytestmark = pytest.mark.file_acceptance


def picture(fmt: str = "PNG", *, color: str = "red", metadata: bool = False) -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGB", (32, 16), color)
    if metadata and fmt == "JPEG":
        exif = Image.Exif()
        exif[274] = 6
        exif[34853] = {1: "N", 2: (1, 2, 3)}
        exif[270] = "SYNTHETIC SECRET EXIF"
        image.save(
            buffer,
            format=fmt,
            exif=exif,
            comment=b"SYNTHETIC COMMENT",
            icc_profile=b"SYNTHETIC ICC",
            xmp=b"SYNTHETIC XMP",
        )
    elif metadata:
        info = PngImagePlugin.PngInfo()
        info.add_text("secret", "SYNTHETIC SECRET PNG")
        info.add_itxt("XML:com.adobe.xmp", "SYNTHETIC XMP")
        image.save(buffer, format=fmt, pnginfo=info)
    else:
        image.save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture
def images(tmp_path: Path) -> ImagePipeline:
    database = tmp_path / "signatures"
    database.mkdir()
    (database / "test.ndb").write_text(f"MVP025.Safe.Test:0:*:{INFECTED.hex()}\n")
    root = tmp_path / "files"
    root.mkdir(mode=0o700)
    return ImagePipeline(root, _scanner=SyntheticClamAV(database))


@pytest.mark.parametrize("fmt,mime", [("JPEG", "image/jpeg"), ("PNG", "image/png")])
def test_double_scan_metadata_and_full_decode(
    images: ImagePipeline, fmt: str, mime: str, monkeypatch: MonkeyPatch
) -> None:
    data = picture(fmt, metadata=True)
    observed: list[bytes] = []
    scan = images.scanner.scan

    def capture(descriptor: int):  # type: ignore[no-untyped-def]
        os.lseek(descriptor, 0, os.SEEK_SET)
        observed.append(os.read(descriptor, MAX_BYTES + 1))
        with pytest.raises(OSError):
            os.write(descriptor, b"changed")
        return scan(descriptor)

    monkeypatch.setattr(images.scanner, "scan", capture)
    accepted = images.accept(
        io.BytesIO(data), filename="client." + fmt.lower(), mime=mime
    )
    output = (images.root / "images/verified" / accepted.image_key).read_bytes()
    assert observed == [data, output]
    assert accepted.checksum_sha256 == hashlib.sha256(output).hexdigest()
    assert accepted.image_key.endswith(".jpg" if fmt == "JPEG" else ".png")
    assert accepted.original_scan.database_version == "synthetic-only"
    assert accepted.output_scan.database_version == "synthetic-only"
    assert b"SYNTHETIC" not in output
    with Image.open(io.BytesIO(output)) as result:
        result.load()
        assert not result.getexif()
        assert result.size == ((16, 32) if fmt == "JPEG" else (32, 16))
        assert set(result.info) <= {"jfif", "jfif_version", "jfif_unit", "jfif_density"}
        pixel = result.convert("RGB").getpixel((0, 0))
        assert isinstance(pixel, tuple) and pixel[0] > 240 and pixel[1] < 10
    with images.prepare(accepted.image_key) as (metadata, descriptor):
        assert metadata == accepted
        run_parser(descriptor, "image-check", limits=IMAGE_LIMITS)
    with pytest.raises(OSError):
        os.fstat(descriptor)


@pytest.mark.parametrize("mode", ["RGBA", "P", "L", "LA", "RGB"])
def test_png_modes_and_transparency(images: ImagePipeline, mode: str) -> None:
    image = Image.new("RGBA", (12, 10), (255, 0, 0, 0)).convert(mode)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    accepted = images.accept(
        io.BytesIO(buffer.getvalue()), filename="a.png", mime="image/png"
    )
    output = (images.root / "images/verified" / accepted.image_key).read_bytes()
    with Image.open(io.BytesIO(output)) as result:
        result.load()
        assert result.mode == "RGBA"
        if mode in {"RGBA", "LA", "P"}:
            pixel = result.getpixel((0, 0))
            assert isinstance(pixel, tuple) and pixel[3] == 0


@pytest.mark.parametrize("fmt", ["GIF", "WEBP", "TIFF", "BMP", "PDF"])
def test_other_formats_rejected(images: ImagePipeline, fmt: str) -> None:
    data = picture(fmt)
    for filename, mime in [
        ("a." + fmt.lower(), "image/" + fmt.lower()),
        ("a.png", "image/png"),
    ]:
        with pytest.raises(FileRejected):
            images.accept(io.BytesIO(data), filename=filename, mime=mime)


@pytest.mark.parametrize(
    "data,filename,mime",
    [
        (b"<svg/>", "a.svg", "image/svg+xml"),
        (b"MZ executable", "a.jpg", "image/jpeg"),
        (b"PK\x03\x04archive", "a.png", "image/png"),
        (picture(), "a.jpg", "image/png"),
        (picture(), "a.png", "image/jpeg"),
        (picture(), "a.jpg", "image/jpeg"),
        (picture(), "../a.png", "image/png"),
        (picture(), "https://example.invalid/a.png", "image/png"),
        (picture(), "a.png", "IMAGE/PNG"),
        (b"", "a.png", "image/png"),
        (b"x" * (MAX_BYTES + 1), "a.png", "image/png"),
    ],
)
def test_untrusted_format_size_and_name(
    images: ImagePipeline, data: bytes, filename: str, mime: str
) -> None:
    with pytest.raises(FileRejected):
        images.accept(io.BytesIO(data), filename=filename, mime=mime)
    assert not list((images.root / "images/verified").glob("*"))


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_truncated_and_corrupt(images: ImagePipeline, fmt: str) -> None:
    data = picture(fmt)
    for broken in (
        data[: len(data) // 2],
        data[:-12],
        data[:30] + b"broken" + data[38:],
    ):
        with pytest.raises(FileRejected):
            images.accept(
                io.BytesIO(broken),
                filename="a." + fmt.lower(),
                mime="image/" + ("jpeg" if fmt == "JPEG" else "png"),
            )


def png_dimensions(width: int, height: int) -> bytes:
    data = bytearray(picture())
    data[16:24] = struct.pack(">II", width, height)
    data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))
    return bytes(data)


@pytest.mark.parametrize(
    "width,height", [(4097, 1), (1, 4097), (4001, 4000), (100000, 100000)]
)
def test_dimensions_bomb_before_allocation(
    images: ImagePipeline, width: int, height: int
) -> None:
    with pytest.raises(FileRejected):
        images.accept(
            io.BytesIO(png_dimensions(width, height)),
            filename="a.png",
            mime="image/png",
        )


@pytest.mark.parametrize("frames", [1, 2])
def test_animated_png(images: ImagePipeline, frames: int) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(
        buffer,
        format="PNG",
        save_all=True,
        append_images=[Image.new("RGB", (10, 10), "blue")] if frames == 2 else [],
    )
    data = buffer.getvalue()
    if frames == 1:
        # Pillow optimizes save_all with one frame to ordinary PNG. Construct a
        # valid one-frame APNG: it still has an animation container, even though
        # Pillow reports is_animated=False and n_frames=1.
        def chunk(name: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + name
                + payload
                + struct.pack(">I", zlib.crc32(name + payload))
            )

        control = chunk(b"acTL", struct.pack(">II", 1, 0))
        frame = chunk(b"fcTL", struct.pack(">IIIIIHHBB", 0, 10, 10, 0, 0, 1, 10, 0, 0))
        data = data[:33] + control + frame + data[33:]
    with Image.open(io.BytesIO(data)) as source:
        assert (
            getattr(source, "n_frames", 1) == frames
            and source.custom_mimetype == "image/apng"
        )
    with pytest.raises(FileRejected):
        images.accept(io.BytesIO(data), filename="a.png", mime="image/png")


def test_infected_original_and_unsigned_stale_missing(images: ImagePipeline) -> None:
    with pytest.raises(FileRejected, match="SCANNER_INFECTED"):
        images.accept(
            io.BytesIO(picture() + INFECTED), filename="a.png", mime="image/png"
        )
    with pytest.raises(FileRejected):
        ClamAV(images.scanner.database)._database()
    with pytest.raises(FileRejected):
        ClamAV(images.root / "missing").scan(0)
    os.utime(images.scanner.database / "test.ndb", (1, 1))
    with pytest.raises(FileRejected, match="SIGNATURES_STALE"):
        images.accept(io.BytesIO(picture()), filename="a.png", mime="image/png")


def test_second_scan_failure_and_encoder_failure(
    images: ImagePipeline, monkeypatch: MonkeyPatch
) -> None:
    scan = images.scanner.scan
    calls = 0

    def fault(descriptor: int):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 2:
            raise FileRejected("SCANNER_FAILURE")
        return scan(descriptor)

    monkeypatch.setattr(images.scanner, "scan", fault)
    with pytest.raises(FileRejected, match="SCANNER_FAILURE"):
        images.accept(io.BytesIO(picture()), filename="a.png", mime="image/png")
    assert calls == 2
    assert not list((images.root / "images/verified").iterdir())
    monkeypatch.setattr(images.scanner, "scan", scan)

    def fail(*args: object, **kwargs: object) -> bytes:
        raise FileRejected("PARSER_REJECTED")

    monkeypatch.setattr(image_module, "run_parser", fail)
    with pytest.raises(FileRejected, match="PARSER_REJECTED"):
        images.accept(io.BytesIO(picture()), filename="a.png", mime="image/png")
    assert not list((images.root / "images/verified").iterdir())


@pytest.mark.parametrize(
    "attack", ["symlink", "hardlink", "changed", "receipt", "mode"]
)
def test_storage_attacks(images: ImagePipeline, attack: str) -> None:
    accepted = images.accept(io.BytesIO(picture()), filename="a.png", mime="image/png")
    path = images.root / "images/verified" / accepted.image_key
    if attack == "symlink":
        path.unlink()
        path.symlink_to("/etc/passwd")
    elif attack == "hardlink":
        os.link(path, path.with_name("hardlink"))
    elif attack in {"changed", "mode"}:
        path.chmod(0o600)
        if attack == "changed":
            path.write_bytes(picture(color="blue"))
            path.chmod(0o400)
    else:
        path = images.root / "images/receipts" / accepted.image_key
        path.chmod(0o600)
        path.write_text("{}")
        path.chmod(0o400)
    with pytest.raises(FileRejected):
        with images.prepare(accepted.image_key):
            pytest.fail("attacked storage must never yield")


def test_snapshot_exit_integrity_and_exclusive_keys(
    images: ImagePipeline, monkeypatch: MonkeyPatch
) -> None:
    accepted = images.accept(io.BytesIO(picture()), filename="a.png", mime="image/png")
    original = (images.root / "images/verified" / accepted.image_key).read_bytes()
    with pytest.raises(FileRejected):
        with images.prepare(accepted.image_key) as (_, descriptor):
            path = images.root / "images/verified" / accepted.image_key
            path.chmod(0o600)
            path.write_bytes(picture(color="blue"))
            path.chmod(0o400)
            os.lseek(descriptor, 0, os.SEEK_SET)
            assert os.read(descriptor, MAX_BYTES) == original
    monkeypatch.setattr(secrets, "token_hex", lambda _: accepted.image_key[:64])
    with pytest.raises(FileRejected):
        images.accept(io.BytesIO(picture()), filename="a.png", mime="image/png")


@pytest.mark.parametrize(
    "key",
    ["../a.png", "/etc/passwd", "a" * 64, "a" * 64 + ".gif", "a" * 64 + ".png/.."],
)
def test_invalid_key(images: ImagePipeline, key: str) -> None:
    with pytest.raises(FileRejected):
        with images.prepare(key):
            pytest.fail("untrusted key")


def test_sandbox_uses_existing_kernel_boundary() -> None:
    fd = sealed_snapshot(picture())
    try:
        run_parser(fd, "image-check", limits=IMAGE_LIMITS)
        import json

        result = json.loads(run_parser(fd, "probe"))
        assert result["uid"] != 0
        assert all(
            result[name] != 0
            for name in ("filesystem", "network", "fork", "input_write")
        )
    finally:
        os.close(fd)


@pytest.mark.parametrize("width,height", [(4096, 1), (4000, 4000)])
def test_real_dimension_boundaries(
    images: ImagePipeline, width: int, height: int
) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format="PNG")
    accepted = images.accept(
        io.BytesIO(buffer.getvalue()), filename="a.png", mime="image/png"
    )
    output = (images.root / "images/verified" / accepted.image_key).read_bytes()
    with Image.open(io.BytesIO(output)) as result:
        result.load()
        assert result.size == (width, height)


def test_output_limit_and_declared_length(
    images: ImagePipeline, monkeypatch: MonkeyPatch
) -> None:
    for declared in (-1, 0, MAX_BYTES + 1, len(picture()) + 1):
        with pytest.raises(FileRejected, match="FILE_SIZE"):
            images.accept(
                io.BytesIO(picture()),
                filename="a.png",
                mime="image/png",
                declared_size=declared,
            )

    def oversized(fd: int, task: str, *, limits: object) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + b"x" * MAX_BYTES

    monkeypatch.setattr(image_module, "run_parser", oversized)
    with pytest.raises(FileRejected, match="IMAGE_OUTPUT"):
        images.accept(io.BytesIO(picture()), filename="a.png", mime="image/png")
    assert not list((images.root / "images/verified").iterdir())


def test_real_clamav_rejects_infected_reencoded_output(images: ImagePipeline) -> None:
    # Derive a synthetic signature present only in the canonical RGBA encoding.
    # This proves the second real scan sees output bytes, not the input again.
    data = picture()
    descriptor = sealed_snapshot(data)
    try:
        output = run_parser(descriptor, "image", limits=IMAGE_LIMITS)
    finally:
        os.close(descriptor)
    needle = next(
        output[i : i + 16]
        for i in range(len(output) - 16)
        if output[i : i + 16] not in data
    )
    (images.scanner.database / "test.ndb").write_text(
        f"MVP025.Output.Test:0:*:{needle.hex()}\n"
    )
    with pytest.raises(FileRejected, match="SCANNER_INFECTED"):
        images.accept(io.BytesIO(data), filename="a.png", mime="image/png")
    assert not list((images.root / "images/verified").iterdir())
