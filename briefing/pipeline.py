"""Daily market-intelligence briefing.
1) collect fresh items from RSS feeds and the Hacker News API
2) de-duplicate and keep the last N hours
3) AI enrichment: relevance to the company profile, category, 2-sentence summary, why it matters (strict JSON)
4) store in SQLite, render an HTML email digest and a Slack message, serve a dashboard
Run once:  python -m briefing.pipeline        Schedule: cron / GitHub Actions (see README)."""

import calendar
import hashlib
import html
import json
import os
import re
import sqlite3
import time
import urllib.request

import feedparser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = json.load(open(os.path.join(ROOT, 'config.json')))
DB = os.path.join(ROOT, 'out', 'briefing.db')
UA = {'User-Agent': 'Mozilla/5.0 (briefing-bot; +https://example.com/bot)'}


# ------------------------------------------------------------------------------------------ collect
def _clean(s, n=600):
    s = html.unescape(re.sub(r'<[^>]+>', ' ', s or ''))
    return re.sub(r'\s+', ' ', s).strip()[:n]


def fetch_rss(name, url):
    d = feedparser.parse(url, request_headers=UA)
    out = []
    for e in d.entries[:40]:
        ts = e.get('published_parsed') or e.get('updated_parsed')
        out.append(
            {
                'source': name,
                'title': _clean(e.get('title'), 220),
                'url': e.get('link', ''),
                'published': calendar.timegm(ts) if ts else int(time.time()),
                'excerpt': _clean(e.get('summary') or e.get('description')),
            }
        )
    return out


def fetch_hn(limit=60):
    def get(url):
        return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15))

    ids = get('https://hacker-news.firebaseio.com/v0/topstories.json')[:limit]
    out = []
    for i in ids:
        it = get(f'https://hacker-news.firebaseio.com/v0/item/{i}.json') or {}
        if it.get('url') and it.get('title'):
            out.append(
                {
                    'source': 'Hacker News',
                    'title': it['title'],
                    'url': it['url'],
                    'published': it.get('time', int(time.time())),
                    'excerpt': f'{it.get("score", 0)} points, {it.get("descendants", 0)} comments on Hacker News',
                }
            )
    return out


def collect(hours):
    items, errors = [], []
    for s in CONFIG['sources']:
        try:
            items += fetch_hn() if s['type'] == 'hn' else fetch_rss(s['name'], s['url'])
        except Exception as e:
            errors.append(f'{s["name"]}: {type(e).__name__}')
    cutoff = time.time() - hours * 3600
    seen, out = set(), []
    for it in sorted(items, key=lambda x: -x['published']):
        key = re.sub(r'[^a-z0-9]', '', it['title'].lower())[:70]
        if it['published'] < cutoff or not it['url'] or key in seen:
            continue
        seen.add(key)
        it['id'] = hashlib.sha1(it['url'].encode()).hexdigest()[:12]
        out.append(it)
    return out, errors


# ------------------------------------------------------------------------------------------ AI enrichment
SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['items'],
    'properties': {
        'items': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['id', 'relevance', 'category', 'summary', 'why_it_matters'],
                'properties': {
                    'id': {'type': 'string'},
                    'relevance': {'type': 'integer', 'minimum': 0, 'maximum': 10},
                    'category': {'type': 'string', 'enum': CONFIG['categories']},
                    'summary': {'type': 'string'},
                    'why_it_matters': {'type': 'string'},
                },
            },
        }
    },
}

PROMPT = """You are a market-intelligence analyst for this company:
{profile}

For each news item below, return:
- relevance: 0-10, how much the company's leadership should care (10 = directly affects their market, customers, competitors or regulation; 0 = unrelated).
- category: one of {cats}
- summary: 2 short sentences in plain English, facts only, based on the title and excerpt (do not invent details).
- why_it_matters: one sentence on the implication for this company (empty string if relevance < 4).

Items:
{items}"""


def enrich(items, batch=25, _retry=True):
    from openai import OpenAI

    cl = OpenAI(api_key=os.environ['OPENAI_API_KEY'])
    model = os.getenv('OPENAI_CHAT_MODEL', CONFIG.get('model', 'gpt-4.1-mini'))
    out = {}
    for i in range(0, len(items), batch):
        chunk = items[i : i + batch]
        listing = '\n'.join(f'[{x["id"]}] {x["source"]} | {x["title"]} | {x["excerpt"][:350]}' for x in chunk)
        extra = {'reasoning_effort': 'low'} if model.startswith(('gpt-5', 'o')) else {}
        r = cl.chat.completions.create(
            model=model,
            **extra,
            messages=[
                {
                    'role': 'user',
                    'content': PROMPT.format(
                        profile=CONFIG['company_profile'], cats=', '.join(CONFIG['categories']), items=listing
                    ),
                }
            ],
            response_format={
                'type': 'json_schema',
                'json_schema': {'name': 'briefing', 'schema': SCHEMA, 'strict': True},
            },
        )
        for e in json.loads(r.choices[0].message.content)['items']:
            out[e['id']] = e
    missing = [x for x in items if x['id'] not in out]
    if missing and _retry:
        out.update(enrich(missing, batch=batch, _retry=False))  # one retry for anything the model skipped
    return out


