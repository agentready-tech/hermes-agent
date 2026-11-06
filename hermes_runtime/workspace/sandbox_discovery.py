"""Unauthenticated mail autoconfiguration on the employee sandbox's network."""

import http.client
import struct
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

if __name__ != "__main__":
    from hermes_runtime.workspace.sandbox_mail import MailError, discover, ipaddress, main, public_socket, re, secrets, socket, ssl


def hostname(value):
    if not isinstance(value, str):
        return None
    try:
        host = value.rstrip(".").encode("idna").decode().lower()
    except UnicodeError:
        return None
    if len(host) > 253 or "." not in host or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        return host


def dns_name(packet, offset, visited=None):
    visited = set() if visited is None else visited
    labels, end = [], None
    while True:
        if offset >= len(packet) or offset in visited or len(visited) > 128:
            raise ValueError("Invalid DNS name")
        visited.add(offset)
        size = packet[offset]
        if size & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise ValueError("Invalid DNS pointer")
            pointer = ((size & 63) << 8) | packet[offset + 1]
            name, _ = dns_name(packet, pointer, visited)
            labels.append(name)
            end = end or offset + 2
            break
        if size > 63 or offset + 1 + size > len(packet):
            raise ValueError("Invalid DNS label")
        offset += 1
        if not size:
            end = end or offset
            break
        labels.append(packet[offset:offset + size].decode("ascii"))
        offset += size
    return ".".join(labels), end


def dns_records(name, kind):
    identity = secrets.randbelow(65536)
    question = b"".join(bytes([len(part)]) + part.encode("ascii") for part in name.split(".")) + b"\0" + struct.pack("!HH", kind, 1)
    query = struct.pack("!HHHHHH", identity, 0x100, 1, 0, 0, 0) + question
    try:
        with open("/etc/resolv.conf", encoding="ascii") as source:
            servers = [line.split()[1] for line in source if line.startswith("nameserver ")][:2]
    except OSError:
        return []
    for server in servers:
        client = None
        try:
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.settimeout(2)
            client.connect((server, 53))
            client.send(query)
            packet = client.recv(65536)
            found, flags, questions, answers, _, _ = struct.unpack("!HHHHHH", packet[:12])
            if found != identity or flags & 0x020F or not flags & 0x8000 or questions != 1 or packet[12:12 + len(question)] != question:
                continue
            offset, records = 12 + len(question), []
            for _ in range(min(answers, 100)):
                _, offset = dns_name(packet, offset)
                record_kind, record_class, _, length = struct.unpack("!HHIH", packet[offset:offset + 10])
                offset += 10
                if offset + length > len(packet):
                    raise ValueError("Invalid DNS record")
                if record_class == 1 and record_kind == kind:
                    if kind == 15 and length >= 3:
                        target, _ = dns_name(packet, offset + 2)
                        records.append({"priority": struct.unpack("!H", packet[offset:offset + 2])[0], "name": target})
                    if kind == 33 and length >= 7:
                        target, _ = dns_name(packet, offset + 6)
                        priority, weight, port = struct.unpack("!HHH", packet[offset:offset + 6])
                        records.append({"priority": priority, "weight": weight, "port": port, "name": target})
                offset += length
            return sorted(records, key=lambda row: (row["priority"], -row.get("weight", 0)))
        except (OSError, ValueError, UnicodeError, struct.error):
            continue
        finally:
            if client:
                client.close()
    return []


class DiscoveryHTTPS(http.client.HTTPSConnection):
    def connect(self):
        self.sock = self._context.wrap_socket(public_socket(self.host, self.port), server_hostname=self.host)


def config_xml(url, redirects=0):
    parsed = urlsplit(url)
    host = hostname(parsed.hostname)
    if not host or parsed.scheme != "https" or parsed.port not in {None, 443} or parsed.username or parsed.password or redirects > 2:
        return ""
    client = DiscoveryHTTPS(host, timeout=3, context=ssl.create_default_context())
    try:
        client.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""), headers={"Accept": "application/xml, text/xml", "User-Agent": "agentsee-mail-setup"})
        response = client.getresponse()
        if response.status in {301, 302, 303, 307, 308}:
            location = response.getheader("Location")
            return config_xml(urljoin(url, location), redirects + 1) if location else ""
        if response.status != 200:
            return ""
        data = response.read(65537)
        return data.decode("utf-8") if len(data) <= 65536 else ""
    except (OSError, ValueError, UnicodeError):
        return ""
    finally:
        client.close()


