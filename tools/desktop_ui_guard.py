"""Refuse known desktop-UI bypasses at agent execution entry points.

This is a structural guard for explicit commands/source, not an arbitrary-code
isolation boundary. Trusted CUA and browser backends never enter this guard.
"""
from __future__ import annotations

import ast
import re
import shlex
from pathlib import PurePosixPath
from typing import Callable

MAX_SCRIPT_BYTES = 128 * 1024
_MAX_DEPTH = 3
_REFUSAL = (
    "Desktop application UI must use computer_use. This command/code was not run. "
    "Use its vision, coordinates, click, type, key and scroll actions; if unavailable "
    "or human_has_control, report the blocker instead of bypassing it."
)
_GUI_BINARIES = frozenset({"xdotool", "ydotool", "wmctrl", "xte", "xvkbd", "xwd", "scrot", "gnome-screenshot", "screencapture"})
_X11_CALLS = frozenset({
    "XTestFakeKeyEvent", "XTestFakeButtonEvent", "XTestFakeMotionEvent", "XTestFakeRelativeMotionEvent",
    "XSendEvent", "XWarpPointer", "XSetInputFocus", "XGetImage", "XGetWindowProperty", "XQueryTree",
})
_PY_UI_CALLS = frozenset({"click", "doubleClick", "mouseDown", "mouseUp", "moveTo", "moveRel", "dragTo", "dragRel",
                          "typewrite", "write", "press", "release", "keyDown", "keyUp", "hotkey", "scroll", "screenshot", "grab", "fake_input"})
