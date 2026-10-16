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

def _carved_length(meta):
    """A TIFF has no length field and ends at its last structure, so zero bytes a
    writer leaves after that are not carved; the manifest records how many."""
    return meta["length"] - meta.get("padding", 0)


def _outside_media(hits, kind, offset, length):
    """Hits other than media found inside a fixture of a kind that hides no
    media: a PDF's page image is still a JPEG."""
    if kind in mediacarve.MEDIA_KINDS:
        return hits
    return [h for h in hits if not (h.kind in mediacarve.MEDIA_KINDS
                                    and offset <= h.offset and h.end <= offset + length)]


@pytest.mark.parametrize("name,meta", _fixtures())
def test_each_fixture_is_recovered_exactly_on_its_own(name, meta):
    data = _load(name)
    with open(os.path.join(MEDIA, name), "rb") as fh:
        hits = list(mediacarve.carve(fh, kinds=mediacarve.DEFAULT_KINDS | {meta["kind"]}))
    hits = _outside_media(hits, meta["kind"], 0, meta["length"])
    assert len(hits) == 1, f"{name}: {[(h.kind, h.offset, h.length) for h in hits]}"
    hit = hits[0]
    assert (hit.offset, hit.length, hit.kind, hit.ext) \
        == (0, _carved_length(meta), meta["kind"], meta["ext"])


@pytest.mark.parametrize("name,meta", _fixtures())
def test_each_fixture_is_recovered_from_inside_a_larger_image(name, meta):
    data = _load(name)
    offset = 8192
    stream = _image([(offset, data)])
    hits = list(mediacarve.carve(stream, kinds=mediacarve.DEFAULT_KINDS | {meta["kind"]}))
    hits = _outside_media(hits, meta["kind"], offset, meta["length"])
    assert len(hits) == 1, name
    hit = hits[0]
    assert (hit.offset, hit.length) == (offset, _carved_length(meta))
    stream.seek(hit.offset)
    assert hashlib.sha256(stream.read(meta["length"])).hexdigest() == meta["sha256"]


def test_several_files_in_one_image_all_come_back_byte_for_byte():
    media = _manifest()
    pieces, expected, offset = [], [], 4096
    rnd = random.Random(3)
    for m in media:
        data = _load(m["name"])
        pieces.append((offset, data))
        expected.append((offset, _carved_length(m), m["kind"], m["sha256"], m["length"]))
        offset += len(data) + rnd.randrange(1000, 9000)
    stream = _image(pieces)
    hits = list(mediacarve.carve(stream, kinds=mediacarve.ALL_KINDS))
    for off, length, kind, _, _ in expected:
        hits = [h for h in hits if h.offset == off or h in _outside_media([h], kind, off, length)]
    assert len(hits) == len(expected)
    for hit, (off, length, kind, sha, size) in zip(hits, expected):
        assert (hit.offset, hit.length, hit.kind) == (off, length, kind)
        stream.seek(hit.offset)
        assert hashlib.sha256(stream.read(size)).hexdigest() == sha


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



# ----------------------------------------------------------------------- mp3

def _mp3_frames(data, start=0):
    """Split a Layer III stream into frames with the carver's own header reader."""
    out, pos = [], start
    while True:
        frame = mediacarve._mp3_frame(data[pos:pos + 4])
        if frame is None or pos + frame[1] > len(data):
            return out, pos
        out.append(data[pos:pos + frame[1]])
        pos += frame[1]


def test_an_untagged_stream_needs_four_frames():
    """Measured: in 4,160 non-MP3 audio files single frames made 4,458 hits, and
    runs of two or more made none; 4 keeps every multi-frame run on an Android
    system partition out and loses 1 of 1,207 real untagged MP3s."""
    frames, _ = _mp3_frames(_load("bare.mp3"))
    assert len(frames) >= 5
    png = _load("plain.png")
    three, four = b"".join(frames[:3]), b"".join(frames[:4])
    stream = _image([(4096, three), (65536, four), (131072, png)])
    assert [(h.offset, h.length, h.kind) for h in mediacarve.carve(stream)] \
        == [(65536, len(four), "mp3"), (131072, len(png), "png")]


def test_a_tagged_stream_needs_two_frames():
    """FFmpeg's own probe wants two consecutive frames, and no real tagged MP3
    measured had fewer than three. One frame behind a tag was a false hit."""
    data = _load("plain.mp3")
    tag = mediacarve._id3v2_length(data[:10])
    frames, _ = _mp3_frames(data, tag)
    one, two = data[:tag] + frames[0], data[:tag] + frames[0] + frames[1]
    stream = _image([(4096, one), (65536, two)])
    assert [(h.offset, h.length) for h in mediacarve.carve(stream)] == [(65536, len(two))]


def test_a_frame_that_changes_sample_rate_ends_the_stream():
    frames, _ = _mp3_frames(_load("bare.mp3"))
    other = bytearray(frames[4])
    other[2] ^= 0x04                              # sample rate index 0 <-> 1
    clip = b"".join(frames[:4]) + bytes(other) + b"".join(frames[5:])
    stream = _image([(4096, clip)])
    assert [(h.offset, h.length) for h in mediacarve.carve(stream)][:1] \
        == [(4096, sum(len(f) for f in frames[:4]))]


def test_ape_and_id3v1_tags_after_the_frames_are_part_of_the_file():
    """Measured: 40 real MP3s end in an APEv2 tag that opens with its header and
    whose size reaches the ID3v1 tag or the end of the file."""
    data = _load("bare.mp3")
    frames, end = _mp3_frames(data)
    audio, id3v1 = data[:end], data[end:]
    assert id3v1[:3] == b"TAG" and len(id3v1) == 128
    items = struct.pack("<II", 7, 0) + b"Title\x00fixture"
    footer = b"APETAGEX" + struct.pack("<IIII", 2000, len(items) + 32, 1, 0x80000000) + bytes(8)
    header = b"APETAGEX" + struct.pack("<IIII", 2000, len(items) + 32, 1, 0xA0000000) + bytes(8)
    clip = audio + header + items + footer + id3v1
    stream = _image([(4096, clip)])
    assert [(h.offset, h.length) for h in mediacarve.carve(stream)] == [(4096, len(clip))]