# ------------------------------------------------------------------------------------------ store
def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("""create table if not exists items(id text primary key, source text, title text, url text, published int,
                 excerpt text, relevance int, category text, summary text, why text, run text)""")
    c.execute(
        'create table if not exists runs(id text primary key, started int, collected int, enriched int, errors text)'
    )
    return c


def save(items, ai, run_id, errors):
    c = db()
    for it in items:
        e = ai.get(it['id'], {})
        c.execute(
            'insert or replace into items values(?,?,?,?,?,?,?,?,?,?,?)',
            (
                it['id'],
                it['source'],
                it['title'],
                it['url'],
                it['published'],
                it['excerpt'],
                e.get('relevance'),
                e.get('category'),
                e.get('summary'),
                e.get('why_it_matters'),
                run_id,
            ),
        )
    c.execute(
        'insert or replace into runs values(?,?,?,?,?)',
        (run_id, int(time.time()), len(items), len(ai), json.dumps(errors)),
    )
    c.commit()
    c.close()


# ------------------------------------------------------------------------------------------ outputs
def top_items(run_id=None, min_rel=6, limit=12):
    c = db()
    c.row_factory = sqlite3.Row
    if run_id is None:
        run_id = c.execute('select id from runs order by started desc limit 1').fetchone()[0]
    rows = c.execute(
        'select * from items where run=? and relevance>=? order by relevance desc, published desc limit ?',
        (run_id, min_rel, limit),
    ).fetchall()
    c.close()
    return [dict(r) for r in rows], run_id


def render_email(items, run_id):
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(os.path.join(ROOT, 'templates')), autoescape=True)
    return env.get_template('email.html').render(
        items=items,
        company=CONFIG['company_name'],
        date=time.strftime('%A, %d %B %Y'),
        run=run_id,
        cats=CONFIG['categories'],
    )


def slack_blocks(items):
    blocks = [
        {
            'type': 'header',
            'text': {
                'type': 'plain_text',
                'text': f'{CONFIG["company_name"]} daily briefing · {time.strftime("%d %b")}',
            },
        }
    ]
    for it in items[:8]:
        blocks.append(
            {
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': f'*<{it["url"]}|{it["title"]}>*\n{it["summary"]}\n_{it["why"] or ""}_  ·  `{it["category"]}` · {it["source"]} · relevance {it["relevance"]}/10',
                },
            }
        )
    return {'blocks': blocks}


def deliver(email_html, blocks):
    hook = os.getenv('SLACK_WEBHOOK_URL')
    if hook:
        urllib.request.urlopen(
            urllib.request.Request(
                hook, data=json.dumps(blocks).encode(), headers={'Content-Type': 'application/json'}
            ),
            timeout=15,
        )
    if os.getenv('SMTP_HOST') and os.getenv('DIGEST_TO'):
        import smtplib
        from email.mime.text import MIMEText

        m = MIMEText(email_html, 'html')
        m['Subject'] = f'{CONFIG["company_name"]} briefing · {time.strftime("%d %b %Y")}'
        m['From'] = os.getenv('SMTP_FROM', os.getenv('SMTP_USER', ''))
        m['To'] = os.getenv('DIGEST_TO')
        with smtplib.SMTP_SSL(os.getenv('SMTP_HOST'), int(os.getenv('SMTP_PORT', '465'))) as s:
            s.login(os.getenv('SMTP_USER'), os.getenv('SMTP_PASSWORD'))
            s.send_message(m)


def run(hours=None):
    hours = hours or CONFIG.get('window_hours', 36)
    run_id = time.strftime('%Y%m%d-%H%M')
    items, errors = collect(hours)
    print(f'collected {len(items)} items ({", ".join(errors) or "all sources ok"})')
    ai = enrich(items) if os.getenv('OPENAI_API_KEY') else {}
    ids = {x['id'] for x in items}
    ai = {k: v for k, v in ai.items() if k in ids}  # ignore any id the model invented
    save(items, ai, run_id, errors)
    top, _ = top_items(run_id)
    email = render_email(top, run_id)
    open(os.path.join(ROOT, 'out', 'latest_email.html'), 'w').write(email)
    json.dump(slack_blocks(top), open(os.path.join(ROOT, 'out', 'latest_slack.json'), 'w'), indent=1)
    deliver(email, slack_blocks(top))
    print(f'enriched {len(ai)}, {len(top)} items made the briefing')
    return run_id


if __name__ == '__main__':
    run()
