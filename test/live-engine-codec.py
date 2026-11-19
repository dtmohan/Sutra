"""Opt-in pack codec check. The optional oracle is used only for validation.

Neither mode loads a model, starts a provider, downloads files, or writes task
memory. Omit --oracle-control to run entirely without a Strata installation.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'harness'))
from engine_codec import EngineCodec


CORPUS = [
    'Hello, how are you?', 'Café e\u0301 中文 العربية नमस्ते', '🎵👩🏽‍💻🙂\u200d↔️',
    'quotes " and \\ paths\nline2\tindent', 'Do not change the kernel; 21 - 18 = 3',
    '<|im_start|>assistant\n<|im_end|> </think> <think>',
    '<tool_response>foo </think> bar</tool_response>', '<tool_call><function=x></function></tool_call>',
    "0 1 23 12345678 don't didn't SHOULD'VE", '\x00\r\n\t \x1f\x7f',
    '\U00016ea0\U00016ea1ABC', 'a\u0301_j ___  --', '漢字' * 300,
]

ORACLE = r'''
import json, sys, importlib.util
from pathlib import Path
control, pack = map(Path, sys.argv[1:])
sys.path.insert(0, str(control / 'tools'))
import strata_tokenizer as bpe
spec = importlib.util.spec_from_file_location('sutra_engine_codec_oracle', control / 'serve/frontend.py')
front = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = front
spec.loader.exec_module(front)
vocab = json.loads((pack / 'vocab.json').read_text())
tokens = [None] * len(vocab)
for token, index in vocab.items(): tokens[index] = token
config = json.loads((pack / 'tokenizer.json').read_text())
tok = bpe.Tokenizer(tokens, (pack / 'merges.txt').read_text().split('\n'),
                    json.loads((pack / 'token_type.json').read_text()), config['pre'], config['special_ids'])
template = front.ChatTemplate(pack / 'chat_template.jinja')
tags, results = front.literal_tags(tok.control_tokens), []
for case in json.load(sys.stdin):
    marked, tools, changed = front.mark_think_literals(case['messages'], None, tags)
    prompt = template.render(marked, tools, **front.effort_kwargs(case['thinking']))
    prompt, plain = front.unmark_think_literals(prompt, tags) if changed else (prompt, ())
    results.append({'prompt': prompt, 'ids': tok.encode(prompt, parse_special=True, plain=plain)})
json.dump(results, sys.stdout, ensure_ascii=False)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pack', type=Path, required=True, help='Installed tokenizer directory')
    parser.add_argument('--oracle-control', type=Path, help='Optional native Control source for a comparison only')
    parser.add_argument('--oracle-python', type=Path, help='Python with the optional oracle dependencies')
    args = parser.parse_args()
    pack = args.pack.resolve()
    codec = EngineCodec(pack)
    for text in CORPUS:
        assert codec.decode(codec.encode(text)) == text, repr(text)
    cases = [{'messages': [{'role': 'system', 'content': 'Be helpful and precise.'},
                           {'role': 'user', 'content': text}], 'thinking': thinking}
             for text in CORPUS for thinking in (False, None, 'low', 'medium', 'high')]
    cases += [
        {'messages': [{'role': 'user', 'content': 'First'}, {'role': 'assistant', 'content': ''},
                      {'role': 'user', 'content': 'Next'}], 'thinking': False},
        {'messages': [{'role': 'user', 'content': 'First'},
                      {'role': 'assistant', 'content': '<think>prior</think>Answer'},
                      {'role': 'user', 'content': 'Next'}], 'thinking': 'low'},
        {'messages': [{'role': 'user', 'content': 'First'},
                      {'role': 'assistant', 'content': 'Answer', 'reasoning_content': 'prior'},
                      {'role': 'user', 'content': 'Next'}], 'thinking': 'high'},
    ]
    result = {'passed': True, 'roundtrip_cases': len(CORPUS), 'prompt_cases': len(cases),
              'oracle_compared': False, 'model_loads': 0}
    if args.oracle_control:
        if args.oracle_python is None:
            parser.error('--oracle-control requires --oracle-python')
        comparison = subprocess.run([str(args.oracle_python.resolve()), '-B', '-c', ORACLE,
                                     str(args.oracle_control.resolve()), str(pack)],
                                    input=json.dumps(cases), capture_output=True, text=True,
                                    check=True, timeout=30)
        expected = json.loads(comparison.stdout)
        assert len(expected) == len(cases)
        for index, (case, reference) in enumerate(zip(cases, expected)):
            assert codec.render_messages(**case) == reference['prompt'], ('Prompt mismatch', index)
            assert codec.encode_messages(**case) == reference['ids'], ('Token ID mismatch', index)
        result['oracle_compared'] = True
    print(json.dumps(result))


if __name__ == '__main__':
    main()