def test_only_layer_three_with_a_known_frame_length_is_carved():
    frames, _ = _mp3_frames(_load("bare.mp3"))
    layer2 = b"".join(bytes([f[0], f[1] ^ 0x06]) + f[2:] for f in frames[:6])  # layer bits 01 -> 11
    free = b"".join(f[:2] + bytes([f[2] & 0x0F]) + f[3:] for f in frames[:6])   # bitrate index 0
    stream = _image([(4096, layer2), (65536, free)])
    assert [h for h in mediacarve.carve(stream) if h.kind == "mp3"] == []


def test_a_malformed_id3v2_header_does_not_vouch_for_a_stream():
    """id3.org ID3v2.4.0 section 3.1: version bytes below 0xFF, size bytes below
    0x80. Three frames behind a valid tag are a file; behind a malformed one they
    are three untagged frames, one short of the four an untagged stream needs.
    The malformed sizes below read as the same 133 bytes, so only the header
    rules can tell them apart."""
    frames, _ = _mp3_frames(_load("bare.mp3"))
    audio = b"".join(frames[:3])
    good = b"ID3\x03\x00\x00" + bytes([0, 0, 1, 5]) + bytes(133)       # 128 + 5
    big_byte = b"ID3\x03\x00\x00" + bytes([0, 0, 0, 0x85]) + bytes(133)  # 0x85 = 133
    version_ff = b"ID3\xff\x00\x00" + bytes([0, 0, 1, 5]) + bytes(133)
    assert mediacarve._id3v2_length(good[:10]) == 143
    # the scan signature only starts on versions 2 to 4, so a bad version can
    # only arrive as a second tag chained after a good one
    chained = good + version_ff
    stream = _image([(4096, good + audio), (65536, big_byte + audio), (131072, chained + audio)])
    assert [(h.offset, h.length) for h in mediacarve.carve(stream)] == [(4096, len(good) + len(audio))]

# -------------------------------------------------------------------- sqlite

def _db():
    return _load("small.sqlite")


def _db_with(blob, tmp_path):
    """A database written by the SQLite library, holding ``blob`` in one record."""
    import sqlite3
    path = tmp_path / "with_blob.sqlite"
    con = sqlite3.connect(path)
    con.execute("PRAGMA page_size = 4096")
    con.execute("PRAGMA journal_mode = DELETE")
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, b BLOB)")
    con.execute("INSERT INTO t (b) VALUES (?)", (blob,))
    con.commit()
    con.close()
    return path.read_bytes()


def test_databases_are_carved_only_when_asked_for():
    """A scan with no kinds is media only, as it was before databases were
    added; databases are asked for by name, with "databases" or with "all"."""
    db, png = _db(), _load("plain.png")
    stream = _image([(4096, db), (65536, png)])
    assert [h.kind for h in mediacarve.carve(stream)] == ["png"]
    for kinds in ({"sqlite"}, mediacarve._kinds_arg("databases"), mediacarve._kinds_arg("all")):
        assert ("sqlite", len(db)) in [(h.kind, h.length) for h in mediacarve.carve(stream, kinds=kinds)]


def test_a_database_hides_nothing_inside_it(tmp_path):
    """Media stored in a database's records are still media."""
    gif = _load("plain.gif")
    db = _db_with(gif, tmp_path)
    at = db.find(gif)
    assert at > 0
    stream = _image([(4096, db)])
    assert sorted((h.offset, h.kind) for h in mediacarve.carve(stream, kinds=mediacarve.ALL_KINDS)) \
        == [(4096, "sqlite"), (4096 + at, "gif")]


def test_nothing_hides_a_database():
    """A blob is spread over a database's pages, so its own sizes can claim the
    next file. Measured once in 8,700 real databases laid end to end: a HEIC
    ran 34,095 bytes into the next database, over its header."""
    db = _db()
    overrun = _FTYP + _box(b"moov", b"m" * 200) + _box(b"mdat", b"d" * 3000)[:8] + b"d" * 1000
    stream = _image([(4096, overrun), (4096 + len(overrun), db)], size=1 << 20)
    hits = [(h.offset, h.kind) for h in mediacarve.carve(stream, kinds=mediacarve.ALL_KINDS)]
    assert (4096 + len(overrun), "sqlite") in hits
    assert hits[0][1] == "mp4" and hits[0][0] == 4096


def test_a_page_count_sqlite_does_not_vouch_for_is_not_used():
    """fileformat2 1.3.7: the in-header size counts only when it is not zero and
    the change counter equals the version-valid-for number. Measured: 15 of
    8,700 real databases have no count, and are not reported."""
    db = bytearray(_db())
    stale = bytearray(db)
    stale[92:96] = (struct.unpack(">I", db[92:96])[0] + 1).to_bytes(4, "big")
    zero = bytearray(db)
    zero[28:32] = bytes(4)
    for bad in (stale, zero):
        assert list(mediacarve.carve(_image([(4096, bytes(bad))]), kinds={"sqlite"})) == []


@pytest.mark.parametrize("at,value,why", [
    (16, b"\x03\x00", "page size not a power of two"),
    (18, b"\x03", "write version neither 1 nor 2"),
    (21, b"\x41", "maximum payload fraction not 64"),
    (44, b"\x00\x00\x00\x05", "schema format above 4"),
    (56, b"\x00\x00\x00\x04", "text encoding above 3"),
    (80, b"\x01", "reserved bytes not zero"),
])
def test_a_header_sqlite_would_refuse_is_not_a_database(at, value, why):
    db = bytearray(_db())
    db[at:at + len(value)] = value
    assert list(mediacarve.carve(_image([(4096, bytes(db))]), kinds={"sqlite"})) == [], why


