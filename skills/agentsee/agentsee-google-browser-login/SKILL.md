---
name: agentsee-google-browser-login
description: "Sign in to Google in the shared browser with the user."
version: 1.0.0
author: Mikhail Arlashkin, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    category: agentsee
    tags: [agentsee, google, browser, login, human-control]
    related_skills: [computer-use]
---

# Google Browser Login Skill

Sign in to a Google account in this agent's existing headed browser, using authorized native protected autofill when available, with the user handling manual entry and security challenges. This is a platform skill shipped with AgentSee; Google Workspace API grants are a separate workflow.

## When to Use

Load this skill before Google account sign-in in a browser, including a Gmail, Drive, Docs or YouTube page asking the user to sign in. A request to connect Google Workspace APIs or mail permissions does not itself authorize a browser login.

## Prerequisites

- The user's request authorizes opening the sign-in page for the requested service.
- Use the available native `browser_exec`, `browser_navigate` or `computer_use` and the agent's own persistent Chrome. Manual entry and security challenges use only the Chrome-window human-control view in that employee's Panel Settings.
- If human control is unavailable, report the limitation before manual entry or a security challenge. An already-authorized protected native fill does not need a new consent gate.

## How to Run

1. Coordinate with the parent and children before using their shared browser. Reuse your own working tab and the existing persistent profile.
2. Open the requested Google service's normal sign-in page through the native browser tools. When `browser_exec` is available, retain the pending page with its native `keep_tab()` helper before the handoff. In a CUA-only session, use the existing Chrome and the same Panel Chrome-window handoff; do not invent a tab-retention tool.
3. If `browser_vault` is available and an authorized opaque handle exists for the requested login, use its `fill` action at the exact saved origin in this same profile. Native fill injects credentials internally; never read input values back. Saving credentials uses the existing protected credential form, never chat, ordinary tool arguments or custom scripts.
4. If manual entry or a challenge is needed and you are a child, report the required human handoff to the parent/coordinator and wait; the parent coordinates the user. Otherwise, when protected fill is unavailable, refused or the fields are not ready, ask the user to open this employee's **Настройки сотрудника → Google в браузере сотрудника → Войти в Google**. The existing Panel view shows only that employee's Chrome window, including its address bar. The user completes sign-in and challenges there, then clicks **Готово** to close the human connection and return control; that button does not verify Google authentication. Preserve existing working tabs; if a challenge is already open, the user can select that tab in this same Chrome window. Do not request the full Desktop, invoke this configuration action through agent tools, or invent a control-status API or poll. A chat message alone is not proof that the native human lease has returned. If the next ordinary native tool call still refuses human control, stop and request the handoff.
5. After protected fill, or after human control returns, check only a minimal visible signed-in state for the requested service. Report a challenge or pending sign-in honestly.

## Quick Reference

- Password: authorized native `browser_vault` autofill when available; otherwise the user. 2FA, CAPTCHA and security checks: the employee's Panel Settings Chrome-window handoff; **Готово** returns control.
- Existing Chrome is occupied or ownership is uncertain: preserve it and report the native refusal.
- Browser login versus API access: a browser session does not grant MCP/API access; API access does not sign the browser in.

## Procedure

### Keep the platform browser

Use the platform's normal launcher and current profile. Its headed Chrome launch uses a verified positive debugging port; the platform manages discovery and ownership. Existing live Chrome keeps its current launch settings until its next ordinary launch.

Do not choose debugging flags or a port, attach to a guessed CDP endpoint, start another Chrome/profile/window, use incognito, delete Singleton files, clear cookies or user data, or kill an existing browser to force login. Do not add automation-hiding patches or bypass Google's security checks. A local browser check is not proof that Google will accept a login.

### Use protected fill or a human handoff

A successful credential save means stored, not filled or signed in: check the separate native fill result/count. Password-only forms are supported; a no-fields result does not mean the vault is empty or require a username field. Check only non-secret page readiness/current tab; a later native fill can retry once fields are ready, without bypassing origin/profile/lease checks. Never inject secrets through custom browser/CUA code.

When protected fill cannot complete the step, leave the page ready and use only **Настройки сотрудника → Google в браузере сотрудника → Войти в Google** for the human handoff. The user works in the real Chrome window and finishes with **Готово**. If this existing Panel view is unavailable, report the limitation; do not substitute a full Desktop view, a new browser/profile or an agent-generated login form. MFA, CAPTCHA, recovery and security checks always go to the user. Never ask for a Google password, verification code, backup code or session token in chat. Do not inspect entered secret values, record authentication artifacts, or extract cookies or tokens. Do not observe the page or issue browser/CUA calls during the user's authentication.

While the user has control, do not navigate, type, close tabs or let a child act on the shared browser. Resume only after the native control lease is returned. Inspect the minimum ordinary page state needed to tell whether the requested service is signed in; do not inspect account-security details or session storage.

### Recover without losing state

A memory watchdog may close Chrome while preserving the profile. Continue the already-authorized goal through the next ordinary native browser/CUA request, whose headroom gate controls lazy restart. Do not use a timer or manually spawn Chrome. If pressure or profile ownership still blocks it, stop and report the refusal; do not restart a container, remove locks or repeat a prior authentication submission. If the interrupted sign-in stage is unknown, first check the requested service's minimal ordinary state; a challenge goes to the human, and an eligible password prompt may use the same protected-fill branch. Stored credentials do not guarantee authentication.

After browser work finishes, normal platform cleanup may close an idle sign-in or service tab. Keep unfinished forms, human-controlled tabs and another executor's active work protected. A closed tab does not imply that the persistent profile's login has been erased.

## Pitfalls

- A challenge, consent prompt or sign-in page is not a completed login. Tell the user what remains.
- If Google rejects an insecure browser, live Chrome may retain earlier launch settings. After all shared work ends and the user agrees, close it normally and let the next native request reopen the same profile. Do not force a restart or change flags; a local browser check does not prove Google acceptance.
- Do not change mail configuration, OAuth applications, provider settings or MCP grants to solve a browser-login request.
- Do not replay an interrupted sign-in, send or payment action without checking its outcome and the user's authorization.

## Verification

Report success only after native control has returned and the requested service shows an ordinary signed-in state. Otherwise say that sign-in is awaiting the user, blocked by a challenge or unavailable. Do not include private account contents or authentication artifacts in the report.
