"""Protect node services before Kubernetes/Docker forwarding rules can accept traffic.

Runs as root on a node. Tenant Pods live only on workers; the tainted control
plane hosts system Pods. NetworkPolicy restricts tenant-to-tenant traffic and
which tenant may reach the remote private API/Panel. No Pod-IP allowlist is used.
"""

import argparse
import ipaddress
import subprocess


def rules(node_ip, peer_ip, panel_ip, controller_cidr, *, ipv6=False):
    protected = "6443,9345,10250,10256,10257,10259"
    lines = ["*mangle", ":AS-KUBE-PRE - [0:0]", "-F AS-KUBE-PRE",
             "-A AS-KUBE-PRE -i lo -j RETURN",
             "-A AS-KUBE-PRE -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN"]
    if not ipv6:
        lines += [
            f"-A AS-KUBE-PRE -i wg-agentsee -s {peer_ip}/32 -d {node_ip}/32 -p tcp -m multiport --dports {protected} -j RETURN",
            f"-A AS-KUBE-PRE -i wg-agentsee -s {peer_ip}/32 -d {node_ip}/32 -p udp --dport 8472 -j RETURN",
            f"-A AS-KUBE-PRE -s {controller_cidr} -d {node_ip}/32 -p tcp --dport 10250 -j RETURN",
            f"-A AS-KUBE-PRE -s 10.240.0.0/16 -d {panel_ip}/32 -p tcp -m multiport --dports 443,6443 -j RETURN",
            # Node-local traffic is exempt from some CNI policies. Block new Pod->host connections here.
            "-A AS-KUBE-PRE -s 10.240.0.0/16 -m addrtype --dst-type LOCAL -j DROP",
            "-A AS-KUBE-PRE -s 10.240.0.0/16 -d 10.240.0.0/16 -j RETURN",
            "-A AS-KUBE-PRE -s 10.240.0.0/16 -d 10.241.0.0/16 -j RETURN",
        ]
        lines += [f"-A AS-KUBE-PRE -s 10.240.0.0/16 -d {network} -j DROP"
                  for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")]
    lines += [
        f"-A AS-KUBE-PRE -m addrtype --dst-type LOCAL -p tcp -m multiport --dports {protected} -j DROP",
        "-A AS-KUBE-PRE -m addrtype --dst-type LOCAL -p udp --dport 8472 -j DROP",
        "-A AS-KUBE-PRE -j RETURN", "COMMIT", "",
    ]
    return "\n".join(lines)


def apply(node_ip, peer_ip, panel_ip, controller_cidr):
    for value in (node_ip, peer_ip, panel_ip):
        ipaddress.IPv4Address(value)
    ipaddress.IPv4Network(controller_cidr)
    for tool, restore, ipv6 in (("iptables", "iptables-restore", False), ("ip6tables", "ip6tables-restore", True)):
        subprocess.run([restore, "--wait", "5", "--noflush"],
                       input=rules(node_ip, peer_ip, panel_ip, controller_cidr, ipv6=ipv6), text=True, check=True, timeout=15)
        existing = subprocess.run([tool, "-w", "5", "-t", "mangle", "-C", "PREROUTING", "-j", "AS-KUBE-PRE"],
                                  capture_output=True, check=False, timeout=15)
        if existing.returncode:
            subprocess.run([tool, "-w", "5", "-t", "mangle", "-I", "PREROUTING", "1", "-j", "AS-KUBE-PRE"],
                           check=True, timeout=15)
    # Preserve the existing Docker sandbox restriction; return CNI packets to CNI policy evaluation.
    chain = subprocess.run(["iptables", "-w", "5", "-S", "AS-MGMT"], capture_output=True, check=False, timeout=15)
    if chain.returncode == 0:
        for port in (443, 6443):
            args = ["AS-MGMT", "-s", "10.240.0.0/16", "-d", panel_ip + "/32", "-p", "tcp", "--dport", str(port), "-j", "RETURN"]
            exists = subprocess.run(["iptables", "-w", "5", "-C", *args], capture_output=True, check=False, timeout=15)
            if exists.returncode:
                subprocess.run(["iptables", "-w", "5", "-I", *args], check=True, timeout=15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("node-ip", "peer-ip", "panel-ip", "controller-cidr"):
        parser.add_argument("--" + key, required=True)
    apply(**vars(parser.parse_args()))


if __name__ == "__main__":
    main()
