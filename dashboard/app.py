import os, time, json, tempfile
from flask import Flask, jsonify, render_template, request, Response
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google.ads.googleads.client import GoogleAdsClient

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_TTL = 300  # 5 min

# --- Credentials: env vars (cloud) or files (local) ---
CLIENT_SECRET = os.path.join(BASE_DIR, '..', 'client_secret.json')
TOKEN_FILE    = os.path.join(BASE_DIR, 'token.json')

def _ads_yaml_path():
    yaml_env = os.environ.get('GOOGLE_ADS_YAML')
    if yaml_env:
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False)
        f.write(yaml_env)
        f.close()
        return f.name
    return os.path.join(BASE_DIR, '..', 'google-ads.yaml')

CUSTOMER_ID = "5053903174"

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/calendar.readonly',
]

SKIP_SENDERS = [
    'google.com', 'accounts.google', 'mailer-daemon', 'noreply', 'no-reply',
    'galaxus', 'anthropic', 'paypal', 'meetup', 'penguinmagic',
    'linkedin', 'facebook', 'instagram', 'twitter', 'postfinance', 'twint',
    'microsoft', 'apple.com', 'amazon', 'trustindex', 'trust-index',
    'newsletter', 'notifications', 'mailchimp', 'sendgrid', 'mailjet', 'sendinblue',
    'stripe', 'cloudflare', 'namecheap', 'hostinger', 'wix.com', 'wordpress',
    'dropbox', 'notion', 'booking.com', 'eventim', 'ticketmaster',
    'swisscom', 'sunrise', 'support@', 'info@google', 'donotreply',
    'do-not-reply', 'automated', 'alert@', 'security@', 'billing@',
]

# --- In-memory cache ---
_cache = {}
_wa_cache = {}  # WhatsApp cache (in-memory on cloud, file on local)

def cached(key, fn):
    now = time.time()
    if key not in _cache or now - _cache[key]['t'] > CACHE_TTL:
        _cache[key] = {'data': fn(), 't': now}
    return _cache[key]['data']

# --- Basic auth (only when DASHBOARD_PASSWORD env var is set) ---
DASHBOARD_PASSWORD = os.environ.get('DASHBOARD_PASSWORD', '')

def _check_auth(username, password):
    return username == 'maksym' and password == DASHBOARD_PASSWORD

@app.before_request
def require_auth():
    if DASHBOARD_PASSWORD and request.path == '/':
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return Response('Login required', 401,
                            {'WWW-Authenticate': 'Basic realm="Kids Magic Show"'})