def test_page_one_copied_into_a_log_is_not_a_database():
    """A WAL frame header (24 bytes) or a journal page record (4 bytes) opens
    with the page number (fileformat2, 4.1 and 3). Measured: all 435 header hits
    on two Android volumes that were not database files were page 1 inside WAL
    files, and the rule takes 14,611 of 14,621 such hits out of 1,740 real WALs."""
    db = _db()
    wal_frame = struct.pack(">IIIIII", 1, 0, 0x1234, 0x5678, 0x9ABC, 0xDEF0)
    journal = b"\x00" * 20 + struct.pack(">I", 1)
    for prefix in (wal_frame, journal):
        stream = _image([(4096 - len(prefix), prefix + db)])
        assert [h.offset for h in mediacarve.carve(stream, kinds={"sqlite"})] == [], prefix[:4]
    stream = _image([(4096, db)])                 # the same bytes, not in a log
    assert [h.offset for h in mediacarve.carve(stream, kinds={"sqlite"})] == [4096]



# ---------------------------------------------------------------------- tiff

def _tiff_entry(data, tag):
    """The position of IFD 0's entry for ``tag`` in a little-endian TIFF."""
    ifd = struct.unpack("<I", data[4:8])[0]
    for i in range(struct.unpack("<H", data[ifd:ifd + 2])[0]):
        at = ifd + 2 + 12 * i
        if struct.unpack("<H", data[at:at + 2])[0] == tag:
            return at
    raise KeyError(tag)


def _tiff_set(data, tag, value):
    data = bytearray(data)
    at = _tiff_entry(data, tag)
    data[at + 8:at + 12] = struct.pack("<I", value)
    return bytes(data)


def _tiff(ifd0, more=b"", data=b""):
    """A little-endian TIFF: header, IFD 0 at 8 with ``ifd0`` as (tag, type,
    count, value) entries, then ``data``, then ``more``."""
    body = struct.pack("<H", len(ifd0)) + b"".join(struct.pack("<HHII", *e) for e in ifd0) + bytes(4)
    return b"II*\x00" + struct.pack("<I", 8) + body + data + more


def test_a_tiff_with_no_image_is_not_a_file():
    """Exif data is a TIFF with no image of its own: IFD 0 describes the JPEG
    around it and has no strips or tiles. Taken out of its JPEG it is not
    reported, and the same IFD with one strip added is."""
    make = b"Maker of the camera, long enough to pass the floor\x00"
    tags = [(271, 2, len(make), 8 + 2 + 12 * 3 + 4), (274, 3, 1, 1), (282, 5, 1, 0)]
    exif = _tiff(tags, data=make)
    assert list(mediacarve.carve(_image([(4096, exif)]), kinds={"tiff"})) == []
    strip = b"\x00" * 64
    start = 8 + 2 + 12 * 4 + 4
    image = _tiff([(256, 3, 1, 8), (257, 3, 1, 8), (273, 4, 1, start), (279, 4, 1, len(strip))], data=strip)
    assert [(h.offset, h.length) for h in mediacarve.carve(_image([(4096, image)]), kinds={"tiff"})] \
        == [(4096, len(image))]


def test_an_exif_ifd_after_the_image_counts():
    """An IFD may be anywhere in the file (TIFF 6.0, section 2), and the Exif IFD
    is found through its tag (Exif 2.3, 4.6.3), so one written after the image
    data is where the file ends."""
    strip = b"\x00" * 64
    start = 8 + 2 + 12 * 5 + 4
    exif_at = start + len(strip)
    exif_ifd = struct.pack("<H", 1) + struct.pack("<HHII", 36864, 7, 4, 0x30333230) + bytes(4)
    image = _tiff([(256, 3, 1, 8), (257, 3, 1, 8), (273, 4, 1, start), (279, 4, 1, len(strip)),
                   (34665, 4, 1, exif_at)], data=strip, more=exif_ifd)
    assert [(h.offset, h.length) for h in mediacarve.carve(_image([(4096, image)]), kinds={"tiff"})] \
        == [(4096, len(image))]


def test_a_tiff_whose_tags_are_not_in_order_is_not_a_file():
    """TIFF 6.0, section 2: "The entries in an IFD must be sorted in ascending
    order by Tag." Measured: all 321 real TIFFs read keep the order."""
    data = bytearray(_load("raw.tif"))
    first, second = _tiff_entry(data, 256), _tiff_entry(data, 257)
    data[first:first + 12], data[second:second + 12] = data[second:second + 12], data[first:first + 12]
    assert list(mediacarve.carve(_image([(4096, bytes(data))]), kinds={"tiff"})) == []


def test_a_tiff_with_a_field_type_it_cannot_size_is_not_a_file():
    data = bytearray(_load("raw.tif"))
    at = _tiff_entry(data, 262)
    data[at + 2:at + 4] = struct.pack("<H", 99)
    assert list(mediacarve.carve(_image([(4096, bytes(data))]), kinds={"tiff"})) == []


def test_every_page_in_the_ifd_chain_counts():
    """pages.tif holds two images in two IFDs, the second after the first's
    data; the end is the second page's."""
    data = _load("pages.tif")
    meta = next(m for m in _manifest() if m["name"] == "pages.tif")
    ifd = struct.unpack("<I", data[4:8])[0]
    n = struct.unpack("<H", data[ifd:ifd + 2])[0]
    assert struct.unpack("<I", data[ifd + 2 + 12 * n:ifd + 6 + 12 * n])[0] > ifd
    hits = list(mediacarve.carve(_image([(4096, data)]), kinds={"tiff"}))
    assert [(h.offset, h.length) for h in hits] == [(4096, meta["length"] - meta["padding"])]


