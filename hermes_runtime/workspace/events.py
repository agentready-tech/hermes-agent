"""Stable mailbox event identities and cursor transitions, owned by the agent."""

import json
from datetime import datetime, UTC
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlencode

from hermes_runtime.workspace.mail import gmail_text, parse_text_mime
from hermes_runtime.workspace.store import WorkspaceError


def timestamp(value):
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        try:
            moment = parsedate_to_datetime(str(value))
        except (TypeError, ValueError):
            return None
    return moment.replace(tzinfo=UTC).timestamp() if moment.tzinfo is None else moment.timestamp()


def accepts(connection, sender, received):
    since = timestamp(connection["since"])
    return bool(sender and received is not None and since is not None and received >= since and
        (connection.get("senderPolicy") == "all" or sender in connection.get("allowedSenders", [])))


def incoming(workspace, record, cursor):
    connection = record["connection"]
    if not connection.get("proactive") or "mailRead" not in connection["services"] or (connection.get("senderPolicy") != "all" and not connection.get("allowedSenders")):
        return {"events": [], "cursor": cursor}
    if connection["provider"] == "google":
        return gmail_incoming(workspace, record, cursor)
    if not connection.get("verifiedAt"):
        return {"events": [], "cursor": cursor}
    batch = workspace.mail(record, "incoming", cursor=cursor, since=connection["since"], format="text-v1")
    events = []
    for message in batch["messages"]:
        if message.get("format") != "text-v1":
            raise WorkspaceError("INVALID_MAIL_CONTENT")
        parsed = parse_text_mime(message)
        if not accepts(connection, parsed["from"], timestamp(message.get("internalDate"))):
            continue
        events.append({"id": f"{batch['cursor']['validity']}:{message['uid']}", "source": "email:" + connection["id"],
            "prompt": json.dumps(parsed, ensure_ascii=False), "metadata": {"connection_id": connection["id"], "message_id": parsed["messageId"]}})
    return {"events": events, "cursor": batch["cursor"]}


def gmail_listing(workspace, record, cursor):
    connection = record["connection"]
    get = lambda path: workspace.google(record, "gmail", "GET", path)
    history_id = cursor.get("historyId") or connection.get("gmailHistoryId")
    if cursor.get("pending"):
        return cursor["pending"], dict(cursor)
    if history_id and not cursor.get("fallback"):
        try:
            params = {"startHistoryId": history_id, "historyTypes": "messageAdded", "labelId": "INBOX", "maxResults": 25}
            if cursor.get("page"):
                params["pageToken"] = cursor["page"]
            history = get("history?" + urlencode(params))
            ids = list(dict.fromkeys(entry["message"]["id"] for row in history.get("history", []) for entry in row.get("messagesAdded", [])))
            return ids, {"historyId": history_id, "nextHistoryId": str(history["historyId"]), "nextPage": history.get("nextPageToken")}
        except WorkspaceError as error:
            if str(error) != "INVALID_GOOGLE_NOT_FOUND":
                raise
            return gmail_listing(workspace, record, {"fallback": True})
    snapshot = cursor.get("snapshot") or str(get("profile")["historyId"])
    sender_query = "" if connection.get("senderPolicy") == "all" else " {" + " ".join("from:" + sender for sender in connection["allowedSenders"]) + "}"
    query = f"in:inbox after:{int(timestamp(connection['since'])) - 60}" + sender_query
    params = {"maxResults": 25, "q": query}
    if cursor.get("page"):
        params["pageToken"] = cursor["page"]
    listing = get("messages?" + urlencode(params))
    next_cursor = {"fallback": True, "snapshot": snapshot, "page": listing["nextPageToken"]} if listing.get("nextPageToken") else {"historyId": snapshot}
    return [row["id"] for row in listing.get("messages", [])], next_cursor


def gmail_prompt(parsed):
    return "\n".join(["Email from: " + parsed["from"], "Subject: " + parsed["subject"], "Date: " + str(parsed.get("date") or ""), "",
        parsed["text"][:16_000] or "(No text body; use mail_read for message metadata.)",
        *(["Attachments: " + ", ".join(item["name"] for item in parsed["attachments"])] if parsed["attachments"] else [])])


def gmail_incoming(workspace, record, cursor):
    connection = record["connection"]
    ids, next_cursor = gmail_listing(workspace, record, cursor)
    if len(ids) > 1000:
        raise WorkspaceError("INVALID_MAIL_HISTORY_BATCH_TOO_LARGE")
    events = []
    for identity in ids[:25]:
        try:
            message = workspace.google(record, "gmail", "GET", "messages/" + quote(identity, safe="") + "?format=full")
        except WorkspaceError as error:
            if str(error) == "INVALID_GOOGLE_NOT_FOUND":
                continue
            raise
        parsed = gmail_text(message)
        try:
            received = float(message["internalDate"]) / 1000
        except (KeyError, TypeError, ValueError):
            continue
        if not accepts(connection, parsed["from"], received):
            continue
        events.append({"id": identity, "source": "gmail:" + connection["id"], "prompt": gmail_prompt(parsed),
            "metadata": {"thread_id": message.get("threadId"), "connection_id": connection["id"]}})
    if len(ids) > 25:
        next_cursor["pending"] = ids[25:]
    else:
        next_cursor.pop("pending", None)
        if next_cursor.get("nextHistoryId"):
            next_cursor = {"historyId": next_cursor["historyId"], "page": next_cursor["nextPage"]} if next_cursor.get("nextPage") else {"historyId": next_cursor["nextHistoryId"]}
    return {"events": events, "cursor": next_cursor}
