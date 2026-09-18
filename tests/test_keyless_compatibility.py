from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from ComfyUI_H3_Continuum_Join.compatibility import ensure_native_h3_base_model
from ComfyUI_H3_Continuum_Join.constants import CONTINUUM_INTEROP_KEY
from ComfyUI_H3_Continuum_Join.model_patch import clone_model_for_chunk, patch_model


KEYLESS_CONTRACT_KEY = "minimax_h3_keyless_contract_v1"


class _KeylessContract:
    api = 1
    architecture = "h3_keyless_core50_v1"
    core_blocks = 50
    token_refiner = "native_qkv"
    token_refiner_blocks = 2
    routing_source = "value"
    retrieval_source = "raw_projected_value"
    projection_attr = "qv_proj"
    qv_order = "q_effective;v"


class _QVAttention:
    def __init__(self):
        self.qv_proj = object()
        self.route_norm = object()


class _KeylessInner:
    __module__ = "minimax_h3_keyless.model"

    def __init__(self):
        self.blocks = [SimpleNamespace(attn=_QVAttention()) for _ in range(50)]
        self.final_layer = object()
        self.patch_size = (1, 2, 2)
        self.latents_dim = 24
        self.audio_latents_dim = 32
        self.rope_freqs = lambda position_ids, device: position_ids
        setattr(self, KEYLESS_CONTRACT_KEY, _KeylessContract())


class _KeylessPatcher:
    def __init__(self, *, inner=None):
        self.model = SimpleNamespace(diffusion_model=inner or _KeylessInner())
        self.model_options = {}
        self.added = []
        self.removed = []
        self.wrappers = {}
        self.patches = {}
        self.object_patches = {}

    def clone(self):
        clone = _KeylessPatcher(inner=self.model.diffusion_model)
        clone.model = self.model
        clone.model_options = {
            key: dict(value) if isinstance(value, dict) else value
            for key, value in self.model_options.items()
        }
        clone.wrappers = self.wrappers
        clone.patches = self.patches
        clone.object_patches = self.object_patches
        return clone

    def add_wrapper_with_key(self, wrapper_type, key, wrapper):
        self.added.append((wrapper_type, key, wrapper))

    def remove_wrappers_with_key(self, wrapper_type, key):
        self.removed.append((wrapper_type, key))


def _install_wrapper_api(monkeypatch):
    extension = ModuleType("comfy.patcher_extension")
    extension.WrappersMP = SimpleNamespace(APPLY_MODEL="apply_model")
    comfy = sys.modules.get("comfy") or ModuleType("comfy")
    monkeypatch.setitem(sys.modules, "comfy", comfy)
    monkeypatch.setitem(sys.modules, "comfy.patcher_extension", extension)


def test_continuum_model_contract_does_not_require_main_block_k_projection():
    inner = _KeylessInner()
    assert all(hasattr(block.attn, "qv_proj") for block in inner.blocks)
    assert all(not hasattr(block.attn, "qkv_proj") for block in inner.blocks)

    ensure_native_h3_base_model(SimpleNamespace(diffusion_model=inner))


def test_patch_model_preserves_keyless_diffusion_object_and_contract(monkeypatch):
    _install_wrapper_api(monkeypatch)
    source = _KeylessPatcher()
    inner = source.model.diffusion_model
    contract = getattr(inner, KEYLESS_CONTRACT_KEY)

    patched = patch_model(source, strict=True, debug=False)

    assert patched.model.diffusion_model is inner
    assert getattr(patched.model.diffusion_model, KEYLESS_CONTRACT_KEY) is contract
    assert patched.removed[0][1] == "h3_continuum_join.apply_model.v1"
    assert patched.added[0][1] == "h3_continuum_join.apply_model.v1"


def test_chunk_clone_preserves_keyless_attention_transport_and_adds_only_continuum_hint(monkeypatch):
    _install_wrapper_api(monkeypatch)
    source = _KeylessPatcher()

    provider = object()
    preprocessors = (object(), object())
    value_domain = object()
    routing_position_domain = object()
    mask = object()
    measure = object()
    exact_blocks = object()
    backend_history = object()
    attention_override = object()

    keyless_options = {
        "minimax_h3_keyless_provider_v1": provider,
        "minimax_h3_keyless_routing_preprocessors_v1": preprocessors,
        "minimax_h3_keyless_value_domain_v1": value_domain,
        "minimax_h3_keyless_routing_position_domain_v1": routing_position_domain,
        "minimax_h3_keyless_mask_v1": mask,
        "minimax_h3_keyless_log_measure_v1": measure,
        "minimax_h3_keyless_exact_blocks_v1": exact_blocks,
        "attention_backend_history_v1": backend_history,
        "optimized_attention_override": attention_override,
    }
    source.model_options = {"transformer_options": dict(keyless_options)}

    chunk = clone_model_for_chunk(
        source,
        strict=True,
        debug=False,
        chunk_index=2,
        context_frames=39,
    )

    source_transformer = source.model_options["transformer_options"]
    chunk_transformer = chunk.model_options["transformer_options"]

    assert CONTINUUM_INTEROP_KEY not in source_transformer
    assert chunk_transformer is not source_transformer
    for key, value in keyless_options.items():
        assert chunk_transformer[key] is value

    assert chunk_transformer[CONTINUUM_INTEROP_KEY] == {
        "api": 1,
        "active": True,
        "min_actual_prefix_steps": 2,
        "chunk_index": 2,
        "context_frames": 39,
    }


def test_chunk_clone_keeps_keyless_inner_model_shared_without_attention_replacement(monkeypatch):
    _install_wrapper_api(monkeypatch)
    source = _KeylessPatcher()
    inner = source.model.diffusion_model

    chunk = clone_model_for_chunk(
        source,
        strict=True,
        debug=False,
        chunk_index=1,
        context_frames=None,
    )

    assert chunk.model.diffusion_model is inner
    assert chunk.object_patches == {}
    assert all(not hasattr(block.attn, "qkv_proj") for block in inner.blocks)
