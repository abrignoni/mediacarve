"""The window's command line re-entry and a hidden carve through it."""

import glob
import json
import os
import subprocess
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUI = os.path.join(ROOT, "mediacarve_gui.py")
MEDIA = sorted(glob.glob(os.path.join(ROOT, "tests", "fixtures", "media", "plain.*")))


def run(*args):
    return subprocess.run([sys.executable, *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=300, check=False)


def tk_display_available():
    """Ask a separate process, so this one never creates a Tk root of its own."""
    return run("-c", "import tkinter; tkinter.Tk().destroy()").returncode == 0


@pytest.fixture(name="image")
def image_fixture(tmp_path):
    path = tmp_path / "image.raw"
    with open(path, "wb") as out:
        for name in MEDIA:
            out.write(b"\0" * 4096)
            with open(name, "rb") as media:
                out.write(media.read())
        out.write(b"\0" * 4096)
    return path


def test_cli_reentry_prints_what_the_command_line_prints(image):
    direct = run(os.path.join(ROOT, "mediacarve.py"), "list", "-q", "-v", str(image))
    through = run(GUI, "--cli", "list", "-q", "-v", str(image))
    assert direct.returncode == through.returncode == 0
    assert through.stdout == direct.stdout
    assert f"total         {len(MEDIA)}" in through.stdout


@pytest.mark.parametrize("name", ["out", "out.zip"])
def test_hidden_window_carves(image, tmp_path, name):
    if not tk_display_available():
        pytest.skip("Tk cannot open a display here")
    output = tmp_path / name
    result = run(GUI, "--smoke", str(image), str(output))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"exit": 0, "written": len(MEDIA)}
    if name.endswith(".zip"):
        assert len(zipfile.ZipFile(output).namelist()) == len(MEDIA)
    else:
        assert len(os.listdir(output)) == len(MEDIA)
