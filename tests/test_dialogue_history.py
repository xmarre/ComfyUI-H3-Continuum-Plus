"""Transport tests; these do not establish trained-model speech quality."""
import copy
import json

import pytest
import torch

from ComfyUI_H3_Continuum_Join.constants import PROMPT_MODE_TIMELINE
from ComfyUI_H3_Continuum_Join.v2 import dialogue_history as history
from ComfyUI_H3_Continuum_Join.v2.physical_prompts import (
    make_physical_sample_descriptor, physical_metadata, validate_physical_metadata,
)
from ComfyUI_H3_Continuum_Join.v2.physical_runtime import compile_invocation_prompt
from ComfyUI_H3_Continuum_Join.v2.prompts import make_prompt_plan


def descriptor(index=1, *, exact=True, external=False):
    retained = [0, 175, 328, 481][index]
    context = 39 if index else 0
    return make_physical_sample_descriptor(
        group_id=f'chunk:{index+1}', logical_indices=(index,), retained_before=retained,
        context_frames=context, total_frames=209 if index else 175,
        target_duration_frames=672, continuation_method='Native Masked' if exact else 'Guide / Motion Context',
        initial_state_origin='external' if external else 'sequence',
        include_first=False, include_last=False, presentation_contract={},
        exact_protected=bool(index and exact), guided_overlap=bool(index and not exact),
    )


def plan(text):
    return make_prompt_plan(mode=PROMPT_MODE_TIMELINE, script=text, chunks=4, chunk_seconds=7.0)


def compile_(p, d):
    return compile_invocation_prompt(p, d, legacy_text='unused', candidate=True)


@pytest.mark.parametrize('exact', [True, False])
def test_adjacent_dialogue_is_encoder_history_only_and_live_text_keeps_ownership(monkeypatch, exact):
    p = plan('Shared: (S1) is a low, breathy voice.\n'
             '[0-7s]\n(S1) says: <d>[English] The old sentence.</d>\n'
             '[7-14s]\n(S2) says: <d>[English] A new answer.</d>\n'
             '[14-21s]\nA deliberate scene change.\n[21-28s]\nSilent landscape.')
    original = copy.deepcopy(p)
    d = descriptor(exact=exact)
    monkeypatch.delenv(history.ENV, raising=False)
    baseline = compile_(p, d)
    monkeypatch.setenv(history.ENV, '1')
    got = compile_(p, d)
    assert p == original
    assert got.text == baseline.text
    assert got.compiler_version == baseline.compiler_version + '+' + history.POLICY
    assert 'The old sentence.' not in got.text
    assert 'The old sentence.' in got.encoder_history
    assert 'A new answer.' in got.text
    assert 'A new answer.' not in got.encoder_history
    assert got.physical_conditioning_hash != baseline.physical_conditioning_hash
    assert validate_physical_metadata(physical_metadata(d, got))
    assert compile_(p, d) == got
    corrupted = physical_metadata(d, got)
    corrupted['compiled']['encoder_history'] += 'changed'
    with pytest.raises(ValueError, match='conditioning hash'):
        validate_physical_metadata(corrupted)


@pytest.mark.parametrize('index', [0, 1, 2, 3])
def test_no_dialogue_is_byte_identical_and_first_chunk_has_no_history(monkeypatch, index):
    p = plan('[0-7s]\nLeaves move.\n[7-14s]\nWind.\n[14-21s]\nA scene cut.\n[21-28s]\nWaves.')
    d = descriptor(index)
    baseline = compile_(p, d)
    monkeypatch.setenv(history.ENV, '1')
    assert compile_(p, d) == baseline


def test_external_state_does_not_invent_a_transcript(monkeypatch):
    monkeypatch.setenv(history.ENV, '1')
    p = plan('[0-7s]\n<d>[English] Earlier.</d>\n[7-14s]\n<d>[English] Later.</d>')
    assert not compile_(p, descriptor(external=True)).encoder_history
    assert not compile_(p, descriptor(0)).encoder_history


def test_boundary_history_respects_inner_intervals_and_author_override(monkeypatch):
    monkeypatch.setenv(history.ENV, '1')
    p = plan('[0-7s]\n0-5s:\n<d>[English] Too old.</d>\n5-7s:\n(S1) says <d>[English] Already spoken.</d>\n'
             '[7-14s]\n7-8s:\n(S1) says <d>[English] Ongoing phrase.</d>\n8-14s:\n<d>[English] Future line.</d>')
    compiled = compile_(p, descriptor())
    assert 'Already spoken.' in compiled.encoder_history
    assert 'Too old.' not in compiled.encoder_history
    assert 'Future line.' not in compiled.encoder_history
    assert 'Future line.' in compiled.text
    data = json.loads(compiled.encoder_history.splitlines()[1])
    assert [item['utterance_continues_past_boundary'] for item in data['intervals']] == [False]
    assert data['intervals'][-1]['global_end'] == '7'
    assert 'Ongoing phrase.' not in compiled.encoder_history