def test_a_tiff_hides_what_it_holds_only_when_its_strips_open_right():
    """A TIFF has no length field; its end is the furthest place its pointers
    reach, and a pointer can reach past the file (measured: 2 of 323 real TIFFs).
    An LZW strip opens with a ClearCode, 0x80 (TIFF 6.0, section 13), so an LZW
    TIFF whose strips all open with it hides a PNG its byte count covers; an
    uncompressed one, which nothing confirms, hides nothing."""
    png = _load("plain.png")
    for name, hides in (("plain.tif", True), ("raw.tif", False)):
        data = _load(name)
        reach = len(data) + 4096 + len(png) + 4096
        strips = struct.unpack("<I", data[_tiff_entry(data, 279) + 8:_tiff_entry(data, 279) + 12])[0]
        start = struct.unpack("<I", data[_tiff_entry(data, 273) + 8:_tiff_entry(data, 273) + 12])[0]
        assert struct.unpack("<I", data[_tiff_entry(data, 279) + 4:_tiff_entry(data, 279) + 8])[0] == 1
        stretched = _tiff_set(data, 279, reach - start)       # one strip, its count over the PNG
        assert strips < reach - start
        stream = _image([(4096, stretched), (4096 + len(data) + 4096, png)])
        kinds = [h.kind for h in mediacarve.carve(stream)]
        assert kinds == (["tiff"] if hides else ["tiff", "png"]), name


def test_an_lzw_strip_that_does_not_open_with_a_clear_code_hides_nothing():
    png = _load("plain.png")
    data = bytearray(_load("plain.tif"))
    start = struct.unpack("<I", data[_tiff_entry(data, 273) + 8:_tiff_entry(data, 273) + 12])[0]
    assert data[start] == 0x80
    data[start] = 0x00
    stretched = _tiff_set(bytes(data), 279, len(data) + 4096 + len(png) + 4096 - start)
    stream = _image([(4096, stretched), (4096 + len(data) + 4096, png)])
    assert [h.kind for h in mediacarve.carve(stream)] == ["tiff", "png"]


# ---------------------------------------------------------- sqlite-encrypted

_ENCRYPTED = ("small-encrypted.sqlite", "small-encrypted-v3.sqlite")


def _random_image(pieces):
    size = max(off + len(data) for off, data in pieces) + 4096
    blob = bytearray(random.Random(5).randbytes(size))
    for off, data in pieces:
        blob[off:off + len(data)] = data
    return io.BytesIO(bytes(blob))


@pytest.mark.parametrize("name", _ENCRYPTED)
@pytest.mark.parametrize("filler", [b"\x00", None])
def test_a_database_encrypted_after_its_header_is_found_at_its_size(name, filler):
    """Written by the SQLCipher shell with a 32-byte plain header, SQLCipher 4's
    settings (80-byte reserve) and 3's (48). The sqlite3 library will not open
    either, and the length is the page count as stored."""
    db = _load(name)
    pieces = [(4096, db), (4096 + len(db) + 8192, _load("small.sqlite"))]
    stream = _image(pieces) if filler else _random_image(pieces)
    for kinds in ({"sqlite-encrypted"}, mediacarve._kinds_arg("databases"), mediacarve.ALL_KINDS):
        hits = [(h.offset, h.length, h.kind, h.ext, h.bounded) for h in mediacarve.carve(stream, kinds=kinds)]
        assert (4096, len(db), "sqlite-encrypted", ".sqlite", "header") in hits


def test_encrypted_and_plain_databases_are_asked_for_apart():
    enc, plain = _load("small-encrypted.sqlite"), _load("small.sqlite")
    stream = _image([(4096, enc), (65536, plain)])
    assert [h.kind for h in mediacarve.carve(stream, kinds={"sqlite"})] == ["sqlite"]
    assert [h.kind for h in mediacarve.carve(stream, kinds={"sqlite-encrypted"})] == ["sqlite-encrypted"]
    assert [h.kind for h in mediacarve.carve(stream)] == []


def test_an_encrypted_database_needs_a_cipher_reserve():
    """The reserve is the IV plus the HMAC rounded up to the AES block, so it is
    a non-zero multiple of 16 (sqlcipher.c, sqlcipher_codec_ctx_reserve_setup)."""
    db = bytearray(_load("small-encrypted.sqlite"))
    for reserve in (0, 12, 81):
        db[20] = reserve
        assert list(mediacarve.carve(_image([(4096, bytes(db))]), kinds={"sqlite-encrypted"})) == [], reserve


def test_an_encrypted_database_still_needs_its_plain_header():
    """The checks on bytes 16 to 31 still apply: a page count and a page size."""
    db = bytearray(_load("small-encrypted.sqlite"))
    zero = bytearray(db)
    zero[28:32] = bytes(4)
    odd = bytearray(db)
    odd[16:18] = b"\x03\x00"
    for bad in (zero, odd):
        assert list(mediacarve.carve(_image([(4096, bytes(bad))]), kinds=mediacarve.ALL_KINDS)) == []


def test_a_plain_database_is_never_called_encrypted():
    """Bytes 72 to 91 are zero in a plain database, so one that fails a check
    after byte 32 is refused, as before, even with a reserve SQLCipher could use."""
    db = bytearray(_db())
    db[20] = 80
    db[92:96] = (struct.unpack(">I", db[92:96])[0] + 1).to_bytes(4, "big")
    assert list(mediacarve.carve(_image([(4096, bytes(db))]), kinds=mediacarve.ALL_KINDS)) == []


