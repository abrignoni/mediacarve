"""mediacarve: recover images, video and audio from any stream of bytes, by signature.

One file, pure Python, standard library only. No compiler, no network, and
nothing to install. It scans a seekable stream for media file signatures and
reports each one it finds as an extent: where it starts, how long it is, and
what kind of file it is.

    import mediacarve

    with open("disk.raw", "rb") as fh:
        for hit in mediacarve.carve(fh):
            print(hit.kind, hit.offset, hit.length, hit.bounded)

The input is any object with ``seek`` and ``read``, so this works on a raw
image, a disk, a file inside an archive, or an E01 opened with ewfprobe. It
never writes to the stream it reads.

Two consumers, two shapes, one core. A tool that keeps its own index wants the
extents and will fetch the bytes on demand. A tool that wants files on disk can
hand the extents to ``extract`` or use the command line, which writes a folder
or a zip. Both come out of the same scan.

**What "bounded" means.** Some formats record their own length, so the extent is
exact. Some do not, and the end is found by parsing to the terminator. Some give
neither, and the extent is a capped guess. Each hit says which it was, because
the difference matters to whoever reads the output:

    header    the file's own header or box structure gave the length
    parsed    the length came from walking the file's internal structure
    capped    the extent was cut at a limit (the ceiling for its kind, the end
              of the stream, or the end of a clipped range), so the length is
              a ceiling and not a fact

**What this does not do.** It finds files that are contiguous. A fragmented file
recovers only as far as its first fragment. It reads no filesystem, so it has no
filenames, paths or timestamps to offer, and it cannot say whether a hit was a
live file, a deleted one, or a fragment of something else. Those are the reasons
carving complements a filesystem parser rather than replacing it.
"""

from __future__ import annotations

import argparse
import os
import re
import struct
import sys
import zipfile
import zlib
from typing import NamedTuple

__version__ = "0.3.0"

# How much to read at a time while scanning, and how much to carry over so a
# signature straddling two blocks is still found.
BLOCK = 8 << 20
_OVERLAP = 32

# Ceilings per kind, so a false header cannot claim the rest of the image. These
# are generous on purpose: a capped hit is reported as capped, not as a length.
# RIFF and ISO-BMFF each hold more than one kind, so their ceiling is chosen once
# the form or brand has said which kind the file is (see _cap).
DEFAULT_CAPS = {
    "jpeg": 64 << 20,
    "png": 64 << 20,
    "gif": 32 << 20,
    "webp": 64 << 20,
    "avi": 4 << 30,
    "mp4": 4 << 30,
    "heic": 64 << 20,
    "m4a": 4 << 30,
    "ogg": 1 << 30,
    "ogv": 4 << 30,
    "amr": 64 << 20,
}
_ABSOLUTE_CAP = 4 << 30

# A header with nothing behind it is not a file. Measured on real evidence: a
# stray "\xff\xd8\xff\xd9" in ordinary data parses as a complete four-byte
# JPEG unless a floor like this rejects it.
MIN_LENGTHS = {
    "jpeg": 128, "png": 128, "gif": 64, "webp": 64,
    "avi": 1024, "mp4": 1024, "heic": 512,
    "m4a": 1024, "ogg": 128, "ogv": 1024, "amr": 256,
}

# The conventional extension for each kind. ISO-BMFF, Ogg and AMR refine it from
# the brand, codec or header they find (see _bmff_ext, _ogg_length, _amr_length).
EXTENSIONS = {
    "jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp",
    "avi": ".avi", "mp4": ".mp4", "heic": ".heic",
    "m4a": ".m4a", "ogg": ".ogg", "ogv": ".ogv", "amr": ".amr",
}

IMAGE_KINDS = frozenset({"jpeg", "png", "gif", "webp", "heic"})
VIDEO_KINDS = frozenset({"avi", "mp4", "ogv"})
AUDIO_KINDS = frozenset({"m4a", "ogg", "amr"})


