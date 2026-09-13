from __future__ import annotations

import copy

from ComfyUI_H3_Continuum_Join.run_storage import _apply_nonce_contract, revision_identity


def _contract(*, source_digest: str, compiler: str = "physical_timeline_text_v1"):
    base = {
        "global": {
            "sampling_contract_version": 5,
            "model": "same-model",
            "sampler": "same-sampler",
        },
        "chunk_count": 3,
        "prompt_mode": "timeline",
        "prompt_hashes": ["1" * 64, "2" * 64, "3" * 64],
        "prompt_source_digest": source_digest,
        "physical_prompt_policy": compiler,
        "timeline_video_adapter": "legacy_nominal_chunk_v1",
        "timeline_video_chunk_contracts": [],
        "reroll_from_chunk": 0,
        "last_frame_hash": "",
        "nonce_lineage_sha256": "a" * 64,
        "nonce_request_sha256": "b" * 64,
    }
    return _apply_nonce_contract(base, requested_nonce=0, effective_nonce=0)


def test_source_and_compiler_identity_change_revision_without_globally_salting_static_chunks():
    first = _contract(source_digest="a" * 64)
    source_changed = _contract(source_digest="b" * 64)
    compiler_changed = _contract(
        source_digest="a" * 64,
        compiler="physical_timeline_text_v2",
    )

    assert revision_identity(first) != revision_identity(source_changed)
    assert revision_identity(first) != revision_identity(compiler_changed)
    assert first["chunk_contract_hashes"] == source_changed["chunk_contract_hashes"]
    assert first["chunk_contract_hashes"] == compiler_changed["chunk_contract_hashes"]


def test_unrelated_later_prompt_edit_preserves_static_prefix_candidate():
    first = _contract(source_digest="a" * 64)
    edited = copy.deepcopy(first)
    edited["prompt_hashes"][-1] = "9" * 64
    edited = _apply_nonce_contract(edited, requested_nonce=0, effective_nonce=0)

    assert first["chunk_contract_hashes"][:2] == edited["chunk_contract_hashes"][:2]
    assert first["chunk_contract_hashes"][2] != edited["chunk_contract_hashes"][2]
