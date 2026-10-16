"""mediacarve: recover images, video, audio and databases from any stream of bytes, by signature.

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

__version__ = "0.8.0"

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
    "wav": 4 << 30,
    "aiff": 4 << 30,
    "caf": 4 << 30,
    "mp3": 1 << 30,
    "sqlite": 4 << 30,
    "bplist": 32 << 20,
    "zip": 4 << 30,
}
_ABSOLUTE_CAP = 4 << 30

# A header with nothing behind it is not a file. Measured on real evidence: a
# stray "\xff\xd8\xff\xd9" in ordinary data parses as a complete four-byte
# JPEG unless a floor like this rejects it. PNG checks a CRC on every chunk, so
# its floor is only the smallest valid file: signature, IHDR, an empty IDAT and
# IEND, 57 bytes.
MIN_LENGTHS = {
    "jpeg": 128, "png": 57, "gif": 64, "webp": 64,
    "avi": 1024, "mp4": 1024, "heic": 512,
    "m4a": 1024, "ogg": 128, "ogv": 1024, "amr": 256,
    "wav": 64, "aiff": 64, "caf": 64, "mp3": 128, "sqlite": 512, "bplist": 41,
    "zip": 22,
}

# The conventional extension for each kind. ISO-BMFF, Ogg and AMR refine it from
# the brand, codec or header they find (see _bmff_ext, _ogg_length, _amr_length).
EXTENSIONS = {
    "jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp",
    "avi": ".avi", "mp4": ".mp4", "heic": ".heic",
    "m4a": ".m4a", "ogg": ".ogg", "ogv": ".ogv", "amr": ".amr",
    "wav": ".wav", "aiff": ".aiff", "caf": ".caf", "mp3": ".mp3",
    "sqlite": ".sqlite", "bplist": ".plist", "zip": ".zip",
}

IMAGE_KINDS = frozenset({"jpeg", "png", "gif", "webp", "heic"})
VIDEO_KINDS = frozenset({"avi", "mp4", "ogv"})
AUDIO_KINDS = frozenset({"m4a", "ogg", "amr", "wav", "aiff", "caf", "mp3"})
MEDIA_KINDS = IMAGE_KINDS | VIDEO_KINDS | AUDIO_KINDS
DATABASE_KINDS = frozenset({"sqlite"})
PLIST_KINDS = frozenset({"bplist"})
ARCHIVE_KINDS = frozenset({"zip"})
ALL_KINDS = MEDIA_KINDS | DATABASE_KINDS | PLIST_KINDS | ARCHIVE_KINDS

# What a scan looks for when no kinds are given. Media only, so a caller that
# has always passed nothing keeps getting what it got before databases were
# added; databases are asked for by name or with "all".
DEFAULT_KINDS = MEDIA_KINDS


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
    b"|#!AMR(?:-WB)?\n"           # single-channel AMR or AMR-WB storage file
    b"|FORM....AIF[FC]"          # IFF FORM holding AIFF or AIFF-C
    b"|caff\x00\x01\x00\x00"      # CAF version 1, flags 0
    b"|SQLite format 3\x00"        # SQLite database header
    b"|bplist00"                  # binary property list
    b"|PK\x03\x04"                # ZIP local file header
    b"|ID3[\x02-\x04]\x00"         # ID3v2 tag, versions 2.2 to 2.4
    b"|\xff[\xe2\xe3\xf2\xf3\xfa\xfb]",  # MPEG audio Layer III frame sync
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

# Start-of-frame markers, ITU-T T.81 Table B.1: SOF0 to SOF15 less DHT (C4),
# JPG (C8) and DAC (CC).
_JPEG_SOF = frozenset(set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC})


def _jpeg_length(stream, offset, cap):
    """Walk JPEG markers to the end-of-image marker.

    Walking rather than searching for FFD9 matters: an EXIF thumbnail is a whole
    JPEG inside the APP1 segment, and the segment's own length field steps over
    it, so the thumbnail's end marker is never mistaken for the file's.

    A scan is only taken after a frame header (SOFn), with a scan header of the
    length ITU-T T.81 fixes: 6 + 2 x Ns bytes for Ns from 1 to 4 (Table B.3;
    the frame header comes first, Figure B.2). Measured on raw audio from public
    phone images: three runs of bytes that went straight from FFD8FF to an FFDA
    "scan" about 65,500 bytes long ended 65 KB later at an FFD9, decode as
    nothing, and one of them hid a WAV file behind it.
    """
    pos = offset + 2
    end = offset + cap
    saw_scan = False
    saw_frame = False
    while pos < end:
        head = _read_at(stream, pos, 5)
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
        if marker in _JPEG_SOF:
            saw_frame = True
        elif marker == 0xDA:
            if not saw_frame or len(head) < 5 or not 1 <= head[4] <= 4 \
                    or seg != 6 + 2 * head[4]:
                return None                      # not a scan header T.81 allows
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
    """Walk PNG chunks to IEND, checking the CRC of every critical chunk.

    The W3C PNG specification (third edition) fixes what is checked: each chunk
    is a length, a type of four ASCII letters, the data and a CRC over the type
    and data (section 5.3); IHDR is the first chunk (11.2.1); there is at least
    one IDAT; and IEND's data field is empty (11.2.4). Apple's iOS build tools
    write a CgBI chunk ahead of IHDR, so that one is allowed first too.

    Critical chunks (an uppercase first letter, section 5.4) have their CRC
    checked, and ancillary ones do not. Measured both ways. On 38,567 PNGs from
    public phone images, a check on every chunk lost 16 animated PNGs whose fcTL
    fails, and 13 of the 20 such files in that image decode. On a Windows volume,
    checking only IHDR and IEND instead let in 800 more PNG-shaped hits that
    neither Pillow nor FFmpeg decodes, each with a failing CRC on a critical
    chunk, and 163 more that one of them renders.

    Without the CRC a declared length was taken at its word. Measured on a
    Windows volume, two small PNGs whose IEND declared 150,994,944 and 589,824
    bytes ran on through what followed them and hid 14 PNGs. The CRC is also what
    lets the length floor sit at the smallest valid PNG: 38 real PNGs of 68 to
    127 bytes on that volume were under the old 128-byte floor.
    """
    pos = offset + 8
    end = offset + cap
    first = True
    saw_idat = False
    while True:
        head = _read_at(stream, pos, 8)
        if len(head) < 8:
            return None
        size = struct.unpack(">I", head[:4])[0]
        ctype = head[4:8]
        if not all(0x41 <= b <= 0x5A or 0x61 <= b <= 0x7A for b in ctype):
            return None
        if size > 0x7FFFFFFF or pos + 12 + size > end:
            return None                           # past the ceiling: not a length
        if first:
            if ctype == b"CgBI":
                pass                              # Apple's, ahead of IHDR
            elif ctype != b"IHDR" or size != 13:
                return None
            first = ctype == b"CgBI"
        body = _read_at(stream, pos + 4, size + 8)
        if len(body) < size + 8:
            return None
        critical = not ctype[0] & 0x20
        if critical and zlib.crc32(body[:size + 4]) != struct.unpack(">I", body[size + 4:])[0]:
            return None
        pos += 12 + size
        if ctype == b"IDAT":
            saw_idat = True
        elif ctype == b"IEND":
            if size or not saw_idat:
                return None
            return pos - offset, "parsed"


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


# The chunk a RIFF form has to open with. An AVI's first chunk is LIST 'hdrl'
# (Microsoft's AVI RIFF File Reference: "the first required LIST chunk"), and a
# WebP's is VP8, VP8L or VP8X (RFC 9649, sections 2.5 to 2.7).
_WEBP_FIRST = frozenset({b"VP8 ", b"VP8L", b"VP8X"})


def _chunks_hold(stream, pos, end, fmt, required):
    """True when walking chunks from ``pos`` finds every chunk ID in ``required``.

    RIFF and IFF chunks are an ID of four printable bytes, a size and the data,
    padded to an even length. The walk stops at the first ID that is not
    printable or the first size that runs past ``end``, so it vouches for the
    chunks it reached, not for the container's size.
    """
    need = set(required)
    while need and pos + 8 <= end:
        head = _read_at(stream, pos, 8)
        if len(head) < 8 or not all(0x20 <= b < 0x7F for b in head[:4]):
            return False
        size = struct.unpack(fmt, head[4:8])[0]
        need.discard(head[:4])
        pos += 8 + size + (size & 1)
        if pos > end + 1:                         # a missing final pad is common
            break
    return not need


def _riff_length(stream, offset, caps):
    """RIFF records its own size, so the extent is exact once the form checks.

    The size is only as good as the header, so the form's first chunk has to be
    the one its specification requires, and has to fit inside the size. A WAVE
    can open with JUNK, bext or LIST as well as fmt, so for it the chunks are
    walked until both fmt and data have been seen. Measured
    on an Android system partition: "RIFF", a size and "AVI " followed by
    timezone abbreviations was taken as a 774,778,422-byte AVI, and hid 455 files
    that PhotoRec recovered.
    """
    head = _read_at(stream, offset, 24)
    if len(head) < 24:
        return None
    size = struct.unpack("<I", head[4:8])[0]
    form = head[8:12]
    first, first_size = head[12:16], struct.unpack("<I", head[16:20])[0]
    if form == b"AVI " and first == b"LIST" and head[20:24] == b"hdrl":
        kind = "avi"
    elif form == b"WEBP" and first in _WEBP_FIRST:
        kind = "webp"
    elif form == b"WAVE":
        kind = "wav"
    else:
        return None
    total = size + 8
    if total < 16 or total > _cap(caps, kind) or 20 + first_size > total:
        return None
    if kind == "wav" and not _chunks_hold(stream, offset + 12, offset + total,
                                          "<I", {b"fmt ", b"data"}):
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
    """True when a box the carver knows, or the end of the stream, is at ``at``.

    A vendor box is taken on its shape alone, so after a file has ended, four
    printable bytes in whatever follows can pass for one, with a size read from
    the four bytes before them. Measured on real voice notes laid into random
    bytes: one such "box" declared 4,265,331,032 bytes, and the extent it gave
    hid 106 files behind it; 0.2.0 reported the same extent. Requiring only the
    shape of a box after it was not enough: on a macOS volume, four spaces sized
    0x20202020 passed, because the text after them ("lass") had that shape too,
    and the extent hid 179 files. So a vendor box has to be followed by a box in
    the list above, or by the end of the stream. A vendor box followed by another
    vendor box ends the file before the first.
    """
    head = _read_at(stream, at, 8)
    if not head:
        stream.seek(0, os.SEEK_END)
        return stream.tell() == at                # exactly at the end, not past it
    if len(head) < 8:
        return False
    size = struct.unpack(">I", head[:4])[0]
    return head[4:8] in _BMFF_BOXES and (size >= 8 or size in (0, 1))


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

def _iff_length(stream, offset, caps):
    """An AIFF or AIFF-C FORM records its own size, big-endian, like RIFF.

    AIFF 1.3 puts no order on the chunks in a FORM but requires the Common chunk,
    COMM, so the chunks are walked until it is found. Measured on 604 AIFF and
    AIFF-C files from public phone images: in 594 the FORM size is the file's
    size and the chunks fill it exactly.
    """
    head = _read_at(stream, offset, 12)
    if len(head) < 12:
        return None
    total = struct.unpack(">I", head[4:8])[0] + 8
    ext = ".aifc" if head[8:12] == b"AIFC" else ".aiff"
    if total < 20 or total > _cap(caps, "aiff"):
        return None
    if not _chunks_hold(stream, offset + 12, offset + total, ">I", {b"COMM"}):
        return None
    return total, "header", "aiff", ext


# The chunk types Apple's Core Audio Format specification defines, by table.
_CAF_CHUNKS = frozenset({
    b"desc", b"data", b"pakt", b"chan", b"kuki", b"strg", b"mark", b"regn",
    b"inst", b"midi", b"ovvw", b"peak", b"edct", b"info", b"umid", b"uuid",
    b"free",
})


def _caf_length(stream, offset, caps):
    """Walk CAF chunks until one is not a type the specification defines.

    A CAF file records no total size. Its chunks each carry a 64-bit size, the
    Audio Description chunk comes first, and the Audio Description and Audio
    Data chunks are required, so the file ends where the chunks stop. Measured
    on 2,049 CAF files from public phone images: in 1,474 the walk ends exactly
    at the end of the file. In 66 an info chunk declares one byte less than it
    holds, so the walk stops there; the audio data comes before it in all 66,
    and 45 to 267 bytes of chunks after it are left out.

    The specification lets the Audio Data chunk give its size as -1, unknown, if
    it is the last chunk. A carver cannot see where such a file ends, so the walk
    stops there, before the data chunk counts, and the file is not reported. Measured: 503 of the 2,049 do this, all but one uncompressed,
    and every such file in two public iOS images, 986 in all, is one of Apple's
    text-to-speech voice assets under MobileAsset.

    A chunk after the Audio Data chunk that runs past the end of the stream is
    taken as not part of the file: it is far more likely to be four letters of
    whatever followed than a chunk cut off by the end of the image.
    """
    stream.seek(0, os.SEEK_END)
    stream_end = stream.tell()
    end = offset + _cap(caps, "caf")
    pos = offset + 8
    saw_desc = saw_data = False
    while pos < end:
        head = _read_at(stream, pos, 12)
        if len(head) < 12 or head[:4] not in _CAF_CHUNKS:
            break
        ctype = head[:4]
        size = struct.unpack(">q", head[4:12])[0]
        if not saw_desc:
            if ctype != b"desc" or size != 32:
                return None
            saw_desc = True
        if size < 0:
            break                                 # -1 on data: an end nobody recorded
        if saw_data and pos + 12 + size > stream_end:
            break
        pos += 12 + size
        if ctype == b"data":
            saw_data = True
    if not saw_data:
        return None
    return pos - offset, "parsed", "caf", ".caf"


# MPEG audio frame header, as FFmpeg reads it (libavcodec/mpegaudiodecheader.c
# and mpegaudiotabs.h at n7.1): kbit/s by [lsf][layer - 1][index], and the
# sample rates of MPEG-1, halved for MPEG-2 and quartered for MPEG-2.5.
_MPA_BITRATES = (
    ((0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
     (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
     (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)),
    ((0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
     (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
     (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)),
)
_MPA_RATES = (44100, 48000, 32000)

# Consecutive frames a stream must show before it is taken as MP3. A frame sync
# is eleven bits, so on its own it is common in other data; a run of frames that
# agree on version, layer and sample rate is not. Set from measurement (see the
# README): with no ID3v2 tag in front, single frames made 4,458 hits in 4,160
# non-MP3 audio files and none at two or more, and 4 keeps every multi-frame hit
# on an Android system partition out while losing 1 of 1,207 real untagged MP3s.
# Behind a tag, FFmpeg's own probe wants two frames ("Failed to find two
# consecutive MPEG audio frames"), and no real tagged MP3 measured had fewer than 3.
_MP3_MIN_BARE_FRAMES = 4
_MP3_MIN_TAGGED_FRAMES = 2


def _mp3_frame(head):
    """(version/layer/rate key, frame length) for a Layer III header, or None.

    The checks are FFmpeg's ff_mpa_check_header: the sync, and no reserved
    version, layer, bitrate or sample rate. Free-format streams (bitrate index
    0) record no frame length and are not walked. Only Layer III is carved.
    """
    if len(head) < 4:
        return None
    x = struct.unpack(">I", head[:4])[0]
    if x & 0xFFE00000 != 0xFFE00000 or x & (3 << 19) == 1 << 19 \
            or (x >> 17) & 3 != 1 or x & (0xF << 12) in (0, 0xF << 12) \
            or x & (3 << 10) == 3 << 10:
        return None
    if x & (1 << 20):
        lsf, mpeg25 = (0 if x & (1 << 19) else 1), 0
    else:
        lsf, mpeg25 = 1, 1
    rate = _MPA_RATES[(x >> 10) & 3] >> (lsf + mpeg25)
    kbps = _MPA_BITRATES[lsf][2][(x >> 12) & 0xF]
    size = kbps * 144000 // (rate << lsf) + ((x >> 9) & 1)
    return x & 0xFFFE0C00, size                   # sync, version, layer, rate


def _id3v2_length(head):
    """Length of an ID3v2 tag from its 10-byte header, or 0 if it is not one.

    id3.org, ID3v2.4.0 structure, section 3.1: "ID3", two version bytes below
    0xFF, a flags byte, and a 28-bit synchsafe size whose four bytes are below
    0x80; the size leaves out the header, and the 10-byte footer when flag 0x10
    says there is one.
    """
    if len(head) < 10 or head[:3] != b"ID3" or head[3] == 0xFF or head[4] == 0xFF \
            or any(b >= 0x80 for b in head[6:10]):
        return 0
    size = head[6] << 21 | head[7] << 14 | head[8] << 7 | head[9]
    return 10 + size + (10 if head[5] & 0x10 else 0)


def _mp3_length(stream, offset, cap):
    """ID3v2 tags, then Layer III frames that agree, then an APE or ID3v1 tag.

    MP3 has no container. After any ID3v2 tags (and the zeros some writers put
    after one), frames are walked while each header agrees with the first on
    version, layer and sample rate. A stream with an ID3v2 tag needs
    _MP3_MIN_TAGGED_FRAMES frames, one without _MP3_MIN_BARE_FRAMES. After the frames an APEv2 tag that
    opens with its header is stepped over: its size counts the items and the
    32-byte footer, and the header adds 32 more (FFmpeg n7.1 apetag.c: bit 31
    "contains header", bit 29 "is header"). Then a 128-byte ID3v1 tag ("TAG").
    Measured on 2,075 MP3 files from public phone images: all are Layer III, 2,031 end at their last frame or an ID3v1 tag, and the 40
    APEv2 tags all open with a header whose size reaches the ID3v1 tag or the end.
    """
    pos = offset
    end = offset + cap
    tagged = False
    while True:
        n = _id3v2_length(_read_at(stream, pos, 10))
        if not n:
            break
        pos += n
        tagged = True
    if tagged:
        while pos < end and _read_at(stream, pos, 1) == b"\x00":
            pos += 1                              # padding after the tag
    first = _mp3_frame(_read_at(stream, pos, 4))
    if first is None:
        return None
    key = first[0]
    frames = 0
    buf, buf_at = b"", pos
    while pos < end:
        if pos + 4 > buf_at + len(buf):
            buf_at, buf = pos, _read_at(stream, pos, 1 << 16)
        frame = _mp3_frame(buf[pos - buf_at:pos - buf_at + 4])
        if frame is None or frame[0] != key:
            break
        pos += frame[1]
        frames += 1
    if frames < (_MP3_MIN_TAGGED_FRAMES if tagged else _MP3_MIN_BARE_FRAMES):
        return None
    ape = _read_at(stream, pos, 32)
    if ape[:8] == b"APETAGEX" and len(ape) == 32:
        size, flags = struct.unpack("<I", ape[12:16])[0], struct.unpack("<I", ape[20:24])[0]
        if flags & 0x20000000:                    # this is the tag's header
            pos += 32 + size
    if _read_at(stream, pos, 3) == b"TAG":
        pos += 128
    return pos - offset, "parsed", "mp3", ".mp3"


def _sqlite_length(stream, offset, caps):
    """A SQLite database is its in-header page count times its page size.

    The header is the one the SQLite file format document defines (sqlite.org,
    fileformat2, section 1.3): the page size is a power of two from 512 to
    65536 (stored as 1), the read and write versions are 1 or 2, the payload
    fractions are 64, 32 and 32, the schema format is 1 to 4 (0 in a database
    with no schema yet), the text encoding 1 to 3 (or 0), and bytes 72 to 91 are
    zero. The page count at offset 28 is valid only when it is not zero and the
    change counter at 24 equals the version-valid-for number at 92 (1.3.7);
    SQLite before 3.7.0 did not keep it, and such a database is not reported.

    Measured on 8,700 databases from public phone images: in 8,669 the page
    count is valid and times the page size is exactly the file's size. 15 have
    no page count. 9 keep only their first 32 bytes in plain text and the rest
    encrypted, so the checks after byte 32 fail; they are not reported.

    A write-ahead log and a rollback journal keep copies of a database's pages,
    page 1 and its header among them, behind a frame header that opens with the
    page number (24 bytes, WAL) or a 4-byte page number (journal) (fileformat2,
    sections 4.1 and 3). Such a copy is not a database: what follows it is the
    log. Measured on two Android ext4 volumes: all 1,056 SQLite files in the
    filesystems were found at their start with their exact size, and the other
    435 header hits were all page 1 copies inside WAL files, page number 1 in the
    frame header 24 bytes before; none of the 1,056 has page number 1 there or 4
    bytes before.
    """
    if offset >= 24:
        before = _read_at(stream, offset - 24, 24)
        if before[:4] == b"\x00\x00\x00\x01" or before[20:] == b"\x00\x00\x00\x01":
            return None                           # page 1 copied into a WAL or journal
    head = _read_at(stream, offset, 100)
    if len(head) < 100:
        return None
    page = struct.unpack(">H", head[16:18])[0]
    page = 65536 if page == 1 else page
    if page < 512 or page > 65536 or page & (page - 1):
        return None
    if head[18] not in (1, 2) or head[19] not in (1, 2) or head[21:24] != b"\x40\x20\x20":
        return None
    counter, pages = struct.unpack(">II", head[24:32])
    schema, encoding = struct.unpack(">I", head[44:48])[0], struct.unpack(">I", head[56:60])[0]
    if schema > 4 or encoding > 3 or head[72:92] != bytes(20):
        return None
    if not pages or counter != struct.unpack(">I", head[92:96])[0]:
        return None
    length = pages * page
    if length > _cap(caps, "sqlite"):
        return None
    return length, "header", "sqlite", ".sqlite"


# A binary plist trailer as it opens: five unused bytes and the sort version,
# all zero, then the offset and object reference sizes, 1 to 8.
_BPLIST_TRAILER = re.compile(b"\x00{6}[\x01-\x08][\x01-\x08]", re.DOTALL)


def _bplist_length(stream, offset, cap):
    """Search forward for the trailer that describes the plist at ``offset``.

    A binary plist is "bplist00", its objects, an offset table and a 32-byte
    trailer, and its length is in the trailer, at its end: offsetTableOffset +
    numObjects x offsetIntSize + 32. The trailer layout is CFBinaryPlistTrailer
    (ForFoundationOnly.h) and the checks are Apple's own, from
    __CFBinaryPlistGetTopLevelInfo (CFBinaryPList.c, CF-1153.18): at least one
    object, the top object among them, the offset table after the header, the
    reference and offset sizes wide enough for the counts they hold, and the
    total length equal to that sum. So a candidate trailer is taken only if its
    offset table ends exactly where the trailer begins, counted from this
    header, and every offset in the table points into the object data.

    Measured on 4,818 binary plists from public phone images: all are bplist00,
    all have zero unused bytes and sort version 0, and in every one the length
    the trailer gives is exactly the file's size.
    """
    if _read_at(stream, offset, 8) != b"bplist00":
        return None
    end = offset + cap
    pos = offset + 9
    while pos < end:
        buf = _read_at(stream, pos, min(1 << 20, end - pos) + 32)
        if len(buf) < 32:
            return None
        for match in _BPLIST_TRAILER.finditer(buf, 0, len(buf) - 24):   # a whole
            # trailer can start anywhere up to 32 bytes from the end of the window
            at = pos + match.start()
            trailer = buf[match.start():match.start() + 32]
            ois, ors = trailer[6], trailer[7]
            count, top, table = struct.unpack(">QQQ", trailer[8:32])
            if count < 1 or top >= count or table < 9:
                continue
            if offset + table + count * ois != at:
                continue
            if ors < 8 and 1 << (8 * ors) <= count:
                continue
            if ois < 8 and 1 << (8 * ois) <= table:
                continue
            raw = _read_at(stream, offset + table, count * ois)
            entries = (int.from_bytes(raw[i:i + ois], "big") for i in range(0, len(raw), ois))
            if len(raw) == count * ois and all(8 <= e < table for e in entries):
                return at + 32 - offset, "parsed", "bplist", ".plist"
        if len(buf) < (1 << 20) + 32:
            return None
        pos += 1 << 20
    return None


# A data descriptor, or the record after one written without its signature.
_ZIP_AFTER_DATA = re.compile(b"PK\x07\x08|PK\x03\x04|PK\x01\x02", re.DOTALL)


def _zip_find_descriptor(stream, data_start, end, zip64=False):
    """Where an entry's data ends, for an entry that left its sizes to a data
    descriptor. Returns the position after the descriptor, or None.

    APPNOTE 6.3.10 4.3.9: the descriptor follows the data and holds the CRC and
    the compressed and uncompressed sizes behind an optional signature PK 07 08.
    The sizes are 8 bytes each when the entry has a ZIP64 extra field (4.3.9.2)
    and 4 otherwise; ``zip64`` says which, because the low half of an 8-byte
    size reads as the same 4-byte number. A candidate is taken only if the
    compressed size it records is exactly the distance from the start of the
    data, so a ZIP stored inside the entry cannot pass for its end.
    """
    width = 8 if zip64 else 4
    size_fmt = "<Q" if zip64 else "<I"
    pos = data_start
    window = 4096                                 # most entries are small: start
    while pos < end:                              # small and grow to 1 MiB
        want = min(window, end - pos)
        buf = _read_at(stream, pos, want + 24)
        if len(buf) < 16:
            return None
        for m in _ZIP_AFTER_DATA.finditer(buf, 0, len(buf) - 15):
            at = pos + m.start()
            if m.group() == b"PK\x07\x08":
                size = buf[m.start() + 8:m.start() + 8 + width]
                if len(size) == width and struct.unpack(size_fmt, size)[0] == at - data_start:
                    return at + 8 + 2 * width
            elif at - 4 - 2 * width >= data_start:    # a descriptor with no signature
                start = at - 4 - 2 * width
                size = _read_at(stream, start + 4, width)
                if struct.unpack(size_fmt, size)[0] == start - data_start:
                    return at
        if len(buf) < want + 24:
            return None
        pos += want
        window = min(window * 2, 1 << 20)
    return None


def _zip64_extra(extra):
    """The data of a local header's ZIP64 extended information field (tag 1,
    APPNOTE 4.5.3), or None when it has none."""
    i = 0
    while i + 4 <= len(extra):
        tag, size = struct.unpack("<HH", extra[i:i + 4])
        if tag == 1:
            return extra[i + 4:i + 4 + size]
        i += 4 + size
    return None


def _zip_ext(names, mimetype):
    """An extension from the names in the central directory, and from the first
    entry's content when it is a stored file named "mimetype"."""
    if mimetype:
        for prefix, ext in ((b"application/epub+zip", ".epub"),
                            (b"application/vnd.oasis.opendocument.text", ".odt"),
                            (b"application/vnd.oasis.opendocument.spreadsheet", ".ods"),
                            (b"application/vnd.oasis.opendocument.presentation", ".odp")):
            if mimetype.startswith(prefix):
                return ext
    if "AndroidManifest.xml" in names:
        return ".apk"
    if any(n.startswith("Payload/") and ".app/" in n for n in names):
        return ".ipa"
    if "[Content_Types].xml" in names:
        for folder, ext in (("word/", ".docx"), ("xl/", ".xlsx"), ("ppt/", ".pptx")):
            if any(n.startswith(folder) for n in names):
                return ext
    if "META-INF/MANIFEST.MF" in names:
        return ".jar"
    return ".zip"