class Candidate(NamedTuple):
    """One recovered extent.

    ``offset`` and ``length`` locate it in the stream that was scanned.
    ``bounded`` says how the length was arrived at: "header", "parsed" or
    "capped". ``kind`` is the media kind and ``ext`` a conventional extension.
    """

    offset: int
    length: int
    kind: str
    ext: str
    bounded: str

    @property
    def end(self) -> int:
        return self.offset + self.length


# Header signatures. ISO-BMFF is matched on its "ftyp" box type, which sits four
# bytes into the file, so that hit is rewound before it is measured.
_SCAN = re.compile(
    b"\xff\xd8\xff"              # JPEG SOI + first marker
    b"|\x89PNG\r\n\x1a\n"        # PNG
    b"|GIF8[79]a"                # GIF
    b"|RIFF"                     # RIFF container, form checked below
    b"|ftyp"                     # ISO-BMFF, four bytes in
    b"|OggS\x00[\x02\x06]"       # Ogg page, version 0, first of its stream
    b"|#!AMR(?:-WB)?\n",          # single-channel AMR or AMR-WB storage file
    re.DOTALL)

# Top-level ISO-BMFF box types the carver knows. A file has to show at least one
# of these after its ftyp to count as a file. A box outside this set is skipped
# when it is well formed (four printable type bytes, a sane size), because
# writers add vendor boxes: four screen recordings on one Windows volume all
# carried a "beam" box between ftyp and moov, and the deleted one was the only
# recoverable file in 1.88 GB of free space, which this walk ended at box two.
# Anything that is not a box at all still ends the walk, which is what stops a
# file running into whatever follows it.
_BMFF_BOXES = frozenset({
    b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot", b"uuid",
    b"meta", b"moof", b"mfra", b"styp", b"sidx", b"ssix", b"prft", b"emsg",
    b"junk", b"pdin", b"mece",
})

# ISO-BMFF major brands that are stills rather than video.
_HEIC_BRANDS = frozenset({
    b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1",
    b"avif", b"avis",
})

