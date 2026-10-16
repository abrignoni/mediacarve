#!/usr/bin/env python3
"""mediacarve GUI: a tkinter window over mediacarve.py.

Standard library only, like the tool it drives. The window runs the command line
tool as a subprocess with the same options it takes, so what it shows is what the
command line prints:

  Count     runs ``list --verbose`` and shows one row per file found, with the totals
            per kind below.
  Carve     runs ``carve`` into the output folder, or into a zip when the output
            name ends in .zip, and lists what was written.

The progress bar follows the percentage mediacarve prints on stderr. The image is
only read.

    python3 mediacarve_gui.py                      open the window
    python3 mediacarve_gui.py IMAGE                open it with the image chosen
    python3 mediacarve_gui.py --cli ...            run the command line tool; this is
                                                   how the window starts it, and what
                                                   makes one frozen executable work
    python3 mediacarve_gui.py --smoke IMAGE OUTPUT no visible window: build it, carve
                                                   through it, print what it listed,
                                                   and exit
"""

import json
import os
import queue
import re
import runpy
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mediacarve  # noqa: E402  pylint: disable=wrong-import-position

TITLE = "mediacarve"
KINDS = ("media", "images", "video", "audio", "databases", "plists", "archives", "all")
PERCENT = re.compile(r"(\d+(?:\.\d+)?)%")


def probe_command():
    """How to start the command line tool as a subprocess (see run_cli)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--cli"]
    return [sys.executable, os.path.abspath(__file__), "--cli"]


def run_cli(argv):
    """Run mediacarve's command line with these arguments, in this process."""
    sys.argv = ["mediacarve.py"] + list(argv)
    try:
        runpy.run_module("mediacarve", run_name="__main__", alter_sys=False)
    except SystemExit as done:
        return done.code if isinstance(done.code, int) else (0 if done.code is None else 1)
    return 0


def open_path(path):
    """Open a file or folder with the system's default application."""
    if sys.platform.startswith("win"):
        os.startfile(path)  # pylint: disable=no-member
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])  # pylint: disable=consider-using-with
    else:
        subprocess.Popen(["xdg-open", path])  # pylint: disable=consider-using-with


