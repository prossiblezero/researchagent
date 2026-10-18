"""One-epoch LoRA: exact listwise option CE plus frozen-base prompt KL anchoring."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_agent.decision_router import LABELS, OptionScorer, option_logprobs
from evals.build_intent_routing_v2 import read, write, sha, verify_frozen
from evals.calibrate_decision_router import distribution_metrics
from evals.run_intent_routing_eval import model_inputs, score
from evals.run_decision_router_eval import hardware, source_manifest

GENERAL_ANCHORS = [
    '将“今天的会议改到明天下午两点”改写为礼貌的通知。',
    '用三句话解释为什么天会下雨。',
    '把“请记得带伞”翻译为英文。',
    '给初学者说明分数与小数的区别。',
    '将下面两件事按顺序列出：洗净水果，切成小块。',
    '用一句话概括：城市新增公园，为居民提供休闲场所。',
    '写一条提醒大家节约用水的标语。',
    '解释成语“循序渐进”，给一个学习中的例子。',
    '把这句话中的错字纠正：大家以经到齐了。',
    '比较自行车和步行这两种短途出行方式。',
    '说明烧水时为什么不应该把水壶装得过满。',
    '为首次搬家的人列出三件需要提前准备的物品。',
    '将“我们应当认真倾听他人的观点”改成更简短的说法。',
    '解释一周为什么有七天，并区分约定与自然周期。',
    '写两句祝贺朋友毕业的话。',
    '把购物清单按蔬菜和文具分类：青菜、铅笔、萝卜、橡皮。',
]


def dev_rows(scorer, cases, order):
    rows = []
    scorer.model.eval()
    for c in cases:
        result = scorer.score(*model_inputs(c), order=order)
        rows.append({'id': c['id'], 'response': {'intent': result['intent']}, 'logits': result['logits'],
                     'error': None, 'seconds': result['intent_seconds'], 'attempts': 0, 'usage': []})
    scorer.model.train()
    return rows


def anchored_loss(scorer, case, order, kl_weight, anchor_positions):
    import torch
    import torch.nn.functional as F
    orders = ('forward', 'reverse') if order == 'average' else (order,)
    distributions, anchors = [], []
    for current in orders:
        ids = scorer.input_ids(*model_inputs(case), order=current)
        values, logits = option_logprobs(scorer.model, ids, scorer.candidates, anchor_positions=anchor_positions)
        if current == 'reverse':
            values = values.flip(0)
        distributions.append(values.softmax(-1))
        with torch.no_grad(), scorer.model.disable_adapter():
            base = scorer.model(input_ids=ids, use_cache=False, logits_to_keep=anchor_positions+1).logits[:, :-1, :]
        anchors.append(F.kl_div(logits.float().log_softmax(-1), base.float().softmax(-1), reduction='none').sum(-1).mean())
    log_probs = torch.stack(distributions).mean(0).clamp_min(1e-30).log()
    ce = -log_probs[LABELS.index(case['expected_intent'])]
    kl = torch.stack(anchors).mean()
    return ce + kl_weight*kl, ce, kl


def general_kl(scorer, prompt, positions):
    import torch.nn.functional as F
    import torch
    encoded = scorer.tokenizer.apply_chat_template([{'role': 'user', 'content': prompt}],
        tokenize=False, add_generation_prompt=True, enable_thinking=False)
    ids = scorer.tokenizer(encoded, return_tensors='pt', add_special_tokens=False).input_ids.to(scorer.device)
    logits = scorer.model(input_ids=ids, use_cache=False, logits_to_keep=positions+1).logits[:, :-1, :]
    with torch.no_grad(), scorer.model.disable_adapter():
        base = scorer.model(input_ids=ids, use_cache=False, logits_to_keep=positions+1).logits[:, :-1, :]
    return F.kl_div(logits.float().log_softmax(-1), base.float().softmax(-1), reduction='none').sum(-1).mean()


def train(args):
    import torch
    from peft import LoraConfig, get_peft_model
    panel = verify_frozen()
    baseline_protocol = read(args.baseline_run/'protocol.json')
    if baseline_protocol['split'] != 'dev_v2' or baseline_protocol['group'] != 'C1' or baseline_protocol['panel_sha256'] != sha(ROOT/'datasets/intent_routing/v2.json'):
        raise ValueError('Frozen C1 dev baseline required')
    train_cases = [c for c in panel['cases'] if c['split'] == 'train_v2']
    dev = [c for c in panel['cases'] if c['split'] == 'dev_v2']
    baseline_metrics = distribution_metrics(dev, read(args.baseline_run/'results.json'), temperature=1.)
    if baseline_metrics['probability_cases'] != len(dev):
        raise ValueError('Complete C1 dev probabilities required for the raw ECE constraint')
    baseline_ece = baseline_metrics['ece']
    args.out.mkdir(parents=True, exist_ok=False)
    write(args.out/'general-anchors.json', {'generator': 'codex-implementation-agent', 'purpose': 'KL only, no target responses or CE', 'prompts': GENERAL_ANCHORS})
    protocol = {'schema_version': 1, 'panel_sha256': sha(ROOT/'datasets/intent_routing/v2.json'), 'source_files': source_manifest(),
                'hardware': hardware(), 'learning_rates': args.learning_rates, 'seed': args.seed, 'epochs': 1,
                'rank': args.rank, 'target_modules': args.modules, 'lora_alpha': 2*args.rank, 'lora_dropout': 0.,
                'order': baseline_protocol['order'], 'kl_lambda': args.kl_weight, 'anchor_positions': args.anchor_positions,
                'general_kl_every_steps': 16, 'general_anchors_sha256': sha(args.out/'general-anchors.json'),
                'checkpoint_policy': 'quarter-epoch dev macro-F1, ECE <= uncalibrated C1 dev ECE + .03; best F1 then lower ECE',
                'baseline_ece': baseline_ece, 'baseline_run': str(args.baseline_run), 'gradient_clip_norm': 1.,
                'batch_size': 1, 'optimizer': 'AdamW, weight_decay=0', 'started_at': time.time()}
    write(args.out/'protocol.json', protocol)
    curve, best = [], None
    for lr in args.learning_rates:
        torch.manual_seed(args.seed)
        scorer = OptionScorer(args.model, device=args.device, seed=args.seed, max_chars=baseline_protocol['max_chars'])
        if scorer.weights_sha256 != baseline_protocol['weights_sha256'] or scorer.prompt_sha256 != baseline_protocol['prompt_sha256']:
            raise ValueError('Training model/prompt differs from the selected C1 baseline')
        protocol.update(model=baseline_protocol['model'], weights_sha256=scorer.weights_sha256,
                        prompt_sha256=scorer.prompt_sha256, tokenization=scorer.tokenization)
        write(args.out/'protocol.json', protocol)
        scorer.model = get_peft_model(scorer.model, LoraConfig(r=args.rank, lora_alpha=2*args.rank,
            target_modules=args.modules, lora_dropout=0., bias='none', task_type='CAUSAL_LM'))
        scorer.model.train()
        optimizer = torch.optim.AdamW((p for p in scorer.model.parameters() if p.requires_grad), lr=lr, weight_decay=0.)
        shuffled = list(train_cases)
        random.Random(args.seed).shuffle(shuffled)
        check_steps = {max(1, round(len(shuffled)*fraction)) for fraction in (.25, .5, .75, 1.)}
        folder = args.out/f'lr-{lr:g}'
        folder.mkdir()
        running = {'loss': 0., 'ce': 0., 'prompt_kl': 0., 'general_kl': 0.}
        for step, case in enumerate(shuffled, 1):
            optimizer.zero_grad(set_to_none=True)
            loss, ce, kl = anchored_loss(scorer, case, baseline_protocol['order'], args.kl_weight, args.anchor_positions)
            gkl = general_kl(scorer, GENERAL_ANCHORS[(step//16-1) % len(GENERAL_ANCHORS)], args.anchor_positions) if step % 16 == 0 else loss.new_zeros(())
            total = loss + args.kl_weight*gkl
            if not torch.isfinite(total):
                raise ValueError('Nonfinite training loss')
            total.backward()
            torch.nn.utils.clip_grad_norm_((p for p in scorer.model.parameters() if p.requires_grad), 1.)
            optimizer.step()
            sample = {'step': step, 'learning_rate': lr, 'loss': float(total.detach()), 'ce': float(ce.detach()),
                      'prompt_kl': float(kl.detach()), 'general_kl': float(gkl.detach())}
            for key in running:
                running[key] += sample[key]
            with (folder/'loss.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(sample)+'\n')
            if step % 50 == 0:
                print(f'lr={lr:g} step={step}/{len(shuffled)} loss={sample["loss"]:.4f}', flush=True)
            if step in check_steps:
                rows = dev_rows(scorer, dev, baseline_protocol['order'])
                metrics = score(dev, rows)
                calibration = distribution_metrics(dev, rows)
                eligible = calibration['ece'] <= baseline_ece + .03
                point = {'learning_rate': lr, 'step': step, 'epoch': step/len(shuffled), 'seed': args.seed,
                         'train_means': {k: v/step for k, v in running.items()}, 'dev_macro_f1': metrics['macro_f1'],
                         'dev_accuracy': metrics['accuracy'], 'dev_ece': calibration['ece'], 'eligible': eligible}
                checkpoint = folder/f'step-{step}'
                checkpoint.mkdir()
                write(checkpoint/'dev-results.json', rows)
                write(checkpoint/'dev-metrics.json', {'classification': metrics, 'calibration': calibration, **point})
                if eligible and (best is None or (point['dev_macro_f1'], -point['dev_ece']) > (best['dev_macro_f1'], -best['dev_ece'])):
                    adapter = checkpoint/'adapter'
                    scorer.model.save_pretrained(adapter)
                    best = {**point, 'adapter_path': str(adapter.resolve())}
                    write(args.out/'selected.json', best)
                curve.append(point)
                write(args.out/'training-curve.json', curve)
                print(json.dumps(point), flush=True)
        del optimizer, scorer, loss, ce, kl, gkl, total
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    protocol.update(finished_at=time.time(), selected=best)
    write(args.out/'protocol.json', protocol)
    if best is None:
        raise ValueError('No checkpoint meets the predeclared ECE constraint; retain C1 for cascade')
    print(json.dumps(best), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--baseline-run', type=Path, required=True)
    parser.add_argument('--model', type=Path, default=ROOT/'data/models/Qwen3-0.6B')
    parser.add_argument('--device', choices=['auto', 'cuda', 'cpu'], default='auto')
    parser.add_argument('--learning-rates', type=float, nargs='+', default=[5e-5, 1e-4, 2e-4])
    parser.add_argument('--seed', type=int, default=17)
    parser.add_argument('--rank', type=int, default=16)
    parser.add_argument('--modules', nargs='+', default=['q_proj', 'k_proj', 'v_proj', 'o_proj'])
    parser.add_argument('--kl-weight', type=float, default=1.)
    parser.add_argument('--anchor-positions', type=int, default=4)
    train(parser.parse_args())
