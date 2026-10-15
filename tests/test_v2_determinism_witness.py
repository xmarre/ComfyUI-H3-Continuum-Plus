"""CPU contract tests for the opt-in H3 sampler-input identity receipt."""

import logging

import torch

from ComfyUI_H3_Continuum_Join.v2 import determinism_witness as witness


def test_tensor_fingerprint_is_content_sensitive_and_non_mutating():
    tensor = torch.tensor([[1.0, 2.0], [3.0, 4.0]])
    original = tensor.clone()
    state = torch.random.get_rng_state().clone()
    first = witness.fingerprint(tensor)
    assert first == witness.fingerprint(tensor.clone())
    assert torch.equal(tensor, original)
    assert torch.equal(torch.random.get_rng_state(), state)
    tensor[1, 1] += 1
    assert witness.fingerprint(tensor)["sha256"] != first["sha256"]
    assert witness.fingerprint(tensor.to(torch.float64))["sha256"] != first["sha256"]


def test_conditioning_hash_includes_tensor_bytes_and_scalar_metadata():
    text = torch.ones((1, 2, 3))
    ref = torch.ones((1, 24, 1, 2, 2))
    cond = [[text, {"minimax_refs": [{"kind": "image", "latent": ref}]}]]
    first = witness.fingerprint(cond)
    assert len(first["tensors"]) == 2
    assert first == witness.fingerprint([[text.clone(), {
        "minimax_refs": [{"latent": ref.clone(), "kind": "image"}],
    }]])
    ref[0, 0, 0, 0, 0] += 0.1
    assert witness.fingerprint(cond)["sha256"] != first["sha256"]
    ref[0, 0, 0, 0, 0] -= 0.1
    cond[0][1]["minimax_refs"][0]["kind"] = "video"
    assert witness.fingerprint(cond)["sha256"] != first["sha256"]


def test_disabled_receipt_does_no_hashing(monkeypatch, caplog):
    monkeypatch.delenv("H3C_DETERMINISM_WITNESS", raising=False)
    monkeypatch.setattr(witness, "fingerprint", lambda unused: 1 / 0)
    witness.emit_sampler_witness("before_sample", seed=12, noise=torch.zeros(1))
    assert not caplog.records


def test_enabled_receipt_preserves_inputs_rng_and_prompt_privacy(monkeypatch, caplog):
    monkeypatch.setenv("H3C_DETERMINISM_WITNESS", "1")
    caplog.set_level(logging.INFO, logger="h3_continuum_join")
    noise = torch.zeros((2, 3))
    state = torch.random.get_rng_state().clone()
    witness.emit_sampler_witness("before_sample", seed=123, noise=noise,
                                 conditioning=[["private prompt", {"ref": torch.ones(2)}]])
    assert torch.equal(torch.random.get_rng_state(), state)
    assert torch.count_nonzero(noise).item() == 0
    assert "H3C-PT233 sampler-determinism receipt" in caplog.text
    assert '"resolved_seed":123' in caplog.text
    assert "private prompt" not in caplog.text


def test_failed_witness_does_not_abort_or_change_sampling_inputs(monkeypatch, caplog):
    monkeypatch.setenv("H3C_DETERMINISM_WITNESS", "1")
    monkeypatch.setattr(witness, "fingerprint", lambda unused: 1 / 0)
    input_tensor = torch.ones(1)
    with caplog.at_level(logging.ERROR, logger="h3_continuum_join"):
        assert witness.emit_sampler_witness("before_sample", seed=3, noise=input_tensor) is None
    assert torch.equal(input_tensor, torch.ones(1))
    assert "receipt failed" in caplog.text


def test_nested_audio_video_tensors_are_hashed_independently():
    class AV:
        is_nested = True

        def __init__(self, video, audio):
            self.pieces = (video, audio)

        def unbind(self):
            return self.pieces

    video = torch.zeros((1, 24, 2, 3, 4))
    audio = torch.zeros((1, 32, 2, 5))
    nested = AV(video, audio)
    result = witness.fingerprint(nested)
    assert [leaf["path"] for leaf in result["tensors"]] == [
        "value[0]", "value[1]",
    ]
    audio[0, 0, 0, 0] = 1
    assert witness.fingerprint(nested)["sha256"] != result["sha256"]
