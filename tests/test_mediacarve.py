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
    assert (hit.offset, hit.length, hit.kind, hit.ext) \
        == (0, meta["length"], meta["kind"], meta["ext"])


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


def test_the_mp4_kind_still_selects_every_video_brand():
    """Only the extension follows the brand, so "-k mp4" keeps meaning what it
    meant, while M4A audio, which used to come out as mp4, is now its own kind."""
    names = ("plain.mp4", "plain.mov", "plain.3gp", "plain.m4a")
    pieces, at = [], 4096
    for name in names:
        pieces.append((at, _load(name)))
        at += len(pieces[-1][1]) + 4096
    stream = _image(pieces)
    assert [h.ext for h in mediacarve.carve(stream, kinds={"mp4"})] == [".mp4", ".mov", ".3gp"]
    assert [h.ext for h in mediacarve.carve(stream, kinds=mediacarve._kinds_arg("audio"))] \
        == [".m4a"]


def _ftyp(major, *compatible):
    body = major + b"\x00\x00\x00\x00" + b"".join(compatible)
    return struct.pack(">I", 8 + len(body)) + b"ftyp" + body


@pytest.mark.parametrize("major,compatible,kind,ext", [
    (b"isom", (b"isom", b"avc1"), "mp4", ".mp4"),
    (b"qt  ", (b"qt  ",), "mp4", ".mov"),
    (b"M4V ", (b"M4V ", b"mp42"), "mp4", ".m4v"),
    (b"3gp5", (b"3gp5", b"isom"), "mp4", ".3gp"),
    (b"3gg6", (), "mp4", ".3gp"),
    (b"3g2a", (b"3g2a",), "mp4", ".3g2"),
    (b"M4A ", (b"M4A ", b"mp42"), "m4a", ".m4a"),
    (b"M4B ", (b"M4B ",), "m4a", ".m4b"),
    (b"M4P ", (), "m4a", ".m4p"),
    (b"heic", (b"mif1", b"heic"), "heic", ".heic"),
    (b"mif1", (b"mif1", b"heic"), "heic", ".heic"),
    (b"mif1", (b"mif1", b"avif"), "heic", ".avif"),
    (b"avis", (b"avis", b"msf1"), "heic", ".avif"),
    (b"mif1", (b"mif1",), "heic", ".heif"),
])
def test_the_brand_names_the_extension(major, compatible, kind, ext):
    """Brands as the MP4 registration authority lists them. A generic HEIF brand
    says nothing about the codec, so its compatible brands decide."""
    clip = _ftyp(major, *compatible) + _box(b"meta", b"m" * 600) + _box(b"mdat", b"d" * 3000)
    stream = _image([(4096, clip)])
    assert [(h.offset, h.length, h.kind, h.ext) for h in mediacarve.carve(stream)] \
        == [(4096, len(clip), kind, ext)]


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
    """Isolates the length floor: this one has a frame header and a well-formed
    scan, so only the floor stands between it and a 27-byte JPEG."""
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, 1, 1, 1) + b"\x01\x11\x00"
    sos = b"\xff\xda" + struct.pack(">HB", 8, 1) + b"\x01\x00\x00\x3f\x00"
    stream = _image([(4096, b"\xff\xd8" + sof + sos + b"\xff\xd9")])
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


def _box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def test_a_vendor_box_after_ftyp_does_not_end_the_file():
    """Writers add top-level boxes of their own. Four screen recordings on one
    Windows volume each carried a "beam" box between ftyp and moov, and the
    deleted one, intact in free space, was the only recoverable file there.
    The walk used to stop at the first box it did not know."""
    clip = (_box(b"ftyp", b"mp42\x00\x00\x00\x00mp42isom") + _box(b"beam", b"\x00" * 16)
            + _box(b"moov", b"m" * 200) + _box(b"mdat", b"d" * 3000))
    stream = _image([(4096, clip)])
    hits = [h for h in mediacarve.carve(stream) if h.kind == "mp4"]
    assert [(h.offset, h.length) for h in hits] == [(4096, len(clip))]


