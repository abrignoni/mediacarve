"""Build the small media files the tests carve, and a manifest of known answers.

This is a development tool, not part of mediacarve. It uses Pillow, piexif,
OpenCV, FFmpeg, the opencore-amr and vo-amrwbenc encoders and, on macOS, sips to write real files with real
encoders, because a fixture produced by
another program is evidence about the format in a way that bytes I hand-assemble
to match my own parser is not. None of those libraries ships or is imported by
mediacarve, and the test suite reads only the committed output, so the tests
themselves need nothing but pytest.

    python tools/make_fixtures.py tests/fixtures/media
    python tools/make_fixtures.py tests/fixtures/media --only av

``--only`` writes one group (stills, opencv, av, pcm, mp3, db or plist) and keeps the manifest entries
of the others, so adding a group does not re-encode the files already committed.
The av group needs ffmpeg, the opencore-amr and vo-amrwbenc libraries for AMR
(Homebrew has both), and sips from macOS for HEIC. The pcm group needs ffmpeg and
macOS's afconvert, which writes AIFF, AIFF-C and CAF with Apple's own code.

One of the JPEGs carries an EXIF thumbnail, which is a whole JPEG inside the
APP1 segment. That is the case a carver has to get right: the thumbnail must not
be mistaken for the end of the file it lives in, and by default it should not be
reported as a separate file either.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile


def _noisy(im, seed, fraction=8):
    rnd = random.Random(seed)
    px = im.load()
    w, h = im.size
    for _ in range((w * h) // fraction):
        px[rnd.randrange(w), rnd.randrange(h)] = (
            rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
    return im


def write_media(out_dir, only=None):
    os.makedirs(out_dir, exist_ok=True)
    made = []

    def record(name, kind, ext=None):
        path = os.path.join(out_dir, name)
        data = open(path, "rb").read()
        made.append({"name": name, "kind": kind, "ext": ext or os.path.splitext(name)[1],
                     "length": len(data), "sha256": hashlib.sha256(data).hexdigest()})

    if only in (None, "stills"):
        _write_stills(out_dir, record)
    if only in (None, "opencv"):
        _write_videos(out_dir, record)
    if only in (None, "av"):
        _write_av(out_dir, record)
    if only in (None, "pcm"):
        _write_pcm(out_dir, record)
    if only in (None, "mp3"):
        _write_mp3(out_dir, record)
    if only in (None, "db"):
        _write_db(out_dir, record)
    if only in (None, "plist"):
        _write_plist(out_dir, record)
    return made


def _write_plist(out_dir, record):
    """A binary plist written by Apple's plutil from an XML plist."""
    import plistlib
    plutil = shutil.which("plutil")
    if not plutil:
        print("  plutil absent: no plist fixture written", file=sys.stderr)
        return
    with tempfile.TemporaryDirectory() as tmp:
        xml = os.path.join(tmp, "small.xml")
        rnd = random.Random(23)
        with open(xml, "wb") as fh:
            plistlib.dump({"name": "fixture", "count": 300,
                           "values": [rnd.randrange(1 << 20) for _ in range(300)],
                           "nested": {"flag": True, "when": "2026-10-16", "data": bytes(range(256))}}, fh)
        _run(plutil, "-convert", "binary1", "-o", os.path.join(out_dir, "small.plist"), xml)
    record("small.plist", "bplist")


def _write_db(out_dir, record):
    """A SQLite database written by the SQLite library Python ships."""
    import sqlite3
    path = os.path.join(out_dir, "small.sqlite")
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.execute("PRAGMA page_size = 4096")
    con.execute("PRAGMA journal_mode = DELETE")
    con.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT, picture BLOB)")
    rnd = random.Random(21)
    for i in range(40):
        con.execute("INSERT INTO notes (body) VALUES (?)", ("note %d %s" % (i, rnd.random()),))
    con.commit()
    con.close()
    record("small.sqlite", "sqlite")


