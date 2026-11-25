"""Customer-owned subscriptions over a separate, configuration-only Panel channel."""

import asyncio
import copy
import json
import logging
import re
import secrets
import threading
from urllib.parse import urlsplit

from hermes_runtime.standalone.panel_agy_login import GeminiSubscriptions, VIEWER
from providers.agy_policy import AgyError, MODEL as AGY_MODEL, PROVIDER as AGY_PROVIDER

CLAUDE = "claude-subscription-directsdk-experimental"
PROVIDERS = {"claude": CLAUDE, "openai": "openai-codex", "gemini": AGY_PROVIDER}


# WebSocket DEBUG logging includes frames and handshake diagnostics.
_PROTECTED_WIRE = logging.Logger("protected-subscriptions-wire")
_PROTECTED_WIRE.disabled = True


class SubscriptionError(ValueError):
    """An intentionally public message; upstream exception text is never forwarded."""


class Subscriptions:
    def __init__(self, home, auth, *, gemini=None):
        self.home, self.auth = home, auth
        self.sessions = {}
        self.lock = threading.RLock()
        self.operations = asyncio.Lock()
        self.gemini = gemini if gemini is not None else GeminiSubscriptions(home)

    async def bridge(self, remote_url, credential, *, viewer=None):
        from websockets.asyncio.client import connect
        async with connect(remote_url, additional_headers={"Authorization": f"Bearer {credential}"},
                max_size=16384, open_timeout=15, close_timeout=5, proxy=None, logger=_PROTECTED_WIRE) as remote:
            async for raw in remote:
                request = json.loads(raw)
                try:
                    result = await self.handle(request.get("method"), request.get("params") or {}, viewer=viewer)
                    response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
                except AgyError as error:
                    message = {"auth_required": "Сначала подключите выбранную подписку.",
                               "auth_expired": "Подключите Gemini заново.",
                               "protocol": "Проверьте данные подключения Gemini."}.get(error.code, "Gemini недоступен в этом окружении.")
                    response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": 403, "message": message,
                                "data": {"reason": error.code}}}
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
        from hermes_runtime.workspace.transport import profile_scope
        from providers import get_provider_profile
        with profile_scope(self.home):
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
        gemini = self.gemini.card()
        # Native account cards may carry token previews, emails and local file paths. None
        # belongs in Panel's response, store or logs.
        return {"selected": selected, "model": model.get("default", ""),
            "managedFallback": selected != "gemini" and managed is not None and any(row.get("base_url") == managed.get("base_url")
                for row in config.get("fallback_providers", []) if isinstance(row, dict)),
            "providers": [{"id": "claude", "available": bool(claude.get("available")), "connected": bool(claude.get("logged_in"))},
                {"id": "openai", "available": True, "connected": bool(openai.get("logged_in"))}, *([gemini] if gemini else [])]}

    def _managed_route(self, config):
        saved = config.get("autonomous", {}).get("panel", {}).get("managed_inference")
        route = saved or config.get("model", {})
        panel = config.get("autonomous", {}).get("panel", {})
        expected = panel.get("url", "").rstrip("/") + "/app/api/agents/" + panel.get("environment_id", "") + "/inference/v1"
        if route.get("provider") != "custom" or route.get("base_url") != expected or route.get("key_env") != "AGENTSEE_INFERENCE_KEY":
            return None
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
            plugin = config.setdefault("plugins", {}).setdefault("entries", {}).setdefault(CLAUDE, {}).setdefault("settings", {})
            if not plugin.get("claude_config_dir"):
                directory = self.home / "subscriptions" / "claude"
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                directory.chmod(0o700)
                plugin["claude_config_dir"] = str(directory)
                self._write(config)
            status = self._claude()
            if not status.get("available") or not status.get("login_command"):
                raise SubscriptionError("Claude DirectSDK пока не установлен. Обратитесь в agentsee.")
            sid = secrets.token_urlsafe(24)
            # The same plugin setting is read for every subsequent DirectSDK request.
            from tools.environments.local import served_profile_child_env
            child = {**served_profile_child_env(target_home=self.home), "CLAUDE_CONFIG_DIR": plugin["claude_config_dir"]}
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

    def submit_claude_code(self, provider, session_id, code):
        from hermes_runtime.standalone.panel_claude_login import ClaudeLoginError

        if provider != "claude":
            raise SubscriptionError("Метод не разрешён.")
        login = self._session(provider, session_id)["login"]
        try:
            login.submit(code)
        except ClaudeLoginError as error:
            raise SubscriptionError(str(error)) from None
        return {"submitted": True}

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
        agy_authority = agy_generation = None
        if provider == "gemini":
            if managed_fallback or model != AGY_MODEL:
                raise AgyError("unavailable")
            agy_authority = self.gemini.connected()
            agy_generation = agy_authority.active(self.home)
        status = await asyncio.to_thread(self.status) if provider not in {"managed", "gemini"} else None
        with self.lock:
            config = self._config()
            managed = copy.deepcopy(self._managed_route(config))
            if managed is None and (provider in {"managed", "gemini"} or managed_fallback):
                raise SubscriptionError("Подписки доступны для сотрудников с базовым инференсом agentsee.")
            if provider != "managed":
                if provider != "gemini" and not next(row["connected"] for row in status["providers"] if row["id"] == provider):
                    raise SubscriptionError("Сначала подключите выбранную подписку.")
                if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/\[\]:-]{0,159}", model):
                    raise SubscriptionError("Укажите корректный ID модели.")
                route = {"provider": PROVIDERS[provider], "default": model}
            else:
                route = managed
            panel = config.setdefault("autonomous", {}).setdefault("panel", {})
            if managed is not None:
                panel["managed_inference"] = managed
                config["fallback_providers"] = [{**{k: v for k, v in managed.items() if k != "default"},
                    "model": managed["default"]}] if managed_fallback and provider != "managed" else []
            if self.auth.gateway_owned or provider == "gemini" or config.get("model", {}).get("provider") == AGY_PROVIDER:
                panel["model_selection_revision"] = secrets.token_hex(16)
            revision = panel.get("model_selection_revision")
            if agy_authority is not None and (self.gemini.connected() is not agy_authority
                                              or agy_authority.active(self.home) != agy_generation):
                raise AgyError("unavailable")
            config["model"] = route
            self._write(config)
        if self.auth.gateway_owned:
            # Main has no frozen local-session policy. Its owner consumes this selection
            # at the next turn boundary, preserving the answer already in progress.
            return {"saved": True, "deferred": True, **({"revision": revision} if provider == "gemini" else {})}
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
            selection = None
            if agy_authority is not None:
                if session.get("session_key") != resume["session_id"] or not session.get("session_id"):
                    raise SubscriptionError("Настройка сохранена, но текущий диалог не переключён. Повторите применение.")
                self._verify_gemini_selection(route, revision, agy_authority, agy_generation)
                selection = {"revision": revision, "generation": agy_generation}
            # Managed custom routes need the endpoint and key-env from config, not the old
            # subscription's endpoint. Native model switching resolves the explicit provider.
            result = await call(2, "config.set", {"session_id": session["session_id"], "key": "model",
                "value": f"{route['default']} --provider {route['provider']}", "confirm_expensive_model": True,
                **({"panel_selection": selection} if selection else {})})
        if agy_authority is not None:
            self._verify_gemini_selection(route, revision, agy_authority, agy_generation)
            expected = {"revision": revision, "generation": agy_generation, "session_id": session["session_id"],
                        "session_key": resume["session_id"], "provider": AGY_PROVIDER, "model": AGY_MODEL}
            if result.get("panel_selection") != expected or result.get("value") != AGY_MODEL or result.get("confirm_required"):
                raise SubscriptionError("Настройка сохранена, но текущий диалог не переключён. Повторите применение.")
        return {"saved": True, "deferred": bool(result.get("deferred")), **({"revision": revision} if provider == "gemini" else {})}

    def _verify_gemini_selection(self, route, revision, authority, generation):
        from hermes_runtime.standalone import agy_runtime
        current = self._config()
        if (current.get("model") != route
                or current.get("autonomous", {}).get("panel", {}).get("model_selection_revision") != revision
                or agy_runtime.admit(self.home, "inference") is not authority
                or authority.active(self.home) != generation):
            raise SubscriptionError("Настройка сохранена, но текущий диалог не переключён. Повторите применение.")

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

    async def handle(self, method, params, *, viewer=None):
        async with self.operations:
            return await self._handle(method, params, viewer)

    async def _handle(self, method, params, viewer):
        provider = params.get("provider")
        if method != "subscriptions.status" and provider not in {"managed", *PROVIDERS}:
            raise SubscriptionError("Неизвестный провайдер.")
        if provider == "gemini":
            if not isinstance(viewer, str) or not VIEWER.fullmatch(viewer):
                raise AgyError("unavailable")
            if method == "subscriptions.disconnect":
                return await asyncio.to_thread(self._disconnect_gemini)
            if method != "subscriptions.select":
                return await asyncio.to_thread(self.gemini.handle, method, params, viewer)
            if params.get("managedFallback") is not False:
                raise AgyError("unavailable")
        if method == "subscriptions.select":
            return await self.select(provider, params.get("model"), params.get("managedFallback") is True)
        handlers = {"subscriptions.status": lambda: self.status(),
            "subscriptions.models": lambda: self.models(provider),
            "subscriptions.login.start": lambda: self.start(provider),
            "subscriptions.login.poll": lambda: self.poll(provider, params.get("sessionId")),
            "subscriptions.login.cancel": lambda: self.cancel(provider, params.get("sessionId")),
            "subscriptions.login.code": lambda: self.submit_claude_code(provider, params.get("sessionId"), params.get("code")),
            "subscriptions.disconnect": lambda: self.disconnect(provider)}
        if method not in handlers or (provider == "managed" and method != "subscriptions.select"):
            raise SubscriptionError("Метод не разрешён.")
        return await asyncio.to_thread(handlers[method])

    def _disconnect_gemini(self):
        from hermes_runtime.standalone import agy_runtime
        with self.lock:
            authority = agy_runtime.admit(self.home, "disconnect")
            config = self._config()
            revision = config.get("autonomous", {}).get("panel", {}).get("model_selection_revision")
            if config.get("model", {}).get("provider") == AGY_PROVIDER or not authority.applied(self.home, revision):
                raise SubscriptionError("Сначала выберите другой источник инференса и сохраните настройку.")
            self.gemini.retire()
            generation = authority.active(self.home)
            if generation is not None:
                authority.withdraw(self.home, generation, "disconnect")
            return {"disconnected": True}