def build_window(initial_image="", smoke=None):
    """Build the window. With ``smoke`` = (image, output) it carves once, hidden."""
    import tkinter as tk  # pylint: disable=import-outside-toplevel
    from tkinter import filedialog, messagebox, ttk  # pylint: disable=import-outside-toplevel

    root = tk.Tk()
    root.title(f"{TITLE} {mediacarve.__version__}")
    root.geometry("900x620")
    root.minsize(700, 460)
    if smoke:
        root.attributes("-alpha", 0.0)
    state = {"proc": None, "streams": 0, "mode": None, "result": None}
    lines = queue.Queue()

    form = ttk.Frame(root, padding=8)
    form.pack(fill="x")
    v_image = tk.StringVar(value=initial_image)
    v_output = tk.StringVar()
    v_kinds = tk.StringVar(value="media")
    v_nested = tk.BooleanVar(value=False)

    ttk.Label(form, text="Image").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=2)
    ttk.Entry(form, textvariable=v_image).grid(row=0, column=1, sticky="ew", pady=2)
    ttk.Button(form, text="Browse...", command=lambda: v_image.set(
        filedialog.askopenfilename(title="Image") or v_image.get())).grid(row=0, column=2, padx=(6, 0))
    ttk.Label(form, text="Output").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=2)
    ttk.Entry(form, textvariable=v_output).grid(row=1, column=1, sticky="ew", pady=2)
    out_buttons = ttk.Frame(form)
    out_buttons.grid(row=1, column=2, padx=(6, 0), sticky="w")
    ttk.Button(out_buttons, text="Folder...", command=lambda: v_output.set(
        filedialog.askdirectory(title="Output folder") or v_output.get())).pack(side="left")
    ttk.Button(out_buttons, text="Zip...", command=lambda: v_output.set(
        filedialog.asksaveasfilename(title="Output zip", defaultextension=".zip",
                                     filetypes=[("Zip", "*.zip")]) or v_output.get())).pack(side="left", padx=(4, 0))
    options = ttk.Frame(form)
    options.grid(row=2, column=1, sticky="w", pady=(4, 0))
    ttk.Label(options, text="Kinds").pack(side="left")
    ttk.Combobox(options, textvariable=v_kinds, values=KINDS, width=24).pack(side="left", padx=(6, 12))
    ttk.Label(options, text="(or a list such as jpeg,png,mp4)", foreground="gray").pack(side="left")
    ttk.Checkbutton(options, text="Also files inside other files (EXIF thumbnails)",
                    variable=v_nested).pack(side="left", padx=(12, 0))
    form.columnconfigure(1, weight=1)

    buttons = ttk.Frame(root, padding=(8, 0))
    buttons.pack(fill="x")
    b_count = ttk.Button(buttons, text="Count")
    b_carve = ttk.Button(buttons, text="Carve")
    b_cancel = ttk.Button(buttons, text="Cancel", state="disabled")
    b_open = ttk.Button(buttons, text="Open output", state="disabled")
    for button in (b_count, b_carve, b_cancel, b_open):
        button.pack(side="left", padx=(0, 6))

    status_bar = ttk.Frame(root, padding=(8, 6))
    status_bar.pack(fill="x")
    progress = ttk.Progressbar(status_bar, mode="determinate", maximum=100)
    progress.pack(side="left", fill="x", expand=True)
    status = ttk.Label(status_bar, text="Choose an image.", width=40)
    status.pack(side="left", padx=(8, 0))

    log = tk.Text(root, wrap="none", state="disabled")
    log.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def write(text):
        log.configure(state="normal")
        log.insert("end", text)
        log.see("end")
        log.configure(state="disabled")

    def pump(stream, tag):
        # mediacarve rewrites its progress line with \r, so split on both endings.
        buffer = b""
        for chunk in iter(lambda: stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096), b""):
            buffer += chunk
            parts = re.split(rb"\r|\n", buffer)
            buffer = parts.pop()
            for part in parts:
                lines.put((tag, part.decode("utf-8", "replace")))
        if buffer:
            lines.put((tag, buffer.decode("utf-8", "replace")))
        lines.put((tag, None))

    def start(mode):
        image = v_image.get().strip()
        if not os.path.exists(image):
            messagebox.showinfo(TITLE, "Choose an image first.")
            return
        args = probe_command() + (["list", "--verbose"] if mode == "count" else ["carve"])
        kinds = v_kinds.get().strip()
        if kinds and kinds != "media":
            args += ["--kinds", kinds]
        if v_nested.get():
            args.append("--nested")
        if mode == "carve":
            output = v_output.get().strip()
            if not output:
                messagebox.showinfo(TITLE, "Choose an output folder or zip first.")
                return
            if os.path.exists(output) and (output.lower().endswith(".zip") or os.listdir(output)):
                messagebox.showerror(TITLE, f"Choose a new or empty output:\n{output}")
                return
            args += ["-o", output]
            state["output"] = output
        args.append(image)
        log.configure(state="normal")
        log.delete("1.0", "end")
        log.configure(state="disabled")
        shown = ["mediacarve.py"] + args[args.index("--cli") + 1:]
        write("$ " + " ".join(a if " " not in a else f'"{a}"' for a in shown) + "\n\n")
        state.update(mode=mode, written=0)
        progress["value"] = 0
        status["text"] = "running..."
        for button in (b_count, b_carve, b_open):
            button["state"] = "disabled"
        b_cancel["state"] = "normal"
        try:
            state["proc"] = subprocess.Popen(  # pylint: disable=consider-using-with
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        except OSError as error:
            messagebox.showerror(TITLE, f"Could not start mediacarve: {error}")
            finish(None)
            return
        state["streams"] = 2
        threading.Thread(target=pump, args=(state["proc"].stdout, "out"), daemon=True).start()
        threading.Thread(target=pump, args=(state["proc"].stderr, "err"), daemon=True).start()
        root.after(50, poll)

    def poll():
        try:
            while True:
                tag, line = lines.get_nowait()
                if line is None:
                    state["streams"] -= 1
                    continue
                match = PERCENT.search(line) if tag == "err" else None
                if match:
                    progress["value"] = float(match.group(1))
                    status["text"] = line.strip()
                elif line.strip():
                    write(line + "\n")
                    match_written = re.match(r"wrote (\d+) file", line)
                    if match_written:
                        state["written"] = int(match_written.group(1))
        except queue.Empty:
            pass
        proc = state["proc"]
        if proc is not None and (proc.poll() is None or state["streams"] > 0):
            root.after(50, poll)
            return
        finish(proc.returncode if proc is not None else None)

    def finish(code):
        for button in (b_count, b_carve):
            button["state"] = "normal"
        b_cancel["state"] = "disabled"
        state["proc"] = None
        if code == 0:
            progress["value"] = 100
            status["text"] = "done" + (f": {state['written']:,} files written" if state["mode"] == "carve" else "")
            if state["mode"] == "carve":
                b_open["state"] = "normal"
        elif code is None:
            status["text"] = "not started"
        elif code < 0:
            status["text"] = "cancelled"
        else:
            status["text"] = f"mediacarve exited {code}"
        state["result"] = (code, state.get("written", 0))
        if smoke:
            root.after(10, root.destroy)

    def cancel():
        proc = state["proc"]
        if proc is not None and proc.poll() is None:
            proc.terminate()
            write("\n[cancelled: the output holds only what was written before the cancel]\n")

    def open_output():
        output = state.get("output", "")
        if output:
            open_path(output if os.path.isdir(output) else os.path.dirname(os.path.abspath(output)))

    b_count.configure(command=lambda: start("count"))
    b_carve.configure(command=lambda: start("carve"))
    b_cancel.configure(command=cancel)
    b_open.configure(command=open_output)

    def on_close():
        cancel()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    if smoke:
        v_image.set(smoke[0])
        v_output.set(smoke[1])
        root.after(100, lambda: start("carve"))
    root.mainloop()
    return state["result"]


def main(argv):
    if argv and argv[0] == "--cli":
        return run_cli(argv[1:])
    if argv and argv[0] == "--smoke":
        if len(argv) != 3:
            print("usage: mediacarve_gui.py --smoke IMAGE OUTPUT", file=sys.stderr)
            return 2
        code, written = build_window(smoke=(os.path.abspath(argv[1]), os.path.abspath(argv[2]))) or (None, 0)
        print(json.dumps({"exit": code, "written": written}))
        return 0 if code == 0 else 1
    build_window(os.path.abspath(argv[0]) if argv and os.path.exists(argv[0]) else "")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