def _write_mp3(out_dir, record):
    """MP3 from FFmpeg's LAME encoder: one with an ID3v2 tag and a Xing frame, one
    with neither, so a bare frame sync starts it, and an ID3v1 tag at the end.

    FFmpeg 7.1's write_id3v1 option did not write the tag here, so the 128-byte
    ID3v1 block ("TAG", title, artist and album of 30 bytes, year of 4, comment
    of 30, genre of 1) is appended to the encoder's output by this tool.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("  ffmpeg absent: no mp3 fixtures written", file=sys.stderr)
        return
    path = lambda name: os.path.join(out_dir, name)
    src = ("-f", "lavfi", "-i", "anoisesrc=duration=1:seed=13", "-ac", "1", "-ar", "22050")
    _run(ffmpeg, "-hide_banner", "-y", *src, "-c:a", "libmp3lame", "-b:a", "32k",
         "-metadata", "title=fixture", "-id3v2_version", "3", "-bitexact", path("plain.mp3"))
    record("plain.mp3", "mp3")
    _run(ffmpeg, "-hide_banner", "-y", *src, "-c:a", "libmp3lame", "-b:a", "32k",
         "-write_xing", "0", "-id3v2_version", "0", "-bitexact", path("bare.mp3"))
    id3v1 = (b"TAG" + b"fixture".ljust(30, b"\x00") + bytes(30) + bytes(30) + b"2026"
             + bytes(30) + b"\xff")
    assert len(id3v1) == 128
    with open(path("bare.mp3"), "ab") as out:
        out.write(id3v1)
    record("bare.mp3", "mp3")


def _write_pcm(out_dir, record):
    """WAV from FFmpeg; AIFF, AIFF-C and CAF from Apple's afconvert."""
    ffmpeg, afconvert = shutil.which("ffmpeg"), shutil.which("afconvert")
    if not ffmpeg:
        print("  ffmpeg absent: no pcm fixtures written", file=sys.stderr)
        return
    path = lambda name: os.path.join(out_dir, name)
    noise = ("-f", "lavfi", "-i", "anoisesrc=duration=0.5:seed=9", "-ac", "1", "-ar", "8000")
    _run(ffmpeg, "-hide_banner", "-y", *noise, "-c:a", "pcm_s16le", "-bitexact",
         "-map_metadata", "-1", path("plain.wav"))
    record("plain.wav", "wav")
    if not afconvert:
        print("  afconvert absent: no AIFF, AIFF-C or CAF fixtures written", file=sys.stderr)
        return
    for name, kind, args in (("plain.aiff", "aiff", ("-f", "AIFF", "-d", "BEI16")),
                             ("plain.aifc", "aiff", ("-f", "AIFC", "-d", "ima4")),
                             ("plain.caf", "caf", ("-f", "caff", "-d", "aac"))):
        _run(afconvert, *args, path("plain.wav"), path(name))
        record(name, kind)


def _write_stills(out_dir, record):
    from PIL import Image

    # plain JPEG
    _noisy(Image.new("RGB", (160, 120), (200, 40, 40)), 1).save(
        os.path.join(out_dir, "plain.jpg"), "JPEG", quality=85)
    record("plain.jpg", "jpeg", ".jpg")

    # JPEG carrying an EXIF thumbnail: a whole JPEG inside APP1
    big = _noisy(Image.new("RGB", (320, 240), (30, 90, 200)), 2)
    big.save(os.path.join(out_dir, "thumbed.jpg"), "JPEG", quality=88)
    try:
        import piexif
        tbuf = io.BytesIO()
        _noisy(Image.new("RGB", (64, 48), (240, 200, 20)), 3).save(
            tbuf, "JPEG", quality=70)
        exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {},
                "thumbnail": tbuf.getvalue()}
        piexif.insert(piexif.dump(exif), os.path.join(out_dir, "thumbed.jpg"))
    except ImportError:
        print("  piexif absent: thumbed.jpg carries no EXIF thumbnail", file=sys.stderr)
    record("thumbed.jpg", "jpeg", ".jpg")

    _noisy(Image.new("RGB", (120, 90), (40, 160, 60)), 4, 16).save(
        os.path.join(out_dir, "plain.png"), "PNG")
    record("plain.png", "png")

    rnd = random.Random(5)
    gif = Image.new("P", (64, 64))
    gif.putpalette([rnd.randrange(256) for _ in range(768)])
    gif.save(os.path.join(out_dir, "plain.gif"), "GIF")
    record("plain.gif", "gif")

    _noisy(Image.new("RGB", (96, 72), (10, 120, 120)), 6, 16).save(
        os.path.join(out_dir, "plain.webp"), "WEBP")
    record("plain.webp", "webp")


