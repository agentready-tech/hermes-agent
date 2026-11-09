"""Closed, own-home native HTTP skill reads over the existing Panel reverse connector."""

import asyncio
import json
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urlencode

from hermes_runtime.skill_inventory import CAPABILITY, MAX_RESPONSE_BYTES, SkillReadError, canonical_name
from hermes_runtime.skill_edit import EDIT_CAPABILITY, MAX_UPDATE_REQUEST_BYTES, UPDATE_METHOD, validate_update_request


class SkillsRead:
    def __init__(self, home, auth):
        self.home = Path(home).resolve()
        if getattr(auth, "home", None) != self.home or not getattr(auth, "gateway_owned", False):
            raise ValueError("A verified own-home gateway authority is required")
        self.auth = auth

    def available(self):
        try:
            self.auth.endpoint()
            self._project(self.auth.authenticated_request("/api/skills/library/system", method="GET"),
                          "skills.system.list")
            return True
        except HTTPError as error:
            # A typed content refusal still proves that the fixed, authenticated read route exists.
            code = self._native_error(error)
            return error.code in {400, 413} and code is not None and code.startswith("INVALID_SKILLS_")
        except (OSError, ValueError):
            return False

    def builtin_available(self):
        from hermes_runtime.builtin_skills import LIST_METHOD
        try:
            self.auth.endpoint()
            self._project(self.auth.authenticated_request('/api/skills/library/builtin', method='GET'), LIST_METHOD)
            return True
        except (HTTPError, OSError, ValueError):
            return False

    @staticmethod
    def _native_error(error):
        try:
            detail = json.loads(error.read(4096)).get("detail")
        except (ValueError, UnicodeError, AttributeError):
            detail = None
        return detail if isinstance(detail, str) and re.fullmatch(
            r"INVALID_SKILLS_[A-Z_]+|CONFLICT_SKILL_CHANGED|CONFLICT_BUILTIN_SKILLS_CHANGED|FORBIDDEN|NOT_FOUND", detail) else None

    @staticmethod
    def _row(value, *, system=False):
        fields = {"name", "description", "version", "origin", "readOnly", "shadowed", "ambiguous",
                  "resources", "resourcesTruncated"}
        if (not isinstance(value, dict) or set(value) != fields or value["readOnly"] is not True
                or not isinstance(value["description"], str) or len(value["description"]) > 1024
                or (value["version"] is not None and not isinstance(value["version"], str))
                or not isinstance(value["origin"], str) or value["origin"] not in ({"system"} if system else {"installed", "unknown"})
                or any(type(value[key]) is not bool for key in ("shadowed", "ambiguous", "resourcesTruncated"))
                or not isinstance(value["resources"], list) or len(value["resources"]) > 16
                or any(not isinstance(path, str) or len(path) > 256 or path.startswith(("/", "\\"))
                       or "\\" in path or ".." in path.split("/") for path in value["resources"])):
            raise SkillReadError("INVALID_SKILLS_UNAVAILABLE")
        canonical_name(value["name"])
        return {key: value[key] for key in fields}

    @classmethod
    def _project(cls, result, method):
        from hermes_runtime.builtin_skills import LIST_METHOD, SELECT_METHOD, project_inventory
        if method in {LIST_METHOD, SELECT_METHOD}:
            return project_inventory(result)
        capability = EDIT_CAPABILITY if method == UPDATE_METHOD else CAPABILITY
        if not isinstance(result, dict) or result.get("capability") != capability:
            raise SkillReadError("INVALID_SKILLS_UNAVAILABLE")
        system = method.startswith("skills.system.")
        if method.endswith(".list"):
            if (set(result) != {"capability", "skills", "truncated"} or type(result["truncated"]) is not bool
                    or not isinstance(result["skills"], list) or len(result["skills"]) > 250):
                raise SkillReadError("INVALID_SKILLS_UNAVAILABLE")
            return {"capability": CAPABILITY, "skills": [cls._row(row, system=system) for row in result["skills"]],
                    "truncated": result["truncated"]}
        if (set(result) != {"capability", "skill", "content", "sha256", "bytes"}
                or not isinstance(result["content"], str) or type(result["bytes"]) is not int
                or not 0 <= result["bytes"] <= 64 * 1024 or not isinstance(result["sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", result["sha256"])):
            raise SkillReadError("INVALID_SKILLS_UNAVAILABLE")
        return {**{key: result[key] for key in ("capability", "content", "sha256", "bytes")},
                "skill": cls._row(result["skill"], system=system)}

    async def handle(self, method, params, *, can_read_system=False, can_edit_personal=False):
        if not isinstance(params, dict) or not isinstance(method, str):
            raise SkillReadError("INVALID_SKILLS_REQUEST")
        if method.startswith("skills.system.") and can_read_system is not True:
            raise SkillReadError("FORBIDDEN")
        from hermes_runtime.builtin_skills import LIST_METHOD, SELECT_METHOD
        if method in {LIST_METHOD, SELECT_METHOD}:
            return await self._builtin(method, params, can_edit_personal)
        if method == UPDATE_METHOD:
            return await self._update(params, can_edit_personal=can_edit_personal)
        sources = {"skills.library.list": ("personal", False), "skills.library.read": ("personal", True),
                   "skills.system.list": ("system", False), "skills.system.read": ("system", True)}
        source = sources.get(method)
        if source is not None and not source[1] and not params:
            path = "/api/skills/library/" + source[0]
        elif source is not None and source[1] and set(params) == {"name"}:
            path = "/api/skills/library/" + source[0] + "/content?" + urlencode({"name": canonical_name(params["name"])})
        else:
            raise SkillReadError("INVALID_SKILLS_REQUEST")
        try:
            result = await asyncio.to_thread(self.auth.authenticated_request, path, method="GET")
        except HTTPError as error:
            raise SkillReadError(self._native_error(error) or "INVALID_SKILLS_UNAVAILABLE") from None
        except (OSError, ValueError):
            raise SkillReadError("INVALID_SKILLS_UNAVAILABLE") from None
        result = self._project(result, method)
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES - 1024:
            raise SkillReadError("INVALID_SKILLS_RESPONSE_TOO_LARGE")
        return result

    async def _builtin(self, method, params, can_edit):
        from hermes_runtime.builtin_skills import SELECT_METHOD, validate_select
        select = method == SELECT_METHOD
        if select and can_edit is not True:
            raise SkillReadError('FORBIDDEN')
        if select:
            validate_select(params)
        elif params:
            raise SkillReadError('INVALID_SKILLS_REQUEST')
        try:
            result = await asyncio.to_thread(self.auth.authenticated_request,
                '/api/skills/library/builtin/selection' if select else '/api/skills/library/builtin',
                params if select else None, method='POST' if select else 'GET')
        except HTTPError as error:
            raise SkillReadError(self._native_error(error) or 'INVALID_SKILLS_BUILTIN_UNAVAILABLE') from None
        except (OSError, ValueError):
            raise SkillReadError('INVALID_SKILLS_BUILTIN_UNAVAILABLE') from None
        return self._project(result, method)

    async def _update(self, params, *, can_edit_personal=False):
        if can_edit_personal is not True:
            raise SkillReadError("FORBIDDEN")
        validate_update_request(params)
        try:
            result = await asyncio.to_thread(self.auth.authenticated_request,
                "/api/skills/library/personal/content", params, method="POST")
        except HTTPError as error:
            raise SkillReadError(self._native_error(error) or "INVALID_SKILLS_UNAVAILABLE") from None
        except (OSError, ValueError):
            raise SkillReadError("INVALID_SKILLS_UNAVAILABLE") from None
        result = self._project(result, UPDATE_METHOD)
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES - 1024:
            raise SkillReadError("INVALID_SKILLS_RESPONSE_TOO_LARGE")
        return result

    async def bridge(self, remote_url, credential, *, can_read_system=False, can_edit_personal=False):
        from websockets.asyncio.client import connect
        async with connect(remote_url, additional_headers={"Authorization": f"Bearer {credential}"},
                           max_size=MAX_UPDATE_REQUEST_BYTES, open_timeout=15, close_timeout=5, proxy=None) as remote:
            async for raw in remote:
                identity = None
                try:
                    if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > MAX_UPDATE_REQUEST_BYTES:
                        raise SkillReadError("INVALID_SKILLS_REQUEST_TOO_LARGE")
                    request = json.loads(raw)
                    if (isinstance(request, dict) and request.get("method") != UPDATE_METHOD
                            and len(raw.encode("utf-8") if isinstance(raw, str) else raw) > 16384):
                        raise SkillReadError("INVALID_SKILLS_REQUEST_TOO_LARGE")
                    if (not isinstance(request, dict) or set(request) != {"jsonrpc", "id", "method", "params"}
                            or request["jsonrpc"] != "2.0" or type(request["id"]) not in {int, str}
                            or len(str(request["id"])) > 120):
                        raise SkillReadError("INVALID_SKILLS_REQUEST")
                    identity = request["id"]
                    result = await self.handle(request["method"], request["params"], can_read_system=can_read_system,
                                               can_edit_personal=can_edit_personal)
                    response = {"jsonrpc": "2.0", "id": identity, "result": result}
                except SkillReadError as error:
                    response = {"jsonrpc": "2.0", "id": identity, "error": {
                        "code": 409 if str(error) in {"CONFLICT_SKILL_CHANGED", "CONFLICT_BUILTIN_SKILLS_CHANGED"} else 400, "message": str(error)}}
                except Exception:  # health: allow BLE001 -- only a stable code crosses this boundary; upstream ticket URLs/body are private
                    response = {"jsonrpc": "2.0", "id": identity, "error": {"code": 503, "message": "INVALID_SKILLS_UNAVAILABLE"}}
                await remote.send(json.dumps(response, ensure_ascii=False))
