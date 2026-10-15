mediacarve {{VERSION}}: standalone executables of `mediacarve.py` and its window, `mediacarve_gui.py`, built by this repository's own GitHub Actions workflow from the tagged commit.

- `mediacarve-{{VERSION}}-windows-x64.zip` runs on Intel and AMD PCs, and on Windows on ARM through its x64 emulation.
- `mediacarve-{{VERSION}}-windows-arm64.zip` is native for Windows on ARM.
- `mediacarve-{{VERSION}}-macos-arm64.zip` for Apple silicon Macs, `mediacarve-{{VERSION}}-macos-x64.zip` for Intel Macs.
- `mediacarve-{{VERSION}}-linux-x64.tar.gz` and `mediacarve-{{VERSION}}-linux-arm64.tar.gz`, built on Ubuntu 22.04 so they run on distributions with a glibc at least that old.
- `mediacarve.py` and `mediacarve_gui.py`, the tagged source files themselves.

Each archive holds the command line tool, the window (`mediacarve_gui`), `README.txt`, `LICENSE`, `LICENSE-ewfprobe` and `SHA256SUMS.txt`. The executables carry ewfprobe {{EWFPROBE}} so they read `.E01` images. `SHA256SUMS.txt` beside the archives covers the archives and the two source files.

Before they were attached, each executable was run on a raw image built from the repository's media fixtures and on an E01 from ewfprobe's fixtures, and it printed and wrote the same bytes as `python mediacarve.py`: the listings, the carved files and the zip's members. The window re-enters itself with `--cli` to run the tool; that call went through the same comparison, and the window was started and checked to stay up.

None of the executables is code signed: Windows SmartScreen and macOS Gatekeeper will each ask once, and the README inside says what to do.

`mediacarve.py` needs nothing built: `python3 mediacarve.py` works on macOS, Linux and Windows with the standard library alone. Put `ewfprobe.py` beside it to read `.E01` images.
