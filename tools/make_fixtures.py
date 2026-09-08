"""Build the small media files the tests carve, and a manifest of known answers.

This is a development tool, not part of mediacarve. It uses Pillow, piexif and
OpenCV to write real files with real encoders, because a fixture produced by
another program is evidence about the format in a way that bytes I hand-assemble
to match my own parser is not. None of those libraries ships or is imported by
mediacarve, and the test suite reads only the committed output, so the tests
themselves need nothing but pytest.

    python tools/make_fixtures.py tests/fixtures/media

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
import sys


def _noisy(im, seed, fraction=8):
    rnd = random.Random(seed)
    px = im.load()
    w, h = im.size
    for _ in range((w * h) // fraction):
        px[rnd.randrange(w), rnd.randrange(h)] = (
            rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
    return im


def write_media(out_dir):
    from PIL import Image
    os.makedirs(out_dir, exist_ok=True)
    made = []

    def record(name, kind):
        path = os.path.join(out_dir, name)
        data = open(path, "rb").read()
        made.append({"name": name, "kind": kind, "length": len(data),
                     "sha256": hashlib.sha256(data).hexdigest()})

    # plain JPEG
    _noisy(Image.new("RGB", (160, 120), (200, 40, 40)), 1).save(
        os.path.join(out_dir, "plain.jpg"), "JPEG", quality=85)
    record("plain.jpg", "jpeg")

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
    record("thumbed.jpg", "jpeg")

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

    _write_videos(out_dir, record)
    return made


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
    if len(argv) != 2:
        print(__doc__)
        return 2
    out = os.path.abspath(argv[1])
    made = write_media(out)
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
