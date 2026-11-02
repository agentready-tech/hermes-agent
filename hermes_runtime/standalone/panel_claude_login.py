"""Drive the provider-owned Claude Code login; never mint or copy subscription tokens."""

import os
import re
import signal
import subprocess
import threading
import time
from urllib.parse import urlsplit


def authorization_url(text):
    for candidate in re.findall(r"https://[^\s\x1b<>]+", text):
        url = urlsplit(candidate)
        if url.hostname in {"claude.ai", "console.anthropic.com", "platform.claude.com"} and url.path == "/oauth/authorize":
            return candidate
    return None


class ClaudeLogin:
    """One bounded login process. Output is private and only an allowlisted URL leaves it."""

    def __init__(self, command, environment, cwd):
        self.created = time.monotonic()
        self.url = None
        self.error = False
        self.cancelled = False
        self.ready = threading.Event()
        self.lock = threading.Lock()
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
            return {"status": "pending", "flow": "authorization_code", **({"url": self.url} if self.url else {})}

    def submit(self, code):
        if not isinstance(code, str) or not re.fullmatch(r"[a-zA-Z0-9_.~#-]{1,4096}", code):
            raise ValueError("Некорректный код авторизации.")
        if self.view()["status"] != "pending":
            raise ValueError("Вход уже завершён. Начните подключение заново.")
        self.process.stdin.write((code + "\n").encode())
        self.process.stdin.flush()

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