def test_random_bytes_after_a_file_are_not_a_vendor_box():
    """Measured on real voice notes laid into random bytes: after an MP4 ended,
    "sIs}" with the four bytes before it read as a 4,265,331,032 byte vendor box,
    and the extent that gave hid 106 files behind it. A vendor box has to be
    followed by another box, or by the end of the stream."""
    clip = (_box(b"ftyp", b"mp42\x00\x00\x00\x00mp42isom") + _box(b"moov", b"m" * 200)
            + _box(b"mdat", b"d" * 3000))
    fake = struct.pack(">I", 4265331032) + b"sIs}" + random.Random(2).randbytes(64)
    jpg = _load("plain.jpg")
    stream = _image([(4096, clip + fake), (65536, jpg)], size=1 << 20)
    assert [(h.offset, h.length, h.kind) for h in mediacarve.carve(stream)] \
        == [(4096, len(clip), "mp4"), (65536, len(jpg), "jpeg")]


def test_text_after_a_file_is_not_a_vendor_box():
    """Measured on a macOS volume: after a real uuid box, four spaces sized
    0x20202020 passed for a vendor box, because the text after them also had the
    shape of a box header. The extent hid 179 files. A vendor box has to be
    followed by a box the carver knows."""
    clip = (_box(b"ftyp", b"mp42\x00\x00\x00\x00mp42isom") + _box(b"moov", b"m" * 200)
            + _box(b"uuid", b"u" * 64) + _box(b"mdat", b"d" * 3000))
    # a "box" of four spaces, then text with the shape of a box header where
    # that box would end: both fit in the image, so only the rule can say no
    text = (struct.pack(">I", 256) + b"    " + b"t" * 248
            + struct.pack(">I", 64) + b"lass" + b"t" * 600)
    png = _load("plain.png")
    stream = _image([(4096, clip + text), (65536, png)], size=1 << 20)
    assert [(h.offset, h.length, h.kind) for h in mediacarve.carve(stream)] \
        == [(4096, len(clip), "mp4"), (65536, len(png), "png")]


def test_ftyp_followed_only_by_unknown_boxes_is_still_not_a_file():
    """Skipping vendor boxes must not let ftyp plus nothing it recognises through:
    the file still has to show a box the carver knows."""
    clip = _box(b"ftyp", b"mp42\x00\x00\x00\x00mp42isom") + _box(b"beam", b"\x00" * 16) \
        + _box(b"zzzz", b"\x00" * 40)
    stream = _image([(4096, clip)])
    assert [h for h in mediacarve.carve(stream) if h.kind in ("mp4", "heic")] == []


def test_bytes_that_are_not_a_box_still_end_the_file():
    """A real box type is four printable characters. Data that is not ends the
    walk exactly as before, so a file cannot run on into what follows it."""
    clip = (_box(b"ftyp", b"mp42\x00\x00\x00\x00mp42isom") + _box(b"moov", b"m" * 200)
            + _box(b"mdat", b"d" * 3000))
    junk = b"\x00\x00\x10\x00" + b"\x01\x02\x03\x04" + b"j" * 4096   # sane size, not a type
    stream = _image([(4096, clip + junk)])
    hits = [h for h in mediacarve.carve(stream) if h.kind == "mp4"]
    assert [(h.offset, h.length) for h in hits] == [(4096, len(clip))]



# ----------------------------------------------------------------------- png

def _chunk(ctype: bytes, data: bytes = b"") -> bytes:
    import zlib
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data))


def _tiny_png(iend: bytes = None) -> bytes:
    """A valid 1x1 grey PNG, 67 bytes, built to the specification."""
    import zlib
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(b"\x00\x80")) + (iend or _chunk(b"IEND")))


def test_a_png_under_the_old_floor_is_recovered():
    """Measured on a Windows volume: 38 real PNGs of 68 to 127 bytes were under
    the 128-byte floor. The CRCs are what make a floor that low safe."""
    png = _tiny_png()
    assert len(png) < 128
    stream = _image([(4096, png)])
    assert [(h.offset, h.length, h.kind) for h in mediacarve.carve(stream)] \
        == [(4096, len(png), "png")]


def test_an_iend_that_declares_data_does_not_swallow_what_follows():
    """Measured on a Windows volume: two small PNGs whose IEND declared 150,994,944
    and 589,824 bytes ran on and hid 14 PNGs. IEND's data field is empty."""
    bad = _tiny_png(iend=struct.pack(">I", 589_824) + b"IEND" + b"\xaeB`\x82")
    real = _load("plain.png")
    stream = _image([(4096, bad), (8192, real)], size=1 << 20)
    assert [(h.offset, h.length) for h in mediacarve.carve(stream)] == [(8192, len(real))]
    # an IEND carrying data under a good CRC: only the empty-IEND rule rejects it
    data_iend = _tiny_png(iend=_chunk(b"IEND", b"tail"))
    stream = _image([(4096, data_iend), (8192, real)], size=1 << 20)
    assert [(h.offset, h.length) for h in mediacarve.carve(stream)] == [(8192, len(real))]