def _zip_after_zeros(stream, pos):
    """The position of the next ZIP record past a run of zeros at ``pos``.

    Writers that align entries (uncompressed native libraries aligned to a page,
    say) leave zeros between one entry's data and the next local header.
    Measured: 70 APKs from public phone images had such gaps, up to 4,057 bytes.
    Up to 64 KiB of zeros is stepped over, and only to reach a local header or
    the central directory.
    """
    buf = _read_at(stream, pos, 1 << 16)
    skip = len(buf) - len(buf.lstrip(b"\x00"))
    if not skip or buf[skip:skip + 4] not in (b"PK\x03\x04", b"PK\x01\x02"):
        return None
    return pos + skip


def _zip_length(stream, offset, cap, dead=None):
    """Walk a ZIP from its first local header to the end of its central directory.

    The records are APPNOTE 6.3.10's (4.3.6 to 4.3.16): local headers and their
    data, an entry's data descriptor when bit 3 of its flags left the sizes out
    of the local header, then the central directory, an optional ZIP64 end
    record and locator, and the end of central directory record with its
    comment. The local entries are walked to find where they end; from there
    the end record is searched for, and taken only if it points back to this
    header: its central directory starts at the recorded offset, counted from
    here, runs to the end record (or the ZIP64 one), and holds as many entries
    as recorded. What lies between the last entry and the directory is not
    read, because real archives put more there than the specification says:
    an APK signing block (source.android.com, APK Signature Scheme v2), zeros
    before it, more entries after it, and entries the directory does not list.

    Measured on 7,167 ZIP-family files from public phone images (3,326 .zip,
    2,912 .apk, 717 .jar, 176 .ipa and others): in every one with an end record
    it ends the file, 2,938 carry an APK signing block, and 2,356 have entries
    whose sizes are in a data descriptor; 665,013 of those 680,264 entries put a
    size of 0 in the local header.

    ``dead`` is a set the scan keeps. A walk that fails marks every local header
    it passed through as one no other walk needs to start from, because each
    would take the same path to the same end; a header an end record points
    back to is left open. Without it an archive whose walk fails is walked
    again from every one of its entries.
    """
    end = offset + cap
    visited, starts = [], set()
    result = _zip_walk(stream, offset, end, visited, starts)
    if result is None:
        if dead is not None:
            dead.update(p for p in visited if p not in starts)
        return None
    return result


