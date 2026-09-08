"""Tests for the signature carver.

The media under ``tests/fixtures/media`` was written by real encoders through
``tools/make_fixtures.py``. The tests here read those committed bytes and build
disk-like images around them with nothing but the standard library, so the suite
needs only pytest even though the fixtures came from Pillow and OpenCV.

Three kinds of test, and they answer different questions:

* recovery: given media at a known offset, is the extent exactly right
* rejection: the false positives measured on a real Windows drive stay rejected
* rate: how often ordinary non-media data produces a hit, per data shape, since
  a carver's false-positive rate is a property of the data as much as of the
  carver
"""

import base64
import hashlib
import io
import json
import os
import random
import struct
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mediacarve  # noqa: E402

MEDIA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "media")
MANIFEST = os.path.join(MEDIA, "manifest.json")

pytestmark = pytest.mark.skipif(
    not os.path.exists(MANIFEST),
    reason="media fixtures absent; run tools/make_fixtures.py tests/fixtures/media")


def _manifest():
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)["media"]


def _load(name):
    with open(os.path.join(MEDIA, name), "rb") as fh:
        return fh.read()


def _fixtures():
    return [(m["name"], m) for m in _manifest()]


def _image(pieces, size=None, filler=b"\x00", seed=11):
    """A disk-like image: filler with each (offset, bytes) laid into it."""
    if size is None:
        size = max(off + len(data) for off, data in pieces) + 4096
    blob = bytearray(filler * (size // len(filler) + 1))[:size]
    for off, data in pieces:
        blob[off:off + len(data)] = data
    return io.BytesIO(bytes(blob))


# ------------------------------------------------------------------ recovery

@pytest.mark.parametrize("name,meta", _fixtures())
def test_each_fixture_is_recovered_exactly_on_its_own(name, meta):
    data = _load(name)
    with open(os.path.join(MEDIA, name), "rb") as fh:
        hits = list(mediacarve.carve(fh))
    assert len(hits) == 1, f"{name}: {[(h.kind, h.offset, h.length) for h in hits]}"
    hit = hits[0]
    assert (hit.offset, hit.length, hit.kind) == (0, meta["length"], meta["kind"])


@pytest.mark.parametrize("name,meta", _fixtures())
def test_each_fixture_is_recovered_from_inside_a_larger_image(name, meta):
    data = _load(name)
    offset = 8192
    stream = _image([(offset, data)])
    hits = list(mediacarve.carve(stream))
    assert len(hits) == 1, name
    hit = hits[0]
    assert (hit.offset, hit.length) == (offset, meta["length"])
    stream.seek(hit.offset)
    assert hashlib.sha256(stream.read(hit.length)).hexdigest() == meta["sha256"]


def test_several_files_in_one_image_all_come_back_byte_for_byte():
    media = _manifest()
    pieces, expected, offset = [], [], 4096
    rnd = random.Random(3)
    for m in media:
        data = _load(m["name"])
        pieces.append((offset, data))
        expected.append((offset, m["length"], m["kind"], m["sha256"]))
        offset += len(data) + rnd.randrange(1000, 9000)
    stream = _image(pieces)
    hits = list(mediacarve.carve(stream))
    assert len(hits) == len(expected)
    for hit, (off, length, kind, sha) in zip(hits, expected):
        assert (hit.offset, hit.length, hit.kind) == (off, length, kind)
        stream.seek(hit.offset)
        assert hashlib.sha256(stream.read(hit.length)).hexdigest() == sha


def test_an_exif_thumbnail_is_not_a_separate_file_by_default():
    """The thumbnail is a whole JPEG inside APP1, stepped over by its length."""
    meta = next(m for m in _manifest() if m["name"] == "thumbed.jpg")
    data = _load("thumbed.jpg")
    stream = _image([(2048, data)])
    plain = list(mediacarve.carve(stream))
    assert len(plain) == 1
    assert plain[0].length == meta["length"], "the thumbnail truncated the file"
    nested = list(mediacarve.carve(stream, nested=True))
    assert len(nested) == 2, "the embedded thumbnail should be findable on request"


def test_kinds_filter_selects_only_what_was_asked_for():
    pieces = [(4096, _load("plain.jpg")), (32768, _load("plain.png"))]
    stream = _image(pieces)
    assert {h.kind for h in mediacarve.carve(stream)} == {"jpeg", "png"}
    assert {h.kind for h in mediacarve.carve(stream, kinds={"png"})} == {"png"}


# ----------------------------------------------------------------- rejection

def test_a_four_byte_jpeg_is_not_a_jpeg():
    """Measured on a real Windows drive: ffd8ffd9 in ordinary data parsed as a
    complete four-byte JPEG before a scan segment was required."""
    stream = _image([(4096, b"\xff\xd8\xff\xd9")])
    assert list(mediacarve.carve(stream)) == []


def test_a_long_jpeg_with_no_scan_is_still_rejected():
    """Isolates the scan requirement from the length floor.

    The four-byte case above is caught by either guard, so neither is proven by
    it. This one is padded well past the floor and carries a real segment, so
    only the missing scan can reject it.
    """
    app0 = b"\xff\xe0" + struct.pack(">H", 500) + b"\x00" * 498
    stream = _image([(4096, b"\xff\xd8" + app0 + b"\xff\xd9")])
    assert list(mediacarve.carve(stream)) == []


def test_a_scan_bearing_jpeg_below_the_floor_is_rejected():
    """Isolates the length floor: this one does have a scan segment, so only the
    floor stands between it and being reported as a fourteen-byte JPEG."""
    sos = b"\xff\xda" + struct.pack(">H", 8) + b"\x00" * 6
    stream = _image([(4096, b"\xff\xd8" + sos + b"\xff\xd9")])
    assert list(mediacarve.carve(stream)) == []


@pytest.mark.parametrize("text,note", [
    (b"\x00href", "hreftyp in a web resource, size 6.8 MB"),
    (b"er I", "er Iftyp, size 1.7 GB"),
])
def test_text_containing_ftyp_is_not_a_video(text, note):
    """A bare ftyp is four bytes and turns up inside text. The preceding bytes
    then parse as a box size, which is where those absurd lengths came from."""
    stream = _image([(4096, text + b"ftypmp42" + b"x" * 512)])
    assert list(mediacarve.carve(stream)) == [], note


def test_a_false_positive_must_not_shadow_the_files_behind_it():
    """The defect that mattered most: a bogus extent claiming 1.7 GB suppressed
    every genuine file after it, so a real scan reported 4 hits instead of 2002."""
    jpg, png = _load("plain.jpg"), _load("plain.png")
    stream = _image([
        (4096, b"er I" + b"ftypmp42" + b"x" * 256),     # would have claimed ~1.7 GB
        (16384, jpg),
        (65536, png),
    ])
    hits = list(mediacarve.carve(stream))
    assert [h.kind for h in hits] == ["jpeg", "png"]
    assert [h.offset for h in hits] == [16384, 65536]


def test_an_ftyp_box_alone_is_not_a_file():
    """A real ISO-BMFF file has at least one box after ftyp."""
    only_ftyp = (b"\x00\x00\x00\x18" b"ftyp" b"mp42" b"\x00\x00\x00\x00"
                 b"mp42isom")
    stream = _image([(4096, only_ftyp)])
    assert [h for h in mediacarve.carve(stream) if h.kind in ("mp4", "heic")] == []


# ---------------------------------------------------------------------- rate

def _prose(n, seed=1):
    words = ("the quick brown fox jumps over a lazy dog while href type ftyp riff "
             "gif png data image video system windows program files user local "
             "application settings temp cache index content type href=").split()
    rnd = random.Random(seed)
    out, total = [], 0
    while total < n:                    # keep a running length: re-summing the
        word = rnd.choice(words)        # list each pass is quadratic and cost
        out.append(word)                # 458s to build one mebibyte
        total += len(word) + 1
    return (" ".join(out)).encode()[:n]


def _shapes(size=1 << 20):
    rnd = random.Random(9)
    prose = _prose(size)
    return {
        "zeros": bytes(size),
        "ones": b"\xff" * size,
        "random": rnd.randbytes(size),
        "prose": prose,
        "json": json.dumps([{"href": "x", "type": "image/gif", "n": i}
                            for i in range(size // 40)]).encode()[:size],
        "base64": base64.b64encode(rnd.randbytes(size))[:size],
        "hex": rnd.randbytes(size // 2).hex().encode()[:size],
        "utf16": prose.decode("latin-1").encode("utf-16-le")[:size],
    }


def test_ordinary_data_does_not_carve_as_media():
    """A carver's false-positive rate depends on the data's own alphabet, so
    each shape is measured separately. One clean control would prove little."""
    rates = {}
    for name, blob in _shapes().items():
        hits = list(mediacarve.carve(io.BytesIO(blob)))
        rates[name] = len(hits)
    assert all(v == 0 for v in rates.values()), f"false positives: {rates}"


def test_the_rate_control_can_actually_fire():
    """A zero that was never shown able to be non-zero measures nothing."""
    blob = bytearray(_shapes(1 << 16)["prose"])
    jpg = _load("plain.jpg")
    blob[4096:4096 + len(jpg)] = jpg
    hits = list(mediacarve.carve(io.BytesIO(bytes(blob))))
    assert len(hits) == 1 and hits[0].kind == "jpeg"


# ----------------------------------------------------------------- extraction

def test_extract_writes_every_candidate_to_a_folder(tmp_path):
    pieces = [(4096, _load("plain.jpg")), (32768, _load("plain.png"))]
    stream = _image(pieces)
    hits = list(mediacarve.carve(stream))
    written = mediacarve.extract(stream, hits, str(tmp_path))
    assert written == 2
    names = sorted(os.listdir(tmp_path))
    assert len(names) == 2
    got = {open(tmp_path / n, "rb").read() for n in names}
    assert got == {_load("plain.jpg"), _load("plain.png")}


def test_extract_writes_a_zip_for_a_zip_seeking_consumer(tmp_path):
    """VLEAPP stages a zip and reuses its zip seeker, so this is its shape."""
    stream = _image([(4096, _load("plain.jpg"))])
    dest = tmp_path / "carved.zip"
    hits = list(mediacarve.carve(stream))
    assert mediacarve.extract(stream, hits, str(dest), zip_output=True) == 1
    with zipfile.ZipFile(dest) as zf:
        assert len(zf.namelist()) == 1
        assert zf.read(zf.namelist()[0]) == _load("plain.jpg")


def test_the_carved_name_records_where_it_came_from():
    hit = mediacarve.Candidate(0x1234, 10, "jpeg", ".jpg", "parsed")
    assert "0000000000001234" in mediacarve._name_for(hit, 1)


# ----------------------------------------------------------------------- CLI

def test_cli_list_counts_what_is_recoverable(tmp_path, capsys):
    raw = tmp_path / "disk.raw"
    stream = _image([(4096, _load("plain.jpg")), (32768, _load("plain.png"))])
    raw.write_bytes(stream.getvalue())
    assert mediacarve.main(["list", str(raw), "-q"]) == 0
    out = capsys.readouterr().out
    assert "jpeg" in out and "png" in out and "total" in out


def test_cli_carve_writes_the_files(tmp_path):
    raw = tmp_path / "disk.raw"
    stream = _image([(4096, _load("plain.jpg"))])
    raw.write_bytes(stream.getvalue())
    out = tmp_path / "carved"
    assert mediacarve.main(["carve", str(raw), "-o", str(out), "-q"]) == 0
    assert [p for p in os.listdir(out)]


def test_cli_carve_to_zip(tmp_path):
    raw = tmp_path / "disk.raw"
    stream = _image([(4096, _load("plain.png"))])
    raw.write_bytes(stream.getvalue())
    dest = tmp_path / "carved.zip"
    assert mediacarve.main(["carve", str(raw), "-o", str(dest), "-q"]) == 0
    with zipfile.ZipFile(dest) as zf:
        assert zf.read(zf.namelist()[0]) == _load("plain.png")


def test_an_e01_without_ewfprobe_says_so(tmp_path, monkeypatch):
    """Reading an EWF container as raw bytes would find nothing and look like an
    empty image, so it refuses instead."""
    fake = tmp_path / "evidence.E01"
    fake.write_bytes(b"EVF\x09\x0d\x0a\xff\x00" + b"\x00" * 4096)
    monkeypatch.setitem(sys.modules, "ewfprobe", None)
    with pytest.raises(SystemExit) as exc:
        mediacarve.open_source(str(fake))
    assert "ewfprobe" in str(exc.value)