def test_an_encrypted_page_one_copied_into_a_log_is_not_a_database():
    db = _load("small-encrypted.sqlite")
    wal_frame = struct.pack(">IIIIII", 1, 0, 0x1234, 0x5678, 0x9ABC, 0xDEF0)
    stream = _image([(4096 - len(wal_frame), wal_frame + db)])
    assert list(mediacarve.carve(stream, kinds={"sqlite-encrypted"})) == []


# -------------------------------------------------------------------- bplist

def _plist():
    return _load("small.plist")


def _set_trailer(data, **fields):
    """Rewrite fields of a binary plist's 32-byte trailer."""
    data = bytearray(data)
    t = len(data) - 32
    if "ois" in fields:
        data[t + 6] = fields["ois"]
    if "ors" in fields:
        data[t + 7] = fields["ors"]
    for name, at in (("count", 8), ("top", 16), ("table", 24)):
        if name in fields:
            data[t + at:t + at + 8] = struct.pack(">Q", fields[name])
    return bytes(data)


def _trailer(data):
    ois, ors = data[-26], data[-25]
    count, top, table = struct.unpack(">QQQ", data[-24:])
    return ois, ors, count, top, table


def test_plists_are_carved_only_when_asked_for():
    plist, png = _plist(), _load("plain.png")
    stream = _image([(4096, plist), (65536, png)])
    assert [h.kind for h in mediacarve.carve(stream)] == ["png"]
    for kinds in ({"bplist"}, mediacarve._kinds_arg("plists"), mediacarve._kinds_arg("all")):
        assert ("bplist", len(plist)) in [(h.kind, h.length) for h in mediacarve.carve(stream, kinds=kinds)]


def test_the_trailer_found_is_the_one_that_points_back_to_this_header():
    """A plist stored inside another one ends first, with a trailer of its own;
    only a trailer whose offset table ends where it begins, counted from this
    header, is this plist's. The inner one is hidden, as a nested file is."""
    import plistlib
    inner = _plist()
    outer = plistlib.dumps({"before": "x" * 50, "archived": inner, "after": list(range(40))},
                           fmt=plistlib.FMT_BINARY)
    at = outer.find(inner)
    assert at > 0
    stream = _image([(4096, outer)])
    assert [(h.offset, h.length) for h in mediacarve.carve(stream, kinds={"bplist"})] == [(4096, len(outer))]
    assert (4096 + at, len(inner)) in [(h.offset, h.length) for h in
                                       mediacarve.carve(stream, kinds={"bplist"}, nested=True)]


def test_a_decoy_trailer_that_does_not_point_back_is_skipped():
    """Bytes inside the plist shaped like a trailer, whose offset table even reads
    as valid from this header, are not this plist's trailer unless that table
    ends exactly where they begin."""
    import plistlib
    decoy = b"\x00" * 6 + b"\x01\x01" + struct.pack(">QQQ", 1, 0, 0) + b"\x08"
    outer = bytearray(plistlib.dumps({"a": decoy, "b": list(range(60))}, fmt=plistlib.FMT_BINARY))
    at = outer.find(decoy)
    eight = at + 32                               # the 0x08 byte after the decoy
    outer[at + 24:at + 32] = struct.pack(">Q", eight)   # its table: one 1-byte offset, 8
    assert plistlib.loads(bytes(outer))["a"][:6] == bytes(6)
    stream = _image([(4096, bytes(outer))])
    assert [(h.offset, h.length) for h in mediacarve.carve(stream, kinds={"bplist"})] \
        == [(4096, len(outer))]


def test_a_plist_hides_no_media():
    """Asking for plists changes no media hit."""
    import plistlib
    png = _load("plain.png")
    plist = plistlib.dumps({"picture": png, "name": "with a picture"}, fmt=plistlib.FMT_BINARY)
    stream = _image([(4096, plist)])
    at = plist.find(png)
    assert sorted((h.offset, h.kind) for h in mediacarve.carve(stream, kinds=mediacarve.ALL_KINDS)) \
        == [(4096, "bplist"), (4096 + at, "png")]


def test_a_header_with_no_trailer_is_not_a_plist():
    rnd = random.Random(31)
    for tail in (rnd.randbytes(1 << 20), bytes(1 << 20), _prose(1 << 20)):
        stream = io.BytesIO(bytes(4096) + b"bplist00" + tail)
        assert list(mediacarve.carve(stream, kinds={"bplist"})) == []


def test_a_trailer_apple_would_refuse_is_not_a_plist():
    """__CFBinaryPlistGetTopLevelInfo (CFBinaryPList.c, CF-1153.18): the top
    object is one of the objects, and object references are wide enough to
    name every object. Neither change moves where the trailer has to be, so
    only these checks can refuse them."""
    plist = _plist()
    ois, ors, count, top, table = _trailer(plist)
    assert count >= 256 and ors >= 2
    for bad, why in ((_set_trailer(plist, top=count), "top object not among the objects"),
                     (_set_trailer(plist, ors=1), "1-byte references cannot name every object")):
        assert list(mediacarve.carve(_image([(4096, bad)]), kinds={"bplist"})) == [], why


def test_an_offset_outside_the_object_data_is_not_a_plist():
    plist = bytearray(_plist())
    ois, ors, count, top, table = _trailer(bytes(plist))
    plist[table:table + ois] = (table + 5).to_bytes(ois, "big")   # first offset past the data
    assert list(mediacarve.carve(_image([(4096, bytes(plist))]), kinds={"bplist"})) == []



# ----------------------------------------------------------------------- zip

class _NoSeek(io.RawIOBase):
    """A write-only stream zipfile cannot seek, so it writes data descriptors."""
    def __init__(self):
        self.out = bytearray()
    def writable(self):
        return True
    def write(self, b):
        self.out += b
        return len(b)


def _zip(members, *, streamed=False, stored=False):
    import zipfile
    method = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    if streamed:
        sink = _NoSeek()
        with zipfile.ZipFile(sink, "w", method) as z:
            for name, data in members:
                z.writestr(name, data)
        return bytes(sink.out)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        for name, data in members:
            z.writestr(name, data)
    return buf.getvalue()