# How far past the last local entry the end record is looked for: the central
# directory, and whatever a writer put before it, has to fit in this.
_ZIP_TAIL = 64 << 20


def _zip_walk(stream, offset, end, visited, starts):
    """The walk _zip_length describes. ``starts`` collects the headers that
    end records found on the way point back to, when they are not this one."""
    pos = offset
    mimetype = None
    first = True
    while pos < end:                              # local entries
        head = _read_at(stream, pos, 30)
        if len(head) < 30 or head[:4] != b"PK\x03\x04":
            after = None if first else _zip_after_zeros(stream, pos)
            if after is None:
                break
            pos = after
            continue
        visited.append(pos)
        flags, method = struct.unpack("<HH", head[6:10])
        csize = struct.unpack("<I", head[18:22])[0]
        nlen, xlen = struct.unpack("<HH", head[26:30])
        data_start = pos + 30 + nlen + xlen
        if first:
            if _read_at(stream, pos + 30, nlen) == b"mimetype" and method == 0 and 0 < csize < 128:
                mimetype = _read_at(stream, data_start, csize)
            first = False
        if flags & 8:
            zip64 = _zip64_extra(_read_at(stream, pos + 30 + nlen, xlen)) is not None
            nxt = _zip_find_descriptor(stream, data_start, end, zip64)
            if nxt is None:
                break
            pos = nxt
        else:
            if csize == 0xFFFFFFFF:               # ZIP64: the size is in the extra field
                field = _zip64_extra(_read_at(stream, pos + 30 + nlen, xlen))
                if field is None or len(field) < 16:
                    break
                csize = struct.unpack("<Q", field[8:16])[0]
            pos = data_start + csize
    if not visited:
        return None
    limit = min(end, pos + _ZIP_TAIL)
    at = max(pos, visited[-1] + 30)
    while at < limit:                             # the end record that points back
        buf = _read_at(stream, at, min(1 << 20, limit - at) + 21)
        if len(buf) < 22:
            return None
        i = buf.find(b"PK\x05\x06")
        while 0 <= i <= len(buf) - 22:            # a whole record fits in the buffer
            got = _zip_end(stream, offset, at + i, end, starts)
            if got:
                length, names = got
                return length, "parsed", "zip", _zip_ext(names, mimetype)
            i = buf.find(b"PK\x05\x06", i + 1)
        if len(buf) < (1 << 20) + 21:
            return None
        at += 1 << 20
    return None