def parse_config(xml, address):
    if len(xml.encode()) > 65536 or re.search(r"<!DOCTYPE|<!ENTITY", xml, re.I):
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    for provider in root.findall(".//emailProvider"):
        result = {"kind": "imap_smtp", "address": address, "username": address}
        users = {}
        for tag, kind, key in (("incomingServer", "imap", "inbound"), ("outgoingServer", "smtp", "outbound")):
            for server in provider.findall(tag):
                host = hostname(server.findtext("hostname"))
                security = {"SSL": "tls", "STARTTLS": "starttls"}.get(server.findtext("socketType"))
                port = server.findtext("port", "")
                auth = [item.text for item in server.findall("authentication")]
                if server.get("type") != kind or not host or not security or not port.isdigit() or not 1 <= int(port) <= 65535 or not {"password-cleartext", "password-encrypted"}.intersection(auth):
                    continue
                # Resolve and validate inside the same namespace before returning a candidate.
                addresses = socket.getaddrinfo(host, int(port), socket.AF_INET, socket.SOCK_STREAM)
                if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
                    continue
                user = server.findtext("username", address).replace("%EMAILADDRESS%", address).replace("%EMAILLOCALPART%", address.split("@")[0]).replace("%EMAILDOMAIN%", address.split("@")[1])
                users[key] = user if len(user) <= 254 and not re.search(r"[%\s]", user) else address
                result[key] = {"host": host, "port": int(port), "security": security}
                break
        if "inbound" in result or "outbound" in result:
            result["username"] = users.get("inbound", users.get("outbound", address))
            if users.get("outbound", result["username"]) != result["username"]:
                result.pop("outbound", None)
            return result
    return None


def srv_config(domain):
    result = {}
    for prefix, security, key in (("_imaps._tcp.", "tls", "inbound"), ("_imap._tcp.", "starttls", "inbound"), ("_submissions._tcp.", "tls", "outbound"), ("_submission._tcp.", "starttls", "outbound")):
        for record in dns_records(prefix + domain, 33):
            host = hostname(record["name"])
            if key not in result and host and 1 <= record["port"] <= 65535:
                try:
                    rows = socket.getaddrinfo(host, record["port"], socket.AF_INET, socket.SOCK_STREAM)
                    if rows and all(ipaddress.ip_address(row[4][0]).is_global for row in rows):
                        result[key] = {"host": host, "port": record["port"], "security": security}
                except OSError:
                    continue
    return result


def discover_address(request):
    global TIMEOUT
    TIMEOUT = 3
    raw = request.get("address", "")
    if not isinstance(raw, str) or len(raw) > 254 or not re.fullmatch(r'[^\s@<>"\\]+@[^\s@<>"\\]+', raw.strip()):
        raise MailError("INVALID_MAIL_ADDRESS")
    local, domain = raw.strip().lower().split("@")
    domain = hostname(domain)
    if not domain or len(local) > 64:
        raise MailError("INVALID_MAIL_ADDRESS")
    address = local + "@" + domain
    mx = dns_records(domain, 15)
    hosts = [hostname(row["name"]) for row in mx]
    if domain in {"gmail.com", "googlemail.com"} or any(re.search(r"(^|\.)(google|googlemail)\.com$", host or "") for host in hosts):
        return {"kind": "google", "address": address}
    provider = next(("outlook.com" if host.endswith(".mail.protection.outlook.com") else "yandex.ru" if re.search(r"(^|\.)yandex\.(net|ru)$", host) else "mail.ru" if re.search(r"(^|\.)mail\.ru$", host) else None for host in hosts if host and (host.endswith(".mail.protection.outlook.com") or re.search(r"(^|\.)(yandex\.(net|ru)|mail\.ru)$", host))), None)
    urls = [f"https://autoconfig.{domain}/mail/config-v1.1.xml", f"https://{domain}/.well-known/autoconfig/mail/config-v1.1.xml"]
    urls += ["https://autoconfig.thunderbird.net/v1.1/" + item for item in dict.fromkeys([domain] + ([provider] if provider else []))]
    configs = []
    for url in urls:
        try:
            item = parse_config(config_xml(url), address)
        except OSError:
            item = None
        if item:
            configs.append(item)
            if item.get("inbound") and item.get("outbound"):
                return item
    result = {"kind": "imap_smtp", "address": address, "username": address, **srv_config(domain)}
    if not result.get("inbound") or not result.get("outbound"):
        if configs:
            result.update(configs[0])
        candidates = list(dict.fromkeys([host for host in hosts[:1] if host] + ["mail." + domain, "imap." + domain, "smtp." + domain]))[:4]
        found = discover({"hosts": candidates})
        for key in ("inbound", "outbound"):
            if not result.get(key) and found.get(key):
                result[key] = found[key]
    return result


if __name__ == "__main__":
    execute = discover_address
    main()
