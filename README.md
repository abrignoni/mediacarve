# mediacarve

Recover images, video and audio from any stream of bytes, by signature. One file, pure
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

From the command line:

```bash
python3 mediacarve.py list disk.raw            # count what is recoverable
python3 mediacarve.py carve disk.raw -o out/   # write it to a folder, or to a .zip
```

## In a window

```bash
python3 mediacarve_gui.py
```

`mediacarve_gui.py` is an optional tkinter window over the same command line, standard
library only. Choose an image, optionally limit the kinds, then **Count** to list every
file found or **Carve** to write them to a folder or zip, with a progress bar. The window
runs `mediacarve.py` and shows what it prints, so the results are the same either way.

## Standalone executables

Each [release](https://github.com/abrignoni/mediacarve/releases) carries `mediacarve`
and its window `mediacarve_gui` built as single executables for Windows x64 and arm64,
macOS on Apple silicon and Intel, and Linux x64 and arm64, beside `mediacarve.py`
itself. They carry [ewfprobe](https://github.com/abrignoni/ewfprobe), so they read
`.E01` images, and its licence is in each archive. The workflow that builds them
(`.github/workflows/build-executables.yml`) requires each executable, and the window
through its `--cli`, to print and write the same bytes as `python mediacarve.py` on a
raw image built from the media fixtures and on an E01 from ewfprobe's fixtures, and
checks the window starts and stays up. The executables are not code signed; the README
inside each archive says what Windows SmartScreen and macOS Gatekeeper will ask.

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
mediacarve carve disk.raw -o out/ -k audio   # m4a, ogg, amr
```

## Lengths are parsed, not guessed

Where the format allows it the extent is exact, and every hit records how its
length was arrived at:

| `bounded` | meaning |
| --- | --- |
| `header` | the file's own header or box structure gave the length |
| `parsed` | the length came from walking the file's internal structure |
| `capped` | the extent was cut at a limit, so the length is a ceiling and not a fact |

JPEG walks its markers rather than searching for the end-of-image marker, which
matters because an EXIF thumbnail is a whole JPEG inside the APP1 segment: the
segment's own length steps over it, so the thumbnail neither truncates the file
nor gets reported as a second one. PNG sums chunks to IEND, GIF walks blocks to
the trailer, and RIFF and ISO-BMFF read their own recorded sizes.

Ogg walks its pages and checks each one's CRC, the page sequence of each stream,
and the end-of-stream flag. Not every writer sets that flag, so a file ends at the
last page that checks, and every byte reported has passed a CRC as part of the
file. A file damaged or overwritten part way comes back as the part before the
damage.

AMR records no length and has no terminator, so its frames are walked until a
byte cannot be a frame header (RFC 4867, section 5.3) or a frame is all zeros.
Bytes after the file can happen to parse as a few more frames, and a file whose
last frame is cut short takes the bytes that complete it. Laid into random bytes,
448 of 500 copies of the AMR fixture came back exactly and the longest ran 47
bytes over.

A recorded size is only what the bytes say, so three limits apply and a hit cut
at one is reported as `capped`:

- the ceiling for its kind (`caps`, 4 GiB for video by default). One ISO-BMFF
  box with a 64-bit size can declare more than any disk holds.
- the end of the stream. A header can record more than the stream has left.
- with `clip=True`, the `end` of the range scanned. Use it when scanning chosen
  ranges, such as a volume's free runs, where the bytes after a range are not the
  file's to claim. Without it a file that starts inside the range ends where its
  own structure says, and hits from two ranges can overlap.

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

## Kinds and extensions

| kind | formats | extension |
| --- | --- | --- |
| `jpeg`, `png`, `gif`, `webp` | the format of that name | `.jpg`, `.png`, `.gif`, `.webp` |
| `avi` | AVI | `.avi` |
| `mp4` | ISO-BMFF video | from the brand: `.mov`, `.3gp`, `.3g2`, `.m4v`, otherwise `.mp4` |
| `heic` | ISO-BMFF stills | from the brands: `.heic`, `.avif`, otherwise `.heif` |
| `m4a` | ISO-BMFF with an iTunes audio brand | `.m4a`, `.m4b`, `.m4p` |
| `ogg` | Ogg audio | from the codec: `.opus`, `.spx`, `.oga` (FLAC), otherwise `.ogg` |
| `ogv` | Ogg with a Theora stream | `.ogv` |
| `amr` | AMR and AMR-WB storage files | `.amr`, `.awb` |

`-k images`, `-k video` and `-k audio` select the groups. The brand is read from
the `ftyp` box against the [MP4 registration authority's list](https://github.com/mp4ra/mp4ra.github.io/blob/35c6ffec3216d1d0d50671a68509ec4ddbf3c9b5/data/brands.csv).
A brand only says what the writer declared: an audio-only file written with the
generic `mp42` brand is reported as `mp4`, and 29 of 59 distinct `.m4a` files
from two public Android images were written that way.

Single-channel AMR only. The multi-channel AMR headers, AMR inside 3GP (that is
ISO-BMFF, and carves as `mp4`), and SILK voice files (`#!SILK_V3`), which one public
Android image stores under `com.tencent.mm` with an `.amr` name, are not covered.

BMP and TIFF are not covered: BMP's signature is two bytes and TIFF's length is
not in its header, so both would cost more in false positives than they return.

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
caught by a neighbouring guard. Six more do the same for the limits above: each
of the three cuts removed, the floor after a cut removed, and the ceiling looked
up under the container's name instead of the kind's, for RIFF and for ISO-BMFF.

**Ceilings per kind.** Up to 0.1.0 the ceiling for RIFF and ISO-BMFF was looked
up under the container's name, which has none, so it fell to 1 MiB. An AVI or
WebP over 1 MiB was not reported, and an MP4 whose `moov` box follows an `mdat`
that crosses 1 MiB was reported without it. Measured on a 1.1 GiB exFAT test
volume where the scan reports 8 MP4 files: 4 came back short by exactly their
`moov` box (7,407 to 16,240 bytes) with 0.1.0, and whole with 0.2.0. The other
259 hits on that volume, and all 2,861 on two more test volumes, were the same
in both.

**Audio from real devices.** 260 distinct audio and video files taken from five
public images (`pixel3_a11`, `hickman_a13`, `galaxys10_a10`, `kevin_pocox7_a15`,
`ctf2020_ios12`) were laid into an 8.4 MiB image between runs of zeros, and again
between runs of random bytes. ffprobe named each file's format first. In zeros,
251 of the 252 that are formats mediacarve covers came back at their offset with
a matching SHA-256 and the expected kind and extension: 179 Ogg Opus, 1 Ogg
Vorbis, 30 M4A, 33 MP4, 1 3GP and 7 of 8 AMR. The eighth AMR came back one byte
long: its last frame is a 32-byte frame with 31 bytes in the file, and the byte
after it completes it. The
8 SILK files gave no hit. In random bytes the same 244 that are not AMR came back
exactly, and 6 of the 8 AMR; the other two ran 1 and 6 bytes long. Neither image
gave a hit anywhere else.

Two defects were found that way, and both are pinned as tests. Requiring an
end-of-stream page missed 9 of the 27 distinct Ogg Opus files from `hickman_a13`,
which end cleanly without one. And in random bytes, one MP4 ran 3.97 MB past its
end: the four printable bytes `sIs}` after it passed for a vendor box declaring
4,265,331,032 bytes, and the extent hid 106 of the files above. 0.2.0 reports the
same extent. A
vendor box now has to be followed by another box, or by the end of the stream.

**Against 0.2.0.** On five constructed volumes in the corpus (`takeout-exfat`,
`takeout-fat32`, `windows-ntfs`, `macos-apfs` and `android-root-ext4` from the
raw image fixtures), every extent 0.2.0 reported came back the same but one. On
`macos-apfs`, 0.2.0 reported an MP4 at 1.86 GB, cut at the end of the image,
because a 2.6 GB vendor "box" `dlm(` followed it. It is now 213,936 bytes, the sum
of its `ftyp`, `moov`, `free` and `mdat`, and the 5,121 files the long extent hid
are reported. One MP4 hit there is now `.mov`. New on the others: 58 Ogg Vorbis and one Ogg Theora video, all of
which decode in FFmpeg without an error. 21 stills are now `.avif` and decode as
AV1, and one `mp4` is now `m4a`.

**Controls.** For the audio kinds, each of these was broken in turn and made its
own test fail: the Ogg CRC check, the page sequence check, the rule that first
pages come before any other, taking a first page cut off by the end of the stream
on trust, ignoring that cut, the AMR padding bits, the AMR frame types a storage
file may carry, the all-zero frame rule, the AMR length floor, the brand to
extension mapping, reading the compatible brands, and the rule that a vendor box
is followed by a box. Ending the walk at the end-of-stream page is not one of
them: whatever follows that page ends the walk at the same byte anyway.

## Tests

```
python -m pytest tests -q
```

The media fixtures are regenerated with:

```
python tools/make_fixtures.py tests/fixtures/media
```

That tool uses Pillow, piexif, OpenCV, FFmpeg, the opencore-amr and vo-amrwbenc
encoders, and on macOS `sips`, and is for development only. `--only av` writes the
audio and other video fixtures alone and keeps the rest. None of them ships or is
imported by `mediacarve`, and the test suite reads only the
committed output, so running the tests needs nothing but pytest.

## License

MIT. See `LICENSE`.
