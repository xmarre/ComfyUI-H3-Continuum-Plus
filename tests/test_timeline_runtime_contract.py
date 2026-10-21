"""No-scan runtime compatibility and duplicate frontend registration tests."""

from ComfyUI_H3_Continuum_Join.timeline.protocol import (
    NATIVE_PATH_CONTRACT,
    native_runtime_manifest,
)
from pathlib import Path

import pytest


def test_runtime_contract_matches_real_served_sources():
    root = Path(__file__).resolve().parents[1]
    version = NATIVE_PATH_CONTRACT
    desktop = (root / "desktop/timeline-native/connection.cjs").read_text(encoding="utf-8")
    editor = (root / "web/timeline/editor.js").read_text(encoding="utf-8")
    entry = (root / "web/timeline_studio.js").read_text(encoding="utf-8")
    assert 'const NATIVE_PATH_CONTRACT = "' + version + '";' in desktop
    assert 'export const NATIVE_PATH_CONTRACT = "' + version + '";' in editor
    assert 'const H3_TIMELINE_ENTRY_CONTRACT = "' + version + '";' in entry
    assert 'editor.js?v=' + version in entry


def test_native_runtime_manifest_identifies_registered_extension(tmp_path):
    web = tmp_path / "installed" / "web"
    web.mkdir(parents=True)
    manifest = native_runtime_manifest(
        {"other": tmp_path / "elsewhere", "Continuum Plus": web}, web
    )
    assert manifest == {
        "contract": NATIVE_PATH_CONTRACT,
        "transport": "native-selected-path",
        "drive_scan": False,
        "browser_movie_upload": False,
        "editor_url": "/extensions/Continuum%20Plus/timeline/editor.js",
        "entry_url": "/extensions/Continuum%20Plus/timeline_studio.js",
    }


def test_missing_or_duplicate_frontend_registration_fails_closed(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    for mapping in ({}, {"first": web, "second": web}):
        with pytest.raises(ValueError, match="missing or ambiguous"):
            native_runtime_manifest(mapping, web)


def test_manifest_uses_exact_registered_directory_only(tmp_path):
    original = tmp_path / "old" / "web"
    current = tmp_path / "new" / "web"
    original.mkdir(parents=True)
    current.mkdir(parents=True)
    with pytest.raises(ValueError, match="missing or ambiguous"):
        native_runtime_manifest({"old": original}, current)
