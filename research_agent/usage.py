"""Lossless per-request accounting; absent provider metrics remain unknown."""
from copy import deepcopy


def normalize_usage(raw):
    raw = raw if isinstance(raw, dict) else {}
    def number(*values):
        return next((v for v in values if type(v) is int and v >= 0), None)
    details = raw.get('prompt_tokens_details') or raw.get('input_tokens_details') or {}
    details = details if isinstance(details,dict) else {}
    incoming = number(raw.get('prompt_tokens'), raw.get('input_tokens'))
    outgoing = number(raw.get('completion_tokens'), raw.get('output_tokens'))
    cached = number(raw.get('prompt_cache_hit_tokens'), details.get('cached_tokens'))
    return {'input_tokens': incoming, 'output_tokens': outgoing,
            'total_tokens': number(raw.get('total_tokens'), incoming + outgoing if incoming is not None and outgoing is not None else None),
            'cache_read_tokens': cached,
            'cache_write_tokens': number(raw.get('cache_creation_input_tokens'), raw.get('cache_write_tokens')),
            'cache_miss_tokens': number(raw.get('prompt_cache_miss_tokens')),
            'cache_reported': cached is not None, 'raw_usage': deepcopy(raw)}


def summarize_usage(records):
    result = {'requests': len(records)}
    for key in ('input_tokens','output_tokens','total_tokens','cache_read_tokens','cache_write_tokens'):
        values = [r[key] for r in records if r.get(key) is not None]
        result[key] = sum(values) if values else None
        result[key + '_coverage'] = len(values) / len(records) if records else None
    covered = [r for r in records if r.get('cache_read_tokens') is not None and r.get('input_tokens') is not None]
    denominator = sum(r['input_tokens'] for r in covered)
    result['cache_hit_rate_on_reported_input'] = sum(r['cache_read_tokens'] for r in covered) / denominator if denominator else None
    return result
