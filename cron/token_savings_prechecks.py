"""Deterministic wake gates for the existing daily GitHub scan and Vault job.
The baseline advances only after successful agent work, so quota skips do not eat events.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import urllib.request
import urllib.parse
import urllib.error
import subprocess
import xml.etree.ElementTree as ET
import sqlite3
import tempfile
from datetime import datetime

def _settings(home):
    """Per-profile private job IDs/paths never belong in published source."""
    try:
        data = json.loads((Path(home) / "cron/prechecks/config.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _state(home, kind):
    directory=Path(home)/'cron/prechecks';directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    return directory/f'{kind}.json'


def _write(path, data):
    with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,delete=False) as stream:
        json.dump(data,stream,ensure_ascii=False,indent=2)
        stream.write('\n')
        tmp=Path(stream.name)
    tmp.replace(path)


def _read(path):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return {}


def _fetch(url):
    req=urllib.request.Request(url,headers={'User-Agent':'Hermes-local-change-check/1.0','Accept':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=12) as response:
            return response.read(2_000_000)
    except urllib.error.HTTPError as exc:
        parsed=urllib.parse.urlsplit(url)
        if parsed.hostname!='api.github.com' or exc.code not in (403,429):raise
        # Reuse gh's existing authenticated, read-only API route. Never print credentials.
        endpoint=parsed.path.lstrip('/')+('?' + parsed.query if parsed.query else '')
        result=subprocess.run(['gh','api','--hostname','github.com',endpoint],
                              capture_output=True,timeout=20,check=False)
        if result.returncode:raise RuntimeError('GitHub API and gh API unavailable') from exc
        return result.stdout


def _feed_items(raw):
    root=ET.fromstring(raw)
    entries=root.findall('{http://www.w3.org/2005/Atom}entry') or root.findall('./channel/item')
    # Feed build timestamps change without new posts. Hash only entry content.
    return sorted([[(child.tag,child.text or '',sorted(child.attrib.items())) for child in entry]
                   for entry in entries],key=lambda item:json.dumps(item,sort_keys=True))


def _github_sources(fetch):
    sources,errors={},{}
    def collect(key,url,parse):
        try:
            value=parse(fetch(url))
            sources[key]=hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
        except Exception as exc:
            # Local diagnostics only; don't include arbitrary remote response bodies.
            errors[key]=f'{type(exc).__name__}: HTTP {exc.code}' if isinstance(exc,urllib.error.HTTPError) else type(exc).__name__
    queries=['AI agent framework','hermes agent','MCP server','agent security','small model agent','code analysis']
    for index,q in enumerate(queries):
        url='https://api.github.com/search/repositories?'+urllib.parse.urlencode({'q':q,'sort':'updated','per_page':15})
        collect(f'github-{index}',url,lambda raw:sorted((r['id'],r.get('pushed_at')) for r in json.loads(raw)['items']))
    collect('hackernews','https://hn.algolia.com/api/v1/search?query=AI+agent+autonomous&tags=story&hitsPerPage=30',
            lambda raw:sorted((r.get('objectID'),r.get('title'),r.get('url')) for r in json.loads(raw)['hits']))
    for subreddit in ('AI_Agents','LocalLLaMA','artificial','machinelearning'):
        collect('reddit-'+subreddit,f'https://www.reddit.com/r/{subreddit}/hot.json?limit=15&raw_json=1',
                lambda raw:sorted((r['data'].get('id'),r['data'].get('title'),r['data'].get('url'),r['data'].get('selftext',''))
                                  for r in json.loads(raw)['data']['children']))
    collect('arxiv','https://export.arxiv.org/api/query?search_query=all:agent+AND+all:language&sortBy=lastUpdatedDate&sortOrder=descending&max_results=15',_feed_items)
    collect('huggingface','https://huggingface.co/blog/feed.xml',_feed_items)
    return sources,errors


def observe(home, kind, *, vault=None, fetch=_fetch, now=None):
    now=now or datetime.now().astimezone();base=_state(home,kind)
    prior=_read(base)
    try:
        weekly=''
        source_errors={}
        sources={}
        if kind=='vault':
            vault=Path(vault or _settings(home).get('vault') or (Path(home) / 'vault'))
            if not vault.is_dir() or not (vault/'daily/今日状态.md').is_file():
                raise ValueError(f'Vault root/input missing: {vault}; do not search unrelated backups')
            values=[]
            for rel in ('daily/今日状态.md','daily/目标追踪.md'):
                p=vault/rel;values.append((rel,hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else 'absent'))
            digest=hashlib.sha256(json.dumps(values).encode()).hexdigest()
            if now.weekday()==6:weekly=now.strftime('%G-W%V')
        elif kind=='github':
            fresh,source_errors=_github_sources(fetch)
            if not fresh:raise ValueError('All source prechecks unavailable')
            sources={**prior.get('sources',{}),**fresh}
            digest=hashlib.sha256(json.dumps(sources,sort_keys=True).encode()).hexdigest()
        elif kind=='reflection':
            # Session metadata only: no prompt bodies, no external memory writes.
            db=Path(home)/'state.db'
            with sqlite3.connect(f'file:{db}?mode=ro',uri=True,timeout=3) as conn:
                values=conn.execute("""SELECT m.id,m.timestamp FROM messages m JOIN sessions s ON s.id=m.session_id
                    WHERE m.role='user' AND m.timestamp>? AND coalesce(s.source,'') NOT IN ('cron','subagent')
                    AND s.id NOT LIKE 'cron_%' ORDER BY m.id""", (now.timestamp()-4*3600,)).fetchall()
            if not values:
                result={'wakeAgent':False,'reason':'no user activity in the original four-hour review window',
                        'kind':kind,'checkedAt':now.isoformat()}
                _write(base.with_name(kind+'.last-check.json'),result)
                return result
            digest=hashlib.sha256(json.dumps(values).encode()).hexdigest()
        else:raise ValueError('Unknown precheck')
        changed=digest!=prior.get('digest') or bool(weekly and weekly!=prior.get('lastWeekly'))
        pending={'digest':digest,'checkedAt':now.isoformat(),'lastWeekly':weekly or prior.get('lastWeekly','')}
        if sources:pending['sources']=sources
        _write(base.with_name(kind+'.pending.json'),pending)
        result={'wakeAgent':changed,'reason':'changed or first successful baseline' if changed else 'unchanged',
                'kind':kind,'checkedAt':now.isoformat()}
        if source_errors:result['unavailableSources']=source_errors
    except Exception as exc:
        result={'wakeAgent':False,'reason':str(exc)[:350],'kind':kind,'checkedAt':now.isoformat(),'precheckError':True}
    _write(base.with_name(kind+'.last-check.json'),result)
    return result


def commit_success(home, job):
    kind=_settings(home).get('jobs', {}).get(job.get('id'))
    if not kind:return
    base=_state(home,kind);pending=base.with_name(kind+'.pending.json')
    if pending.is_file():pending.replace(base)