def _zip_end(stream, offset, eocd_at, end, starts):
    """(length, names) when the end record at ``eocd_at`` points back to
    ``offset``, or None. A record that points somewhere else adds that header
    to ``starts``."""
    eocd = _read_at(stream, eocd_at, 22)
    n_total, size, cd_off, clen = struct.unpack("<HIIH", eocd[10:22])
    dir_end = eocd_at
    if 0xFFFFFFFF in (size, cd_off) or n_total == 0xFFFF:
        loc = _read_at(stream, eocd_at - 20, 20)
        if loc[:4] != b"PK\x06\x07":
            return None
        rec_at = offset + struct.unpack("<Q", loc[8:16])[0]
        rec = _read_at(stream, rec_at, 56)
        if rec[:4] != b"PK\x06\x06" or len(rec) < 56:
            return None
        n_total, size, cd_off = struct.unpack("<QQQ", rec[32:56])
        dir_end = rec_at
    cd_start = dir_end - size
    if cd_start - cd_off != offset:
        if cd_start - cd_off >= 0:
            starts.add(cd_start - cd_off)
        return None
    names, pos, entries = set(), cd_start, 0
    while pos < dir_end:
        head = _read_at(stream, pos, 46)
        if head[:4] != b"PK\x01\x02" or len(head) < 46:
            return None
        nlen, xlen, clen2 = struct.unpack("<HHH", head[28:34])
        if len(names) < 4096:
            names.add(_read_at(stream, pos + 46, nlen).decode("utf-8", "replace"))
        pos += 46 + nlen + xlen + clen2
        entries += 1
    if pos != dir_end or entries != n_total or not entries:
        return None
    if eocd_at + 22 + clen > end:
        return None
    return eocd_at + 22 + clen - offset, names


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

