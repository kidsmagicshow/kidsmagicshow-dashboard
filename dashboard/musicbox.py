import os, json, re
import requests
from flask import Blueprint, jsonify, render_template, request

bp = Blueprint('musicbox', __name__)

GEMINI_URL = 'https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent'
FAST_MODEL = os.environ.get('GEMINI_FAST_MODEL', 'gemini-3.8-flash')
SMART_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.8-flash')

_melody_cache = {}


class GeminiError(Exception):
    pass


def gemini_json(model, prompt, schema, think=True, timeout=120):
    key = os.environ.get('GEMINI_API_KEY', '')
    if not key:
        raise GeminiError('На сервере не задан ключ GEMINI_API_KEY.')
    config = {'responseMimeType': 'application/json', 'responseSchema': schema, 'temperature': 0.2}
    # без ограничения Gemini думает над нотами больше минуты
    config['thinkingConfig'] = {'thinkingBudget': 2048 if think else 0}
    try:
        r = requests.post(
            GEMINI_URL.format(model),
            headers={'x-goog-api-key': key},
            json={'contents': [{'role': 'user', 'parts': [{'text': prompt}]}], 'generationConfig': config},
            timeout=timeout,
        )
    except requests.RequestException:
        raise GeminiError('Не удалось связаться с Gemini.')
    if r.status_code in (401, 403):
        raise GeminiError('Ключ GEMINI_API_KEY не подходит.')
    if r.status_code == 429:
        raise GeminiError('Слишком много запросов подряд. Подожди минуту.')
    if r.status_code != 200:
        raise GeminiError(f'Gemini ответил ошибкой {r.status_code}.')
    try:
        content = r.json()['candidates'][0]['content']['parts']
        text = ''.join(p.get('text', '') for p in content if not p.get('thought'))
        data = json.loads(re.sub(r'^```(?:json)?|```$', '', text.strip()).strip())
    except (KeyError, IndexError, ValueError):
        raise GeminiError('Gemini вернул непонятный ответ.')
    return data if isinstance(data, dict) else {}


IDENTIFY_SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'title': {'type': 'STRING'},
        'artist': {'type': 'STRING'},
        'sure': {'type': 'BOOLEAN'},
    },
    'required': ['title', 'artist', 'sure'],
    'propertyOrdering': ['title', 'artist', 'sure'],
}

MELODY_SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'title': {'type': 'STRING'},
        'artist': {'type': 'STRING'},
        'part': {'type': 'STRING'},
        'hook': {'type': 'STRING'},
        'bpm': {'type': 'NUMBER'},
        'beatsPerBar': {'type': 'INTEGER'},
        'confidence': {'type': 'STRING', 'enum': ['high', 'medium', 'low']},
        'notes': {
            'type': 'ARRAY',
            'items': {
                'type': 'OBJECT',
                'properties': {'syl': {'type': 'STRING'}, 'pitch': {'type': 'INTEGER'}, 'start': {'type': 'NUMBER'}, 'duration': {'type': 'NUMBER'}},
                'required': ['syl', 'pitch', 'start', 'duration'],
                'propertyOrdering': ['syl', 'pitch', 'start', 'duration'],
            },
        },
        'bass': {
            'type': 'ARRAY',
            'items': {
                'type': 'OBJECT',
                'properties': {'pitch': {'type': 'INTEGER'}, 'start': {'type': 'NUMBER'}},
                'required': ['pitch', 'start'],
                'propertyOrdering': ['pitch', 'start'],
            },
        },
    },
    'required': ['title', 'artist', 'part', 'hook', 'bpm', 'beatsPerBar', 'confidence', 'notes', 'bass'],
    'propertyOrdering': ['title', 'artist', 'part', 'hook', 'bpm', 'beatsPerBar', 'confidence', 'notes', 'bass'],
}


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _rows(items, fields):
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and all(_num(it.get(f)) for f in fields):
            out.append([it[f] for f in fields])
    return out


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
        g = gemini_json(FAST_MODEL, prompt, IDENTIFY_SCHEMA, think=False, timeout=40)
    except GeminiError as e:
        return jsonify({'error': str(e)}), 502
    title = str(g.get('title') or '').strip()
    artist = str(g.get('artist') or '').strip()
    return jsonify({'title': title, 'artist': artist, 'sure': bool(g.get('sure') is True and title)})


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
        'Запиши мелодию самого известного, узнаваемого куска песни — того, который все сразу узнают и напевают (главный припев, известный рифф или тема). Не бери вступление или куплет, если их узнают хуже. Длина 4–8 тактов, начни с начала фразы этого куска.',
        'Сначала в hook напиши слова этого куска так, как их поют. Если это инструментальная тема или рифф, напиши в hook её название.',
        'Потом запиши notes строго по этим словам: одна нота на каждый слог по порядку, в syl — этот слог. Если на слог поётся несколько нот, у следующих нот syl равен "-". У инструментальной темы syl всегда "-".',
        'Ритм и высоты как в оригинале, чтобы мелодию узнали с первых нот.',
        'notes: pitch — высота по стандарту MIDI в тональности оригинала, start — начало в долях от 0, duration — длительность в долях.',
        'Четвертная нота равна 1 доле. Мелодия одноголосная, ноты не перекрываются.',
        'bass: основной тон аккорда на сильных долях, pitch от 36 до 55.',
        'beatsPerBar: 2, 3, 4 или 6.',
        'part: какая это часть песни, по-русски, например "припев".',
        'confidence: "high", если уверен в нотах; "medium", если примерно; "low", если песню плохо знаешь.',
    ])
    try:
        r = gemini_json(SMART_MODEL, prompt, MELODY_SCHEMA)
    except GeminiError as e:
        return jsonify({'error': str(e)}), 502
    notes = [n for n in _rows(r.get('notes'), ('pitch', 'start', 'duration'))
             if 20 < n[0] < 110 and n[1] >= 0 and n[2] > 0]
    if len(notes) < 4:
        return jsonify({'error': 'Мелодию записать не удалось. Попробуй другую песню.'}), 502
    result = {
        'title': str(r.get('title') or title),
        'artist': str(r.get('artist') or artist),
        'part': str(r.get('part') or ''),
        'bpm': r['bpm'] if _num(r.get('bpm')) and r['bpm'] > 0 else 100,
        'beatsPerBar': r.get('beatsPerBar') if r.get('beatsPerBar') in (2, 3, 4, 6) else 4,
        'confidence': r.get('confidence') if r.get('confidence') in ('high', 'medium', 'low') else 'medium',
        'notes': notes,
        'bass': [b for b in _rows(r.get('bass'), ('pitch', 'start')) if 20 < b[0] < 110 and b[1] >= 0],
    }
    _melody_cache[key] = result
    return jsonify(result)