class CausalClip:
    """Small deterministic causal encoder with variable-size visual expansion."""
    def __init__(self):
        self.calls = []

    def tokenize(self, text, **options):
        refs = [( {'image': 3}, 1.0)] if options else []
        return {'qwen3vl_32b': [[*refs, *[(ord(c), 1.0) for c in text]]]}

    def encode_from_tokens_scheduled(self, tokens):
        self.calls.append(copy.deepcopy(tokens))
        values, tags = [], []
        for token, _ in tokens['qwen3vl_32b'][0]:
            if isinstance(token, dict):
                values.extend([1000., 1001., 1002.]); tags.extend([0, 0, 0])
            else:
                values.append(float(token)); tags.append(1)
        x = torch.tensor(values)
        # Each state contains its own token and the sum of all preceding tokens.
        states = torch.stack((x, x.cumsum(0)), dim=-1)[None]
        return [[states, {'minimax_token_tags': torch.tensor(tags), 'start_percent': .2, 'end_percent': .8}]]


@pytest.mark.parametrize('options', [{}, {'images': ['reference']}])
def test_history_rows_removed_while_causal_effect_and_reference_rows_survive(options):
    clip = CausalClip()
    plain = history.encode_with_dialogue_history(clip, 'LIVE', '', options)
    state = torch.random.get_rng_state().clone()
    got = history.encode_with_dialogue_history(clip, 'LIVE', 'HISTORY\n', options)
    assert len(clip.calls) == 2  # one forward for each arm, no double encoding
    assert torch.equal(torch.random.get_rng_state(), state)
    tensor, metadata = got[0]
    prefix = 3 if options else 0
    assert tensor.shape == plain[0][0].shape
    assert torch.equal(tensor[0, prefix:, 0], torch.tensor([ord(c) for c in 'LIVE']))
    assert torch.equal(tensor[:, :prefix], plain[0][0][:, :prefix])
    assert not torch.equal(tensor[:, prefix:, 1], plain[0][0][:, prefix:, 1])
    assert torch.equal(metadata['minimax_token_tags'], plain[0][1]['minimax_token_tags'])
    assert metadata['start_percent'] == .2 and metadata['end_percent'] == .8
    assert tensor.untyped_storage().nbytes() == tensor.numel() * tensor.element_size()


def test_incompatible_tokenizer_and_missing_tags_fall_back_to_current_prompt():
    class OtherClip:
        def tokenize(self, text, **opts):
            return {'other': [[(text, 1.)]]}
        def encode_from_tokens_scheduled(self, tokens):
            return tokens
    assert history.encode_with_dialogue_history(OtherClip(), 'live', 'past', {}) == {'other': [[('live', 1.)]]}
    class MissingTags(CausalClip):
        def encode_from_tokens_scheduled(self, tokens):
            result = super().encode_from_tokens_scheduled(tokens)
            result[0][1].pop('minimax_token_tags')
            return result
    clip = MissingTags()
    got = history.encode_with_dialogue_history(clip, 'LIVE', 'PAST', {})
    assert len(clip.calls) == 2
    assert got[0][0].shape[1] == 4


@pytest.mark.parametrize('index', [1, 2, 3])
def test_n_chunk_history_uses_preceding_logical_body_only(monkeypatch, index):
    monkeypatch.setenv(history.ENV, '1')
    p = plan('\n'.join(f'[{7*i}-{7*(i+1)}s]\n(S{i+1}) says <d>[English] Line {i+1}.</d>' for i in range(4)))
    got = compile_(p, descriptor(index))
    assert f'Line {index}.' in got.encoder_history
    assert f'Line {index+1}.' not in got.encoder_history
    for i in range(index-1):
        assert f'Line {i+1}.' not in got.encoder_history
    assert f'Line {index+1}.' in got.text


def test_crossing_utterance_preserves_authored_words_without_guessing_word_alignment(monkeypatch):
    monkeypatch.setenv(history.ENV, '1')
    p = plan('[0-6s]\nQuiet.\n[6-8s]\n(S1) says <d>[English] A sentence crosses the boundary.</d>\n[8-28s]\nWaves.')
    got = compile_(p, descriptor())
    assert 'A sentence crosses the boundary.' in got.encoder_history
    assert 'A sentence crosses the boundary.' in got.text
    data = json.loads(got.encoder_history.splitlines()[1])
    assert data['intervals'][0]['utterance_continues_past_boundary'] is True


def test_historical_script_change_invalidates_identical_active_prompt(monkeypatch):
    monkeypatch.setenv(history.ENV, '1')
    p = plan('[0-7s]\n(S1) says <d>[English] Old wording.</d>\n[7-14s]\nCurrent scene.')
    q = plan('[0-7s]\n(S1) says <d>[English] Revised wording.</d>\n[7-14s]\nCurrent scene.')
    a, b = compile_(p, descriptor()), compile_(q, descriptor())
    assert a.text == b.text
    assert a.physical_conditioning_hash != b.physical_conditioning_hash


def test_reference_picture_numbers_shift_in_both_history_and_live_text():
    from ComfyUI_H3_Continuum_Join.reference import ReferenceAssets, encode_reference_prompt
    clip = CausalClip()
    image = torch.zeros(1, 32, 32, 3)
    latent = torch.zeros(1, 24, 1, 2, 2)
    assets = ReferenceAssets((image,), (latent,), ('hash',), 'combined', 'match')
    result = encode_reference_prompt(clip, '<Picture 1> speaks.', assets,
                                    first_image=image, dialogue_history='<Picture 1> spoke.\n')
    rows = clip.calls[-1]['qwen3vl_32b'][0]
    text = ''.join(chr(token) for token, _ in rows if isinstance(token, int))
    assert text == '<Picture 2> spoke.\n<Picture 2> speaks.'
    assert result[0][1]['minimax_refs'][0]['latent'] is latent