def _run(*cmd):
    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _load_lib(name):
    import ctypes
    import ctypes.util
    found = ctypes.util.find_library(name)
    for candidate in filter(None, (found, f"/opt/homebrew/lib/lib{name}.dylib",
                                   f"/usr/local/lib/lib{name}.dylib", f"lib{name}.so")):
        try:
            return ctypes.CDLL(candidate)
        except OSError:
            continue
    raise OSError(f"lib{name} not found (Homebrew: opencore-amr, vo-amrwbenc)")


def _amr_nb(pcm):
    """Encode with opencore-amr's AMR-NB encoder, which writes storage frames."""
    import ctypes
    lib = _load_lib("opencore-amrnb")
    lib.Encoder_Interface_init.restype = ctypes.c_void_p
    lib.Encoder_Interface_Encode.argtypes = (ctypes.c_void_p, ctypes.c_int,
                                             ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int)
    state = lib.Encoder_Interface_init(1)                 # discontinuous transmission on
    out, frame = bytearray(), ctypes.create_string_buffer(64)
    for i in range(0, len(pcm) - 319, 320):               # 160 samples, 20 ms
        n = lib.Encoder_Interface_Encode(state, 7, pcm[i:i + 320], frame, 0)   # 12.2 kbps
        out += frame.raw[:n]
    lib.Encoder_Interface_exit(ctypes.c_void_p(state))
    return bytes(out)


def _amr_wb(pcm):
    """Encode with VisualOn's AMR-WB encoder, which writes storage frames."""
    import ctypes
    lib = _load_lib("vo-amrwbenc")
    lib.E_IF_init.restype = ctypes.c_void_p
    lib.E_IF_encode.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                ctypes.c_char_p, ctypes.c_int)
    state = lib.E_IF_init()
    out, frame = bytearray(), ctypes.create_string_buffer(64)
    for i in range(0, len(pcm) - 639, 640):               # 320 samples, 20 ms
        n = lib.E_IF_encode(state, 2, pcm[i:i + 640], frame, 1)               # 12.65 kbps, DTX
        out += frame.raw[:n]
    lib.E_IF_exit(ctypes.c_void_p(state))
    return bytes(out)