def test_a_bad_ihdr_or_iend_crc_rejects_the_file():
    """IHDR's CRC is what makes a false start unlikely, and IEND's what makes the
    end a fact."""
    png = bytearray(_tiny_png())
    bad_ihdr = bytes(png[:20]) + bytes([png[20] ^ 1]) + bytes(png[21:])
    bad_iend = bytes(png[:-1]) + bytes([png[-1] ^ 1])
    for bad in (bad_ihdr, bad_iend):
        assert list(mediacarve.carve(_image([(4096, bad)]))) == []


def test_a_bad_crc_on_any_critical_chunk_rejects_the_file():
    """Measured on a Windows volume: checking only IHDR and IEND let in 800 more
    PNG-shaped hits that neither Pillow nor FFmpeg decodes, each with a failing
    CRC on a critical chunk between them."""
    data = bytearray(_load("plain.png"))
    data[60] ^= 0x01                              # inside IDAT's data
    assert list(mediacarve.carve(_image([(4096, bytes(data))]))) == []


def test_an_ancillary_chunk_with_a_bad_crc_does_not_reject_the_file():
    """Measured on 38,567 real PNGs: the 16 a CRC check on every chunk lost all
    failed on fcTL, an animation chunk, and most such files decode. Only critical
    chunks, whose type starts with an uppercase letter, have their CRC checked."""
    png = _tiny_png()
    text = _chunk(b"tEXt", b"Comment\x00hello")
    broken = text[:-4] + bytes(4)                 # same chunk, CRC zeroed
    clip = png[:33] + broken + png[33:]
    stream = _image([(4096, clip)])
    assert [(h.offset, h.length) for h in mediacarve.carve(stream)] == [(4096, len(clip))]


def test_ihdr_comes_first_an_idat_is_required_and_cgbi_may_lead():
    import zlib
    png = _tiny_png()
    cgbi = png[:8] + _chunk(b"CgBI", b"\x50\x00\x20\x02") + png[8:]
    ihdr = png[8:33]
    idat = _chunk(b"IDAT", zlib.compress(random.Random(3).randbytes(400)))
    # long enough to clear the floor, so only the order rules can reject them
    no_ihdr = png[:8] + idat + _chunk(b"IEND")
    no_idat = png[:8] + ihdr + _chunk(b"tEXt", b"Comment\x00" + b"x" * 200) + _chunk(b"IEND")
    stream = _image([(4096, cgbi), (8192, no_ihdr), (16384, no_idat)])
    assert [(h.offset, h.length) for h in mediacarve.carve(stream)] == [(4096, len(cgbi))]


def test_a_png_walk_never_passes_its_ceiling():
    """A chunk that ends past the ceiling used to be stepped over, so a PNG could
    come back longer than its ceiling."""
    real = _load("plain.png")
    stream = _image([(4096, real)])
    assert list(mediacarve.carve(stream, caps={"png": len(real) - 1})) == []
    assert [h.length for h in mediacarve.carve(stream, caps={"png": len(real)})] == [len(real)]


# ------------------------------------------------------------- false extents

def test_a_capped_hit_hides_nothing():
    """Measured on an Android system partition: an mdat declaring 4.5 x 10^18
    bytes was cut at the 4 GiB ceiling and still hid 1,747 files. A capped
    length is a ceiling, not the file's."""
    huge = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 4557430888798830399)
    clip = _FTYP + _box(b"free", b"f" * 200) + huge
    jpg, png = _load("plain.jpg"), _load("plain.png")
    stream = _image([(4096, clip), (65536, jpg), (131072, png)], size=1 << 20)
    hits = [(h.offset, h.kind, h.bounded) for h in mediacarve.carve(stream, caps={"mp4": 1 << 19})]
    assert hits == [(4096, "mp4", "capped"), (65536, "jpeg", "parsed"), (131072, "png", "parsed")]


