"""Check that a built mediacarve gives the same output as the Python file it was built from.

    python tools/smoke_frozen.py <source folder> <ewfprobe checkout> <executable> [<executable args> ...]

<source folder> is a checkout holding mediacarve.py, ewfprobe.py copied beside it, and
tests/fixtures (the build workflow checks out the tag being released there).
<ewfprobe checkout> is the ewfprobe repository at the release the executables bundle.

A raw image is built from the media fixtures, each file between runs of zero bytes. Then
`list -q -v`, `carve -o <folder>` and `carve -o <file>.zip` are run once through
`python mediacarve.py` and once through the executable: the listings must be identical,
the carved files byte-identical, and the zip members the same names and bytes. To prove
the executable carries ewfprobe, `list -q -v` is also run on an E01 from ewfprobe's own
fixtures, written by EnCase, and must print what the source prints. The executable's
--version must name the source's __version__.
"""

from __future__ import annotations

import glob
import hashlib
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
    if r.returncode != 0:
        sys.exit(f"FAILED ({r.returncode}): {' '.join(cmd)}\n{r.stdout[-2000:]}\n{r.stderr[-4000:]}")
    return r.stdout


def tree(folder: Path) -> dict[str, str]:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def members(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {name: hashlib.sha256(archive.read(name)).hexdigest() for name in sorted(archive.namelist())}


def main(src: Path, ewf: Path, exe: list[str]) -> int:
    py = [sys.executable, str(src / "mediacarve.py")]
    source_version, built_version = run(py + ["--version"]).strip(), run(exe + ["--version"]).strip()
    print("source:", source_version, "| built:", built_version)
    assert built_version == source_version, (built_version, source_version)
    media = sorted(glob.glob(str(src / "tests" / "fixtures" / "media" / "plain.*")))
    e01 = ewf / "tests" / "fixtures" / "encase5-fast.E01"
    assert len(media) >= 6 and e01.is_file(), (media, e01)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        image = work / "image.raw"
        with open(image, "wb") as out:
            for name in media:
                out.write(b"\0" * 4096)
                out.write(Path(name).read_bytes())
            out.write(b"\0" * 4096)
        results = {}
        for label, cmd in (("source", py), ("executable", exe)):
            results[label] = (
                run(cmd + ["list", "-q", "-v", str(image)]),
                run(cmd + ["list", "-q", "-v", str(e01)]),
            )
            run(cmd + ["carve", "-q", str(image), "-o", str(work / label)])
            run(cmd + ["carve", "-q", str(image), "-o", str(work / f"{label}.zip")])
        if results["executable"] != results["source"]:
            sys.exit("the executable's listings differ from the source's")
        want, got = tree(work / "source"), tree(work / "executable")
        if want != got:
            sys.exit(f"carved files differ: {sorted(set(want.items()) ^ set(got.items()))}")
        if members(work / "source.zip") != members(work / "executable.zip"):
            sys.exit("zip members differ")
        assert len(want) == len(media), (len(want), len(media))
    print(f"the executable wrote the same bytes as the source: {len(want)} carved files, "
          "the zip, and both listings, the E01's read through the bundled ewfprobe")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    executable = [str(Path(sys.argv[3]).resolve()) if os.path.exists(sys.argv[3]) else sys.argv[3], *sys.argv[4:]]
    sys.exit(main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), executable))
