"""Integration tests for the deployer: they really create a venv, start/stop uvicorn and swap code.

Uses a temp runtime dir and port 18000 so it never touches the real runtime or port 8000.
Needs the mynews repo (default C:\\mywork\\mynews, override with MYNEWS_REPO) and network for pip.
"""
import os
import zipfile
from pathlib import Path

import pytest

from deployer import Config, Deployer

MYNEWS_REPO = Path(os.environ.get("MYNEWS_REPO", r"C:\mywork\mynews"))
PORT = 18000


def make_zip(dest: Path, version: str, main_py: str | None = None, drop: str | None = None) -> Path:
    """Build a package from the mynews repo, with a different VERSION and optional tweaks."""
    with zipfile.ZipFile(dest, "w") as z:
        for p in sorted((MYNEWS_REPO / "app").rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                arc = p.relative_to(MYNEWS_REPO).as_posix()
                if arc == "app/main.py" and main_py is not None:
                    z.writestr(arc, main_py)
                else:
                    z.write(p, arc)
        z.write(MYNEWS_REPO / "requirements.txt", "requirements.txt")
        if drop != "VERSION":
            z.writestr("VERSION", version + "\n")
    return dest


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    base = tmp_path_factory.mktemp("deployer")
    pkgs = base / "dist"
    pkgs.mkdir()
    d = Deployer(Config(runtime_dir=base / "runtime", package_dir=pkgs, app_port=PORT))
    yield d, pkgs
    d.stop()


def test_01_first_deploy(env):
    d, pkgs = env
    r = d.apply_patch(str(make_zip(pkgs / "mynews-1.0.0.zip", "1.0.0")))
    assert r["ok"], r
    st = d.status()
    assert st["running"] and st["running_version"] == "1.0.0"


def test_02_upgrade_creates_backup(env):
    d, pkgs = env
    r = d.apply_patch(str(make_zip(pkgs / "mynews-1.1.0.zip", "1.1.0")))
    assert r["ok"], r
    assert r["old_version"] == "1.0.0" and r["new_version"] == "1.1.0"
    st = d.status()
    assert st["running_version"] == "1.1.0"
    assert any(b.endswith("v1.0.0") for b in st["backups"])


def test_03_broken_patch_rolls_back(env):
    d, pkgs = env
    broken = make_zip(pkgs / "mynews-2.0.0.zip", "2.0.0", main_py="this is not python(\n")
    r = d.apply_patch(str(broken))
    assert not r["ok"] and r["rolled_back"], r
    st = d.status()
    assert st["running"] and st["running_version"] == "1.1.0"


def test_04_crash_at_import_rolls_back(env):
    # A package whose app process dies right at startup (as opposed to never turning healthy).
    d, pkgs = env
    crashing = make_zip(pkgs / "mynews-3.0.0.zip", "3.0.0",
                        main_py="raise RuntimeError('boom at import')\n")
    r = d.apply_patch(str(crashing))
    assert not r["ok"] and r["rolled_back"], r
    assert d.status()["running_version"] == "1.1.0"


def test_05_explicit_rollback(env):
    d, _ = env
    r = d.rollback()
    assert r["ok"], r
    assert r["from_version"] == "1.1.0" and r["to_version"] == "1.0.0"
    assert d.status()["running_version"] == "1.0.0"


def test_06_rejects_bad_packages(env, tmp_path):
    d, pkgs = env
    outside = make_zip(tmp_path / "mynews-9.9.9.zip", "9.9.9")
    assert "must be inside" in d.apply_patch(str(outside))["error"]

    missing = make_zip(pkgs / "mynews-4.0.0.zip", "4.0.0", drop="VERSION")
    assert "missing VERSION" in d.apply_patch(str(missing))["error"]

    evil = pkgs / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../escape.txt", "x")
    assert "unsafe path" in d.apply_patch(str(evil))["error"]

    notzip = pkgs / "fake.zip"
    notzip.write_text("not a zip")
    assert "valid zip" in d.apply_patch(str(notzip))["error"]

    # none of the rejected patches may have disturbed the running app
    assert d.status()["running_version"] == "1.0.0"


def test_07_restart(env):
    d, _ = env
    old_pid = d.status()["pid"]
    r = d.restart()
    assert r["started"] and r["pid"] != old_pid
    assert d.status()["running_version"] == "1.0.0"