def test_an_avi_must_open_with_its_hdrl_list():
    """Measured on an Android system partition: RIFF, a size and "AVI " followed
    by timezone abbreviations was taken as a 774,778,422-byte AVI and hid 455
    files. A real AVI's first chunk is LIST hdrl."""
    fake = b"RIFF" + struct.pack("<I", 700_000) + b"AVI LIST\x00LHST\x00WGST\x00EGST\x00MAGST"
    # a LIST of the wrong type whose size does fit, so only the hdrl rule can say no
    wrong = b"RIFF" + struct.pack("<I", 700_000) + b"AVI LIST" + struct.pack("<I", 64) + b"LHST"
    # an hdrl list that claims more than the whole RIFF holds
    overrun = b"RIFF" + struct.pack("<I", 700_000) + b"AVI LIST" + struct.pack("<I", 900_000) + b"hdrl"
    png = _load("plain.png")
    for bad in (fake, wrong, overrun):
        stream = _image([(4096, bad), (65536, png)], size=1 << 20)
        assert [(h.offset, h.kind) for h in mediacarve.carve(stream)] == [(65536, "png")]


def test_a_webp_must_open_with_a_vp8_chunk():
    fake = b"RIFF" + struct.pack("<I", 700_000) + b"WEBPJUNK" + b"\x00" * 64
    png = _load("plain.png")
    stream = _image([(4096, fake), (65536, png)], size=1 << 20)
    assert [(h.offset, h.kind) for h in mediacarve.carve(stream)] == [(65536, "png")]



# ---------------------------------------------------------------- jpeg scans

def _jpeg_segments(data):
    """(marker, start, length) for each segment before the first scan."""
    out, pos = [], 2
    while True:
        marker, seg = data[pos + 1], struct.unpack(">H", data[pos + 2:pos + 4])[0]
        out.append((marker, pos, seg))
        if marker == 0xDA:
            return out
        pos += 2 + seg


def test_a_scan_with_no_frame_header_is_not_a_jpeg():
    """Measured on raw audio from public phone images: FFD8FF straight into an
    FFDA "scan" about 65,500 bytes long, ending 65 KB later at an FFD9. ITU-T
    T.81 puts the frame header first (Figure B.2). One such run hid a WAV."""
    fake = b"\xff\xd8\xff\xda" + struct.pack(">H", 65502) + b"\x11" * 65500 + b"\xff\xd9"
    wav = _load("plain.wav")
    stream = _image([(4096, fake[:30000]), (4096 + 30000, wav), (4096 + 30000 + len(wav), fake[30000:])])
    assert [(h.offset, h.kind) for h in mediacarve.carve(stream)] == [(4096 + 30000, "wav")]
    # a scan header T.81 allows, but no frame header before it
    sos = b"\xff\xda" + struct.pack(">HB", 8, 1) + b"\x01\x00\x00\x3f\x00"
    no_frame = b"\xff\xd8" + sos + b"\x11" * 4000 + b"\xff\xd9"
    assert list(mediacarve.carve(_image([(4096, no_frame)]))) == []


def test_a_scan_header_of_the_wrong_length_is_not_a_jpeg():
    """T.81 Table B.3: a scan header is 6 + 2 x Ns bytes, Ns from 1 to 4."""
    data = bytearray(_load("plain.jpg"))
    marker, at, seg = _jpeg_segments(bytes(data))[-1]
    assert marker == 0xDA
    data[at + 4] = 5                              # Ns out of range, with its
    data[at + 2:at + 4] = struct.pack(">H", 16)   # matching length of 6 + 2 x 5
    assert list(mediacarve.carve(_image([(4096, bytes(data))]))) == []
    data = bytearray(_load("plain.jpg"))
    data[at + 2:at + 4] = struct.pack(">H", seg + 2)   # length that does not match Ns
    assert list(mediacarve.carve(_image([(4096, bytes(data))]))) == []


# ------------------------------------------------------- wav, aiff and caf

def test_a_wav_without_fmt_and_data_is_not_a_wav():
    body = b"WAVE" + b"LIST" + struct.pack("<I", 4) + b"INFO" + b"\x00" * 400
    fake = b"RIFF" + struct.pack("<I", len(body)) + body
    assert list(mediacarve.carve(_image([(4096, fake)]))) == []


