"""Problem reports: a GitHub issue with the port's output and the game's Player.log."""

import os
import platform
import subprocess
import urllib.parse
import webbrowser
from pathlib import Path

from . import __version__, bundled, version, wsl

REPO_URL = "https://github.com/Randomno/unityport"
NEW_ISSUE_URL = REPO_URL + "/issues/new"
# Browsers and GitHub cope with URLs up to about this long; longer reports go by clipboard
MAX_URL = 8000
# GitHub rejects issue bodies over 65536 characters
LOG_HEAD, LOG_TAIL = 150, 350


def app_info(data_dir: Path) -> tuple[str, str] | None:
    """(company, product) from <Game>_Data/app.info, which name the Player.log folder."""
    try:
        lines = (data_dir / "app.info").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return (lines[0], lines[1]) if len(lines) >= 2 else None


def player_logs(data_dir: Path) -> list[Path]:
    """Where the Linux player writes Player.log for this game, newest first.

    Linux players log to ~/.config/unity3d/<company>/<product>/Player.log. From the
    Windows build, look in each WSL distribution's home folders.
    """
    info = app_info(data_dir)
    if not info:
        return []
    rel = Path(".config", "unity3d", *info, "Player.log")
    if os.name == "nt":
        homes = []
        for distro in wsl.preferred_distros():
            try:
                homes += [p for p in Path(f"\\\\wsl.localhost\\{distro}\\home").iterdir() if p.is_dir()]
            except OSError:
                pass
    else:
        homes = [Path.home()]
    found = [h / rel for h in homes if (h / rel).is_file()]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def _system() -> str:
    if os.name == "nt":
        ver = platform.version()
        # Windows 11 still calls itself 10.0, and Python before 3.12 says "10"; its builds start at 22000
        build = ver.split(".")[2] if ver.count(".") >= 2 else ""
        release = "11" if build.isdigit() and int(build) >= 22000 else platform.release()
        return f"Windows {release} ({ver})"
    try:
        name = platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
    except OSError:
        name = "Linux"
    return name + (" under WSL" if wsl.in_wsl() else "")


def _private(text: str) -> str:
    """Show the home folder as ~, so the report doesn't carry the user's name."""
    for home in {str(Path.home()), os.environ.get("USERPROFILE", "")}:
        if len(home) > 3:
            text = text.replace(home, "~")
    return text


def _trim(lines: list[str]) -> list[str]:
    # The player prints the driver's whole extension list, wrapped over several long lines
    out = []
    for line in lines:
        if line.count("GL_") > 5:
            if not out or out[-1] != "[OpenGL extension list left out]":
                out.append("[OpenGL extension list left out]")
        else:
            out.append(line)
    lines = out
    if len(lines) <= LOG_HEAD + LOG_TAIL:
        return lines
    return lines[:LOG_HEAD] + [f"[... {len(lines) - LOG_HEAD - LOG_TAIL} lines left out ...]"] + lines[-LOG_TAIL:]


def _fenced(text: str) -> str:
    fence = "````" if "```" in text else "```"
    return f"{fence}\n{text.rstrip()}\n{fence}"


def _details(summary: str, text: str) -> str:
    return f"<details><summary>{summary}</summary>\n\n{_fenced(text)}\n\n</details>"


def build(port_output: str = "", game_dir: Path | None = None, output_dir: Path | None = None) -> str:
    """A filled-in issue body, in the layout of .github/ISSUE_TEMPLATE/port-problem.md."""
    data_dir = name = ver = None
    for d in (output_dir, game_dir):
        if d and d.is_dir():
            try:
                data_dir = version.find_data_dir(d)
                break
            except FileNotFoundError:
                continue
    if data_dir:
        info = app_info(data_dir)
        name = info[1] if info else None
        ver = version.version_from_data(data_dir)
    # Only the Linux build's log is interesting; the Windows game's would be from another player
    logs = player_logs(data_dir) if data_dir and output_dir and output_dir in data_dir.parents else []
    build_kind = "standalone executable" if bundled.BUNDLE_DIR else f"Python {platform.python_version()}"

    parts = [
        "## What happened",
        "<!-- Did the port fail, does the game crash, or does something look wrong?",
        "     Screenshots help, especially of the Windows version next to the Linux one. -->",
        "",
        "",
        "",
        "## Game",
        "",
        f"- Name: {name or ''}",
        f"- Unity version: {ver or ''}",
        "- Where it's from: <!-- e.g. Steam, itch.io, with a link, and the game's own version -->",
        "",
        "## System",
        "",
        f"- Ported on: {_system()}",
        f"- unityport: {__version__} ({build_kind})",
        "- Game run on: <!-- the distribution, and your graphics card and driver -->",
        "",
        "## unityport output",
        "",
        _details("Output", _private(port_output)) if port_output.strip() else "<!-- Paste what unityport printed -->",
        "",
        "## Player.log",
        "",
    ]
    if logs:
        lines = logs[0].read_text(encoding="utf-8", errors="replace").splitlines()
        parts.append(_details(_private(f"{logs[0]}"), _private("\n".join(_trim(lines)))))
    else:
        parts.append("<!-- If you ran the ported game, paste ~/.config/unity3d/<company>/<game>/Player.log -->")
    return "\n".join(parts) + "\n"


def issue_url(body: str, title: str = "") -> tuple[str, bool]:
    """A new-issue URL, and whether it carries the body (long ones go by clipboard instead)."""
    query = {"title": title, "body": body}
    url = NEW_ISSUE_URL + "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
    if len(url) <= MAX_URL:
        return url, True
    query["body"] = "<!-- The report is on your clipboard: paste it here. -->\n"
    return NEW_ISSUE_URL + "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote), False


def open_url(url: str) -> None:
    """Open a web page in the browser, also from WSL and from the bundled executable."""
    if os.name == "nt":
        os.startfile(url)
        return
    # In WSL, the Windows browser (xdg-open there often has none to hand over to), through
    # Windows' URL handler. cmd's start would treat & as a command separator, and
    # explorer.exe opens a File Explorer window for some URLs.
    commands = [["rundll32.exe", "url.dll,FileProtocolHandler", url], ["wslview", url]] if wsl.in_wsl() else []
    commands.append(["xdg-open", url])
    for command in commands:
        try:
            # With the system's library path: the bundle's would break the browser
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=bundled.host_env())
            return
        except OSError:
            continue
    webbrowser.open(url)