_EXECUTORS = frozenset({"os.system", "os.popen", "subprocess.run", "subprocess.call", "subprocess.check_call",
                        "subprocess.check_output", "subprocess.Popen"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
_WRAPPER_VALUE_OPTIONS = {
    "sudo": {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "--close-from",
             "-T", "--command-timeout", "-R", "--chroot", "-D", "--chdir", "-r", "--role", "-t", "--type"},
    "env": {"-u", "--unset", "-C", "--chdir"},
    "timeout": {"-k", "--kill-after", "-s", "--signal"},
}


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _dotted(node.value) + "." + node.attr
    return ""


def _literal(node: ast.AST):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def _python_uses_ui(code: str, reader, depth: int) -> bool:
    if depth > _MAX_DEPTH:
        return False
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return False
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update({a.asname or a.name.split(".")[0]: a.name for a in node.names})
        elif isinstance(node, ast.ImportFrom):
            aliases.update({a.asname or a.name: f"{node.module}.{a.name}" for a in node.names})
    ui_modules = any(n.startswith(("pyautogui", "pynput", "Xlib", "mss", "PIL.ImageGrab")) for n in aliases.values())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        raw = _dotted(node.func)
        parts = raw.split(".")
        name = ".".join([aliases.get(parts[0], parts[0]), *parts[1:]])
        last = name.rsplit(".", 1)[-1]
        if last in _X11_CALLS or (ui_modules and last in _PY_UI_CALLS):
            return True
        if name in {"ctypes.CDLL", "ctypes.PyDLL", "ctypes.cdll.LoadLibrary"} and node.args:
            library = _literal(node.args[0])
            if isinstance(library, str) and re.search(r"(?:lib)?[Xx]tst(?:[.\-]|$)", library):
                return True
        if name in _EXECUTORS and node.args and depth < _MAX_DEPTH:
            command = _literal(node.args[0])
            if isinstance(command, (list, tuple)) and all(isinstance(p, str) for p in command):
                command = shlex.join(command)
            if isinstance(command, str) and _shell_uses_ui(command, reader, depth + 1):
                return True
    return False


def _script_entry(words: list[str], python: bool, shell: bool):
    if not python and not shell:
        path = words[0]
        return (path, path.endswith(".py")) if path.endswith((".py", ".sh")) else None
    i = 1
    while i < len(words):
        word = words[i]
        if word in {"-m", "-"}:
            return None
        if word in ({"-W", "-X"} if python else {"-o", "+o"}):
            i += 2
            continue
        if word.startswith("-"):
            i += 1
            continue
        return word, python
    return None


def _wrapper_command(words: list[str], exe: str) -> list[str]:
    i = 1
    while i < len(words):
        word = words[i]
        if word == "--":
            i += 1
            break
        if word in _WRAPPER_VALUE_OPTIONS[exe]:
            i += 2
        elif word.startswith("-") or _ASSIGNMENT.match(word):
            i += 1
        else:
            break
    return words[i + (exe == "timeout"):]


def _command_head(words: list[str]) -> list[str]:
    """Strip only literal assignments and known wrapper option/value prefixes."""
    for _ in range(_MAX_DEPTH + 1):
        while words and _ASSIGNMENT.match(words[0]):
            words = words[1:]
        if not words:
            return []
        exe = PurePosixPath(words[0]).name
        if exe not in _WRAPPER_VALUE_OPTIONS:
            return words
        words = _wrapper_command(words, exe)
    return []


def _entry_uses_ui(words: list[str], reader, depth: int) -> bool:
    words = _command_head(words)
    if not words:
        return False
    exe = PurePosixPath(words[0]).name
    if exe in _GUI_BINARIES:
        return True
    if exe in {"import", "magick"} and ("-window" in words or "-display" in words):
        return True
    if exe == "osascript" and any(re.search(r"\b(?:System Events|keystroke|key code|click)\b", x) for x in words[1:]):
        return True
    python = bool(re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", exe))
    shell = exe in {"sh", "bash", "zsh", "dash"}
    code_flag = next((i for i, word in enumerate(words[1:], 1)
                      if word == "-c" or (shell and re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", word))), None)
    if (python or shell) and code_flag is not None:
        i = code_flag
        if i + 1 < len(words) and depth < _MAX_DEPTH:
            return (_python_uses_ui if python else _shell_uses_ui)(words[i + 1], reader, depth + 1)
    entry = _script_entry(words, python, shell)
    if reader is not None and entry is not None and depth < _MAX_DEPTH:
        source = reader(entry[0])
        if isinstance(source, str) and len(source.encode("utf-8")) <= MAX_SCRIPT_BYTES:
            return (_python_uses_ui if entry[1] else _shell_uses_ui)(source, reader, depth + 1)
    return False


def _shell_uses_ui(command: str, reader, depth: int) -> bool:
    if depth > _MAX_DEPTH:
        return False
    # Inspect only interpreter-fed heredocs; a cat/patch heredoc writes code,
    # and is not a desktop operation. Bodies are not executed during inspection.
    heredoc = re.compile(r"(?m)^([^\n]*?)<<-?\s*['\"]?([A-Za-z_][A-Za-z_0-9]*)['\"]?[^\n]*\n(.*?)^\2\s*$", re.DOTALL)
    for match in heredoc.finditer(command):
        try:
            head = _command_head(shlex.split(match[1]))
        except ValueError:
            continue
        if head and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", PurePosixPath(head[0]).name):
            if _python_uses_ui(match[3], reader, depth + 1):
                return True
        elif head and PurePosixPath(head[0]).name in {"sh", "bash", "zsh"}:
            if _shell_uses_ui(match[3], reader, depth + 1):
                return True
    # Do not reinterpret source-only heredoc body lines as shell commands.
    command = heredoc.sub(lambda match: match[1], command)
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        segments, current = [], []
        for word in lexer:
            if word and all(ch in ";&|()\n" for ch in word):
                segments.append(current)
                current = []
            else:
                current.append(word)
        segments.append(current)
    except ValueError:
        return False
    return any(_entry_uses_ui(words, reader, depth) for words in segments)


def desktop_ui_refusal(source: str, *, language: str = "shell", read_script: Callable | None = None) -> str | None:
    """Return fixed guidance for explicit UI execution; never echo source/paths.

    The optional reader is the selected backend's bounded read, never a host
    fallback. Opaque imports, dynamic commands and unreadable entry scripts are
    outside this known-form detector; prompt policy still prohibits bypasses.
    """
    if not isinstance(source, str):
        return None
    found = (_python_uses_ui if language == "python" else _shell_uses_ui)(source, read_script, 0)
    return _REFUSAL if found else None


def read_backend_script(env, cwd: str, path: str) -> str | None:
    """Bounded entry-script read through the selected backend, without host fallback."""
    target = PurePosixPath(path)
    if not target.is_absolute():
        target = PurePosixPath(cwd) / target
    try:
        result = env.execute(f"/usr/bin/head -c {MAX_SCRIPT_BYTES + 1} < {shlex.quote(str(target))}", timeout=10)
    except (OSError, RuntimeError, TimeoutError):
        return None
    source = result.get("output")
    if (result.get("returncode") == 0 and isinstance(source, str) and "\x00" not in source
            and len(source.encode("utf-8")) <= MAX_SCRIPT_BYTES):
        return source
    return None