def test_a_wav_whose_size_is_a_placeholder_is_not_reported():
    """Measured: 2 of 1,247 real WAVs carry RIFF and data sizes of 0xFFFFFFFF,
    a streaming writer's "unknown". The end cannot be found, so no hit."""
    wav = bytearray(_load("plain.wav"))
    wav[4:8] = b"\xff\xff\xff\xff"
    png = _load("plain.png")
    stream = _image([(4096, bytes(wav)), (65536, png)])
    assert [(h.offset, h.kind) for h in mediacarve.carve(stream)] == [(65536, "png")]


def test_an_aiff_needs_its_common_chunk():
    aiff = bytearray(_load("plain.aiff"))
    at = aiff.find(b"COMM")
    aiff[at:at + 4] = b"COMX"
    assert list(mediacarve.carve(_image([(4096, bytes(aiff))]))) == []
    # another IFF form, even one holding a COMM chunk, is not AIFF
    body = b"8SVX" + aiff[12:]
    other = b"FORM" + struct.pack(">I", len(body)) + body.replace(b"COMX", b"COMM")
    assert list(mediacarve.carve(_image([(4096, other)]))) == []


def test_a_caf_must_open_with_its_description_chunk():
    caf = _load("plain.caf")
    swapped = caf[:8] + b"free" + caf[12:]       # first chunk no longer desc
    assert list(mediacarve.carve(_image([(4096, swapped)]))) == []


def _caf_chunks(data):
    out, pos = [], 8
    while pos + 12 <= len(data):
        size = struct.unpack(">q", data[pos + 4:pos + 12])[0]
        out.append((data[pos:pos + 4], pos, size))
        pos += 12 + size
    return out


def test_a_caf_whose_data_size_is_unknown_is_not_reported():
    """Apple's spec lets the data chunk say -1 when it is last. Measured: 503 of
    2,049 real CAF files do, and every such file in two public iOS images, 986
    in all, is a text-to-speech voice asset. Where they end cannot be found."""
    caf = bytearray(_load("plain.caf"))
    t, at, size = _caf_chunks(bytes(caf))[-1]
    assert t == b"data"
    caf[at + 4:at + 12] = struct.pack(">q", -1)
    png = _load("plain.png")
    stream = _image([(4096, bytes(caf)), (65536, png)])
    assert [(h.offset, h.kind) for h in mediacarve.carve(stream)] == [(65536, "png")]


def test_a_caf_ends_where_its_chunks_stop():
    """CAF records no total size. The walk ends at the first chunk type the
    spec does not define, or at a listed type whose size runs past the stream,
    which is text after the file far more often than a chunk cut short."""
    caf = _load("plain.caf")
    text = b"free" + struct.pack(">q", 1 << 40) + b" and other words that follow " * 4
    png = _load("plain.png")
    for tail in (b"\x00ID3 trailing metadata, one byte off" * 3, text):
        stream = _image([(4096, caf + tail), (65536, png)], size=1 << 20)
        assert [(h.offset, h.length, h.kind) for h in mediacarve.carve(stream)] \
            == [(4096, len(caf), "caf"), (65536, len(png), "png")]



# ----------------------------------------------------------------------- ogg

def _crc_reference(data):
    """Ogg's CRC the slow way: MSB first, polynomial 0x04c11db7, zero start, no
    final inversion. Independent of the zlib route the carver takes."""
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
            crc &= 0xFFFFFFFF
    return crc


def _ogg_pages(data):
    pos, pages = 0, []
    while pos < len(data):
        nseg = data[pos + 26]
        size = 27 + nseg + sum(data[pos + 27:pos + 27 + nseg])
        pages.append(data[pos:pos + size])
        pos += size
    return pages


def _page(serial, seq, flags, payload):
    assert len(payload) < 255
    head = (b"OggS\x00" + bytes([flags]) + struct.pack("<qII", 0, serial, seq)
            + b"\x00\x00\x00\x00" + bytes([1, len(payload)]))
    page = head + payload
    return page[:22] + struct.pack("<I", _crc_reference(page)) + page[26:]


def test_the_ogg_crc_matches_the_reference_on_every_encoder_page():
    """Every page FFmpeg wrote must check, by both routes."""
    for name in ("plain.opus", "plain.ogg"):
        pages = _ogg_pages(_load(name))
        assert len(pages) > 2
        for page in pages:
            zeroed = page[:22] + b"\x00\x00\x00\x00" + page[26:]
            stored = struct.unpack("<I", page[22:26])[0]
            assert _crc_reference(zeroed) == stored
            assert mediacarve._ogg_crc(zeroed) == stored
    blob = random.Random(4).randbytes(5000)
    assert mediacarve._ogg_crc(blob) == _crc_reference(blob)


