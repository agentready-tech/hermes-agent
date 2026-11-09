"""Customer-owned subscriptions over a separate, configuration-only Panel channel."""

import asyncio
import copy
import json
import re
import secrets
import threading
from urllib.parse import urlsplit

CLAUDE = "claude-subscription-directsdk-experimental"
PROVIDERS = {"claude": CLAUDE, "openai": "openai-codex"}


class SubscriptionError(ValueError):
    """An intentionally public message; upstream exception text is never forwarded."""


class Subscriptions:
    def __init__(self, home, auth):
        self.home, self.auth = home, auth
        self.sessions = {}
        self.lock = threading.RLock()
        self.operations = asyncio.Lock()

    async def bridge(self, remote_url, credential):
        from websockets.asyncio.client import connect
        async with connect(remote_url, additional_headers={"Authorization": f"Bearer {credential}"},
                max_size=16384, open_timeout=15, close_timeout=5, proxy=None) as remote:
            async for raw in remote:
                request = json.loads(raw)
                try:
                    result = await self.handle(request.get("method"), request.get("params") or {})
                    response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
                except SubscriptionError as error:
                    response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": 400, "message": str(error)}}
                except Exception:  # health: allow BLE001 -- public RPC boundary must redact every upstream exception, including token-bearing URLs; never log raw auth failures
                    response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": 503, "message": "Не удалось выполнить подключение. Повторите попытку."}}
                await remote.send(json.dumps(response))

    def _config(self):
        from hermes_yaml import roundtrip_yaml
        return roundtrip_yaml().load((self.home / "config.yaml").read_text())

    def _write(self, config):
        from hermes_cli.config import atomic_config_replace
        # A provider switch intentionally removes the old endpoint/key-env from model.
        atomic_config_replace(self.home / "config.yaml", config)

    def _claude(self):
        from providers import get_provider_profile
        profile = get_provider_profile(CLAUDE)
        return profile.setup_status() if profile and hasattr(profile, "setup_status") else {"available": False, "logged_in": False}

    def _request(self, path, payload=None, method=None):
        return self.auth.authenticated_request(path, payload, method=method)

    def status(self):
        config = self._config()
        managed = self._managed_route(config)
        model = config.get("model", {})
        selected = next((name for name, provider in PROVIDERS.items() if model.get("provider") == provider), "managed")
        catalog = self._request("/api/providers/oauth").get("providers", [])
        openai = next((row["status"] for row in catalog if row.get("id") == "openai-codex"), {})
        claude = self._claude()
        # Native account cards may carry token previews, emails and local file paths. None
        # belongs in Panel's response, store or logs.
        return {"selected": selected, "model": model.get("default", ""),
            "managedFallback": any(row.get("base_url") == managed.get("base_url")
                for row in config.get("fallback_providers", []) if isinstance(row, dict)),
            "providers": [{"id": "claude", "available": bool(claude.get("available")), "connected": bool(claude.get("logged_in"))},
                {"id": "openai", "available": True, "connected": bool(openai.get("logged_in"))}]}

    def _managed_route(self, config):
        saved = config.get("autonomous", {}).get("panel", {}).get("managed_inference")
        route = saved or config.get("model", {})
        panel = config.get("autonomous", {}).get("panel", {})
        expected = panel.get("url", "").rstrip("/") + "/app/api/agents/" + panel.get("environment_id", "") + "/inference/v1"
        if route.get("provider") != "custom" or route.get("base_url") != expected or route.get("key_env") != "AGENTSEE_INFERENCE_KEY":
            raise SubscriptionError("Подписки доступны для сотрудников с базовым инференсом agentsee.")
        return route

    def start(self, provider):
        with self.lock:
            for session_id, row in list(self.sessions.items()):
                if row["provider"] == provider:
                    self.cancel(provider, session_id)
            if provider == "openai":
                result = self._request("/api/providers/oauth/openai-codex/start", {})
                sid = result["session_id"]
                self.sessions[sid] = {"provider": provider}
                url = result.get("verification_url", "")
                if urlsplit(url).hostname != "auth.openai.com" or urlsplit(url).scheme != "https":
                    self.cancel(provider, sid)
                    raise SubscriptionError("Провайдер вернул неподдерживаемую ссылку входа.")
                return {"sessionId": sid, "status": "pending", "flow": "device_code", "url": url,
                    "userCode": result.get("user_code"), "expiresIn": result.get("expires_in", 900),
                    "pollInterval": max(5, result.get("poll_interval", 5))}
            from hermes_runtime.standalone.panel_claude_login import ClaudeLogin
            config = self._config()
            self._managed_route(config)
            plugin = config.setdefault("plugins", {}).setdefault("entries", {}).setdefault(CLAUDE, {}).setdefault("settings", {})
            directory = self.home / "subscriptions" / "claude"
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
            plugin.setdefault("claude_config_dir", str(directory))
            self._write(config)
            status = self._claude()
            if not status.get("available") or not status.get("login_command"):
                raise SubscriptionError("Claude DirectSDK пока не установлен. Обратитесь в agentsee.")
            sid = secrets.token_urlsafe(24)
            # The same plugin setting is read for every subsequent DirectSDK request.
            from tools.environments.local import served_profile_child_env
            child = {**served_profile_child_env(), "CLAUDE_CONFIG_DIR": plugin["claude_config_dir"]}
            login = ClaudeLogin(status["login_command"][:-2], child, str(self.home))
            self.sessions[sid] = {"provider": provider, "login": login}
            return {"sessionId": sid, **login.view()}

    def _session(self, provider, session_id):
        row = self.sessions.get(session_id)
        if not row or row["provider"] != provider:
            raise SubscriptionError("Вход истёк. Начните подключение заново.")
        return row

    def poll(self, provider, session_id):
        row = self._session(provider, session_id)
        if provider == "claude":
            view = row["login"].view()
            if view["status"] == "approved" and not self._claude().get("logged_in"):
                view = {"status": "error"}
        else:
            result = self._request("/api/providers/oauth/openai-codex/poll/" + session_id)
            view = {"status": result.get("status", "error")}
        return {"sessionId": session_id, **view}

    def cancel(self, provider, session_id):
        with self.lock:
            row = self._session(provider, session_id)
            if provider == "claude":
                row["login"].cancel()
            else:
                self._request("/api/providers/oauth/sessions/" + session_id, method="DELETE")
            del self.sessions[session_id]
        return {"status": "cancelled"}

    def models(self, provider):
        from hermes_cli.models import cached_provider_model_ids
        from hermes_runtime.workspace.transport import profile_scope
        with profile_scope(self.home):
            status = next(row for row in self.status()["providers"] if row["id"] == provider)
            if not status["available"] or not status["connected"]:
                raise SubscriptionError("Сначала подключите выбранную подписку.")
            # Inventory reads are cache-only and warm a later open. An explicit subscription
            # load must discover this account's current IDs now, retaining native offline fallback.
            models = cached_provider_model_ids(PROVIDERS[provider], force_refresh=True)
        return {"models": [{"id": model, "label": model} for model in models[:200]]}

    async def select(self, provider, model, managed_fallback):
        status = await asyncio.to_thread(self.status) if provider != "managed" else None
        with self.lock:
            config = self._config()
            managed = copy.deepcopy(self._managed_route(config))
            if provider != "managed":
                if not next(row["connected"] for row in status["providers"] if row["id"] == provider):
                    raise SubscriptionError("Сначала подключите выбранную подписку.")
                if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/\[\]:-]{0,159}", model):
                    raise SubscriptionError("Укажите корректный ID модели.")
                route = {"provider": PROVIDERS[provider], "default": model}
            else:
                route = managed
            config.setdefault("autonomous", {}).setdefault("panel", {})["managed_inference"] = managed
            if self.auth.gateway_owned:
                config["autonomous"]["panel"]["model_selection_revision"] = secrets.token_hex(16)
            config["model"] = route
            config["fallback_providers"] = [{**{k: v for k, v in managed.items() if k != "default"}, "model": managed["default"]}] if managed_fallback and provider != "managed" else []
            self._write(config)
        if self.auth.gateway_owned:
            # Main has no frozen local-session policy. Its owner consumes this selection
            # at the next turn boundary, preserving the answer already in progress.
            return {"saved": True, "deferred": True}
        # Use the native session model switch, not a chat prompt or a second agent. Busy
        # sessions defer it until the next turn. Credentials are resolved by Hermes itself.
        from websockets.asyncio.client import connect
        target, options = await asyncio.to_thread(self.auth.rpc_connection)
        async with connect(target, proxy=None, max_size=32 * 1024 * 1024, **options) as socket:
            async def call(identity, method, params):
                await socket.send(json.dumps({"jsonrpc": "2.0", "id": identity, "method": method, "params": params}))
                async with asyncio.timeout(45):
                    async for raw in socket:
                        result = json.loads(raw)
                        if result.get("id") == identity:
                            if result.get("error"):
                                raise SubscriptionError("Настройка сохранена, но текущий диалог не переключён. Повторите применение.")
                            return result["result"]
            resume = {"session_id": "agentready_home"}
            if not self.auth.gateway_owned:
                resume.update(source="agentready", close_on_disconnect=False, inline_images=False)
            session = await call(1, "session.resume", resume)
            # Managed custom routes need the endpoint and key-env from config, not the old
            # subscription's endpoint. Native model switching resolves the explicit provider.
            result = await call(2, "config.set", {"session_id": session["session_id"], "key": "model",
                "value": f"{route['default']} --provider {route['provider']}", "confirm_expensive_model": True})
        return {"saved": True, "deferred": bool(result.get("deferred"))}

    def disconnect(self, provider):
        if self.status()["selected"] == provider:
            raise SubscriptionError("Сначала выберите другой источник инференса и сохраните настройку.")
        if provider == "openai":
            self._request("/api/providers/oauth/openai-codex", method="DELETE")
        else:
            import subprocess
            from tools.environments.local import served_profile_child_env
            status = self._claude()
            command = status.get("login_command")
            if not command:
                raise SubscriptionError("Claude DirectSDK недоступен.")
            directory = self._config().get("plugins", {}).get("entries", {}).get(CLAUDE, {}).get("settings", {}).get("claude_config_dir")
            if not directory:
                raise SubscriptionError("Этот вход был настроен вне Panel. Отключите его через Claude Code.")
            run = subprocess.run([*command[:-2], "auth", "logout"], env={**served_profile_child_env(), "CLAUDE_CONFIG_DIR": directory},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
            if run.returncode:
                raise SubscriptionError("Claude не подтвердил выход. Повторите попытку.")
        return {"disconnected": True}

    async def handle(self, method, params):
        async with self.operations:
            return await self._handle(method, params)

    async def _handle(self, method, params):
        provider = params.get("provider")
        if method != "subscriptions.status" and provider not in {"managed", *PROVIDERS}:
            raise SubscriptionError("Неизвестный провайдер.")
        if method == "subscriptions.select":
            return await self.select(provider, params.get("model"), params.get("managedFallback") is True)
        handlers = {"subscriptions.status": lambda: self.status(),
            "subscriptions.models": lambda: self.models(provider),
            "subscriptions.login.start": lambda: self.start(provider),
            "subscriptions.login.poll": lambda: self.poll(provider, params.get("sessionId")),
            "subscriptions.login.cancel": lambda: self.cancel(provider, params.get("sessionId")),
            "subscriptions.login.code": lambda: self._session(provider, params.get("sessionId"))["login"].submit(params.get("code")) or {"submitted": True},
            "subscriptions.disconnect": lambda: self.disconnect(provider)}
        if method not in handlers or (provider == "managed" and method != "subscriptions.select"):
            raise SubscriptionError("Метод не разрешён.")
        return await asyncio.to_thread(handlers[method])
