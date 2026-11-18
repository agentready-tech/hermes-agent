"""Drive the provider-owned Claude Code login; never mint or copy subscription tokens."""

import os
import re
import signal
import subprocess
import threading
import time
from urllib.parse import urlsplit


class ClaudeLoginError(ValueError):
    """A sign-in error that can be displayed without exposing provider output."""


def authorization_url(text):
    for candidate in re.findall(r"https://[^\s\x1b<>]+", text):
        url = urlsplit(candidate)
        if url.username is None and url.password is None and (url.hostname, url.path) in {
            ("claude.com", "/cai/oauth/authorize"),
            ("claude.ai", "/oauth/authorize"),
            ("console.anthropic.com", "/oauth/authorize"),
            ("platform.claude.com", "/oauth/authorize"),
        }:
            return candidate
    return None


class ClaudeLogin:
    """One bounded login process. Output is private and only an allowlisted URL leaves it."""

    def __init__(self, command, environment, cwd):
        self.created = time.monotonic()
        self.url = None
        self.error = False
        self.cancelled = False
        self.submitted = False
        self.ready = threading.Event()
        self.lock = threading.RLock()
        # The provider client owns PKCE and credential persistence. Never open a browser on
        # the worker: the customer opens its official URL in their own browser instead.
        self.process = subprocess.Popen([*command, "auth", "login", "--claudeai"],
            env={**environment, "BROWSER": "/bin/true"}, cwd=cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True)
        self.timer = threading.Timer(900, self.cancel)
        self.timer.daemon = True
        self.timer.start()
        from agent.memory_provider import spawn_context_thread
        spawn_context_thread(self._read, name="panel-claude-login").start()

    def _read(self):
        output = ""
        try:
            while chunk := os.read(self.process.stdout.fileno(), 2048):
                output = (output + chunk.decode(errors="replace"))[-16384:]
                if url := authorization_url(output):
                    with self.lock:
                        self.url = url
                    self.ready.set()
            self.error = self.process.wait(timeout=5) != 0
        except (OSError, subprocess.TimeoutExpired):
            self.error = True
            self.cancel()
        finally:
            self.timer.cancel()
            self.ready.set()

    def view(self):
        if time.monotonic() - self.created >= 900:
            return {"status": "expired"}
        if self.cancelled:
            return {"status": "cancelled"}
        if self.process.poll() is not None:
            return {"status": "error" if self.process.returncode else "approved"}
        with self.lock:
            return {"status": "pending", "flow": "authorization_code", "submitted": self.submitted,
                    **({"url": self.url} if self.url else {})}

    def submit(self, code):
        # Claude Code owns the code format. Accept an opaque value on one input line.
        if not isinstance(code, str) or not re.fullmatch(r"[\x21-\x7e]{1,4096}", code):
            raise ClaudeLoginError("Некорректный код авторизации.")
        with self.lock:
            # The browser can lose the reply after stdin accepted the code.
            if self.submitted:
                return
            if self.view()["status"] != "pending":
                raise ClaudeLoginError("Вход уже завершён. Начните подключение заново.")
            try:
                self.process.stdin.write((code + "\n").encode("ascii"))
                self.process.stdin.flush()
            except BrokenPipeError:
                raise ClaudeLoginError("Вход уже завершён. Начните подключение заново.") from None
            self.submitted = True

    def cancel(self):
        self.cancelled = True
        self.timer.cancel()
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=3)
            except ProcessLookupError:
                pass