# Brands that name a file's extension, as the MP4 registration authority lists
# them (mp4ra.org, data/brands.csv at 35c6ffe). "M4A " is iTunes MPEG-4 audio;
# the registry notes it can also carry video, but the brand is the writer's own
# statement that the file is audio. A brand not listed here keeps ".mp4".
_AUDIO_BRANDS = {b"M4A ": ".m4a", b"M4B ": ".m4b", b"M4P ": ".m4p"}
_VIDEO_BRAND_EXT = {b"qt  ": ".mov", b"M4V ": ".m4v"}
_AVIF_BRANDS = frozenset({b"avif", b"avis"})
_HEVC_STILL_BRANDS = frozenset({b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx"})


def _read_at(stream, offset, size):
    stream.seek(offset)
    return stream.read(size)


# ------------------------------------------------------------ length finding

def _jpeg_length(stream, offset, cap):
    """Walk JPEG markers to the end-of-image marker.

    Walking rather than searching for FFD9 matters: an EXIF thumbnail is a whole
    JPEG inside the APP1 segment, and the segment's own length field steps over
    it, so the thumbnail's end marker is never mistaken for the file's.
    """
    pos = offset + 2
    end = offset + cap
    saw_scan = False
    while pos < end:
        head = _read_at(stream, pos, 4)
        if len(head) < 2 or head[0] != 0xFF:
            return None
        marker = head[1]
        if marker == 0xD8:                       # another SOI, not ours
            return None
        if marker in (0x01,) or 0xD0 <= marker <= 0xD7:
            pos += 2                             # standalone markers
            continue
        if marker == 0xD9:                       # EOI
            if not saw_scan:
                return None                      # no image data: not a JPEG
            return pos + 2 - offset, "parsed"
        if len(head) < 4:
            return None
        seg = struct.unpack(">H", head[2:4])[0]
        if seg < 2:
            return None
        pos += 2 + seg
        if marker == 0xDA:                       # start of scan: entropy follows
            saw_scan = True
            scan = pos
            while scan < end:
                block = _read_at(stream, scan, 1 << 16)
                if not block:
                    return None
                i = 0
                while True:
                    i = block.find(b"\xff", i)
                    if i < 0 or i + 1 >= len(block):
                        break
                    nxt = block[i + 1]
                    if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                        i += 2                   # stuffed byte or restart
                        continue
                    if nxt == 0xD9:
                        return scan + i + 2 - offset, "parsed"
                    # any other marker: hand back to the marker walk
                    pos = scan + i
                    break
                else:
                    scan += len(block) - 1
                    continue
                if i < 0 or i + 1 >= len(block):
                    scan += max(1, len(block) - 1)
                    continue
                break
    return None


def _png_length(stream, offset, cap):
    """Sum PNG chunks to IEND. Each chunk is length, type, data and CRC."""
    pos = offset + 8
    end = offset + cap
    while pos < end:
        head = _read_at(stream, pos, 8)
        if len(head) < 8:
            return None
        size = struct.unpack(">I", head[:4])[0]
        ctype = head[4:8]
        if not ctype.isalpha():
            return None
        pos += 12 + size                          # length + type + data + CRC
        if ctype == b"IEND":
            return pos - offset, "parsed"
        if size > cap:
            return None
    return None


def _gif_length(stream, offset, cap):
    """Walk GIF blocks to the trailer."""
    head = _read_at(stream, offset, 13)
    if len(head) < 13:
        return None
    flags = head[10]
    pos = offset + 13
    if flags & 0x80:                              # global colour table
        pos += 3 * (2 ** ((flags & 0x07) + 1))
    end = offset + cap
    while pos < end:
        marker = _read_at(stream, pos, 1)
        if not marker:
            return None
        if marker == b"\x3b":                     # trailer
            return pos + 1 - offset, "parsed"
        if marker == b"\x21":                     # extension
            pos += 2
        elif marker == b"\x2c":                   # image descriptor
            desc = _read_at(stream, pos + 1, 9)
            if len(desc) < 9:
                return None
            pos += 10
            if desc[8] & 0x80:                    # local colour table
                pos += 3 * (2 ** ((desc[8] & 0x07) + 1))
            pos += 1                              # LZW minimum code size
        else:
            return None
        while pos < end:                          # sub-block chain
            size = _read_at(stream, pos, 1)
            if not size:
                return None
            if size == b"\x00":
                pos += 1
                break
            pos += 1 + size[0]
    return None


def _cap(caps, kind):
    """The ceiling for a kind, never above the absolute one."""
    return min(caps.get(kind, DEFAULT_CAPS.get(kind, 1 << 20)), _ABSOLUTE_CAP)


def _riff_length(stream, offset, caps):
    """RIFF records its own size, so the extent is exact."""
    head = _read_at(stream, offset, 12)
    if len(head) < 12:
        return None
    size = struct.unpack("<I", head[4:8])[0]
    form = head[8:12]
    if form == b"AVI ":
        kind = "avi"
    elif form == b"WEBP":
        kind = "webp"
    else:
        return None
    total = size + 8
    if total < 16 or total > _cap(caps, kind):
        return None
    return total, "header", kind


def _bmff_ext(major, compatible):
    """The kind and extension an ftyp names.

    Stills keep the kind "heic" and video "mp4", so a filter on those kinds
    selects what it did before, and only the extension follows the brand. Audio
    brands are their own kind. A generic HEIF brand (mif1, msf1) says nothing
    about the codec, so the compatible brands decide between .avif, .heic and
    .heif.
    """
    if major in _AUDIO_BRANDS:
        return "m4a", _AUDIO_BRANDS[major]
    if major in _HEIC_BRANDS:
        brands = {major, *compatible}
        if brands & _AVIF_BRANDS:
            return "heic", ".avif"
        if brands & _HEVC_STILL_BRANDS:
            return "heic", ".heic"
        return "heic", ".heif"
    if major in _VIDEO_BRAND_EXT:
        return "mp4", _VIDEO_BRAND_EXT[major]
    if major.startswith(b"3g2"):
        return "mp4", ".3g2"
    if major.startswith(b"3g"):
        return "mp4", ".3gp"
    return "mp4", ".mp4"


def _box_follows(stream, at):
    """True when a well-formed box header, or the end of the stream, is at ``at``.

    A vendor box is taken on its shape alone, so after a file has ended, four
    printable bytes in whatever follows can pass for one, with a size read from
    the four bytes before them. Measured on real voice notes laid into random
    bytes: one such "box" declared 4,265,331,032 bytes, and the extent it gave
    hid 106 files behind it. 0.2.0 reported the same extent. A real box is followed by another box, or by the end
    of the stream.
    """
    head = _read_at(stream, at, 8)
    if not head:
        stream.seek(0, os.SEEK_END)
        return stream.tell() == at                # exactly at the end, not past it
    if len(head) < 8:
        return False
    size = struct.unpack(">I", head[:4])[0]
    return all(0x20 <= b < 0x7F for b in head[4:8]) and (size >= 8 or size in (0, 1))


def _bmff_length(stream, offset, caps):
    """Sum ISO-BMFF top-level boxes. The brand decides the kind and extension.

    The ftyp box is validated before anything is summed. A bare "ftyp" is only
    four bytes and turns up inside ordinary text: measured on a real Windows
    drive, "hreftyp" in a web resource read as a 6.8 MB file and "er Iftyp" as a
    1.7 GB one, because the preceding four text bytes parse as a box size. A
    real ftyp box is small, and a real file has at least one box after it.

    A box size is taken as written, so the sum can pass the ceiling: one box with
    a 64-bit size can declare more than any disk holds. The extent then ends at
    the ceiling and is reported as capped.
    """
    head = _read_at(stream, offset, 12)
    if len(head) < 12:
        return None
    ftyp_size = struct.unpack(">I", head[:4])[0]
    if not (8 <= ftyp_size <= 1024) or ftyp_size % 4:
        return None
    brand = head[8:12]
    if not all(0x20 <= b < 0x7F for b in brand):
        return None
    ftyp = _read_at(stream, offset, ftyp_size)
    compatible = {ftyp[i:i + 4] for i in range(16, len(ftyp) - 3, 4)}
    kind, ext = _bmff_ext(brand, compatible)
    cap = _cap(caps, kind)
    pos = offset
    end = offset + cap
    seen_ftyp = False
    boxes = known = 0
    while pos < end:
        head = _read_at(stream, pos, 16)
        if len(head) < 8:
            break
        size = struct.unpack(">I", head[:4])[0]
        btype = head[4:8]
        vendor = btype not in _BMFF_BOXES
        if not vendor:
            if btype == b"ftyp":
                if seen_ftyp:
                    break                         # the next file starts here
                seen_ftyp = True
            known += 1
        elif not all(0x20 <= b < 0x7F for b in btype):
            break                                 # not a box: the file has ended
        # a well-formed box of a type not listed above is a vendor box; skipped
        if size == 1:                             # 64-bit largesize
            if len(head) < 16:
                break
            size = struct.unpack(">Q", head[8:16])[0]
        elif size == 0:
            break                                 # runs to end of stream
        if size < 8:
            break
        if vendor and not _box_follows(stream, pos + size):
            break                                 # four printable bytes after a
                                                  # file, not a box of it
        pos += size
        boxes += 1
    length = pos - offset
    if boxes < 2 or known < 2 or length < 16:
        return None                              # ftyp alone, or ftyp plus
                                                 # only boxes it cannot vouch for
    if length > cap:
        return cap, "capped", kind, ext
    return length, "header", kind, ext

# Ogg pages carry a CRC-32 with the generator polynomial 0x04c11db7 (RFC 3533,
# section 6), computed MSB first with a zero start and no final inversion, over
# the page with its CRC field zeroed. zlib computes the same polynomial LSB
# first, so the bytes go in bit-reversed and the result comes out reversed.
_BITREV = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


def _ogg_crc(page):
    reflected = zlib.crc32(page.translate(_BITREV), 0xFFFFFFFF) ^ 0xFFFFFFFF
    return int(f"{reflected:032b}"[::-1], 2)


# The first packet of a logical stream names its codec.
_OGG_CODECS = (
    (b"OpusHead", "opus"), (b"\x01vorbis", "vorbis"), (b"\x80theora", "theora"),
    (b"Speex   ", "speex"), (b"\x7fFLAC", "flac"),
)


def _ogg_length(stream, offset, caps):
    """Walk Ogg pages to the end of the streams they began.

    Each page records its own size and a CRC over its bytes, so the walk checks
    every page, and stops at the page that sets the end-of-stream flag on the
    last open stream. Streams that start together (grouped) all count; a new
    stream that starts after those have ended is chained, and RFC 3533 requires
    a group to stand on its own once unchained, so the file is taken to end
    there and the next group is reported separately.

    Not every writer sets the end-of-stream flag: 9 of the 27 distinct Ogg Opus
    files in one public Android 13 image (hickman_a13) end on a clean page
    boundary with every CRC good and no such page. So a page that fails its CRC,
    breaks its stream's page sequence, or is not a page at all ends the file at
    the last page that checked, rather than rejecting it. Every byte reported
    has passed a CRC as part of this file; a file that was damaged or overwritten
    part way comes back as the part before the damage.

    A page cut off by the end of the stream returns the length the page declares,
    which carve() then cuts to the stream and reports as capped. The first page
    is never taken on trust: it has to be whole and pass its CRC, or nothing is
    reported.

    The codec is only known once the walk is done, so the walk runs to the larger
    of the "ogg" and "ogv" ceilings and the kind's own ceiling is applied after.
    """
    pos = offset
    end = offset + max(_cap(caps, "ogg"), _cap(caps, "ogv"))
    expect = {}                                   # serial -> next page sequence
    ended = 0
    grouping = True
    codecs = []
    cut = None                                    # a page the stream cut short
    while pos < end:
        head = _read_at(stream, pos, 27)
        if len(head) < 27:
            if head and expect:
                cut = pos + 27
            break
        if head[:4] != b"OggS" or head[4] != 0 or head[5] & ~0x07:
            break
        flags = head[5]
        serial, seq = struct.unpack("<II", head[14:22])
        nseg = head[26]
        table = _read_at(stream, pos + 27, nseg)
        size = 27 + nseg + sum(table)
        page = _read_at(stream, pos, size)
        if len(page) < size:
            if expect:
                cut = pos + size
            break
        stored = struct.unpack("<I", page[22:26])[0]
        if _ogg_crc(page[:22] + b"\x00\x00\x00\x00" + page[26:]) != stored:
            break
        if flags & 0x02:                          # first page of a stream
            if not grouping or serial in expect or seq != 0 or flags & 0x01:
                break
            body = page[27 + nseg:]
            codecs.append(next((name for magic, name in _OGG_CODECS
                                if body.startswith(magic)), "other"))
            expect[serial] = 1
        else:
            if not expect or expect.get(serial) != seq:
                break
            grouping = False
            expect[serial] = seq + 1
        pos += size
        if flags & 0x04:                          # last page of a stream
            expect[serial] = None
            ended += 1
            if ended == len(expect):
                break
    if not expect:
        return None                               # not even a first page checked
    if cut is not None:
        pos = cut
    length = pos - offset
    if "theora" in codecs:
        kind, ext = "ogv", ".ogv"
    elif codecs == ["opus"] * len(codecs):
        kind, ext = "ogg", ".opus"
    elif codecs == ["speex"] * len(codecs):
        kind, ext = "ogg", ".spx"
    elif codecs == ["flac"] * len(codecs):
        kind, ext = "ogg", ".oga"
    else:
        kind, ext = "ogg", ".ogg"
    if length > _cap(caps, kind):
        return _cap(caps, kind), "capped", kind, ext
    return length, "parsed", kind, ext


# AMR storage frame sizes in bytes, frame header included, by frame type (FT),
# as FFmpeg's demuxer has them (libavformat/amr.c at n7.1). Only the frame types
# RFC 4867 allows in a storage file are listed: speech modes, SID and NO_DATA,
# plus SPEECH_LOST for AMR-WB. FT 9 to 11 "SHALL NOT be used" in AMR files
# (section 5.3), and the rest are reserved.
_AMR_NB_SIZES = {0: 13, 1: 14, 2: 16, 3: 18, 4: 20, 5: 21, 6: 27, 7: 32, 8: 6, 15: 1}
_AMR_WB_SIZES = {0: 18, 1: 24, 2: 33, 3: 37, 4: 41, 5: 47, 6: 51, 7: 59, 8: 61,
                 9: 6, 14: 1, 15: 1}


def _amr_length(stream, offset, cap):
    """Walk AMR storage frames to the first byte that cannot be a frame header.

    An AMR file records no length and has no terminator, so its end is where the
    frames stop. A frame header is |P|FT|Q|P|P| with the P bits written as zero
    (RFC 4867, section 5.3), and FT must be one of the types listed above. A frame
    of all zero bytes also ends the walk: its header would say FT 0 with the Q bit
    clear, which is a damaged frame an encoder does not write, and zeros are what
    unwritten space holds. Bytes that follow a file can still happen to parse as a
    few frames, so the length can run a little past the real end.
    """
    head = _read_at(stream, offset, 9)
    if head.startswith(b"#!AMR-WB\n"):
        sizes, pos, ext = _AMR_WB_SIZES, offset + 9, ".awb"
    elif head.startswith(b"#!AMR\n"):
        sizes, pos, ext = _AMR_NB_SIZES, offset + 6, ".amr"
    else:
        return None
    end = offset + cap
    buf, buf_at = b"", pos
    while pos < end:
        if pos + 61 > buf_at + len(buf):          # keep a whole frame in hand
            buf_at = pos
            buf = _read_at(stream, pos, 1 << 16)
        i = pos - buf_at
        if i >= len(buf):
            break
        frame_header = buf[i]
        if frame_header & 0x83:
            break
        size = sizes.get(frame_header >> 3 & 0x0F)
        if size is None:
            break
        frame = buf[i:i + size]
        if len(frame) < size or not any(frame):
            break
        pos += size
    return pos - offset, "parsed", "amr", ext


# ------------------------------------------------------------------ scanning

def _measure(stream, offset, kind, caps):
    """Return a Candidate for a header at ``offset``, or None if it is not one."""
    if kind == "jpeg":
        got = _jpeg_length(stream, offset, _cap(caps, kind))
    elif kind == "png":
        got = _png_length(stream, offset, _cap(caps, kind))
    elif kind == "gif":
        got = _gif_length(stream, offset, _cap(caps, kind))
    elif kind == "riff":
        got = _riff_length(stream, offset, caps)
    elif kind == "bmff":
        got = _bmff_length(stream, offset, caps)
    elif kind == "ogg":
        got = _ogg_length(stream, offset, caps)
    elif kind == "amr":
        got = _amr_length(stream, offset, _cap(caps, kind))
    else:
        return None
    if got is None:
        return None
    ext = None
    if len(got) == 4:
        length, bounded, kind, ext = got
    elif len(got) == 3:
        length, bounded, kind = got
    else:
        length, bounded = got
    if length < MIN_LENGTHS.get(kind, 1):
        return None
    return Candidate(offset, length, kind, ext or EXTENSIONS.get(kind, ".bin"), bounded)


def carve(stream, *, kinds=None, caps=None, start=0, end=None,
          nested=False, clip=False, progress=None):
    """Scan ``stream`` and yield a Candidate for every media file found.

    ``kinds`` limits the search, for example ``{"jpeg", "png"}``. ``caps`` maps a
    kind to a maximum length. ``nested`` set True also reports files found inside
    another file, such as an EXIF thumbnail, which are suppressed by default.
    ``progress`` is called as ``progress(position, end)`` while scanning.

    ``start`` and ``end`` say where to look for headers. A file that starts inside
    that range ends where its own structure says, which can be past ``end``, so
    scanning a stream as several ranges can report hits that overlap. Set ``clip``
    when the bytes past ``end`` are not the file's to claim (the range is a run of
    free space, say): a hit is then cut at ``end`` and reported as capped. With or
    without it, no hit runs past the end of the stream; one whose header records
    more than the stream holds is cut there and reported as capped.

    The stream is left where the scan finished. Nothing is written to it.
    """
    caps = dict(DEFAULT_CAPS, **(caps or {}))
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    if end is None or end > size:
        end = size
    limit = end if clip else size
    pos = start
    covered_to = start
    while pos < end:
        buf = _read_at(stream, pos, min(BLOCK, end - pos))
        if not buf:
            break
        for match in _SCAN.finditer(buf):
            sig = match.group()
            at = pos + match.start()
            if sig == b"ftyp":
                at -= 4
                kind = "bmff"
            elif sig == b"RIFF":
                kind = "riff"
            elif sig.startswith(b"OggS"):
                kind = "ogg"
            elif sig.startswith(b"#!AMR"):
                kind = "amr"
            elif sig.startswith(b"\xff\xd8"):
                kind = "jpeg"
            elif sig.startswith(b"\x89PNG"):
                kind = "png"
            else:
                kind = "gif"
            if at < start:
                continue
            if not nested and at < covered_to:
                continue
            hit = _measure(stream, at, kind, caps)
            if hit is None:
                continue
            if hit.end > limit:
                hit = hit._replace(length=limit - hit.offset, bounded="capped")
                if hit.length < MIN_LENGTHS.get(hit.kind, 1):
                    continue
            if kinds and hit.kind not in kinds:
                continue
            if not nested:
                covered_to = max(covered_to, hit.end)
            yield hit
        if progress:
            progress(min(pos + len(buf), end), end)
        if len(buf) < BLOCK:
            break
        pos += len(buf) - _OVERLAP


# ---------------------------------------------------------------- extraction

def _name_for(hit, index):
    return f"{index:06d}_{hit.offset:016x}_{hit.kind}{hit.ext}"


def extract(stream, candidates, dest, *, zip_output=False, progress=None):
    """Write each candidate's bytes out, to a folder or into a zip.

    Names carry the index and the source offset, so a carved file can always be
    traced back to where in the image it came from. Returns the number written.
    """
    written = 0
    archive = None
    if zip_output:
        archive = zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED)
    else:
        os.makedirs(dest, exist_ok=True)
    try:
        for index, hit in enumerate(candidates, 1):
            stream.seek(hit.offset)
            data = stream.read(hit.length)
            if not data:
                continue
            name = _name_for(hit, index)
            if archive is not None:
                archive.writestr(name, data)
            else:
                with open(os.path.join(dest, name), "wb") as out:
                    out.write(data)
            written += 1
            if progress:
                progress(written, hit)
    finally:
        if archive is not None:
            archive.close()
    return written


# --------------------------------------------------------------------- input

def open_source(path):
    """Open a raw image, or an E01 when ewfprobe is available.

    ewfprobe is optional. When it is not importable and the file is an EWF
    acquisition, this says so rather than reading the container as raw bytes,
    which would find nothing useful and look like an empty image.
    """
    with open(path, "rb") as probe:
        magic = probe.read(8)
    if magic == b"EVF\x09\x0d\x0a\xff\x00":
        try:
            import ewfprobe
        except ImportError:
            raise SystemExit(
                f"{os.path.basename(path)} is an EWF (.E01) acquisition. "
                "mediacarve reads it through ewfprobe, which is not importable "
                "here. Put ewfprobe.py beside this file, or export the image to "
                "raw first.")
        return ewfprobe.open_ewf(path)
    return open(path, "rb")


# --------------------------------------------------------------------- CLI

def _size(n):
    v = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if v < 1024 or unit == "TiB":
            return f"{v:,.1f} {unit}" if unit != "B" else f"{int(v)} B"
        v /= 1024
    return f"{v} B"


def _kinds_arg(value):
    if not value:
        return None
    wanted = set()
    for part in value.split(","):
        part = part.strip().lower()
        if part == "images":
            wanted |= set(IMAGE_KINDS)
        elif part in ("video", "videos"):
            wanted |= set(VIDEO_KINDS)
        elif part == "audio":
            wanted |= set(AUDIO_KINDS)
        elif part:
            wanted.add(part)
    return wanted or None


def _progress(pos, end):
    if end:
        sys.stderr.write(f"\r  scanned {100.0 * pos / end:5.1f}%  {_size(pos)}")
        sys.stderr.flush()


def _cmd_list(args):
    kinds = _kinds_arg(args.kinds)
    with open_source(args.image) as src:
        counts = {}
        total = 0
        for hit in carve(src, kinds=kinds, nested=args.nested,
                         progress=None if args.quiet else _progress):
            counts[hit.kind] = counts.get(hit.kind, 0) + 1
            total += hit.length
            if args.verbose:
                print(f"{hit.offset:>16}  {hit.length:>12}  {hit.kind:<5} {hit.bounded}")
        if not args.quiet:
            sys.stderr.write("\r" + " " * 48 + "\r")
    for kind in sorted(counts):
        print(f"  {kind:<6} {counts[kind]:>8,}")
    print(f"  {'total':<6} {sum(counts.values()):>8,}  ({_size(total)})")
    return 0


def _cmd_carve(args):
    kinds = _kinds_arg(args.kinds)
    with open_source(args.image) as src:
        hits = carve(src, kinds=kinds, nested=args.nested,
                     progress=None if args.quiet else _progress)
        written = extract(src, hits, args.output,
                          zip_output=args.output.lower().endswith(".zip"))
        if not args.quiet:
            sys.stderr.write("\r" + " " * 48 + "\r")
    print(f"wrote {written:,} file(s) to {args.output}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mediacarve",
        description="Recover images, video and audio from a disk image by signature. "
                    "Read only.")
    ap.add_argument("--version", action="version", version=f"mediacarve {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("image", help="raw image, or an .E01 when ewfprobe is present")
    common.add_argument("-k", "--kinds", default=None,
                        help="limit to these kinds: images, video, audio, or a comma "
                             "separated list such as jpeg,png,mp4")
    common.add_argument("--nested", action="store_true",
                        help="also report files inside another file, such as "
                             "EXIF thumbnails")
    common.add_argument("-q", "--quiet", action="store_true", help="no progress output")

    s = sub.add_parser("list", parents=[common], help="count what is recoverable")
    s.add_argument("-v", "--verbose", action="store_true",
                   help="print one line per file found")
    s.set_defaults(func=_cmd_list)

    s = sub.add_parser("carve", parents=[common],
                       help="write the recovered files to a folder, or to a zip "
                            "when the output name ends in .zip")
    s.add_argument("-o", "--output", required=True, help="output folder or .zip")
    s.set_defaults(func=_cmd_carve)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