def _measure(stream, offset, kind, caps, dead=None):
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
    elif kind == "iff":
        got = _iff_length(stream, offset, caps)
    elif kind == "caf":
        got = _caf_length(stream, offset, caps)
    elif kind == "mp3":
        got = _mp3_length(stream, offset, _cap(caps, kind))
    elif kind == "sqlite":
        got = _sqlite_length(stream, offset, caps)
    elif kind == "bplist":
        got = _bplist_length(stream, offset, _cap(caps, kind))
    elif kind == "zip":
        got = _zip_length(stream, offset, _cap(caps, kind), dead)
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


# The kinds a signature can turn out to be. A signature whose kinds were not asked
# for is not measured: its hit would be dropped, and one never kept can cover
# nothing, so every MP3 frame sync inside one long MP3 stream was measured to
# the stream's end. Measured on one 33.8 MB APK carved for ZIPs only: 89,932
# MP3 walks and 25 seconds, against a third of a second without them.
_SIGNATURE_KINDS = {
    "jpeg": {"jpeg"}, "png": {"png"}, "gif": {"gif"}, "riff": {"avi", "webp", "wav"},
    "bmff": {"mp4", "heic", "m4a"}, "ogg": {"ogg", "ogv"}, "amr": {"amr"},
    "iff": {"aiff"}, "caf": {"caf"}, "mp3": {"mp3"}, "sqlite": {"sqlite"},
    "bplist": {"bplist"}, "zip": {"zip"},
}


