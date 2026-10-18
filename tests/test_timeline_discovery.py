"""No-copy resolution from a browser File identity to an existing source."""

import hashlib

import pytest

from ComfyUI_H3_Continuum_Join.timeline import discovery
from ComfyUI_H3_Continuum_Join.timeline.media import (
    AssetContentChanged,
    AssetStore,
)
from tests.test_timeline_media import movie  # noqa: F401  (shared FFmpeg fixture)


def fingerprint(path):
    content = path.read_bytes()
    size = discovery.CHUNK_BYTES
    return {
        "name": path.name,
        "size": len(content),
        "chunk_size": size,
        "chunks": [
            hashlib.sha256(content[i : i + size]).hexdigest()
            for i in range(0, len(content), size)
        ],
    }


def test_drag_uses_existing_movie_without_upload_copy(movie, tmp_path):
    drive = tmp_path / "d"
    (drive / "Movies").mkdir(parents=True)
    original = drive / "Movies" / "Source.mkv"
    movie.rename(original)
    store = AssetStore(tmp_path / "studio" / "assets", link_roots=[drive])
    identity = fingerprint(original)
    path, chunks = discovery.find_original(identity, [drive])
    assert path == original.resolve()
    linked = store.link(path, expected_chunks=chunks)
    assert linked["storage"] == "linked"
    assert store.verify(linked["id"]) == original.resolve()
    files = list(store.root.iterdir())
    assert len(files) == 1
    assert files[0].name == linked["id"] + ".link.json"
    assert files[0].stat().st_size < original.stat().st_size
    assert AssetStore(store.root, link_roots=[drive]).verify(linked["id"]) == original


def test_drag_lookups_are_case_insensitive_but_verify_content(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "CHUNK_BYTES", 64)
    drive = tmp_path / "d"
    drive.mkdir()
    good = drive / "MY Movie.MKV"
    payload = bytes(range(256)) * 2
    good.write_bytes(payload)
    search = fingerprint(good)
    search["name"] = "my movie.mkv"
    path, hashes = discovery.find_original(search, [drive])
    assert path == good.resolve()
    assert hashes == tuple(search["chunks"])
    bad = bytearray(payload)
    bad[0] ^= 1
    good.write_bytes(bad)
    with pytest.raises(ValueError, match="not found"):
        discovery.find_original(search, [drive])


def test_all_chunks_checked_not_only_search_samples(movie, tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "CHUNK_BYTES", 64)
    drive = tmp_path / "d"
    drive.mkdir()
    source = drive / "different.mkv"
    # The source has six chunks. Mutate chunk 1, outside sampled 0/3/5.
    original = bytearray(movie.read_bytes())
    if len(original) < 64 * 6:
        original.extend(b"x" * (64 * 6 - len(original)))
    source.write_bytes(original)
    identity = fingerprint(source)
    changed = bytearray(original)
    changed[70] ^= 1
    source.write_bytes(changed)
    path, hashes = discovery.find_original(identity, [drive])
    store = AssetStore(tmp_path / "studio" / "assets", link_roots=[drive])
    with pytest.raises(AssetContentChanged, match="does not match"):
        store.link(path, expected_chunks=hashes)
    assert not list(store.root.glob("*.link.json"))


def test_ambiguous_same_content_is_rejected_without_writing(movie, tmp_path):
    drive = tmp_path / "d"
    (drive / "a").mkdir(parents=True)
    (drive / "b").mkdir()
    first = drive / "a" / "original.mkv"
    second = drive / "b" / "ORIGINAL.mkv"
    first.write_bytes(movie.read_bytes())
    second.write_bytes(movie.read_bytes())
    with pytest.raises(ValueError, match="Multiple identical candidates"):
        discovery.find_original(fingerprint(first), [drive])


def test_no_matching_source_never_imports_data(movie, tmp_path):
    drive = tmp_path / "d"
    drive.mkdir()
    store = AssetStore(tmp_path / "studio" / "assets", link_roots=[drive])
    with pytest.raises(ValueError, match="not found"):
        discovery.find_original(fingerprint(movie), [drive])
    assert not list(store.root.iterdir())


def test_cancelled_or_unsafe_fingerprint_fails_before_linking(movie, tmp_path):
    drive = tmp_path / "d"
    drive.mkdir()
    file = drive / "original.mkv"
    file.write_bytes(movie.read_bytes())
    identity = fingerprint(file)
    called = []

    def cancel():
        called.append(True)
        raise InterruptedError("cancel")

    with pytest.raises(InterruptedError):
        discovery.find_original(identity, [drive], cancel=cancel)
    assert called
    for modification in (
        {"name": "../original.mkv"},
        {"name": "C:\\original.mkv"},
        {"chunk_size": 1},
        {"chunks": ["0" * 63]},
        {"size": -1},
        {"size": True},
    ):
        invalid = {**identity, **modification}
        with pytest.raises(ValueError, match="Invalid"):
            discovery.validate_fingerprint(invalid)


def test_symlink_outside_root_is_never_selected(movie, tmp_path):
    drive = tmp_path / "d"
    drive.mkdir()
    shortcut = drive / "source.mkv"
    shortcut.symlink_to(movie)
    with pytest.raises(ValueError, match="not found"):
        discovery.find_original(fingerprint(movie), [drive])


def test_duplicate_nested_search_root_does_not_create_ambiguity(movie, tmp_path):
    drive = tmp_path / "d"
    nested = drive / "Movies"
    nested.mkdir(parents=True)
    target = nested / "source.mkv"
    movie.rename(target)
    path, _ = discovery.find_original(fingerprint(target), [drive, nested])
    assert path == target.resolve()


def test_only_mounted_drive_letters_are_discovered(tmp_path, monkeypatch):
    mount = tmp_path / "mnt"
    mount.mkdir()
    for child in ("c", "d", "shared", "e"):
        (mount / child).mkdir()
    monkeypatch.setattr(
        discovery.os.path, "ismount",
        lambda p: p.name in {"c", "d"},
    )
    assert [p.name for p in discovery.wsl_drive_roots(mount)] == ["d", "c"]
    assert discovery.wsl_drive_roots(tmp_path / "unmounted") == ()
