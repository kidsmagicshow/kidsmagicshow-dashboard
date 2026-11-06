import os, json, re
import requests
from flask import Blueprint, jsonify, render_template, request

bp = Blueprint('musicbox', __name__)

GEMINI_URL = 'https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent'
FAST_MODEL = os.environ.get('GEMINI_FAST_MODEL', 'gemini-2.5-flash')
SMART_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-pro')
MAX_AUDIO_B64 = 8 * 1024 * 1024

_melody_cache = {}


class GeminiError(Exception):
    pass


def gemini_json(model, parts, timeout=120):
    key = os.environ.get('GEMINI_API_KEY', '')
    if not key:
        raise GeminiError('На сервере не задан ключ GEMINI_API_KEY.')
    try:
        r = requests.post(
            GEMINI_URL.format(model),
            headers={'x-goog-api-key': key},
            json={
                'contents': [{'role': 'user', 'parts': parts}],
                'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.2},
            },
            timeout=timeout,
        )
    except requests.RequestException:
        raise GeminiError('Не удалось связаться с Gemini.')
    if r.status_code != 200:
        raise GeminiError(f'Gemini ответил ошибкой {r.status_code}.')
    try:
        content = r.json()['candidates'][0]['content']['parts']
        text = ''.join(p.get('text', '') for p in content)
        return json.loads(re.sub(r'^```(?:json)?|```$', '', text.strip()).strip())
    except (KeyError, IndexError, ValueError):
        raise GeminiError('Gemini вернул непонятный ответ.')


IDENTIFY_RULES = '\n'.join([
    'Определи самую вероятную известную песню. Слова могут содержать ошибки распознавания, смесь русского, немецкого и английского,',
    'обрывки названия или текста песни, имя исполнителя, мультфильм или фильм.',
    'Пример: "песня группы пинк флойд там что-то брик" это "Another Brick in the Wall (Part 2)" группы Pink Floyd.',
    'Если по словам песню определить нельзя, верни пустой title.',
    'sure равно true, только если ты уверен в песне.',
])


@bp.route('/shkatulka')
def page():
    return render_template('shkatulka.html')


@bp.route('/api/musicbox/identify', methods=['POST'])
def identify():
    body = request.get_json(silent=True) or {}
    text = (body.get('text') or '').strip()[:500]
    audio = body.get('audio') or ''
    try:
        if audio:
            if len(audio) > MAX_AUDIO_B64:
                return jsonify({'error': 'Запись слишком длинная. Скажи короче.'}), 400
            mime = body.get('mime') or 'audio/webm'
            prompt = '\n'.join([
                'В записи человек на детском празднике говорит, какую песню он хочет.',
                IDENTIFY_RULES,
                'Верни только JSON: {"heard":"что человек сказал","title":"точное название","artist":"исполнитель","sure":true}',
            ])
            parts = [{'text': prompt}, {'inline_data': {'mime_type': mime, 'data': audio}}]
        elif text:
            prompt = '\n'.join([
                'Человек на детском празднике сказал вслух, какую песню он хочет. Текст получен распознаванием речи.',
                f'Его слова: "{text}".',
                IDENTIFY_RULES,
                'Верни только JSON: {"title":"точное название","artist":"исполнитель","sure":true}',
            ])
            parts = [{'text': prompt}]
        else:
            return jsonify({'error': 'Пустой запрос.'}), 400
        g = gemini_json(FAST_MODEL, parts, timeout=40)
    except GeminiError as e:
        return jsonify({'error': str(e)}), 502
    if not isinstance(g, dict):
        g = {}
    return jsonify({
        'title': str(g.get('title') or '').strip(),
        'artist': str(g.get('artist') or '').strip(),
        'sure': bool(g.get('sure')),
        'heard': str(g.get('heard') or '').strip(),
    })


def _num_list(rows, size):
    out = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, list) and len(row) >= size and all(isinstance(x, (int, float)) for x in row[:size]):
            out.append([float(x) for x in row[:size]])
    return out


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
        'Верни только JSON в таком виде:',
        '{"title":"название","artist":"исполнитель","part":"припев","bpm":100,"beatsPerBar":4,"confidence":"high",'
        '"notes":[[midiPitch,startBeat,durationBeats]],"bass":[[midiPitch,startBeat]]}',
        'Правила: startBeat начинается с 0, четвертная нота равна 1 доле. Высоты в тональности оригинала.',
        'Мелодия одноголосная, ноты не перекрываются. bass: основной тон аккорда на сильных долях, от 36 до 55.',
        'confidence: "high", если уверен в нотах; "medium", если примерно; "low", если песню плохо знаешь.',
    ])
    try:
        r = gemini_json(SMART_MODEL, [{'text': prompt}])
    except GeminiError as e:
        return jsonify({'error': str(e)}), 502
    if not isinstance(r, dict):
        r = {}
    notes = [n for n in _num_list(r.get('notes'), 3) if 20 < n[0] < 110 and n[2] > 0 and n[1] >= 0]
    if len(notes) < 4:
        return jsonify({'error': 'Мелодию записать не удалось. Попробуй другую песню.'}), 502
    result = {
        'title': str(r.get('title') or title),
        'artist': str(r.get('artist') or artist),
        'part': str(r.get('part') or ''),
        'bpm': r.get('bpm') if isinstance(r.get('bpm'), (int, float)) else 100,
        'beatsPerBar': r.get('beatsPerBar') if r.get('beatsPerBar') in (2, 3, 4, 6) else 4,
        'confidence': r.get('confidence') if r.get('confidence') in ('high', 'medium', 'low') else 'medium',
        'notes': notes,
        'bass': [b for b in _num_list(r.get('bass'), 2) if 20 < b[0] < 110 and b[1] >= 0],
    }
    _melody_cache[key] = result
    return jsonify(result)