def _ogg_hits(stream):
    return [(h.offset, h.length, h.ext, h.bounded) for h in mediacarve.carve(stream)]


def test_a_damaged_ogg_page_ends_the_file_at_the_page_before_it():
    """A page that fails its CRC is not the rest of this file, so the file ends
    at the last page that checked. The pages before it are still this file's."""
    data = bytearray(_load("plain.ogg"))
    pages = _ogg_pages(bytes(data))
    good = len(pages[0]) + len(pages[1])
    data[good + 27 + pages[2][26] + 10] ^= 0x01      # inside the payload: only the CRC sees it
    assert _ogg_hits(_image([(4096, bytes(data))])) == [(4096, good, ".ogg", "parsed")]


def test_a_lost_ogg_page_ends_the_file_at_the_gap():
    pages = _ogg_pages(_load("plain.ogg"))
    gapped = b"".join(pages[:2] + pages[3:])
    good = len(pages[0]) + len(pages[1])
    assert _ogg_hits(_image([(4096, gapped)])) == [(4096, good, ".ogg", "parsed")]


def test_an_ogg_file_with_no_end_of_stream_page_ends_at_its_last_page():
    """Measured: 9 of the 27 distinct Ogg Opus files in one public Android 13
    image end with every CRC good and no end-of-stream page. Requiring that page
    rejected every one of them."""
    pages = _ogg_pages(_load("plain.opus"))
    last = bytearray(pages[-1])
    last[5] &= ~0x04
    last[22:26] = struct.pack("<I", _crc_reference(bytes(last[:22]) + bytes(4) + bytes(last[26:])))
    clip = b"".join(pages[:-1]) + bytes(last)
    assert _ogg_hits(_image([(4096, clip)])) == [(4096, len(clip), ".opus", "parsed")]
    rnd = random.Random(8)
    stream = io.BytesIO(bytes(4096) + clip + rnd.randbytes(8192))
    assert _ogg_hits(stream) == [(4096, len(clip), ".opus", "parsed")]


def test_two_chained_ogg_files_come_back_as_two():
    """A stream that begins after the last one ended is chained. RFC 3533 has
    each group stand on its own, so each is reported by itself."""
    opus, vorbis = _load("plain.opus"), _load("plain.ogg")
    stream = _image([(4096, opus + vorbis)])
    assert [(h.offset, h.length, h.ext) for h in mediacarve.carve(stream)] \
        == [(4096, len(opus), ".opus"), (4096 + len(opus), len(vorbis), ".ogg")]


def test_a_stream_that_begins_inside_an_open_one_is_another_file():
    """First pages may only come before any other page. One that turns up while
    a stream is still open is a different file overwriting the end of this one:
    the cut file ends where the other begins, and both are found."""
    opus_head = b"".join(_ogg_pages(_load("plain.opus"))[:3])
    vorbis = _load("plain.ogg")
    stream = _image([(4096, opus_head + vorbis)])
    assert _ogg_hits(stream) == [(4096, len(opus_head), ".opus", "parsed"),
                                 (4096 + len(opus_head), len(vorbis), ".ogg", "parsed")]
    # the same when both streams do end, so only the ordering rule can say so
    body = b"\x00" * 200
    a = _page(1, 0, 0x02, b"OpusHead" + body) + _page(1, 1, 0x00, body)
    b = _page(2, 0, 0x02, b"OpusHead" + body) + _page(2, 1, 0x04, body)
    stream = _image([(4096, a + b + _page(1, 2, 0x04, body))])
    assert [(h.offset, h.length) for h in mediacarve.carve(stream)] \
        == [(4096, len(a)), (4096 + len(a), len(b))]


def test_grouped_streams_end_with_the_last_of_them():
    """Streams that begin together all have to end. A Theora stream among them
    makes the file video."""
    body = b"\x00" * 200
    clip = (_page(1, 0, 0x02, b"\x80theora" + body) + _page(2, 0, 0x02, b"\x01vorbis" + body)
            + _page(1, 1, 0x00, body) + _page(2, 1, 0x04, body)
            + _page(1, 2, 0x04, body))
    stream = _image([(4096, clip)])
    assert [(h.offset, h.length, h.kind, h.ext, h.bounded) for h in mediacarve.carve(stream)] \
        == [(4096, len(clip), "ogv", ".ogv", "parsed")]


