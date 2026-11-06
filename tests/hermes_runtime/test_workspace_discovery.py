"""Autoconfiguration parses only bounded secure public metadata, without accounts."""

import socket
import struct

import pytest

from hermes_runtime.workspace import sandbox_discovery as discovery

XML = '''<clientConfig version="1.1"><emailProvider id="example.invalid">
<incomingServer type="imap"><hostname>mail.example.invalid</hostname><port>993</port><socketType>SSL</socketType><authentication>password-cleartext</authentication><username>%EMAILADDRESS%</username></incomingServer>
<outgoingServer type="smtp"><hostname>mail.example.invalid</hostname><port>587</port><socketType>STARTTLS</socketType><authentication>password-cleartext</authentication><username>%EMAILADDRESS%</username></outgoingServer>
</emailProvider></clientConfig>'''


def public(*args):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 993))]


def test_xml_autoconfig_matches_existing_secure_form_contract(monkeypatch):
    monkeypatch.setattr(discovery.socket, "getaddrinfo", public)
    result = discovery.parse_config(XML, "fixture@example.invalid")
    assert result["inbound"] == {"host": "mail.example.invalid", "port": 993, "security": "tls"}
    assert result["outbound"]["security"] == "starttls"
    assert result["username"] == "fixture@example.invalid"
    assert discovery.parse_config('<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>' + XML, "fixture@example.invalid") is None
    assert discovery.parse_config("x" * 65537, "fixture@example.invalid") is None


def test_private_xml_endpoint_is_not_offered(monkeypatch):
    monkeypatch.setattr(discovery.socket, "getaddrinfo", lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 993))])
    assert discovery.parse_config(XML, "fixture@example.invalid") is None


def test_corporate_mx_is_only_a_candidate_until_native_tls_protocol_probe(monkeypatch):
    monkeypatch.setattr(discovery, "dns_records", lambda name, kind: [{"priority": 0, "name": "mail.corporate.invalid"}] if kind == 15 else [])
    monkeypatch.setattr(discovery, "config_xml", lambda url: "")
    seen = []
    monkeypatch.setattr(discovery, "discover", lambda request: seen.append(request) or {"inbound": {"host": "mail.corporate.invalid", "port": 993, "security": "tls"}, "outbound": {"host": "mail.corporate.invalid", "port": 587, "security": "starttls"}})
    result = discovery.discover_address({"address": "synthetic@corporate.invalid"})
    assert result["inbound"]["host"] == "mail.corporate.invalid"
    assert seen[0]["hosts"][0] == "mail.corporate.invalid"


def test_dns_compression_rejects_pointer_loops_and_truncated_labels():
    with pytest.raises(ValueError):
        discovery.dns_name(b"\xc0\x00", 0)
    with pytest.raises(ValueError):
        discovery.dns_name(b"\x08abc", 0)
    assert discovery.dns_name(b"\x04mail\x07example\x00", 0) == ("mail.example", 14)
