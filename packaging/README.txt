mediacarve, built as standalone executables
===========================================

  mediacarve          the command line tool (mediacarve.exe on Windows). Open a
                      terminal in this folder and run, for example:
                          mediacarve list  disk.raw
                          mediacarve carve disk.raw -o out/
                          mediacarve carve image.E01 -o out.zip
                          mediacarve --help
                      On Windows write mediacarve.exe; on macOS and Linux write
                      ./mediacarve from this folder. It reads a raw image or an
                      EnCase .E01 (through the ewfprobe it carries) and finds
                      images and video by signature.

  mediacarve_gui      the same tool in a window (mediacarve_gui.exe on Windows,
                      mediacarve_gui.app on macOS). Choose an image, then Count
                      to list what it holds or Carve to write it to a folder or
                      zip, with a progress bar.

  SHA256SUMS.txt      the hashes of the executables as built. Check them with
                          certutil -hashfile mediacarve.exe SHA256     (Windows)
                          shasum -a 256 -c SHA256SUMS.txt              (macOS)
                          sha256sum -c SHA256SUMS.txt                  (Linux)

  LICENSE             mediacarve's licence. LICENSE-ewfprobe is the licence of
                      ewfprobe (https://github.com/abrignoni/ewfprobe), which the
                      executables carry to read .E01 images.

Both read the image read-only and write only to the output folder or zip you
choose. They install nothing and need no administrator rights. They are built from
mediacarve.py and mediacarve_gui.py in https://github.com/abrignoni/mediacarve by
the repository's own GitHub Actions workflow, and are not code signed:

  Windows   SmartScreen may ask once before running them. Unzip to a local
            folder rather than running from a network share.
  macOS     Gatekeeper will refuse a downloaded, unsigned program on first
            run. Right-click (Control-click) it in Finder and choose Open,
            once, or remove the quarantine mark from this folder:
                xattr -dr com.apple.quarantine .
  Linux     mark the files executable if the archive did not keep the bit:
                chmod +x mediacarve mediacarve_gui

Builds: windows-x64, windows-arm64 (native for Windows on ARM; the x64 build
also runs there through emulation), macos-arm64 (Apple silicon), macos-x64
(Intel), linux-x64, linux-arm64.
