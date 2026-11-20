"""Dashboard for the briefing pipeline.  uvicorn briefing.server:app --port 8020"""

import json
import os
import sqlite3
import threading
import time

from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import HTMLResponse

from . import pipeline

ROOT = pipeline.ROOT
app = FastAPI(title='Briefing dashboard')


def rows(q, a=()):
    c = pipeline.db()
    c.row_factory = sqlite3.Row
    r = [dict(x) for x in c.execute(q, a).fetchall()]
    c.close()
    return r


@app.get('/', response_class=HTMLResponse)
def page():
    return open(os.path.join(ROOT, 'static', 'dashboard.html'), encoding='utf-8').read()


@app.get('/api/latest')
def latest():
    runs = rows('select * from runs order by started desc limit 14')
    if not runs:
        return {'runs': [], 'items': []}
    rid = runs[0]['id']
    items = rows('select * from items where run=? order by coalesce(relevance,-1) desc, published desc', (rid,))
    return {
        'runs': runs,
        'items': items,
        'company': pipeline.CONFIG['company_name'],
        'categories': pipeline.CONFIG['categories'],
        'sources': [s['name'] for s in pipeline.CONFIG['sources']],
    }


@app.get('/email', response_class=HTMLResponse)
def email():
    p = os.path.join(ROOT, 'out', 'latest_email.html')
    return open(p, encoding='utf-8').read() if os.path.exists(p) else '<p>No digest yet.</p>'


@app.get('/slack')
def slack():
    p = os.path.join(ROOT, 'out', 'latest_slack.json')
    return json.load(open(p)) if os.path.exists(p) else {}


_last_manual = {'t': 0.0}


@app.post('/api/run')
def run_now(bg: BackgroundTasks):
    if time.time() - _last_manual['t'] < 1800:  # public demo: one manual run per 30 minutes
        return {'started': False, 'reason': 'A fresh run happened recently.'}
    _last_manual['t'] = time.time()
    bg.add_task(pipeline.run)
    return {'started': True}


def _scheduler():
    """Run on start if the database is empty, then every REFRESH_HOURS (in production: cron or GitHub Actions)."""
    hours = float(os.getenv('REFRESH_HOURS', '12'))
    while True:
        try:
            if not rows('select id from runs limit 1') or hours:
                pipeline.run()
        except Exception as e:  # keep the dashboard alive if a source fails
            print('scheduled run failed:', e)
        time.sleep(hours * 3600)


@app.on_event('startup')
def start_scheduler():
    if os.getenv('AUTO_REFRESH', '1') == '1':
        threading.Thread(target=_scheduler, daemon=True).start()
