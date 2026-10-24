"""A small window for porting a game without the command line."""

import importlib.util
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path

from . import __version__, bcl, bundled, config, report, version, wsl

# Log colours, by the prefix of the block's first line
TAGS = {"Warning": "#b35900", "Note": "#1f62b0", "Error": "#c42b1c", "Traceback": "#c42b1c"}


def available() -> bool:
    if os.name != "nt" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    return importlib.util.find_spec("tkinter") is not None


class _QueueWriter:
    """Stands in for stdout while a port runs, passing the text to the window."""

    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, text: str) -> int:
        self.q.put(("log", text))
        return len(text)

    def flush(self) -> None:
        pass


def _native_folder_dialog(title: str, start: str) -> list[str] | None:
    """Command for the desktop's own folder picker on Linux, if one is installed.
    Tk's built-in dialog is used otherwise (and on Windows, where Tk's is the native one)."""
    if os.name == "nt":
        return None
    kde = "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    for tool in (["kdialog", "zenity"] if kde else ["zenity", "kdialog"]):
        if shutil.which(tool):
            if tool == "kdialog":
                return [tool, "--getexistingdirectory", start, "--title", title]
            return [tool, "--file-selection", "--directory", f"--title={title}",
                    f"--filename={os.path.join(start, '')}"]
    return None


def _game_info(game_dir: str) -> tuple[str, str | None]:
    """A line describing the chosen game folder, and its Unity version if it needs the
    editor's Linux class libraries."""
    if not game_dir or not Path(game_dir).is_dir():
        return "", None
    try:
        data_dir = version.find_data_dir(Path(game_dir))
    except FileNotFoundError:
        return "No <Game>_Data or Data folder here; choose the folder that contains the game's .exe.", None
    ver = version.version_from_data(data_dir)
    if not ver:
        return "This doesn't look like a Unity game.", None
    if (Path(game_dir) / "GameAssembly.dll").exists():
        return f"Unity {ver}, built with IL2CPP, which unityport can't port.", None
    return f"Unity {ver}", ver if bcl.needs_linux_bcl(data_dir / "Managed") else None


