from __future__ import annotations


def test_physical_timeline_video_transport_is_parked(monkeypatch):
    import ComfyUI_H3_Continuum_Join.v2.physical_prompts as physical_prompts
    import ComfyUI_H3_Continuum_Join.v2.physical_sequence as physical_sequence
    import ComfyUI_H3_Continuum_Join.v2.sequence as sequence

    monkeypatch.setenv("H3_CONTINUUM_PHYSICAL_TIMELINE_VIDEO", "1")

    assert physical_prompts.physical_timeline_video_enabled() is False
    assert physical_sequence.physical_timeline_video_enabled() is False
    assert sequence.physical_timeline_video_enabled() is False