def _write_av(out_dir, record):
    """Audio, and video in the ISO-BMFF brands other than mp4, from real encoders.

    Each file is labelled by its container's own brand, codec or header, so the
    name, kind and extension recorded here are what the carver must report.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("  ffmpeg absent: no av fixtures written", file=sys.stderr)
        return
    ff = (ffmpeg, "-hide_banner", "-y")
    tone = ("-f", "lavfi", "-i", "sine=frequency=440:duration=1")
    noise = ("-f", "lavfi", "-i", "anoisesrc=duration=1:seed=7")
    picture = ("-f", "lavfi", "-i", "testsrc=size=128x96:rate=10:duration=1")

    def path(name):
        return os.path.join(out_dir, name)

    def dest(name):                           # output options, then the file
        return ("-bitexact", "-map_metadata", "-1", path(name))

    _run(*ff, *picture, "-c:v", "mpeg4", "-f", "mov", *dest("plain.mov"))
    record("plain.mov", "mp4")
    _run(*ff, *picture, *tone, "-c:v", "h263", "-c:a", "aac", "-ac", "1", "-ar", "8000",
         "-f", "3gp", *dest("plain.3gp"))
    record("plain.3gp", "mp4")
    _run(*ff, *noise, "-c:a", "aac", "-b:a", "32k", "-f", "ipod", *dest("plain.m4a"))
    record("plain.m4a", "m4a")
    _run(*ff, *noise, "-c:a", "libopus", "-b:a", "24k", "-f", "ogg", *dest("plain.opus"))
    record("plain.opus", "ogg")
    _run(*ff, *noise, "-c:a", "vorbis", "-strict", "-2", "-ac", "2", "-f", "ogg",
         *dest("plain.ogg"))
    record("plain.ogg", "ogg")

    sips = shutil.which("sips")
    for name, rate, magic, encode in (("plain.amr", 8000, b"#!AMR\n", _amr_nb),
                                      ("plain.awb", 16000, b"#!AMR-WB\n", _amr_wb)):
        # a second of noise, then half a second of silence, so the encoder's
        # discontinuous transmission writes SID and NO_DATA frames as well
        pcm = subprocess.run((*ff, *noise, "-af", "apad=pad_dur=0.5", "-ac", "1",
                              "-ar", str(rate), "-f", "s16le", "-"),
                             check=True, capture_output=True, stdin=subprocess.DEVNULL).stdout
        try:
            frames = encode(pcm)
        except OSError as exc:
            print(f"  no {name} written: {exc}", file=sys.stderr)
            continue
        with open(path(name), "wb") as out:
            out.write(magic + frames)
        record(name, "amr")

    with tempfile.TemporaryDirectory() as tmp:
        if sips:
            png = os.path.join(tmp, "still.png")
            _run(*ff, "-f", "lavfi", "-i", "testsrc=size=160x120", "-frames:v", "1", png)
            _run(sips, "-s", "format", "heic", png, "--out", path("plain.heic"))
            record("plain.heic", "heic")
        else:
            print("  sips absent: no HEIC fixture written", file=sys.stderr)

    try:
        from PIL import Image
        _noisy(Image.new("RGB", (96, 72), (90, 30, 150)), 8, 16).save(
            path("plain.avif"), "AVIF")
        record("plain.avif", "heic")
    except (ImportError, KeyError, OSError) as exc:
        print(f"  no AVIF fixture written: {exc}", file=sys.stderr)


def _write_videos(out_dir, record):
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("  OpenCV absent: no mp4 or avi fixture written", file=sys.stderr)
        return
    rnd = np.random.default_rng(7)
    for name, fourcc, kind in (("plain.mp4", "mp4v", "mp4"),
                               ("plain.avi", "MJPG", "avi")):
        path = os.path.join(out_dir, name)
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*fourcc), 10.0, (96, 64))
        if not writer.isOpened():
            print(f"  could not open a writer for {name}", file=sys.stderr)
            continue
        for _ in range(12):
            writer.write(rnd.integers(0, 256, (64, 96, 3), dtype="uint8"))
        writer.release()
        if os.path.exists(path) and os.path.getsize(path) > 0:
            record(name, kind)
        else:
            print(f"  {name} came out empty", file=sys.stderr)


def main(argv):
    only = None
    if len(argv) == 4 and argv[2] == "--only" and argv[3] in ("stills", "opencv", "av", "pcm", "mp3", "db", "plist"):
        only = argv[3]
    elif len(argv) != 2:
        print(__doc__)
        return 2
    out = os.path.abspath(argv[1])
    made = write_media(out, only)
    if only:
        names = {m["name"] for m in made}
        with open(os.path.join(out, "manifest.json"), encoding="utf-8") as fh:
            kept = [m for m in json.load(fh)["media"] if m["name"] not in names]
        for m in kept:                        # manifests before 0.3.0 had no ext
            m.setdefault("ext", os.path.splitext(m["name"])[1])
        made = kept + made
    manifest = {"media": made}
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    total = sum(m["length"] for m in made)
    for m in made:
        print(f"  {m['name']:<14} {m['kind']:<5} {m['length']:>8,} bytes")
    print(f"\n{len(made)} files, {total:,} bytes, manifest written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