def _coverage_group(kind):
    """Which hits a hit of this kind can hide, and be hidden by.

    Media hide media: an EXIF thumbnail inside a JPEG is not a second file. A
    plist hides the plists stored in it, and a ZIP the ZIPs stored in it, but
    neither hides media, so asking for them changes no media hit. A database hides nothing and nothing hides it, because
    a blob is spread over the database's pages and its own sizes can claim the
    next file.
    """
    if kind in ("sqlite",):
        return None
    if kind in ("bplist",):
        return "bplist"
    if kind in ("zip",):
        return "zip"
    return "media"


def carve(stream, *, kinds=None, caps=None, start=0, end=None,
          nested=False, clip=False, progress=None):
    """Scan ``stream`` and yield a Candidate for every file of the kinds asked for.

    ``kinds`` says what to look for, for example ``{"jpeg", "png"}``. Left out,
    it is DEFAULT_KINDS, which is the media kinds; databases are asked for by
    name, with DATABASE_KINDS, or with ALL_KINDS. A database hides nothing found
    inside it, because media stored in a database's records are still media, and
    nothing hides a database: a picture stored as a blob is spread over the
    database's pages, so its own sizes can claim the next file. Measured: in
    8,700 real databases laid end to end, one such HEIC ran 34,095 bytes into the
    next database, over its header. ``caps`` maps a
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

    A hit inside another hit is suppressed only when the outer hit's length is a
    fact. A capped length is a ceiling, so a capped hit hides nothing: measured on
    an Android system partition, an mdat declaring 4.5 x 10^18 bytes was cut at
    the 4 GiB ceiling and hid 1,747 files that PhotoRec recovered.

    The stream is left where the scan finished. Nothing is written to it.
    """
    caps = dict(DEFAULT_CAPS, **(caps or {}))
    kinds = DEFAULT_KINDS if kinds is None else frozenset(kinds)
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    if end is None or end > size:
        end = size
    limit = end if clip else size
    pos = start
    covered = {"media": start, "bplist": start, "zip": start}   # each group's reach
    zip_dead = set()                              # local headers a failed walk passed
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
            elif sig.startswith(b"FORM"):
                kind = "iff"
            elif sig.startswith(b"caff"):
                kind = "caf"
            elif sig.startswith(b"ID3") or len(sig) == 2:
                kind = "mp3"
            elif sig.startswith(b"SQLite"):
                if "sqlite" not in kinds:
                    continue
                kind = "sqlite"
            elif sig == b"bplist00":
                if "bplist" not in kinds:
                    continue
                kind = "bplist"
            elif sig == b"PK\x03\x04":
                if "zip" not in kinds:
                    continue
                kind = "zip"
            elif sig.startswith(b"\xff\xd8"):
                kind = "jpeg"
            elif sig.startswith(b"\x89PNG"):
                kind = "png"
            else:
                kind = "gif"
            if at < start or kinds.isdisjoint(_SIGNATURE_KINDS[kind]):
                continue                          # nothing asked for can come of it
            group = _coverage_group(kind)
            if not nested and group and at < covered[group]:
                continue
            if kind == "zip" and at in zip_dead:
                continue
            hit = _measure(stream, at, kind, caps, zip_dead)
            if hit is None:
                continue
            if hit.end > limit:
                hit = hit._replace(length=limit - hit.offset, bounded="capped")
                if hit.length < MIN_LENGTHS.get(hit.kind, 1):
                    continue
            if hit.kind not in kinds:
                continue
            group = _coverage_group(hit.kind)
            if not nested and hit.bounded != "capped" and group:
                covered[group] = max(covered[group], hit.end)
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
        if part == "all":
            wanted |= set(ALL_KINDS)
        elif part == "media":
            wanted |= set(MEDIA_KINDS)
        elif part in ("databases", "database"):
            wanted |= set(DATABASE_KINDS)
        elif part in ("plists", "plist"):
            wanted |= set(PLIST_KINDS)
        elif part in ("archives", "archive"):
            wanted |= set(ARCHIVE_KINDS)
        elif part == "images":
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
        description="Recover images, video, audio and, when asked, databases from a "
                    "disk image by signature. "
                    "Read only.")
    ap.add_argument("--version", action="version", version=f"mediacarve {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("image", help="raw image, or an .E01 when ewfprobe is present")
    common.add_argument("-k", "--kinds", default=None,
                        help="what to carve: media (the default), images, video, "
                             "audio, databases, plists, archives, all, or a comma "
                             "separated "
                             "list such as jpeg,png,sqlite")
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