def test_archives_are_carved_only_when_asked_for():
    archive, png = _load("small.zip"), _load("plain.png")
    stream = _image([(4096, archive), (65536, png)])
    assert [h.kind for h in mediacarve.carve(stream)] == ["png"]
    for kinds in ({"zip"}, mediacarve._kinds_arg("archives"), mediacarve._kinds_arg("all")):
        assert ("zip", len(archive)) in [(h.kind, h.length) for h in mediacarve.carve(stream, kinds=kinds)]


def test_a_zip_hides_the_zips_in_it_but_no_media():
    import zipfile
    inner = _zip([("a.txt", b"inner " * 50)])
    png = _load("plain.png")
    outer = _zip([("inner.zip", inner), ("picture.png", png)], stored=True)
    stream = _image([(4096, outer)])
    hits = sorted((h.offset, h.kind, h.length) for h in mediacarve.carve(stream, kinds=mediacarve.ALL_KINDS))
    assert hits == [(4096, "zip", len(outer)), (4096 + outer.find(png), "png", len(png))]


def test_a_descriptor_is_taken_only_at_the_distance_it_records():
    """A ZIP stored inside a streamed entry has local headers, a central
    directory and descriptors of its own; none of them ends the outer entry,
    because their recorded sizes do not match the distance from its start."""
    inner = _zip([("x.txt", b"x" * 400), ("y.txt", b"y" * 300)], streamed=True)
    outer = _zip([("inner.zip", inner), ("after.txt", b"after " * 40)], streamed=True, stored=True)
    stream = _image([(4096, outer)])
    assert [(h.offset, h.length) for h in mediacarve.carve(stream, kinds={"zip"})] == [(4096, len(outer))]


def _with_apk_block(archive, gap):
    """Insert ``gap`` zeros and an APK signing block before the central directory
    and move the end record's directory offset to match."""
    eocd = archive.rfind(b"PK\x05\x06")
    cd_off = struct.unpack("<I", archive[eocd + 16:eocd + 20])[0]
    pairs = struct.pack("<Q", 4 + 20) + struct.pack("<I", 0x7109871A) + b"s" * 20
    size = len(pairs) + 8 + 16
    block = struct.pack("<Q", size) + pairs + struct.pack("<Q", size) + b"APK Sig Block 42"
    moved = cd_off + gap + len(block)
    tail = archive[cd_off:eocd] + archive[eocd:eocd + 16] + struct.pack("<I", moved) + archive[eocd + 20:]
    return archive[:cd_off] + bytes(gap) + block + tail


def test_whatever_lies_before_the_central_directory_is_part_of_the_file():
    """source.android.com: an APK signing block sits before the central
    directory. Measured on real APKs: zeros before it, more entries after it,
    and entries the directory does not list. The walk does not read that span;
    the end record, pointing back to the first header, decides."""
    archive = _zip([("AndroidManifest.xml", b"<manifest/>"), ("classes.dex", b"dex\n035" + bytes(500))])
    for gap in (0, 4094):
        apk = _with_apk_block(archive, gap)
        stream = _image([(4096, apk)])
        assert [(h.offset, h.length, h.ext) for h in mediacarve.carve(stream, kinds={"zip"})] \
            == [(4096, len(apk), ".apk")], gap
    odd = bytearray(_with_apk_block(archive, 64))
    odd[archive.find(b"PK\x01\x02") + 10] = 0xFF  # not zeros before the block
    at = bytes(odd).find(b"APK Sig Block 42")
    odd[at - 8] ^= 1                              # and its two sizes disagree
    stream = _image([(4096, bytes(odd))])
    assert [(h.offset, h.length) for h in mediacarve.carve(stream, kinds={"zip"})] == [(4096, len(odd))]


def _zip_with_junk(junk_after, junk):
    """An archive with ``junk`` inserted after entry ``junk_after``, its central
    directory's local header offsets and its own offset moved to match."""
    archive = _zip([(f"part{i}.txt", (f"part {i} " * 80).encode()) for i in range(4)])
    eocd = archive.rfind(b"PK\x05\x06")
    cd_off = struct.unpack("<I", archive[eocd + 16:eocd + 20])[0]
    cd = bytearray(archive[cd_off:eocd])
    offsets, pos = [], 0
    while pos < len(cd):
        offsets.append(struct.unpack("<I", cd[pos + 42:pos + 46])[0])
        nl, xl, cl = struct.unpack("<HHH", cd[pos + 28:pos + 34])
        pos += 46 + nl + xl + cl
    cut = offsets[junk_after + 1]
    pos = 0
    while pos < len(cd):
        lo = struct.unpack("<I", cd[pos + 42:pos + 46])[0]
        if lo >= cut:
            cd[pos + 42:pos + 46] = struct.pack("<I", lo + len(junk))
        nl, xl, cl = struct.unpack("<HHH", cd[pos + 28:pos + 34])
        pos += 46 + nl + xl + cl
    end = bytearray(archive[eocd:])
    end[16:20] = struct.pack("<I", cd_off + len(junk))
    return archive[:cut] + junk + archive[cut:cd_off] + bytes(cd) + bytes(end)


def test_bytes_no_entry_accounts_for_do_not_stop_the_walk():
    """Measured on real APKs: about 15 KB after an entry the central directory
    does not list, and a stray central directory header followed by more
    entries. The end record is searched for from wherever the entries stop."""
    for junk in (random.Random(4).randbytes(15000), b"PK\x01\x02" + bytes(60) + b"stray"):
        archive = _zip_with_junk(1, junk)
        stream = _image([(4096, archive)])
        assert [(h.offset, h.length) for h in mediacarve.carve(stream, kinds={"zip"})] \
            == [(4096, len(archive))], junk[:4]