# --- Google credentials ---
def get_creds():
    token_env = os.environ.get('TOKEN_JSON')
    if token_env:
        creds = Credentials.from_authorized_user_info(json.loads(token_env), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds
    # Local fallback: file-based OAuth
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
    return creds

def gmail_get_msgs(svc, q, n=20):
    res = svc.users().messages().list(userId='me', q=q, maxResults=n).execute()
    msgs = []
    for m in res.get('messages', []):
        data = svc.users().messages().get(
            userId='me', id=m['id'], format='metadata',
            metadataHeaders=['From', 'Subject', 'Date']
        ).execute()
        h = {x['name']: x['value'] for x in data['payload']['headers']}
        msgs.append({'id': m['id'], 'from': h.get('From', ''), 'subject': h.get('Subject', ''), 'date': h.get('Date', '')})
    return msgs

def gmail_count(svc, q):
    res = svc.users().messages().list(userId='me', q=q, maxResults=1).execute()
    return res.get('resultSizeEstimate', 0)

# --- Routes ---

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/ads')
def api_ads():
    def fetch():
        try:
            client = GoogleAdsClient.load_from_storage(_ads_yaml_path())
            ga = client.get_service("GoogleAdsService")
            req = client.get_type("SearchGoogleAdsRequest")
            req.customer_id = CUSTOMER_ID
            req.query = """
                SELECT campaign.name, metrics.impressions, metrics.clicks,
                       metrics.cost_micros, metrics.conversions, metrics.ctr
                FROM campaign
                WHERE segments.date DURING LAST_30_DAYS
                  AND campaign.status = 'ENABLED'
                ORDER BY metrics.impressions DESC
            """
            campaigns, total_cost, total_clicks, total_impressions = [], 0, 0, 0
            for row in ga.search(request=req):
                cost = row.metrics.cost_micros / 1_000_000
                total_cost += cost
                total_clicks += row.metrics.clicks
                total_impressions += row.metrics.impressions
                campaigns.append({
                    'name': row.campaign.name,
                    'impressions': f"{row.metrics.impressions:,}",
                    'clicks': row.metrics.clicks,
                    'ctr': f"{row.metrics.ctr * 100:.1f}%",
                    'cost': f"{cost:.2f}",
                    'conversions': int(row.metrics.conversions),
                })
            return {
                'campaigns': campaigns,
                'total_cost': f"{total_cost:.2f}",
                'total_clicks': total_clicks,
                'total_impressions': f"{total_impressions:,}",
            }
        except Exception as e:
            return {'error': str(e), 'campaigns': []}
    return jsonify(cached('ads', fetch))

@app.route('/api/leads')
def api_leads():
    def fetch():
        try:
            svc = build('gmail', 'v1', credentials=get_creds())

            def is_real_lead(m):
                return not any(s.lower() in m['from'].lower() for s in SKIP_SENDERS)

            raw_inquiries = gmail_get_msgs(
                svc,
                'in:inbox (Anfrage OR Geburtstag OR Zaubershow OR Buchung OR booking OR "magic show"'
                ' OR Kooperation OR Kinderprogramm OR Abendshow OR Firmenfeier OR Kindergeburtstag) newer_than:30d'
            )
            inquiries = [m for m in raw_inquiries if is_real_lead(m)]

            raw_replies = gmail_get_msgs(
                svc,
                'in:inbox (subject:Re: OR subject:AW: OR subject:RE:) newer_than:60d', 30
            )
            replies = [r for r in raw_replies if is_real_lead(r)]

            raw_declined = gmail_get_msgs(
                svc,
                'in:inbox (leider OR "kein Interesse" OR "other plans" OR absagen OR "no need" OR "not interested") newer_than:60d',
                20
            )
            declined = [r for r in raw_declined if is_real_lead(r)]

            sent_count = gmail_count(
                svc,
                'in:sent (Kooperation OR Kinderprogramm OR Abendshow OR Zaubershow) newer_than:60d'
            )
            no_reply = max(0, sent_count - len(replies) - len(declined))

            return {
                'inquiries': inquiries,
                'replies': replies,
                'declined': declined,
                'status': {
                    'sent': sent_count,
                    'replied': len(replies),
                    'no_reply': no_reply,
                    'declined': len(declined),
                }
            }
        except Exception as e:
            return {'error': str(e), 'inquiries': [], 'replies': [], 'declined': [], 'status': {}}
    return jsonify(cached('leads', fetch))

@app.route('/api/whatsapp')
def api_whatsapp():
    def fetch():
        # Cloud: use in-memory cache updated via POST /api/whatsapp/update
        if _wa_cache:
            return _wa_cache
        # Local: read from file
        wa_file = os.path.join(BASE_DIR, 'whatsapp_cache.json')
        if os.path.exists(wa_file):
            with open(wa_file) as f:
                return json.load(f)
        return {'error': 'Нет данных WhatsApp', 'chats': [], 'updated': None, 'summary': {}}
    return jsonify(cached('whatsapp', fetch))

@app.route('/api/whatsapp/update', methods=['POST'])
def api_whatsapp_update():
    token = request.headers.get('X-Update-Token', '')
    update_token = os.environ.get('WA_UPDATE_TOKEN', '')
    if update_token and token != update_token:
        return jsonify({'error': 'unauthorized'}), 401
    global _wa_cache
    _wa_cache = request.get_json()
    _cache.pop('whatsapp', None)
    return jsonify({'status': 'ok'})

@app.route('/api/calendar')
def api_calendar():
    def fetch():
        try:
            from datetime import datetime, timedelta, timezone
            svc = build('calendar', 'v3', credentials=get_creds())
            now = datetime.now(timezone.utc).isoformat()
            end = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
            result = svc.events().list(
                calendarId='primary', timeMin=now, timeMax=end,
                maxResults=20, singleEvents=True, orderBy='startTime'
            ).execute()
            events = []
            for e in result.get('items', []):
                start = e['start'].get('dateTime', e['start'].get('date', ''))
                events.append({
                    'title': e.get('summary', '—'),
                    'start': start,
                    'location': e.get('location', ''),
                })
            return {'events': events}
        except Exception as e:
            return {'error': str(e), 'events': []}
    return jsonify(cached('calendar', fetch))

@app.route('/api/refresh')
def api_refresh():
    _cache.clear()
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"\n🎩 Kids Magic Show Dashboard → http://localhost:{port}\n")
    app.run(debug=False, host='0.0.0.0', port=port)
