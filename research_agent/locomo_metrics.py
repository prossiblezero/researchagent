"""Host scoring for the frozen LoCoMo subset; labels never enter experiment input."""
from __future__ import annotations

import hashlib
import json


def answer_scores(prediction, reference):
    """Match AgenticMemory utils.py's exact match and set-token F1, not a judge."""
    prediction, reference = str(prediction).strip(), str(reference).strip()
    def tokens(text):
        for character in '.,!?':
            text = text.replace(character, ' ')
        return set(text.lower().split())
    predicted, gold = tokens(prediction), tokens(reference)
    f1 = 2 * len(predicted & gold) / (len(predicted) + len(gold)) if predicted and gold else 0.0
    return {'answer_f1': f1, 'exact_match': float(bool(reference) and prediction.lower() == reference.lower())}


def score_predictions(tasks, corpus, labels, rows, seeds):
    if (not isinstance(seeds, list) or not 1 <= len(seeds) <= 64
            or any(type(seed) is not int for seed in seeds) or len(set(seeds)) != len(seeds)):
        raise ValueError('LoCoMo requires 1..64 distinct integer seeds')
    def indexed(values, key):
        if not isinstance(values, list) or not values or len(values) > 10000:
            raise ValueError('LoCoMo inputs must be nonempty bounded lists')
        result = {}
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get(key), str) or not value[key] or value[key] in result:
                raise ValueError('LoCoMo inputs contain invalid or duplicate IDs')
            result[value[key]] = value
        return result
    task_map, label_map = indexed(tasks, 'id'), indexed(labels, 'id')
    conversations = indexed(corpus, 'conversation_id')
    if set(task_map) != set(label_map):
        raise ValueError('LoCoMo label coverage must match frozen tasks')
    valid_evidence = {}
    for cid, item in conversations.items():
        conversation = item.get('conversation')
        if not isinstance(conversation, dict):
            raise ValueError('LoCoMo conversation is invalid')
        ids = [turn.get('dia_id') for name, turns in conversation.items()
               if name.startswith('session_') and isinstance(turns, list)
               for turn in turns if isinstance(turn, dict)]
        if not ids or any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
            raise ValueError('LoCoMo dialogue IDs are invalid')
        valid_evidence[cid] = set(ids)
    for task in tasks:
        cid = task.get('conversation_id')
        label = label_map[task['id']]
        if (cid not in conversations or task.get('category') not in (1, 2, 3, 4)
                or task.get('split') != conversations[cid].get('split')
                or type(label.get('answer')) not in (str, int, float)
                or type(label.get('retrieval_scorable')) is not bool):
            raise ValueError('LoCoMo task/label metadata is invalid')
        gold = label.get('gold_evidence')
        if (not isinstance(gold, list) or any(not isinstance(i, str) for i in gold)
                or len(gold) != len(set(gold)) or not set(gold) <= valid_evidence[cid]
                or (label['retrieval_scorable'] and not gold)):
            raise ValueError('LoCoMo gold evidence mapping is invalid')
    if not isinstance(rows, list) or len(rows) != len(tasks) * len(seeds):
        raise ValueError('LoCoMo predictions must cover every task and seed exactly once')
    seen, scored = set(), []
    for row in rows:
        if not isinstance(row, dict) or type(row.get('seed')) is not int or not isinstance(row.get('task_id'), str):
            raise ValueError('LoCoMo prediction needs integer seed and task_id')
        ident = (row['seed'], row['task_id'])
        if row['seed'] not in seeds or row['task_id'] not in task_map or ident in seen:
            raise ValueError('LoCoMo prediction coverage is invalid')
        seen.add(ident)
        answer, evidence = row.get('answer'), row.get('evidence_ids')
        task, label = task_map[row['task_id']], label_map[row['task_id']]
        if not isinstance(answer, str) or len(answer) > 64000:
            raise ValueError('LoCoMo prediction answer must be bounded text')
        if (not isinstance(evidence, list) or len(evidence) > 10000
                or any(not isinstance(i, str) for i in evidence) or len(evidence) != len(set(evidence))
                or not set(evidence) <= valid_evidence[task['conversation_id']]):
            raise ValueError('LoCoMo prediction evidence must belong to its conversation')
        score = {**answer_scores(answer, label['answer']), 'nonempty_answer_rate': float(bool(answer.strip()))}
        if label['retrieval_scorable']:
            gold = set(label['gold_evidence'])
            score['evidence_recall_at_5'] = len(gold.intersection(evidence[:5])) / len(gold)
            score['evidence_recall_at_10'] = len(gold.intersection(evidence[:10])) / len(gold)
        scored.append({'task_id': row['task_id'], 'seed': row['seed'], 'category': task['category'],
                       'conversation_id': task['conversation_id'], 'metrics': score})
    def aggregate(items):
        keys = set().union(*(item['metrics'] for item in items))
        return {key: sum(i['metrics'][key] for i in items if key in i['metrics']) /
                sum(key in i['metrics'] for i in items) for key in sorted(keys)}
    return {'metrics': aggregate(scored), 'per_task': scored,
            'per_seed': [{'seed': seed, 'metrics': aggregate([r for r in scored if r['seed'] == seed])} for seed in seeds],
            'per_category': [{'category': category, 'metrics': aggregate([r for r in scored if r['category'] == category])}
                             for category in sorted({r['category'] for r in scored})],
            'answer_task_count': len(tasks), 'evidence_task_count': sum(l['retrieval_scorable'] for l in labels)}