def test_an_archive_that_fails_is_walked_once_not_once_per_entry(monkeypatch):
    """Every local header after the first is a candidate too. A walk that fails
    marks the headers it passed, so they are not each walked to the same end."""
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for i in range(200):
            z.writestr(f"f{i}.txt", b"x" * 50)
    broken = buf.getvalue()
    broken = broken[:broken.rfind(b"PK\x05\x06")]          # no end record
    calls = []
    real = mediacarve._zip_walk
    monkeypatch.setattr(mediacarve, "_zip_walk", lambda *a: calls.append(1) or real(*a))
    assert list(mediacarve.carve(_image([(4096, broken)]), kinds={"zip"})) == []
    assert len(calls) == 1


def test_the_walk_reaches_every_entry_before_the_directory():
    """The end record decides whether an archive is one. The walk is what gets
    there without reading every byte, so it has to pass every entry: across a
    stored ZIP's own descriptors, ZIP64 descriptors, and zeros between entries.
    A walk that stops early still finds the end record, only more slowly."""
    import zipfile
    inner = _zip([("x.txt", b"x" * 400), ("y.txt", b"y" * 300)], streamed=True)
    nested = _zip([("inner.zip", inner), ("after.txt", b"after " * 40)], streamed=True, stored=True)
    padded = _zip_with_junk(1, bytes(4000))
    sink = _NoSeek()                              # ZIP64 extra fields, 8-byte descriptors
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(3):
            with z.open(f"big{i}.txt", "w", force_zip64=True) as w:
                w.write(b"zip64 " * 300)
    cases = ((nested, "nested"), (bytes(sink.out), "zip64 descriptors"),
             (_load("stream.zip"), "descriptors"), (padded, "zeros"))
    for archive, name in cases:
        headers = sorted(i.header_offset for i in zipfile.ZipFile(io.BytesIO(archive)).infolist())
        visited = []
        assert mediacarve._zip_walk(io.BytesIO(archive), 0, len(archive), visited, set())
        assert visited == headers, name


def test_a_signature_whose_kinds_were_not_asked_for_is_not_measured(monkeypatch):
    """Measured on a 33.8 MB APK carved for ZIPs only: 89,932 MP3 walks and 25
    seconds, because a hit that is dropped covers nothing."""
    calls = []
    real = mediacarve._mp3_length
    monkeypatch.setattr(mediacarve, "_mp3_length", lambda *a: calls.append(1) or real(*a))
    frames, _ = _mp3_frames(_load("bare.mp3"))
    stream = _image([(4096, b"".join(frames * 20))])
    assert list(mediacarve.carve(stream, kinds={"zip"})) == []
    assert calls == []
    assert [h.kind for h in mediacarve.carve(stream, kinds={"mp3"})][:1] == ["mp3"]


def test_an_end_record_that_does_not_point_back_is_not_a_zip():
    archive = bytearray(_load("small.zip"))
    eocd = bytes(archive).rfind(b"PK\x05\x06")
    for field, delta in ((16, 1), (12, 1), (10, 1)):  # directory offset, size, entries
        bad = bytearray(archive)
        if field == 10:
            bad[10 + eocd] += delta
        else:
            value = struct.unpack("<I", bad[eocd + field:eocd + field + 4])[0] + delta
            bad[eocd + field:eocd + field + 4] = struct.pack("<I", value)
        assert list(mediacarve.carve(_image([(4096, bytes(bad))]), kinds={"zip"})) == [], field


@pytest.mark.parametrize("members,ext", [
    ([("mimetype", b"application/epub+zip"), ("OEBPS/a.xhtml", b"<html/>")], ".epub"),
    ([("mimetype", b"application/vnd.oasis.opendocument.text"), ("content.xml", b"<x/>")], ".odt"),
    ([("[Content_Types].xml", b"<Types/>"), ("word/document.xml", b"<w/>")], ".docx"),
    ([("[Content_Types].xml", b"<Types/>"), ("xl/workbook.xml", b"<w/>")], ".xlsx"),
    ([("[Content_Types].xml", b"<Types/>"), ("ppt/presentation.xml", b"<p/>")], ".pptx"),
    ([("AndroidManifest.xml", b"<m/>"), ("classes.dex", b"dex")], ".apk"),
    ([("Payload/App.app/Info.plist", b"<plist/>")], ".ipa"),
    ([("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n")], ".jar"),
    ([("readme.txt", b"hello")], ".zip"),
])
def test_the_extension_comes_from_the_entry_names(members, ext):
    archive = _zip(members, stored=True)
    stream = _image([(4096, archive)])
    assert [h.ext for h in mediacarve.carve(stream, kinds={"zip"})] == [ext]



# ----------------------------------------------------------------------- pdf

def _pdf(objects, *, header=b"%PDF-1.4\n", start=0, update=None, xref_stream=False):
    """A PDF laid out as ISO 32000-1 7.5 describes, with real offsets. ``start``
    is where the file will sit in a bigger one, which the offsets ignore."""
    out = bytearray(header)
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    if xref_stream:
        xref_at = len(out)
        out += b"%d 0 obj\n<< /Type /XRef /Size %d >>\nstream\n\nendstream\nendobj\n" % (
            len(objects) + 1, len(objects) + 2)
    else:
        xref_at = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
        out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref_at
    if update is not None:                        # an incremental update (7.5.6)
        comment, body = update
        out += comment
        at = len(out)
        out += b"%d 0 obj\n" % (len(objects) + 1) + body + b"\nendobj\n"
        upd_xref = len(out)
        out += b"xref\n%d 1\n%010d 00000 n \n" % (len(objects) + 1, at)
        out += b"trailer\n<< /Size %d /Root 1 0 R /Prev %d >>\n" % (len(objects) + 2, xref_at)
        out += b"startxref\n%d\n%%%%EOF\n" % upd_xref
    return bytes(out)