def test_an_ogg_stream_cut_by_the_end_of_the_image_is_capped():
    data = _load("plain.opus")
    stream = io.BytesIO(b"\x00" * 4096 + data[:len(data) - 100])
    assert [(h.offset, h.length, h.bounded) for h in mediacarve.carve(stream)] \
        == [(4096, len(data) - 100, "capped")]


def test_a_lone_ogg_header_is_not_a_file():
    """The first page is never taken on trust: a page header at the very end of
    the image, or one whose CRC fails, is nothing."""
    head = b"OggS\x00\x02" + b"\x00" * 20 + b"\x02\xff\xff" + b"OpusHead" + b"x" * 200
    assert list(mediacarve.carve(io.BytesIO(b"\x00" * 4096 + head))) == []
    assert list(mediacarve.carve(_image([(4096, head + b"x" * 400)]))) == []


# ----------------------------------------------------------------------- amr

def test_amr_ends_at_the_first_byte_that_cannot_be_a_frame_header():
    """AMR records no length. A header byte with a P bit set, or a frame type a
    storage file may not carry, ends the walk exactly there."""
    data = _load("plain.amr")
    for tail in (b"\xff" * 64, b"\x48" + b"\x00" * 64):   # P bits; FT 9
        stream = _image([(4096, data + tail)])
        assert [(h.offset, h.length, h.ext) for h in mediacarve.carve(stream)] \
            == [(4096, len(data), ".amr")], tail[:1]


def test_unwritten_space_after_amr_is_not_more_frames():
    """Zeros read as frame type 0 with the Q bit clear, a damaged frame an
    encoder does not write. Without the all-zero rule the walk runs on through
    every zero after the file."""
    data = _load("plain.awb")
    stream = _image([(4096, data)], size=1 << 20)
    assert [(h.offset, h.length, h.ext) for h in mediacarve.carve(stream)] \
        == [(4096, len(data), ".awb")]


def test_how_far_amr_runs_into_random_bytes():
    """Random bytes after a file can parse as a few frames, so the length can run
    past the real end. Measured here over 500 tails so the claim has a number:
    with this seed 448 ended exactly and the longest overrun was 47 bytes. A
    header check that passes 20 of the 256 byte values predicts about 461."""
    data = _load("plain.amr")
    rnd = random.Random(21)
    over = []
    for _ in range(500):
        stream = io.BytesIO(data + rnd.randbytes(4096))
        hit = next(mediacarve.carve(stream))
        assert hit.length >= len(data)
        over.append(hit.length - len(data))
    exact = sum(1 for o in over if o == 0)
    assert exact >= 425, (exact, max(over))
    assert max(over) < 200


@pytest.mark.parametrize("magic", [b"#!AMR\n", b"#!AMR-WB\n"])
def test_an_amr_magic_in_text_is_not_a_file(magic):
    """A magic followed by text, by random bytes or by nothing at all is caught
    by the frame rules and the length floor."""
    rnd = random.Random(5)
    for tail in (_prose(4096), rnd.randbytes(4096), b""):
        assert list(mediacarve.carve(_image([(4096, magic + tail)]))) == [], tail[:16]



# ------------------------------------------------------------------- ceilings

_FTYP = struct.pack(">I", 24) + b"ftyp" + b"mp42\x00\x00\x00\x00mp42isom"


_HDRL = b"LIST" + struct.pack("<I", 4 + 56) + b"hdrl" + b"\x00" * 56


def _avi(body_bytes: int) -> bytes:
    body = b"AVI " + _HDRL + b"\x00" * body_bytes
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_an_avi_over_a_mebibyte_is_recovered():
    """The ceiling for a RIFF file is the one its form names. It used to be looked
    up under "riff", which has no ceiling of its own, so it fell to 1 MiB and an
    AVI or WebP larger than that was not reported at all."""
    avi = _avi(2 << 20)
    stream = _image([(4096, avi)])
    assert [(h.offset, h.length, h.kind, h.bounded) for h in mediacarve.carve(stream)] \
        == [(4096, len(avi), "avi", "header")]


def test_a_video_whose_index_follows_its_media_data_keeps_the_index():
    """Phones write ftyp, mdat, then moov. The box walk used to stop at 1 MiB (the
    ceiling was looked up under "bmff"), so once mdat crossed that the moov box
    after it was left out and the carved file had no index."""
    clip = _FTYP + _box(b"mdat", b"d" * (2 << 20)) + _box(b"moov", b"m" * 200)
    stream = _image([(4096, clip)])
    assert [(h.offset, h.length, h.kind, h.bounded) for h in mediacarve.carve(stream)] \
        == [(4096, len(clip), "mp4", "header")]


