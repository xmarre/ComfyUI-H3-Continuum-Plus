"""Mounted drive allowlisting never recursively enumerates media files."""
from ComfyUI_H3_Continuum_Join.timeline import mounts


def test_mount_discovery_is_mountpoint_only(tmp_path, monkeypatch):
    root = tmp_path / "mnt"
    root.mkdir()
    for name in ("c", "d", "shared", "e"):
        (root / name).mkdir()
    monkeypatch.setattr(mounts.os.path, "ismount", lambda p: p.name in ("c", "d"))
    def forbid_walk(*args, **kwargs):
        raise AssertionError("Recursive scanning is forbidden")
    monkeypatch.setattr(mounts.os, "walk", forbid_walk)
    assert [p.name for p in mounts.wsl_drive_roots(root)] == ["c", "d"]
    assert mounts.wsl_drive_roots(tmp_path / "nonexistent") == ()
