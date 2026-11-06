import os, json
import anthropic
from flask import Blueprint, jsonify, render_template, request

bp = Blueprint('musicbox', __name__)

FAST_MODEL = os.environ.get('CLAUDE_FAST_MODEL', 'claude-haiku-5-5')
SMART_MODEL = os.environ.get('CLAUDE_MODEL', 'claude-opus-5-5')

_client = None
_melody_cache = {}


class ClaudeError(Exception):
    pass


def client():
    global _client
    if not os.environ.get('ANTHROPIC_API_KEY'):
        raise ClaudeError('На сервере не задан ключ ANTHROPIC_API_KEY.')
    if _client is None:
        _client = anthropic.Anthropic(timeout=120.0)
    return _client


def claude_json(model, prompt, schema, effort, fallback=False):
    params = dict(
        model=model,
        max_tokens=16000,
        output_config={'effort': effort, 'format': {'type': 'json_schema', 'schema': schema}},
        messages=[{'role': 'user', 'content': prompt}],
    )
    try:
        if fallback:
            r = client().beta.messages.create(betas=['server-side-fallback-2026-07-01'], fallbacks='default', **params)
        else:
            r = client().messages.create(**params)
    except anthropic.AuthenticationError:
        raise ClaudeError('Ключ ANTHROPIC_API_KEY не подходит.')
    except anthropic.RateLimitError:
        raise ClaudeError('Слишком много запросов подряд. Подожди минуту.')
    except anthropic.APIStatusError as e:
        raise ClaudeError(f'Claude ответил ошибкой {e.status_code}.')
    except anthropic.APIConnectionError:
        raise ClaudeError('Не удалось связаться с Claude.')
    if r.stop_reason == 'refusal':
        raise ClaudeError('Claude отказался отвечать на этот запрос.')
    text = next((b.text for b in r.content if b.type == 'text'), '')
    try:
        return json.loads(text)
    except ValueError:
        raise ClaudeError('Claude вернул непонятный ответ.')


IDENTIFY_SCHEMA = {
    'type': 'object',
    'properties': {
        'title': {'type': 'string'},
        'artist': {'type': 'string'},
        'sure': {'type': 'boolean'},
    },
    'required': ['title', 'artist', 'sure'],
    'additionalProperties': False,
}

MELODY_SCHEMA = {
    'type': 'object',
    'properties': {
        'title': {'type': 'string'},
        'artist': {'type': 'string'},
        'part': {'type': 'string'},
        'bpm': {'type': 'number'},
        'beatsPerBar': {'type': 'integer', 'enum': [2, 3, 4, 6]},
        'confidence': {'type': 'string', 'enum': ['high', 'medium', 'low']},
        'notes': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {'pitch': {'type': 'integer'}, 'start': {'type': 'number'}, 'duration': {'type': 'number'}},
                'required': ['pitch', 'start', 'duration'],
                'additionalProperties': False,
            },
        },
        'bass': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {'pitch': {'type': 'integer'}, 'start': {'type': 'number'}},
                'required': ['pitch', 'start'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['title', 'artist', 'part', 'bpm', 'beatsPerBar', 'confidence', 'notes', 'bass'],
    'additionalProperties': False,
}


@bp.route('/shkatulka')
def page():
    return render_template('shkatulka.html')


@bp.route('/api/musicbox/identify', methods=['POST'])
def identify():
    body = request.get_json(silent=True) or {}
    text = (body.get('text') or '').strip()[:500]
    if not text:
        return jsonify({'error': 'Пустой запрос.'}), 400
    prompt = '\n'.join([
        'Человек на детском празднике сказал вслух, какую песню он хочет. Текст получен распознаванием речи.',
        f'Его слова: "{text}".',
        'Определи самую вероятную известную песню. Слова могут содержать ошибки распознавания, смесь русского, немецкого и английского,',
        'обрывки названия или текста песни, имя исполнителя, мультфильм или фильм.',
        'Пример: "песня группы пинк флойд там что-то брик" это "Another Brick in the Wall (Part 2)" группы Pink Floyd.',
        'Если по словам песню определить нельзя, верни пустые title и artist.',
        'sure равно true, только если ты уверен в песне.',
    ])
    try:
        g = claude_json(FAST_MODEL, prompt, IDENTIFY_SCHEMA, 'low')
    except ClaudeError as e:
        return jsonify({'error': str(e)}), 502
    return jsonify({'title': g['title'].strip(), 'artist': g['artist'].strip(), 'sure': bool(g['sure'] and g['title'].strip())})


@bp.route('/api/musicbox/melody', methods=['POST'])
def melody():
    body = request.get_json(silent=True) or {}
    title = (body.get('title') or '').strip()[:200]
    artist = (body.get('artist') or '').strip()[:200]
    if not title:
        return jsonify({'error': 'Не указана песня.'}), 400
    key = (title + '|' + artist).lower()
    if key in _melody_cache:
        return jsonify(_melody_cache[key])
    prompt = '\n'.join([
        f'Ты музыкальный аранжировщик. Песня: "{title}"' + (f', исполнитель {artist}.' if artist else '.'),
        'Запиши мелодию основного голоса самой узнаваемой части песни, обычно припева, длиной 4–8 тактов.',
        'notes: pitch — высота по стандарту MIDI в тональности оригинала, start — начало в долях от 0, duration — длительность в долях.',
        'Четвертная нота равна 1 доле. Мелодия одноголосная, ноты не перекрываются.',
        'bass: основной тон аккорда на сильных долях, pitch от 36 до 55.',
        'part: какая это часть песни, по-русски, например "припев".',
        'confidence: "high", если уверен в нотах; "medium", если примерно; "low", если песню плохо знаешь.',
    ])
    try:
        r = claude_json(SMART_MODEL, prompt, MELODY_SCHEMA, 'high', fallback=True)
    except ClaudeError as e:
        return jsonify({'error': str(e)}), 502
    notes = [[n['pitch'], n['start'], n['duration']] for n in r['notes']
             if 20 < n['pitch'] < 110 and n['start'] >= 0 and n['duration'] > 0]
    if len(notes) < 4:
        return jsonify({'error': 'Мелодию записать не удалось. Попробуй другую песню.'}), 502
    result = {
        'title': r['title'] or title,
        'artist': r['artist'] or artist,
        'part': r['part'],
        'bpm': r['bpm'] if r['bpm'] > 0 else 100,
        'beatsPerBar': r['beatsPerBar'],
        'confidence': r['confidence'],
        'notes': notes,
        'bass': [[b['pitch'], b['start']] for b in r['bass'] if 20 < b['pitch'] < 110 and b['start'] >= 0],
    }
    _melody_cache[key] = result
    return jsonify(result)
