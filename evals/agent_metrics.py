"""Repeated-trial estimators and per-request metering, independent of product behavior."""
import hashlib
import json
import math
import random
import statistics
import time
from collections import defaultdict

from research_agent.trace import redact


def pass_at_k(n, c, k):
    if not 0 <= c <= n or not 1 <= k <= n:
        raise ValueError('Require 0 <= correct <= trials and 1 <= k <= trials')
    return 1 - math.comb(n-c, k)/math.comb(n, k) if n-c >= k else 1.0


def pass_power_k(n, c, k):
    if not 0 <= c <= n or not 1 <= k <= n:
        raise ValueError('Require 0 <= correct <= trials and 1 <= k <= trials')
    return math.comb(c, k)/math.comb(n, k) if c >= k else 0.0


def interval(values, seed=20260918):
    """Percentile bootstrap over tasks, not falsely independent repeated trials."""
    if not values:
        return None
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(2000))
    return [means[49], means[1949]]


def distribution(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return {'n': 0, 'mean': None, 'p50': None, 'p95': None}
    return {'n':len(values), 'mean':statistics.mean(values),
            'p50':values[math.ceil(.5*len(values))-1], 'p95':values[math.ceil(.95*len(values))-1]}


def repeated_metrics(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row['case_id']].append(row['passed'])
    eligible = {key:values for key,values in groups.items() if None not in values}
    result = {'tasks':len(groups), 'fully_judged_tasks':len(eligible),
              'unjudged_trials':sum(r['passed'] is None for r in rows), 'per_task':{}, 'pass_at_k':{}, 'pass_power_k':{}}
    for key, values in groups.items():
        result['per_task'][key] = {'n':len(values), 'successes':sum(v is True for v in values), 'unjudged':values.count(None)}
    if not eligible:
        return result
    for k in range(1, min(map(len,eligible.values()))+1):
        for field, function in [('pass_at_k', pass_at_k), ('pass_power_k', pass_power_k)]:
            estimates = [function(len(v),sum(v),k) for v in eligible.values()]
            result[field][str(k)] = {'estimate':statistics.mean(estimates), 'task_bootstrap_95ci':interval(estimates)}
    return result


def usage_summary(calls):
    known = [c for c in calls if c['usage'] is not None]
    totals = {key:sum(c['usage'][key] for c in known) for key in ('prompt_tokens','completion_tokens','total_tokens')}
    return {'requests':len(calls), 'requests_with_usage':len(known), 'unknown_usage_requests':len(calls)-len(known),
            'known_token_subtotal':totals, 'complete_token_total':totals if len(known)==len(calls) else None}


class MeteredModel:
    """Wrap every model factory, so routing, planning, answers and corrections are counted."""
    def __init__(self, inner, ledger, phase, folder, run_id, deadline=None, anchor=''):
        self.inner, self.ledger, self.phase = inner, ledger, phase
        self.folder, self.run_id, self.deadline, self.anchor = folder, run_id, deadline, anchor
        self.name = inner.name

    @property
    def request_deadline(self):
        values=[d for d in (self.deadline,getattr(self.inner,"request_deadline",None)) if d is not None]
        return min(values) if values else None

    @request_deadline.setter
    def request_deadline(self,value):
        self.inner.request_deadline=min(value,self.deadline) if value is not None and self.deadline is not None else value

    @property
    def supports_answer_verification(self):
        return getattr(self.inner,'supports_answer_verification',False)

    @property
    def usage_records(self):
        return getattr(self.inner,'usage_records',[])

    @property
    def usage_callback(self):
        return getattr(self.inner,'usage_callback',None)

    @usage_callback.setter
    def usage_callback(self,value):
        self.inner.usage_callback=value

    @property
    def usage_purpose(self):
        return getattr(self.inner,'usage_purpose','answer')

    @usage_purpose.setter
    def usage_purpose(self,value):
        self.inner.usage_purpose=value

    @property
    def total_usage(self):
        return self.inner.total_usage

    def complete(self, messages, tools):
        if self.deadline and time.monotonic()>self.deadline:
            raise TimeoutError('Frozen trial deadline exceeded before next model request')
        self.inner.request_deadline=self.request_deadline
        before = dict(self.inner.total_usage)
        before_records = len(self.usage_records)
        encoded = json.dumps({'messages':messages,'tools':tools},ensure_ascii=False,sort_keys=True).encode()
        number = len(self.ledger)+1
        phase = self.phase
        prompt = str(messages[0].get('content','')) if messages else ''
        if '意图路由器' in prompt: phase = 'route'
        elif '从清单选择相关资料' in prompt: phase = 'local_plan'
        elif '本地摘录回答' in prompt: phase = 'local_answer'
        elif '根据提供的原文摘录分析资料' in prompt: phase = 'material_analysis'
        started = time.monotonic()
        row = {'number':number, 'phase':phase, 'model':self.name, 'input_bytes':len(encoded),
               'input_sha256':hashlib.sha256(encoded).hexdigest(), 'anchor_visible':self.anchor in encoded.decode() if self.anchor else None,
               'message_count':len(messages), 'tool_schemas':len(tools), 'usage':None}
        try:
            result = self.inner.complete(messages, tools)
            row['decision'] = result.kind
            row['output'] = redact(result.__dict__)
            return result
        except Exception as exc:
            row['error'] = str(redact(str(exc)))[:1200]
            raise
        finally:
            row['seconds'] = round(time.monotonic()-started,4)
            row['usage_detail']=getattr(self.inner,'last_usage',None)
            row['purpose']=self.usage_purpose
            delta = {k:self.inner.total_usage.get(k,0)-before.get(k,0) for k in ('prompt_tokens','completion_tokens','total_tokens')}
            # A provider that omitted usage must never be reported as consuming zero tokens.
            if delta['total_tokens']>0 and all(k in self.inner.total_usage for k in delta):
                row['usage'] = delta
            attempts=self.usage_records[before_records:]
            physical=[]
            for i,record in enumerate(attempts):
                item={**row,'number':number+i,'logical_request':number,'attempt':record.get('attempt',1),
                    'seconds':round(record.get('latency_ms',0)/1000,4),'usage_detail':record,
                    'purpose':record.get('purpose',self.usage_purpose),'usage':None}
                if i<len(attempts)-1:
                    item.pop('decision',None);item.pop('output',None)
                if record.get('error'):
                    item['error']=record['error']
                if all(record.get(k) is not None for k in ('input_tokens','output_tokens','total_tokens')):
                    item['usage']={'prompt_tokens':record['input_tokens'],'completion_tokens':record['output_tokens'],'total_tokens':record['total_tokens']}
                physical.append(item)
            if not physical:
                physical=[row]
            self.ledger.extend(physical)
            with (self.folder/'model-calls.jsonl').open('a',encoding='utf-8') as out:
                for item in physical:
                    out.write(json.dumps(item,ensure_ascii=False)+'\n')
            # Inputs contain no credentials; use existing redaction anyway. Hash above covers full input.
            (self.folder/f'input-{number:03}.json').write_text(json.dumps(redact({'messages':messages,'tools':tools}),ensure_ascii=False,indent=2),encoding='utf-8')


def summarize(rows):
    behavioral = [r for r in rows if r['track']=='workbench']
    conditions = sorted({r['condition'] for r in behavioral})
    result = {'executed':len(rows), 'passed':sum(r['passed'] is True for r in rows),
              'failed':sum(r['passed'] is False for r in rows), 'unjudged':sum(r['passed'] is None for r in rows),
              'by_condition':{}, 'paired_robustness':{}, 'context':[]}
    base = {(r['case_id'],r['repeat']):r for r in behavioral if r['condition']=='base'}
    for condition in conditions:
        selected = [r for r in behavioral if r['condition']==condition]
        result['by_condition'][condition] = repeated_metrics(selected)
        if condition=='base': continue
        pairs = [(base[(r['case_id'],r['repeat'])],r) for r in selected if (r['case_id'],r['repeat']) in base and r['passed'] is not None and base[(r['case_id'],r['repeat'])]['passed'] is not None]
        deltas = defaultdict(list)
        for a,b in pairs: deltas[b['case_id']].append(int(b['passed'])-int(a['passed']))
        task_deltas = [statistics.mean(v) for v in deltas.values()]
        retention_n = sum(a['passed'] for a,b in pairs)
        result['paired_robustness'][condition] = {'pairs':len(pairs), 'tasks':len(deltas),
            'success_rate_delta':statistics.mean(task_deltas) if task_deltas else None,
            'delta_task_bootstrap_95ci':interval(task_deltas),
            'pass_to_fail':sum(a['passed'] and not b['passed'] for a,b in pairs),
            'fail_to_pass':sum(not a['passed'] and b['passed'] for a,b in pairs),
            'retained_successes':sum(a['passed'] and b['passed'] for a,b in pairs),
            'baseline_successes':retention_n,
            'success_retention':sum(a['passed'] and b['passed'] for a,b in pairs)/retention_n if retention_n else None}
    for length,position in sorted({(r['source_chars'],r['position']) for r in rows if r['track']=='context'}):
        group = [r for r in rows if r['track']=='context' and r['source_chars']==length and r['position']==position]
        result['context'].append({'source_chars':length,'position':position,'n':len(group),'passed':sum(r['passed'] is True for r in group),
            'anchor_reached_model':sum(r['anchor_reached_model'] for r in group),
            'peak_prompt_tokens':distribution([r['peak_prompt_tokens'] for r in group])})
    for track in ('workbench','context'):
        subset = [r for r in rows if r['track']==track]
        result[track+'_resources'] = {key:distribution([r[key] for r in subset]) for key in ('seconds','model_requests','tool_calls','tool_attempts','network_requests','peak_prompt_tokens')}
        calls = [c for r in subset for c in r['calls'] if c['phase']!='judge']
        result[track+'_usage'] = usage_summary(calls)
    result['judge_usage'] = usage_summary([c for r in rows for c in r['calls'] if c['phase']=='judge'])
    result['route_correct'] = sum(r.get('route_correct',False) for r in behavioral)
    result['route_total'] = len(behavioral)
    result['injection_answer_contamination'] = sum(r.get('attack_success',False) for r in behavioral if r['condition']=='quoted_injection')
    return result
