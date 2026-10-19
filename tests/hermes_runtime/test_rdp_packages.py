"""Provision the shared desktop using native packages or a verified source backport."""

import subprocess
from types import SimpleNamespace

import pytest


def test_package_versions_handle_epochs_and_vendor_revisions():
    from hermes_runtime.standalone.rdp_packages import compatible

    assert compatible({"xrdp": "0.10.1-3.1+deb13u2", "xorgxrdp": "1:0.10.2-1"})
    assert not compatible({"xrdp": "0.9.24-4", "xorgxrdp": "1:0.10.2-1"})
    assert not compatible({"xrdp": "0.10.6.1-1", "xorgxrdp": "1:0.9.19-1"})
    assert not compatible({"xrdp": "0.10.6.1-1"})


def test_missing_packages_install_from_compatible_native_repository(monkeypatch):
    from hermes_runtime.standalone import rdp_packages as packages

    installed = {}
    calls = []
    monkeypatch.setattr(packages, "versions", lambda: installed.copy())
    monkeypatch.setattr(packages, "candidates", lambda: {"xrdp": "0.10.1-1", "xorgxrdp": "1:0.10.2-1"})
    monkeypatch.setattr(packages, "command", lambda name: [name])
    monkeypatch.setattr(packages, "back_up_config", lambda: None)
    def apt(*args):
        calls.append(args)
        if args[0] == "install":
            installed.update(xrdp="0.10.1-1", xorgxrdp="1:0.10.2-1")
    monkeypatch.setattr(packages, "apt", apt)
    monkeypatch.setattr(packages, "build_backport", lambda: pytest.fail("unnecessary source build"))
    packages.ensure()
    assert any("xrdp" in call and "xorgxrdp" in call for call in calls)


def test_old_repository_is_backported_instead_of_silently_installing_09(monkeypatch):
    from hermes_runtime.standalone import rdp_packages as packages

    installed = {"xrdp": "0.9.24-4", "xorgxrdp": "1:0.9.19-1"}
    calls = []
    monkeypatch.setattr(packages, "versions", lambda: installed.copy())
    monkeypatch.setattr(packages, "candidates", lambda: installed.copy())
    monkeypatch.setattr(packages, "command", lambda name: [name])
    monkeypatch.setattr(packages, "back_up_config", lambda: calls.append("backup"))
    monkeypatch.setattr(packages, "apt", lambda *args: calls.append(args))
    def build():
        calls.append("build")
        installed.update(xrdp="0.10.6.1+hermes1", xorgxrdp="1:0.10.5+hermes1")
    monkeypatch.setattr(packages, "build_backport", build)
    packages.ensure()
    assert calls.index("backup") < calls.index("build")


def test_compatible_installation_does_not_reinstall_the_rdp_stack(monkeypatch):
    from hermes_runtime.standalone import rdp_packages as packages

    monkeypatch.setattr(packages, "versions", lambda: {"xrdp": "0.10.6.1-1", "xorgxrdp": "1:0.10.5-1"})
    monkeypatch.setattr(packages, "command", lambda name: [name])
    monkeypatch.setattr(packages, "locate_command", lambda name: SimpleNamespace(command=(name,)))
    monkeypatch.setattr(packages, "apt", lambda *args: pytest.fail("reinstalled a working RDP stack"))
    packages.ensure()


def test_failed_package_installation_propagates_without_claiming_success(monkeypatch):
    from hermes_runtime.standalone import rdp_packages as packages

    monkeypatch.setattr(packages, "versions", dict)
    monkeypatch.setattr(packages, "command", lambda name: [name])
    monkeypatch.setattr(packages, "back_up_config", lambda: None)
    def fail(*args):
        raise subprocess.CalledProcessError(100, ["apt-get"])
    monkeypatch.setattr(packages, "apt", fail)
    with pytest.raises(subprocess.CalledProcessError):
        packages.ensure()


def test_unverified_source_is_discarded_before_it_can_be_extracted(tmp_path, monkeypatch):
    import io
    from hermes_runtime.standalone import rdp_backport

    monkeypatch.setattr(rdp_backport.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"tampered source"))
    path = tmp_path / "source.tar.gz"
    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        rdp_backport.download("https://example.invalid/source.tar.gz", path, "0" * 64)
    assert not path.exists()


def test_backport_keeps_native_packaging_and_marks_its_local_version(tmp_path):
    from hermes_runtime.standalone.rdp_backport import adapt_packaging

    folder = tmp_path / "debian"
    folder.mkdir()
    (folder / "control").write_text("Build-Depends:\n systemd-dev [linux-any],\n libssl-dev,\nDepends:\n sysvinit-utils (>= 3.06-4),\n ${shlibs:Depends},\n")
    (folder / "xrdp.init").write_text("unused SysV script")
    (folder / "changelog").write_text("xrdp (0.10.6.1-2~bpo13+1) unstable; urgency=medium\n")
    (folder / "xrdp.postinst").write_text("native certificate and PAM setup")
    adapt_packaging(tmp_path)
    assert "libsystemd-dev" in (folder / "control").read_text()
    assert "${shlibs:Depends}" in (folder / "control").read_text()
    assert not (folder / "xrdp.init").exists()
    assert (folder / "xrdp.postinst").read_text() == "native certificate and PAM setup"
    assert "+hermes1)" in (folder / "changelog").read_text()
