"""CPU transport check using a tiny instance of Core's Qwen causal backbone.

No trained weights are loaded. This checks information flow and row ownership,
not speech intelligibility or full H3 numerical reproducibility.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys

import torch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comfy-root', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.comfy_root.resolve()))
    import comfy.cli_args
    comfy.cli_args.args.cpu = True
    import comfy.ops
    from comfy.text_encoders.llama import Llama2_, Qwen3VL_32BConfig

    # Match standalone tests' package setup, without importing UI registration.
    root = Path(__file__).resolve().parents[1]
    name = 'ComfyUI_H3_Continuum_Join'
    spec = importlib.util.spec_from_file_location(name, root / '__init__.py', submodule_search_locations=[str(root)])
    sys.modules[name] = importlib.util.module_from_spec(spec)
    from ComfyUI_H3_Continuum_Join.v2.dialogue_history import encode_with_dialogue_history

    class TinyQwenClip:
        def __init__(self):
            config = Qwen3VL_32BConfig(vocab_size=256, hidden_size=32, intermediate_size=64,
                                     num_hidden_layers=2, num_attention_heads=4, num_key_value_heads=2)
            config.head_dim = 8
            config.rope_dims = None
            config.final_norm = False
            self.model = Llama2_(config, device='cpu', dtype=torch.float32, ops=comfy.ops.manual_cast)
            for key, param in self.model.named_parameters():
                if 'norm.weight' in key:
                    param.fill_(1)
                else:
                    param.normal_(0, .03)

        def tokenize(self, text, **options):
            refs = [(1, 1.), (2, 1.)] if options else []
            return {'qwen3vl_32b': [[*refs, *[(ord(char), 1.) for char in text]]]}

        def encode_from_tokens_scheduled(self, tokens):
            ids = torch.tensor([[item[0] for item in tokens['qwen3vl_32b'][0]]])
            hidden = self.model(ids, dtype=torch.float32)[0]
            return [[hidden, {'minimax_token_tags': torch.ones(hidden.shape[1], dtype=torch.long)}]]

    with torch.random.fork_rng(devices=[]), torch.no_grad():
        torch.manual_seed(824)
        clip = TinyQwenClip()
        # Two synthetic reference-presentation rows precede history and live text.
        options = {'images': ['synthetic presentation']}
        a = encode_with_dialogue_history(clip, 'LIVE', 'PAST A\n', options)[0][0]
        b = encode_with_dialogue_history(clip, 'LIVE', 'PAST B\n', options)[0][0]
        assert a.shape == b.shape == (1, 6, 32)
        assert torch.equal(a[:, :2], b[:, :2])
        assert not torch.equal(a[:, 2:], b[:, 2:])
        assert torch.equal(a, encode_with_dialogue_history(clip, 'LIVE', 'PAST A\n', options)[0][0])
    print('PASS: Core Qwen causal backbone; reference states unchanged; current states context-sensitive; repeated call bitwise equal. Untrained CPU transport only.')


if __name__ == '__main__':
    main()
