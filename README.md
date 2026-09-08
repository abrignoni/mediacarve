# mediacarve

Recover images and video from any stream of bytes, by signature. One file, pure
Python, standard library only. No compiler, no network, nothing to install.

```python
import mediacarve

with open("disk.raw", "rb") as fh:
    for hit in mediacarve.carve(fh):
        print(hit.kind, hit.offset, hit.length, hit.bounded)
```

The input is anything with `seek` and `read`, so it works on a raw image, a
disk, a file inside an archive, or an E01 opened with
[ewfprobe](https://github.com/abrignoni/ewfprobe). It never writes to what it
reads.

## Why this exists

The established carvers are PhotoRec and Scalpel, both GPL. This one exists so
an MIT-licensed tool can carve without taking on a copyleft dependency or a
build toolchain, and so two different tools can share one implementation instead
of each growing their own.

## Two shapes, one scan

A tool that keeps its own index wants the **extents** and will fetch bytes on
demand. A tool that wants **files on disk** uses `extract` or the command line,
which writes a folder or a zip. Both come out of the same scan.

```
mediacarve list  disk.raw                    # count what is recoverable
mediacarve list  disk.raw -v                 # one line per file
mediacarve carve disk.raw -o carved/         # write the files out
mediacarve carve disk.raw -o carved.zip      # or into a zip
mediacarve carve disk.raw -o out/ -k images  # jpeg, png, gif, webp, heic
```

## Lengths are parsed, not guessed

Where the format allows it the extent is exact, and every hit records how its
length was arrived at:

| `bounded` | meaning |
| --- | --- |
| `header` | the file's own header or box structure gave the length |
| `parsed` | the length came from walking the file's internal structure |
| `capped` | neither was available, so the length is a ceiling and not a fact |

JPEG walks its markers rather than searching for the end-of-image marker, which
matters because an EXIF thumbnail is a whole JPEG inside the APP1 segment: the
segment's own length steps over it, so the thumbnail neither truncates the file
nor gets reported as a second one. PNG sums chunks to IEND, GIF walks blocks to
the trailer, and RIFF and ISO-BMFF read their own recorded sizes.

Files found inside another file are suppressed by default and reported with
`nested=True`. On an MJPG AVI that is the difference between one video and the
twelve JPEG frames inside it.

## What it does not do

It finds files that are **contiguous**. A fragmented file recovers only as far
as its first fragment.

It reads no filesystem, so it has no filenames, paths or timestamps to offer,
and it cannot say whether a hit was a live file, a deleted one, or a fragment of
something else. That is why carving complements a filesystem parser rather than
replacing it: carving is the floor that still works on a filesystem nobody has
written a parser for, and a parser is what turns anonymous blobs into named,
dated, path-anchored files.

Formats covered are JPEG, PNG, GIF, WebP, AVI, ISO-BMFF video (MP4, MOV, 3GP)
and ISO-BMFF stills (HEIC, HEIF, AVIF). BMP and TIFF are not covered: BMP's
signature is two bytes and TIFF's length is not in its header, so both would cost
more in false positives than they return.

## How it was validated

**Known answers.** Media written by real encoders is laid into a synthetic image
at recorded offsets, and every file must come back at the exact offset and
length with a matching SHA-256.

**Real evidence.** One GiB of a live NTFS volume, read through ewfprobe: 2,002
files recovered, and all 1,974 of the stills among them decode in Pillow. That
number is a property of that sample, not a general rate, because a carver's
false-positive rate depends on the data's own alphabet as much as on the carver.

**Rejection.** Three false-positive classes were found on that drive and are
pinned as tests:

- `ffd8ffd9` in ordinary data parsed as a complete four-byte JPEG.
- The text `hreftyp` in a web resource, where `href` read as a big-endian box
  size, became a 6.8 MB file.
- `er Iftyp` became a 1.7 GB one.

The third mattered most. Its extent suppressed everything behind it, so the same
scan reported 4 hits before the fix and 2,002 after: one false positive was
hiding two thousand real files.

**Rate.** Zeros, all-ones, random bytes, prose, JSON, base64, hex and UTF-16 are
each carved separately and must yield nothing, alongside a positive control
proving that zero can be non-zero.

**Controls.** Four deliberate breaks confirm the tests fail when they should:
accepting a JPEG with no scan segment, dropping the length floor, skipping the
ftyp validation, and finding the JPEG end by searching for `ffd9` instead of
walking markers. Each is isolated so it fails its own test rather than being
caught by a neighbouring guard.

## Tests

```
python -m pytest tests -q
```

The media fixtures are regenerated with:

```
python tools/make_fixtures.py tests/fixtures/media
```

That tool uses Pillow, piexif and OpenCV and is for development only. None of
them ships or is imported by `mediacarve`, and the test suite reads only the
committed output, so running the tests needs nothing but pytest.

## License

MIT. See `LICENSE`.
