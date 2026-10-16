from __future__ import annotations

import hashlib

import pytest
import torch

# Import the public sequence path first (the package has eager node exports).
from ComfyUI_H3_Continuum_Join.v2 import sequence
from ComfyUI_H3_Continuum_Join.v2 import conditioning_lifecycle as lifecycle
from ComfyUI_H3_Continuum_Join.masked_continuation import (
    CONTINUATION_GUIDE, CONTINUATION_NATIVE_MASKED, continuation_method_scope,
)
from ComfyUI_H3_Continuum_Join.constants import PROMPT_MODE_TIMELINE
from ComfyUI_H3_Continuum_Join.v2.prompts import make_prompt_plan
from tests.test_native_masked_sequence import _install_common_runtime


def run_case(monkeypatch, *, enabled, method, chunks=2, fail_prefetch=False):
    events = []
    kwargs = _install_common_runtime(monkeypatch, events)
    monkeypatch.setattr(sequence, "apply_native_masked_continuation",
                        lambda latent, **_: {**latent, "noise_mask": "native-mask"})
    monkeypatch.setenv("H3C_CONDITIONING_LOOKAHEAD", str(int(enabled)))
    kwargs["prompt_plan"] = make_prompt_plan(
        mode=PROMPT_MODE_TIMELINE,
        script="\n".join(f"[{i*5}-{(i+1)*5}s]\nAction {i}" for i in range(chunks)),
        chunks=chunks, chunk_seconds=5,
    )

    class Clip:
        encode_count = 0

        def tokenize(self, prompt, **options):
            return prompt

        def encode_from_tokens_scheduled(self, tokens):
            self.encode_count += 1
            events.append(("encode", tokens))
            if fail_prefetch and self.encode_count == 2:
                raise RuntimeError("simulated speculative-only encode failure")
            digest = hashlib.sha256(tokens.encode()).digest()
            return [[torch.tensor(list(digest), dtype=torch.float32)[None, None], {}]]

    kwargs["clip"] = Clip()
    original = sequence.sample_chunk
    received = []

    def sample(**args):
        received.append(args["conditioning"][0][0].clone())
        return original(**args)

    monkeypatch.setattr(sequence, "sample_chunk", sample)
    with continuation_method_scope(method):
        sequence.run_sequence(**kwargs)
    return [e[0] for e in events if e[0] in {"encode", "sample"}], received


@pytest.mark.parametrize("chunks", [1, 2, 3, 4])
def test_lookahead_preserves_conditioning_and_pairs_encodes(monkeypatch, chunks):
    sequential_order, sequential = run_case(
        monkeypatch, enabled=False, method=CONTINUATION_NATIVE_MASKED, chunks=chunks)
    planned_order, planned = run_case(
        monkeypatch, enabled=True, method=CONTINUATION_NATIVE_MASKED, chunks=chunks)
    assert sequential_order == [v for _ in range(chunks) for v in ("encode", "sample")]
    assert planned_order == [v for i in range(0, chunks, 2)
                             for v in ["encode"] * min(2, chunks-i) + ["sample"] * min(2, chunks-i)]
    assert len(planned) == len(sequential) == chunks
    for a, b in zip(planned, sequential):
        assert torch.equal(a, b)


def test_motion_dependent_auto_remains_sequential(monkeypatch):
    order, _ = run_case(monkeypatch, enabled=True, method=CONTINUATION_GUIDE)
    assert order == ["encode", "sample", "encode", "sample"]


def test_cpu_snapshot_is_independent_and_bounded(monkeypatch):
    source = [[torch.ones(1, 2, 3), {"tags": torch.arange(2)}]]
    snapshot = lifecycle.bounded_cpu_conditioning(source)
    source[0][0].zero_()
    source[0][1]["tags"].zero_()
    assert torch.equal(snapshot[0][0], torch.ones(1, 2, 3))
    assert torch.equal(snapshot[0][1]["tags"], torch.arange(2))
    monkeypatch.setattr(lifecycle, "MAX_PREFETCH_BYTES", 1)
    assert lifecycle.bounded_cpu_conditioning(source) is None


def test_lookahead_failure_does_not_abort_current_chunk(monkeypatch):
    order, received = run_case(
        monkeypatch, enabled=True, method=CONTINUATION_NATIVE_MASKED,
        fail_prefetch=True,
    )
    assert order == ["encode", "encode", "sample", "encode", "sample"]
    assert len(received) == 2


def test_snapshot_rejects_opaque_gpu_owner_and_preserves_aliases():
    tensor = torch.arange(6)
    source = [[tensor, {"ref": tensor, "bad": object()}]]
    assert lifecycle.bounded_cpu_conditioning(source) is None
    source[0][1].pop("bad")
    snapshot = lifecycle.bounded_cpu_conditioning(source)
    assert snapshot[0][0] is snapshot[0][1]["ref"]
    assert snapshot[0][0].data_ptr() != tensor.data_ptr()
    tensor.add_(3)
    assert torch.equal(snapshot[0][0], torch.arange(6))
