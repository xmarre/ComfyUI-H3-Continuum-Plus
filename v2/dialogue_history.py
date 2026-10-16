"""Experimental Qwen-only authored dialogue context, excluded from DiT rows.

Authored intervals do not identify which words were actually spoken. This module
does not transcribe audio, align words, or split an utterance at an estimated word.
"""
from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
import json
import logging
import os
import re

import torch

from .physical_prompts import (
    _body_for_atomic_interval,
    _resolved_candidates,
    canonical_sha256,
    fraction_string,
    parse_fraction,
    text_sha256,
)

POLICY = "qwen_causal_dialogue_history_v1"
ENV = "H3C_CAUSAL_DIALOGUE_HISTORY"
_DIALOGUE = re.compile(r"<d>.*?</d>", re.DOTALL)
LOG = logging.getLogger("h3_continuum_join")


def dialogue_history_enabled():
    return os.environ.get(ENV, "").strip() == "1"


def with_dialogue_history(compiled, plan, descriptor):
    """Bind authored overlap context into compilation/reuse identity when enabled."""
    if not dialogue_history_enabled():
        return compiled
    source = plan.get("source") or {}
    if (source.get("kind") != "timeline" or descriptor.context_frames <= 0
            or descriptor.initial_state_origin != "sequence"):
        return compiled
    start = descriptor.global_start_seconds
    end = Fraction(descriptor.retained_before, 1) / descriptor.fps
    candidates = _resolved_candidates(source, parse_fraction(source["chunk_seconds"]))
    points = sorted({start, end, *(max(start, min(end, item[key]))
                                 for item in candidates for key in ("_start", "_end"))})
    records = []
    for left, right in zip(points, points[1:]):
        body, chosen, _ = _body_for_atomic_interval(candidates, left, right)
        if not body or not _DIALOGUE.search(body):
            continue
        # Retain the delivery/speaker declaration alongside each complete native
        # dialogue block. Never guess the spoken word boundary from event time.
        records.append({
            "global_start": fraction_string(left),
            "global_end": fraction_string(right),
            "utterance_continues_past_boundary": chosen[0]["_end"] > end,
            "authored_context": body,
        })
    if not records:
        return compiled
    history = (
        "Authored dialogue context for the carried audio interval. Event times are not word alignment.\n"
        + json.dumps({"speaker_context": source.get("preamble", ""), "intervals": records},
                     ensure_ascii=False, sort_keys=True)
        + "\nEnd of authored context. Current physical generation instructions follow.\n"
    )
    version = f"{compiled.compiler_version}+{POLICY}"
    identity = {
        "compiler_version": version, "descriptor": descriptor.semantic_dict(),
        "text": compiled.text, "presentation_contract": descriptor.presentation_contract,
        "terminal_contract": descriptor.terminal_contract, "encoder_history": history,
    }
    return replace(compiled, compiler_version=version, encoder_history=history,
                   physical_conditioning_hash=canonical_sha256(identity),
                   diagnostics=tuple(compiled.diagnostics) + ({
                       "code": "H3C-PT234", "level": "info",
                       "message": "experimental authored overlap dialogue is Qwen-only context; its rows are excluded from DiT conditioning",
                       "history_sha256": text_sha256(history), "interval_count": len(records),
                   },))


def encode_with_dialogue_history(clip, prompt, history, tokenize_options, *, current_tokens=None):
    """Encode [reference presentation | history | current text], remove history rows.

    MiniMax's tokenizer adds no BOS/EOS/padding to nonempty text. Tokenizing
    text separately makes the boundary independent of BPE joins and image/video
    expansion. Reference rows remain at the front; history cannot change their
    causal hidden states. Only current text states can read the history.
    """
    current = clip.tokenize(prompt, **tokenize_options) if current_tokens is None else current_tokens
    if not history:
        return clip.encode_from_tokens_scheduled(current)
    key = "qwen3vl_32b"
    plain = clip.tokenize(prompt)
    past = clip.tokenize(history)
    # Unsupported token representations retain ordinary encoding. No prompt or
    # backend is rejected because an optional context experiment is unavailable.
    if any(set(tokens) != {key} or len(tokens[key]) != 1 for tokens in (current, plain, past)):
        LOG.warning("Qwen dialogue history skipped: unsupported tokenizer layout")
        return clip.encode_from_tokens_scheduled(current)
    text_rows, history_rows = plain[key][0], past[key][0]
    if (not text_rows or not history_rows
            or any(not isinstance(item[0], int) for item in (*text_rows, *history_rows))):
        LOG.warning("Qwen dialogue history skipped: unsupported text token representation")
        return clip.encode_from_tokens_scheduled(current)
    rows = current[key][0]
    # Text is the final block in Core's MiniMax presentation tokenizer.
    if rows[-len(text_rows):] != text_rows:
        LOG.warning("Qwen dialogue history skipped: text boundary is not preserved")
        return clip.encode_from_tokens_scheduled(current)
    tokens = {key: [[*rows[:-len(text_rows)], *history_rows, *text_rows]]}
    encoded = clip.encode_from_tokens_scheduled(tokens)
    result = []
    for tensor, metadata in encoded:
        prefix = tensor.shape[1] - len(text_rows) - len(history_rows)
        tags = metadata.get("minimax_token_tags")
        if prefix < 0 or not torch.is_tensor(tags) or tags.ndim != 1 or tags.numel() != tensor.shape[1]:
            # A changed encoder ABI cannot safely be sliced. Encode the current
            # prompt normally rather than pass historical rows to the DiT.
            LOG.warning("Qwen dialogue history skipped: encoder row/tag layout changed")
            return clip.encode_from_tokens_scheduled(current)
        live_start = prefix + len(history_rows)
        result.append([
            torch.cat((tensor[:, :prefix], tensor[:, live_start:]), dim=1),
            {**metadata, "minimax_token_tags": torch.cat((tags[:prefix], tags[live_start:]))},
        ])
    return result