def test_a_ceiling_given_for_a_kind_is_the_one_applied():
    avi = _avi(500_000)
    clip = _FTYP + _box(b"moov", b"m" * 200) + _box(b"mdat", b"d" * 500_000)
    stream = _image([(4096, avi), (1 << 20, clip)])
    assert sorted(h.kind for h in mediacarve.carve(stream)) == ["avi", "mp4"]
    stream.seek(0)
    hits = list(mediacarve.carve(stream, caps={"avi": 100_000, "mp4": 100_000}))
    # RIFF records one size, so a file over its ceiling is not that file; a box
    # walk that passes its ceiling is cut there and says so
    assert [(h.kind, h.length, h.bounded) for h in hits] == [("mp4", 100_000, "capped")]


def test_a_box_that_declares_more_than_the_ceiling_is_capped():
    """A box size is whatever four (or eight) bytes say. The walk stopped at the
    ceiling but returned the sum it had reached, so one box declaring 150 GiB
    came back as a 150 GiB file."""
    huge = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 150 << 30)
    clip = _FTYP + _box(b"moov", b"m" * 200) + huge
    stream = _image([(4096, clip)], size=8 << 20)
    hits = list(mediacarve.carve(stream, caps={"mp4": 1 << 20}))
    assert [(h.offset, h.length, h.kind, h.bounded) for h in hits] \
        == [(4096, 1 << 20, "mp4", "capped")]


def test_no_hit_runs_past_the_end_of_the_stream():
    """A header can record a size the stream does not hold: a file cut off by the
    end of the image, or a false size. The extent ends where the stream does."""
    size = 1 << 20
    huge = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 150 << 30)
    clip = _FTYP + _box(b"moov", b"m" * 200) + huge
    stream = _image([(4096, clip)], size=size)
    assert [(h.offset, h.length, h.bounded) for h in mediacarve.carve(stream)] \
        == [(4096, size - 4096, "capped")]
    avi = b"RIFF" + struct.pack("<I", 900_000) + b"AVI " + _HDRL
    stream = _image([(8192, avi)], size=size // 2)
    assert [(h.offset, h.length, h.kind, h.bounded) for h in mediacarve.carve(stream)] \
        == [(8192, size // 2 - 8192, "avi", "capped")]


def test_a_file_that_ends_with_the_stream_is_not_called_capped():
    clip = _FTYP + _box(b"moov", b"m" * 200) + _box(b"mdat", b"d" * 3000)
    stream = io.BytesIO(b"\x00" * 4096 + clip)
    assert [(h.offset, h.length, h.bounded) for h in mediacarve.carve(stream)] \
        == [(4096, len(clip), "header")]


def test_clip_keeps_every_hit_inside_the_range_scanned():
    """A caller that scans chosen ranges (the free runs of a volume, say) passes
    ``clip`` so a file found in one range cannot claim the bytes after it. Without
    it a hit ends where its own structure says, and hits from two ranges can
    overlap."""
    span = 64 * 1024
    first = _FTYP + _box(b"moov", b"m" * 200) + struct.pack(">I", 3 << 20) + b"mdat"
    second = _FTYP + _box(b"moov", b"m" * 200) + _box(b"mdat", b"d" * 3000)
    stream = _image([(0, first), (1 << 20, second)], size=4 << 20)
    spans = ((0, span), (1 << 20, span))

    def scan(**kw):
        return [(h.offset, h.length, h.bounded) for at, n in spans
                for h in mediacarve.carve(stream, start=at, end=at + n, **kw)]

    whole = len(first) - 8 + (3 << 20)
    assert scan() == [(0, whole, "header"), (1 << 20, len(second), "header")]
    assert scan(clip=True) == [(0, span, "capped"), (1 << 20, len(second), "header")]


def test_a_clipped_remainder_below_the_floor_is_not_a_file():
    clip = _FTYP + _box(b"moov", b"m" * 200) + _box(b"mdat", b"d" * 3000)
    stream = _image([(4096, clip)])
    assert list(mediacarve.carve(stream, start=0, end=4096 + 512, clip=True)) == []


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