def audit_locomo_result(workspace, result, contract, labels_path):
    from .coding_tool import regular_file, validate_metric_contract, workspace_artifact_path
    contract = validate_metric_contract(contract)
    if contract['name'] != 'locomo_qa_v1':
        raise ValueError('LoCoMo scoring contract required')
    config, diagnostics = result.get('config'), result.get('diagnostics')
    if (not isinstance(config, dict) or config.get('dataset') != 'locomo'
            or not isinstance(config.get('split'), str) or not config['split']
            or not isinstance(diagnostics, dict) or not isinstance(result.get('metrics'), dict)):
        raise ValueError('LoCoMo result requires metrics/config/diagnostics')
    if config.get('seeds') != contract['seeds']:
        raise ValueError('LoCoMo seeds must match the frozen host contract')
    inputs = {}
    for prefix in ('tasks', 'corpus', 'labels'):
        raw = (regular_file(labels_path.parent, labels_path.name, limit=16 * 1024 * 1024) if prefix == 'labels'
               else regular_file(workspace, contract[prefix + '_path'], limit=16 * 1024 * 1024))
        if hashlib.sha256(raw).hexdigest() != contract[prefix + '_sha256']:
            raise ValueError('LoCoMo frozen input changed: ' + prefix)
        inputs[prefix] = json.loads(raw)
    tasks = inputs['tasks']
    if (not isinstance(tasks, list) or not tasks or any(not isinstance(t, dict) for t in tasks)
            or {t.get('split') for t in tasks} != {config.get('split')}
            or config.get('dataset_version') != contract['tasks_sha256']):
        raise ValueError('LoCoMo split/version must match frozen tasks')
    path = workspace_artifact_path(workspace, diagnostics.get('predictions_path'))
    raw = regular_file(workspace, path, limit=16 * 1024 * 1024)
    rows = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
    audit = score_predictions(tasks, inputs['corpus'], inputs['labels'], rows, config.get('seeds'))
    audit.update(contract=contract['name'], predictions_sha256=hashlib.sha256(raw).hexdigest(),
                 reported_metrics=dict(result['metrics']))
    # Diagnostic examples use only frozen public input and actual predictions;
    # the private reference answer and gold evidence remain scoring-only.
    task_map = {task['id']: task for task in tasks}
    predictions = {(row['seed'], row['task_id']): row for row in rows}
    failures = sorted((row for row in audit['per_task'] if any(value < 1 for value in row['metrics'].values())),
                      key=lambda row: row['metrics']['answer_f1'])[:3]
    audit['failure_examples'] = []
    for row in failures:
        prediction = predictions[(row['seed'], row['task_id'])]
        question = task_map[row['task_id']].get('question', '')
        question = question if isinstance(question, str) else ''
        audit['failure_examples'].append({**row, 'question': question[:400],
            'question_truncated': len(question) > 400, 'prediction': prediction['answer'][:400],
            'prediction_truncated': len(prediction['answer']) > 400,
            'retrieved_evidence_ids': prediction['evidence_ids'][:10]})
    turns = {item['conversation_id']: sum(isinstance(turn, dict) for name, session in item['conversation'].items()
             if name.startswith('session_') and isinstance(session, list) for turn in session)
             for item in inputs['corpus']}
    audit['input_summary'] = {'origin': 'host_frozen_inputs', 'task_count': len(tasks), 'conversation_count': len(turns),
                              'conversation_turns': turns, 'total_turns': sum(turns.values())}
    # Script-reported costs/scores remain inspectable, but cannot satisfy host targets.
    metrics = dict(audit['metrics'])
    audit['host_metrics'] = metrics
    updated = {**diagnostics, 'per_seed': audit['per_seed'], 'per_category': audit['per_category'],
               'metric_audit': {'contract': contract['name'], 'reported_metrics': audit['reported_metrics'],
                                'host_metrics': metrics, 'answer_task_count': audit['answer_task_count'],
                                'evidence_task_count': audit['evidence_task_count']}}
    return {**result, 'metrics': metrics, 'diagnostics': updated}, audit