def main() -> None:
    import tkinter as tk
    from tkinter import filedialog, font, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    from .__main__ import port

    root = tk.Tk()
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        TkinterDnD._require(root)
    except (ImportError, RuntimeError, tk.TclError):
        DND_FILES = None
    root.title("unityport")
    root.minsize(680, 460)
    menubar = tk.Menu(root)
    menubar.add_command(label="Advanced", command=lambda: show_advanced())
    help_menu = tk.Menu(menubar, tearoff=False)
    help_menu.add_command(label="Report a problem...", command=lambda: show_report())
    help_menu.add_separator()
    help_menu.add_command(label="About unityport", command=lambda: show_about())
    menubar.add_cascade(label="Help", menu=help_menu)
    root.configure(menu=menubar)
    frame = ttk.Frame(root, padding=10)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)
    frame.rowconfigure(5, weight=1)

    game = tk.StringVar()
    output = tk.StringVar()
    editor = tk.StringVar()
    info = tk.StringVar()
    auto = {"output": "", "editor": ""}  # values last filled in automatically

    def shown(text: str) -> str:
        """A path as the fields show it: converted if it is in the other side's format, and
        with backslashes on Windows, where Tk's folder dialog returns forward slashes."""
        p = wsl.normalize(text)
        return os.path.normpath(p) if os.name == "nt" and p else p

    def path(var) -> str:
        """A field's path, converted if it was typed in the other side's format."""
        return shown(var.get())

    # Places to browse, as (menu label, default start folder, whether a path is in that place).
    # Under WSL, Browse offers both the Windows and the Linux side.
    if os.name == "nt" and wsl.preferred_distros():
        places = [("Windows folders...", lambda: str(Path.home()), lambda p: not p.startswith("\\\\"))]
        for d in wsl.preferred_distros():
            prefixes = (f"\\\\wsl.localhost\\{d}".lower(), f"\\\\wsl$\\{d}".lower())
            places.append((f"Linux folders ({d})...", lambda d=d: wsl.linux_start(d),
                           lambda p, prefixes=prefixes: p.lower().startswith(prefixes)))
    elif wsl.in_wsl():
        places = [("Windows folders...", wsl.windows_start, lambda p: p.startswith("/mnt/")),
                  ("Linux folders...", lambda: str(Path.home()), lambda p: not p.startswith("/mnt/"))]
    else:
        places = [("Browse...", lambda: str(Path.home()), lambda p: True)]

    def browse_row(parent, row, label, var, title):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        entry = ttk.Entry(parent, textvariable=var)
        entry.grid(row=row, column=1, sticky="ew", padx=6)

        def tidy(*_):
            if var.get() != path(var):
                var.set(path(var))

        # Convert pasted paths straight away, and typed ones on leaving the field
        entry.bind("<<Paste>>", lambda e: root.after_idle(tidy))
        entry.bind("<FocusOut>", tidy)
        if DND_FILES:
            entry.drop_target_register(DND_FILES)
            entry.dnd_bind("<<Drop>>", lambda e: drop(var, e.data))

        def browse(place):
            _, default, contains = place
            start = path(var) or path(game)
            if not (start and contains(start) and Path(start).is_dir()):
                start = default()
            command = _native_folder_dialog(title, start)
            if command is None:
                chosen = filedialog.askdirectory(title=title, initialdir=start)
                if chosen:
                    var.set(shown(chosen))
                return

            # In a thread, so the window keeps redrawing while the dialog is open
            def pick():
                r = subprocess.run(command, capture_output=True, text=True, env=bundled.host_env())
                if r.returncode == 0 and r.stdout.strip():
                    events.put(("pick", (var, r.stdout.strip())))

            threading.Thread(target=pick, daemon=True).start()

        if len(places) == 1:
            ttk.Button(parent, text="Browse...", command=lambda: browse(places[0])).grid(row=row, column=2)
        else:
            button = ttk.Menubutton(parent, text="Browse...")
            menu = tk.Menu(button, tearoff=False)
            for place in places:
                menu.add_command(label=place[0], command=lambda place=place: browse(place))
            button["menu"] = menu
            button.grid(row=row, column=2)

    def drop(var, data):
        """A folder dropped on a field. A dropped file stands for its folder (e.g. Game.exe)."""
        items = root.tk.splitlist(data)
        if not items:
            return
        p = shown(items[0])
        if Path(p).is_file():
            p = str(Path(p).parent)
        var.set(p)

    browse_row(frame, 0, "Windows game folder", game, "Folder containing <Game>.exe and <Game>_Data")
    ttk.Label(frame, textvariable=info, foreground="gray").grid(row=1, column=1, sticky="w", padx=6)
    browse_row(frame, 2, "Output folder", output, "Folder for the Linux build")

    # Only shown for games that need the editor's class libraries
    editor_box = ttk.LabelFrame(frame, text="Unity editor (optional)", padding=8)
    editor_box.columnconfigure(1, weight=1)
    editor_text = ttk.Label(editor_box, wraplength=620, justify="left")
    editor_text.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
    browse_row(editor_box, 1, "Editor folder", editor, "Unity editor install of the game's version")
    editor_status = ttk.Label(editor_box, wraplength=620, justify="left")
    editor_status.grid(row=2, column=1, columnspan=2, sticky="w", padx=6)

    def on_editor(*_):
        if not editor.get():
            text, colour = "", "gray"
        elif not bcl.has_profile(Path(path(editor))):
            text, colour = "This folder isn't a Unity editor install of 2021.2 or newer.", TAGS["Error"]
        elif editor.get() == auto["editor"]:
            text, colour = ("Filled in from Unity Hub's install folder. The class libraries will be copied "
                            "from this editor instead of downloaded."), "gray"
        else:
            text, colour = "The class libraries will be copied from this editor instead of downloaded.", "gray"
        editor_status.configure(text=text, foreground=colour)

    editor.trace_add("write", on_editor)

    def on_game(*_):
        game_dir = Path(path(game))
        if game.get() and output.get() in ("", auto["output"]):
            auto["output"] = str(game_dir.with_name(game_dir.name + "-linux"))
            output.set(auto["output"])
        text, needs_editor = _game_info(str(game_dir) if game.get() else "")
        info.set(text)
        if not needs_editor:
            editor_box.grid_remove()
            return
        if (config.CACHE / needs_editor / "unityjit-linux" / "mscorlib.dll").is_file():
            editor_box.grid_remove()  # saved from an earlier port
            return
        found = bcl.hub_editor(needs_editor)
        example = found or (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Unity" / "Hub" / "Editor"
                            if os.name == "nt" else Path.home() / "Unity" / "Hub" / "Editor") / needs_editor
        editor_text.configure(text=(
            "Games made with Unity 2021.2 or newer need Linux versions of their .NET class libraries "
            "(mscorlib.dll and others). Only the Unity editor includes them, so they are downloaded from "
            f"Unity: about 600 MB, once per Unity version. To skip the download, choose an install of "
            f"Unity {needs_editor} for any OS, such as {example}."))
        if editor.get() in ("", auto["editor"]):
            auto["editor"] = str(found or "")
            editor.set(auto["editor"])
        on_editor()
        editor_box.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))

    game.trace_add("write", on_game)

    controls = ttk.Frame(frame)
    controls.grid(row=4, column=0, columnspan=3, sticky="ew", pady=8)
    open_button = ttk.Button(controls, text="Open output folder", state="disabled")
    open_button.pack(side="right")
    port_button = ttk.Button(controls, text="Port")
    port_button.pack(side="right", padx=6)

    log = ScrolledText(frame, height=16, state="disabled", wrap="word")
    log.grid(row=5, column=0, columnspan=3, sticky="nsew")
    for tag, colour in TAGS.items():
        log.tag_configure(tag, foreground=colour)

    events: queue.Queue = queue.Queue()
    state = {"done_dir": None, "partial": "", "tag": None, "warned": False}

    def append(text):
        """Add whole lines, coloured by the block they belong to (a block ends at a blank line)."""
        text = state["partial"] + text
        *lines, state["partial"] = text.split("\n")
        log.configure(state="normal")
        for line in lines:
            if not line.strip():
                state["tag"] = None
            elif state["tag"] is None or not line.startswith(" "):
                state["tag"] = next((t for t in TAGS if line.startswith(t)), state["tag"])
            if state["tag"] == "Warning":
                state["warned"] = True
            log.insert("end", line + "\n", state["tag"] or ())
        log.see("end")
        log.configure(state="disabled")

    def worker(args):
        old = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = _QueueWriter(events)
        try:
            events.put(("done", port(*args)))
        except SystemExit as e:
            events.put(("error", str(e)))
        except Exception as e:
            traceback.print_exc()
            events.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            sys.stdout, sys.stderr = old

    def poll():
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == "log":
                    append(value)
                    continue
                if kind == "pick":
                    var, chosen = value
                    var.set(shown(chosen))
                    continue
                port_button.configure(state="normal")
                if kind == "error":
                    append(f"\nError: {value}\n")
                    messagebox.showerror("unityport", f"{value}\n\nTo report it, use Help > Report a problem.")
                    continue
                state["done_dir"] = value[0]
                open_button.configure(state="normal")
                if state["warned"]:
                    messagebox.showwarning("unityport", f"Linux build written to {state['done_dir']}, with "
                                           "warnings. Check the log for what to do about them.")
                else:
                    messagebox.showinfo("unityport", f"Linux build written to {state['done_dir']}")
        except queue.Empty:
            pass
        root.after(100, poll)

    def start():
        if not game.get():
            messagebox.showerror("unityport", "Choose the Windows game folder first.")
            return
        log.configure(state="normal")
        log.delete("1.0", "end")
        log.configure(state="disabled")
        state.update(partial="", tag=None, warned=False)
        port_button.configure(state="disabled")
        open_button.configure(state="disabled")
        args = (Path(path(game)), Path(path(output)) if output.get() else None, config.get("arch"),
                Path(path(editor)) if editor.get() else None)
        threading.Thread(target=worker, args=(args,), daemon=True).start()

    def open_output():
        if not state["done_dir"]:
            return
        if os.name == "nt":
            os.startfile(state["done_dir"])
        else:
            subprocess.Popen(["xdg-open", str(state["done_dir"])], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=bundled.host_env())

    def dialog(title):
        win = tk.Toplevel(root)
        win.title(title)
        win.transient(root)
        body = ttk.Frame(win, padding=12)
        body.pack(fill="both", expand=True)
        return win, body

    def show_report():
        out = state["done_dir"] or (Path(path(output)) if output.get() else None)
        text = report.build(log.get("1.0", "end-1c"), Path(path(game)) if game.get() else None, out)
        win, body = dialog("Report a problem")
        win.minsize(560, 420)
        ttk.Label(body, wraplength=640, justify="left", text=(
            "This report has unityport's output and, if you've run the ported game, its Player.log. "
            "Fill in the parts between <!-- and -->, and check it for anything you'd rather not share "
            "(your home folder is already shown as ~). Then post it as an issue on GitHub, which needs a "
            "GitHub account.")).pack(anchor="w")
        box = ScrolledText(body, wrap="word", height=22, width=90)
        box.insert("1.0", text)
        box.pack(fill="both", expand=True, pady=8)
        status = tk.StringVar()
        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Label(row, textvariable=status, foreground="gray", wraplength=360, justify="left").pack(side="left")

        def post():
            body_text = box.get("1.0", "end-1c")
            title = f"{Path(path(game)).name}: " if game.get() else ""
            url, inline = report.issue_url(body_text, title)
            root.clipboard_clear()
            root.clipboard_append(body_text)
            report.open_url(url)
            status.set("Copied to the clipboard, and opened GitHub with the report filled in." if inline else
                       "Copied to the clipboard and opened GitHub. The report is too long to fill in "
                       "automatically: paste it into the issue.")

        def save():
            chosen = filedialog.asksaveasfilename(parent=win, title="Save report", initialfile="unityport-report.md",
                                                  defaultextension=".md",
                                                  filetypes=[("Markdown", "*.md"), ("Text", "*.txt")])
            if chosen:
                Path(chosen).write_text(box.get("1.0", "end-1c"), encoding="utf-8")
                status.set(f"Saved to {chosen}")

        ttk.Button(row, text="Close", command=win.destroy).pack(side="right")
        ttk.Button(row, text="Save as...", command=save).pack(side="right", padx=6)
        ttk.Button(row, text="Copy and open GitHub", command=post).pack(side="right")

    def show_advanced():
        win, body = dialog("Advanced settings")
        win.resizable(False, False)
        body.columnconfigure(1, weight=1)
        values = {name: tk.StringVar(value=config.get(name)) for name in config.DEFAULTS}

        ttk.Label(body, text="Linux player").grid(row=0, column=0, sticky="w", padx=(0, 6))
        arches = ttk.Frame(body)
        arches.grid(row=0, column=1, sticky="w")
        for arch in config.ARCHES:
            ttk.Radiobutton(arches, text=arch, variable=values["arch"], value=arch).pack(side="left", padx=(0, 12))
        ttk.Label(body, text="x86 is the 32-bit player, which only older Unity versions include.",
                  foreground="gray").grid(row=1, column=1, sticky="w", pady=(0, 10))

        ttk.Label(body, wraplength=600, justify="left", text=(
            "Where Unity's files are downloaded from. Change these only if Unity moves them and ports "
            "fail to download; the new location must have the same layout.")).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(0, 6))
        labels = {"download_url": ("Per-build downloads",
                                   "Linux players and class libraries; {rev} is the build revision"),
                  "releases_api": ("Release lookup",
                                   "Finds a version's build revision when the game's files don't have it; "
                                   "{version} is the Unity version")}
        row = 3
        for name, (label, hint) in labels.items():
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=(0, 6))
            ttk.Entry(body, textvariable=values[name], width=80).grid(row=row, column=1, sticky="ew")
            ttk.Label(body, text=hint, foreground="gray").grid(row=row + 1, column=1, sticky="w", pady=(0, 6))
            row += 2
        ttk.Label(body, wraplength=600, justify="left", foreground="gray", text=(
            f"Saved in {config.SETTINGS}, and used by the command line too.")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))

        def reset():
            for name, var in values.items():
                var.set(config.DEFAULTS[name])

        def save():
            chosen = {name: var.get().strip() for name, var in values.items()}
            for name, value in chosen.items():
                problem = config.check(name, value)
                if problem:
                    messagebox.showerror("unityport", f"{labels[name][0]}: {problem}", parent=win)
                    return
            try:
                config.save(chosen)
            except OSError as e:
                messagebox.showerror("unityport", f"Couldn't save {config.SETTINGS}: {e}", parent=win)
                return
            win.destroy()

        buttons = ttk.Frame(body)
        buttons.grid(row=row + 1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(buttons, text="Reset to defaults", command=reset).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", command=save).pack(side="right", padx=6)

    def show_about():
        win, body = dialog("About unityport")
        win.resizable(False, False)
        title_font = font.nametofont("TkDefaultFont").copy()
        title_font.configure(size=title_font.cget("size") + 6, weight="bold")
        link_font = font.nametofont("TkDefaultFont").copy()
        link_font.configure(underline=True)
        ttk.Label(body, text="unityport", font=title_font).pack(anchor="w")
        ttk.Label(body, text=f"Version {__version__}").pack(anchor="w")
        ttk.Label(body, wraplength=420, justify="left", text=(
            "Ports Windows Unity (Mono) games to native Linux builds, translating their Direct3D 11 "
            "shaders to OpenGL.")).pack(anchor="w", pady=(8, 8))
        link = ttk.Label(body, text=report.REPO_URL, foreground=TAGS["Note"], cursor="hand2", font=link_font)
        link.pack(anchor="w")
        link.bind("<Button-1>", lambda e: report.open_url(report.REPO_URL))
        credits = "Translates shaders with Unity's HLSLcc (MIT licence)."
        if bundled.BUNDLE_DIR:
            credits += " Includes 7-Zip by Igor Pavlov (GNU LGPL), from 7-zip.org."
        ttk.Label(body, wraplength=420, justify="left", foreground="gray", text=(
            f"{credits}\nPython {platform.python_version()}, Tk {root.tk.call('info', 'patchlevel')}")
        ).pack(anchor="w", pady=(8, 8))
        ttk.Button(body, text="Close", command=win.destroy).pack(anchor="e")

    port_button.configure(command=start)
    open_button.configure(command=open_output)
    root.after(100, poll)
    root.mainloop()
