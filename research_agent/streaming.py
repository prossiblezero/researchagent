"""Chat Completions SSE decoding and public progress (never private reasoning)."""
import json
import re
import time
from .trace import redact


def reply_prefix(text):
    """Decode a possibly unfinished routing reply string without exposing its JSON."""
    match = re.search(r'"reply"\s*:\s*"', text)
    if not match:
        return ''
    raw = text[match.end():]
    escaped = False
    for i, char in enumerate(raw):
        if char == '"' and not escaped:
            raw = raw[:i]
            break
        escaped = char == '\\' and not escaped
    # Only incomplete JSON escapes can require dropping up to six suffix chars.
    for trim in range(min(6, len(raw)) + 1):
        try:
            return json.loads('"' + (raw[:-trim] if trim else raw) + '"')
        except (ValueError, TypeError):
            pass
    return ''


def public_text(text, purpose, *, final=False, secret=''):
    if purpose == 'routing':
        text = reply_prefix(text)
    elif purpose != 'answer':
        return ''
    if secret:
        text = text.replace(secret, '[REDACTED]')
    if not final:
        # Hold incomplete credentials/words until a boundary, then redact the whole prefix.
        text = re.sub(r'[\w+/=~-]+$', '', text)
    return str(redact(text))


def read_chat_stream(response, notify, *, check=lambda: None):
    """Accumulate tool fragments by index; reject truncated streams before execution."""
    check()
    first = response.readline()
    check()
    if first.lstrip().startswith(b'{'):
        # Some compatible gateways ignore stream=true and return ordinary JSON.
        payload = json.loads((first + response.read()).decode('utf-8'))
        check()
        notify(payload.get('choices', [{}])[0].get('message', {}), True, False)
        return payload
    message = {'content': '', 'reasoning_content': ''}
    calls, usage, finish = {}, None, None
    last_emit = 0.0
    data = []
    done = False

    def consume():
        nonlocal usage, finish, last_emit, done
        if not data:
            return
        raw = '\n'.join(data)
        data.clear()
        if raw == '[DONE]':
            done = True
            return
        chunk = json.loads(raw)
        if chunk.get('error'):
            error=chunk['error']
            error_text='模型流返回错误：' + str(redact(error))[:300]
            if isinstance(error,dict) and (error.get('type') in {'upstream_error','server_error','overloaded_error','rate_limit_error'} or error.get('code') in {408,429,500,502,503,504}):
                raise ConnectionError(error_text)
            raise RuntimeError(error_text)
        if chunk.get('usage') is not None:
            usage = chunk['usage']
        for choice in chunk.get('choices', []):
            if choice.get('index', 0) != 0:
                continue
            delta = choice.get('delta') or {}
            for key in ('content', 'reasoning_content'):
                if isinstance(delta.get(key), str):
                    message[key] += delta[key]
            for part in delta.get('tool_calls') or []:
                index = part.get('index', 0)
                call = calls.setdefault(index, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                if part.get('id'):
                    call['id'] = part['id']
                for key in ('name', 'arguments'):
                    call['function'][key] += (part.get('function') or {}).get(key) or ''
            if choice.get('finish_reason'):
                finish = choice['finish_reason']
        if calls:
            message['tool_calls'] = [calls[i] for i in sorted(calls)]
        if time.monotonic() - last_emit >= .2:
            notify(message, False, True)
            last_emit = time.monotonic()

    line = first
    while line:
        decoded = line.decode('utf-8').rstrip('\r\n')
        if not decoded:
            consume()
            if done:
                break
        elif decoded.startswith('data:'):
            data.append(decoded[5:].lstrip(' '))
        check()
        line = response.readline()
        check()
    consume()
    if not finish:
        raise ConnectionError('模型流在结束标记前中断，未执行不完整工具调用')
    notify(message, True, True)
    return {'choices': [{'message': message, 'finish_reason': finish}], 'usage': usage}