_CATALOG = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [] /Count 0 >>"]


def test_documents_are_carved_only_when_asked_for():
    pdf, png = _load("sips.pdf"), _load("plain.png")
    stream = _image([(4096, pdf), (65536, png)])
    assert "pdf" not in [h.kind for h in mediacarve.carve(stream)]
    for kinds in ({"pdf"}, mediacarve._kinds_arg("documents"), mediacarve._kinds_arg("all")):
        assert ("pdf", len(pdf)) in [(h.kind, h.length) for h in mediacarve.carve(stream, kinds=kinds)]


def test_an_update_after_the_first_end_is_part_of_the_file():
    """7.5.6: an update appends objects, an xref section and a trailer with its
    own %%EOF. Measured: ExifTool opens its update with a comment line, and a
    real file came back 3,365 bytes short until comments were allowed."""
    for comment in (b"", b"%BeginExifToolUpdate\n"):
        pdf = _pdf(_CATALOG, update=(comment, b"<< /Title (updated) >>"))
        assert pdf.count(b"%%EOF") == 2
        stream = _image([(4096, pdf)])
        assert [(h.offset, h.length) for h in mediacarve.carve(stream, kinds={"pdf"})] == [(4096, len(pdf))]


def test_a_dummy_first_page_trailer_is_not_the_end():
    """Annex F.3.4: a linearized file's first-page trailer may end in
    "startxref 0" and %%EOF, which are to be ignored."""
    body = _pdf(_CATALOG)
    first = b"1000 0 obj\n<< /Linearized 1 >>\nendobj\ntrailer\n<< /Size 3 >>\nstartxref\n0\n%%EOF\n"
    header = b"%PDF-1.4\n"
    pdf = header + first + body[len(header):]
    # move the real startxref by what was inserted
    real = int(pdf.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    pdf = pdf.rsplit(b"startxref\n", 1)[0] + b"startxref\n%d\n%%%%EOF\n" % (real + len(first))
    stream = _image([(4096, pdf)])
    assert [(h.offset, h.length) for h in mediacarve.carve(stream, kinds={"pdf"})] == [(4096, len(pdf))]


def test_an_eof_marker_that_does_not_point_back_is_skipped():
    """%%EOF in content, or after a startxref that lands on no cross-reference
    section counted from this header, is not this file's end."""
    pdf = _pdf([_CATALOG[0], b"<< /Note (startxref\n12\n%%EOF\n) >>"] + _CATALOG[1:])
    stream = _image([(4096, pdf)])
    assert [(h.offset, h.length) for h in mediacarve.carve(stream, kinds={"pdf"})] == [(4096, len(pdf))]


def test_a_cross_reference_stream_is_a_section_startxref_can_point_to():
    """7.5.8: from PDF 1.5 startxref may give the offset of a cross-reference
    stream object rather than the xref keyword."""
    pdf = _pdf(_CATALOG, header=b"%PDF-1.5\n", xref_stream=True)
    stream = _image([(4096, pdf)])
    assert [(h.offset, h.length) for h in mediacarve.carve(stream, kinds={"pdf"})] == [(4096, len(pdf))]


def test_a_header_needs_its_own_line():
    """7.5.2: the header is the file's first line. Measured: all 1,294 real PDFs
    break the line after the version, and two "%PDF-1.4" strings in native
    libraries on an Android partition did not lead to a PDF."""
    pdf = _pdf(_CATALOG)
    joined = pdf.replace(b"%PDF-1.4\n", b"%PDF-1.4 ", 1)
    stream = _image([(4096, joined)])
    assert list(mediacarve.carve(stream, kinds={"pdf"})) == []


def test_bytes_after_the_end_are_not_the_file():
    pdf = _pdf(_CATALOG)
    for tail in (bytes(500), b"garbage that is not a pdf " * 20, b"\r\n" + bytes(100)):
        stream = io.BytesIO(bytes(4096) + pdf + tail)
        assert [(h.offset, h.length) for h in mediacarve.carve(stream, kinds={"pdf"})] \
            == [(4096, len(pdf))], tail[:8]           # its own line break is already in


def test_a_pdf_hides_the_pdfs_in_it_but_no_media():
    inner = _pdf(_CATALOG)
    jpg = _load("plain.jpg")
    outer = _pdf([_CATALOG[0], b"<< /Length %d >>\nstream\n" % len(inner) + inner + b"\nendstream",
                  b"<< /Length %d >>\nstream\n" % len(jpg) + jpg + b"\nendstream", _CATALOG[1]])
    stream = _image([(4096, outer)])
    hits = sorted((h.offset, h.kind) for h in mediacarve.carve(stream, kinds=mediacarve.ALL_KINDS))
    assert hits == [(4096, "pdf"), (4096 + outer.find(jpg), "jpeg")]



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


def test_cli_list_verbose_shows_each_hits_extension_in_aligned_columns(tmp_path, capsys):
    """An mp4 can be a .mov and a zip a .docx, so the listing says which, and a
    six-letter kind such as sqlite does not push its row out of line."""
    raw = tmp_path / "disk.raw"
    stream = _image([(4096, _load("plain.mov")), (65536, _load("small.sqlite")),
                     (131072, _load("plain.jpg"))])
    raw.write_bytes(stream.getvalue())
    assert mediacarve.main(["list", str(raw), "-q", "-v", "-k", "all"]) == 0
    rows = [line for line in capsys.readouterr().out.splitlines() if line.strip()[:1].isdigit()]
    assert [line.split()[2:4] for line in rows] == [["mp4", ".mov"], ["sqlite", ".sqlite"], ["jpeg", ".jpg"]]
    assert len({line.index(line.split()[4]) for line in rows}) == 1   # bounded column lines up


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
