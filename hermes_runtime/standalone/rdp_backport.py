"""Build pinned official Debian source packages against the VM's own libraries.

No foreign binary repository, PPA, or untracked `make install` is introduced.
Debian's package scripts own PAM, certificates, service units and conffile upgrades.
"""

import hashlib
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from hermes_runtime.standalone.rdp import command

# The orig tarballs match the corresponding signed upstream GitHub releases.
SOURCES = {
    "xrdp": {
        "source": "0.10.6.1", "revision": "0.10.6.1-2~bpo13+1",
        "dsc": "dfeb569d21e98944782c6c4ec6a14d4d9ca0e7a6c8e29153476fa3b920c4b063",
        "orig.tar.gz": "2f7beb5a3b2529c8d72dc0df9b8cdca31ab0e0c14d1e3421210f5e6ec0ab3b75",
        "debian.tar.xz": "5b3ee13cb9b11379f097d3564d098d0cbd9d5d7fde7fadb9a80ecb429be11d71",
    },
    "xorgxrdp": {
        "source": "0.10.5", "revision": "0.10.5-2~bpo13+1",
        "dsc": "87a57e0e6895c991a127809d9abc1716c903bc66e074110bb53270ba49759e02",
        "orig.tar.gz": "a5d03435f0ef48bf3d5010e63d9264f2334e7063cba3ecd8d4c0a15616a4f712",
        "debian.tar.xz": "18bde92e95772d9877cb07a55a41f0f84e9532009211a8da321c4fa24e31992c",
    },
}


def download(url, destination, checksum):
    digest = hashlib.sha256()
    # A fixed maximum also bounds failure on a misconfigured remote endpoint.
    size = 0
    with urllib.request.urlopen(url, timeout=60) as response, destination.open("wb") as output:
        while chunk := response.read(65536):
            size += len(chunk)
            if size > 20_000_000:
                raise RuntimeError(f"Oversized RDP source archive: {destination.name}")
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != checksum:
        destination.unlink()
        raise RuntimeError(f"SHA256 mismatch for RDP source: {destination.name}")


def prepare(folder, name):
    spec = SOURCES[name]
    for suffix in ("dsc", "orig.tar.gz", "debian.tar.xz"):
        version = spec["source"] if suffix == "orig.tar.gz" else spec["revision"]
        filename = f"{name}_{version}.{suffix}"
        download(f"https://deb.debian.org/debian/pool/main/x/{name}/{filename}", folder / filename, spec[suffix])
    target = folder / name
    subprocess.run([*command("dpkg-source"), "--no-check", "-x", str(folder / f"{name}_{spec['revision']}.dsc"), str(target)],
        check=True, timeout=60, stdout=sys.stderr)
    adapt_packaging(target)
    return target


def adapt_packaging(source):
    """Only systemd is supported here; omit the newer SysV-only helper dependency.

    Ubuntu 22.04 provides systemd.pc through libsystemd-dev, whereas newer
    distributions split it into systemd-dev (a dependency of libsystemd-dev).
    """
    control = source / "debian" / "control"
    text = control.read_text().replace(" systemd-dev [linux-any],", " libsystemd-dev [linux-any],")
    text = text.replace(" sysvinit-utils (>= 3.06-4),\n", "")
    control.write_text(text)
    (source / "debian" / "xrdp.init").unlink(missing_ok=True)
    changelog = source / "debian" / "changelog"
    changelog.write_text(changelog.read_text().replace(") ", "+hermes1) ", 1))


def install():
    from hermes_runtime.standalone.rdp_packages import apt

    # Build as the invoking desktop user. Only the package manager runs with sudo.
    with tempfile.TemporaryDirectory(prefix="hermes-rdp-build-") as temporary:
        folder = Path(temporary)
        for name in SOURCES:
            print(f"Building {name} for this VM from verified sources...", file=sys.stderr, flush=True)
            source = prepare(folder, name)
            apt("build-dep", str(source))
            subprocess.run([*command("dpkg-buildpackage"), "-b", "-us", "-uc", "-j2"], cwd=source,
                env={**os.environ, "DEB_BUILD_OPTIONS": "parallel=2 nocheck", "DEB_BUILD_PROFILES": "nocheck"},
                check=True, timeout=1800, stdout=sys.stderr)
            packages = [str(path) for path in folder.glob(f"{name}_*.deb") if "-dbgsym_" not in path.name]
            if len(packages) != 1:
                raise RuntimeError(f"Expected one built {name} package; got {packages}")
            apt("install", *packages)
