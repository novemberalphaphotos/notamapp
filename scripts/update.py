#!/usr/bin/env python3
"""Merge a fetched NAIPS briefing into data/state.json, publish site/data/notams.json,
and send once-only ntfy alerts (silent on the very first successful run).

Usage:
  update.py --raw out/briefing.txt            # normal run (fetch succeeded)
  update.py --fetch-failed                    # fetch step failed; keep old data, track health
Env: NTFY_TOPIC_<RULE> (one per rule), NTFY_TOPIC_HEALTH (optional),
     NTFY_DRY_RUN=1 (print instead of sending), NTFY_SERVER (default https://ntfy.sh)
"""
import argparse
import json
import os
import sys
import urllib.request
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notamlib as L  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS_BEFORE_HEALTH_ALERT = 3
RETRY_WINDOW_HOURS = 48   # un-alerted matches older than this are never alerted (e.g. after a rules edit)


def empty_state():
    return {
        'version': 1,
        'seeded': False,
        'notams': {},
        'superseded': {},      # old id -> {"by": id, "kind": "R"|"C"} (in case the old NOTAM shows up later)
        'health': {'fails': 0, 'alerted': False},
        'last_success': None,
        'last_run': None,
    }


def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=1, ensure_ascii=False, sort_keys=True)
        f.write('\n')
    os.replace(tmp, path)


# ---------- ntfy ----------
def ascii_only(s):
    return s.encode('ascii', 'replace').decode('ascii')


def send_ntfy(topic, title, body, priority='default', tags='', click=None):
    if os.environ.get('NTFY_DRY_RUN'):
        print(f'[dry-run ntfy] topic={"***" if topic else None} title={title!r}\n{body}\n')
        return True
    if not topic:
        return False
    server = os.environ.get('NTFY_SERVER', 'https://ntfy.sh').rstrip('/')
    headers = {'Title': ascii_only(title), 'Priority': priority}
    if tags:
        headers['Tags'] = tags
    if click:
        headers['Click'] = click
    req = urllib.request.Request(f'{server}/{topic}', data=body.encode('utf-8'),
                                 headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except Exception as e:  # never print the topic (it is a secret)
        print(f'ntfy send failed: {type(e).__name__}', file=sys.stderr)
        return False


def fmt_sydney(iso_s):
    d = L.parse_iso(iso_s)
    if not d:
        return '?'
    try:
        from zoneinfo import ZoneInfo
        d = d.astimezone(ZoneInfo('Australia/Sydney'))
        return d.strftime('%a %d %b %H:%M %Z')
    except Exception:
        return d.strftime('%a %d %b %H:%MZ')


def alert_text(rec, rule):
    kind = {'N': 'New', 'R': 'Replacement', 'C': 'Cancel'}.get(rec['kind'], 'New')
    title = f"{rule['label']} NOTAM {rec['id']} ({rec.get('fir') or '?'})"
    end = 'PERM' if rec.get('perm') else fmt_sydney(rec.get('end'))
    lines = [
        f"{kind}: {rec['id']}" + (f" replaces {rec['ref']}" if rec.get('ref') and rec['kind'] == 'R' else ''),
        f"From {fmt_sydney(rec.get('start'))}",
        f"To   {end}" + (' (est)' if rec.get('estimated') else ''),
    ]
    if rec.get('location'):
        lines.append(f"Loc  {rec['location']}")
    lines.append('')
    lines.append((rec.get('text') or rec['raw'])[:600])
    return title, '\n'.join(lines)


# ---------- core ----------
def run_update(raw_text, state, cfg, now, send=send_ntfy, env=None):
    """Pure-ish core. Returns (state, alerts_sent_summary). Raises ValueError if the briefing has no NOTAMs."""
    env = os.environ if env is None else env
    rules = L.compile_rules(cfg)
    rule_by_id = {r['id']: r for r in rules}
    allowed_firs = set(cfg.get('firs', []))
    prefixes = tuple(cfg.get('keep_fir_prefixes', []))

    parsed = L.parse_briefing(raw_text)
    if allowed_firs:
        parsed = [p for p in parsed if p['fir'] is None or p['fir'] in allowed_firs
                  or (prefixes and p['fir'].startswith(prefixes))]   # aerodrome NOTAMs may name another Australian FIR code
    if not parsed:
        raise ValueError('briefing contained no recognisable NOTAMs')

    notams = state['notams']
    superseded = state['superseded']
    first_run = not state['seeded']
    now_iso = L.iso(now)
    new_ids = []

    for p in parsed:
        old = notams.get(p['key'])
        if old is None:
            rec = dict(p)
            rec.update({'first_seen': now_iso, 'last_seen': now_iso, 'alerted': [],
                        'replaced_by': None, 'cancelled_by': None})
            notams[p['key']] = rec
            new_ids.append(p['key'])
        else:
            keep = {k: old.get(k) for k in ('first_seen', 'alerted', 'replaced_by', 'cancelled_by')}
            old.update(p)
            old.update(keep)
            old['last_seen'] = now_iso

    # supersession links (R replaces ref, C cancels ref)
    for p in parsed:
        if p['kind'] in ('R', 'C') and p.get('ref'):
            superseded[p['ref']] = {'by': p['key'], 'kind': p['kind']}
    for old_id, info in superseded.items():
        target = notams.get(old_id)
        if target:
            field = 'cancelled_by' if info['kind'] == 'C' else 'replaced_by'
            target[field] = info['by']

    # classification (R/C with no own keyword match inherit the type of what they refer to)
    for p in parsed:
        rec = notams[p['key']]
        t = L.classify(rec, rules)
        if t == 'other':
            t = L.categorize(rec, cfg.get('categories'))
        ref_rec = notams.get(rec.get('ref')) if rec['kind'] in ('R', 'C') else None
        if ref_rec and t not in rule_by_id and (t == 'other' or ref_rec.get('type') in rule_by_id):
            t = ref_rec.get('type', t)      # a cancel/replace of a fireworks/military NOTAM counts as one
        rec['type'] = t

    # alerts
    summary = []
    if first_run:
        for rec in notams.values():           # seed silently: mark everything as already alerted
            if rec['type'] in rule_by_id and rec['type'] not in rec['alerted']:
                rec['alerted'].append(rec['type'])
        state['seeded'] = True
    else:
        per_rule_sent = {}
        cap = int(cfg.get('max_alerts_per_rule_per_run', 10))
        kinds = set(cfg.get('alert_kinds', ['N', 'R']))
        window = dt.timedelta(hours=RETRY_WINDOW_HOURS)
        candidates = [k for k, r in notams.items()
                      if r['type'] in rule_by_id and r['type'] not in r['alerted']
                      and now - (L.parse_iso(r['first_seen']) or now) <= window]
        for key in candidates:
            rec = notams[key]
            rid = rec['type']
            if rid not in rule_by_id or rid in rec['alerted']:
                continue
            if rec['kind'] not in kinds:
                rec['alerted'].append(rid)
                continue
            if L.compute_state(rec, now) in ('expired', 'replaced', 'cancelled'):
                rec['alerted'].append(rid)    # already over: nothing to warn about
                continue
            rule = rule_by_id[rid]
            topic = env.get(rule['topic_secret'], '').strip()
            n = per_rule_sent.get(rid, 0)
            if n >= cap:
                rec['alerted'].append(rid)
                per_rule_sent[rid] = n + 1
                continue
            if not topic and not env.get('NTFY_DRY_RUN'):
                print(f"warning: secret {rule['topic_secret']} is empty; skipping {rec['id']}", file=sys.stderr)
                rec['alerted'].append(rid)
                continue
            title, body = alert_text(rec, rule)
            tags = 'fireworks' if rid == 'fireworks' else 'airplane' if rid == 'military' else 'bell'
            if send(topic, title, body, priority='default', tags=tags):
                rec['alerted'].append(rid)
                per_rule_sent[rid] = n + 1
                summary.append((rid, rec['id']))
            # on failure: not marked alerted, so the next run retries
        for rid, n in per_rule_sent.items():
            if n > cap:
                rule = rule_by_id[rid]
                send(env.get(rule['topic_secret'], '').strip(),
                     f"{rule['label']}: {n - cap} more NOTAMs",
                     f'{n - cap} further matching NOTAMs arrived in one run. Open the site to see them.',
                     tags='bell')

    # retention: keep everything unless keep_days_after_end is set in rules.json
    keep = cfg.get('keep_days_after_end')
    if keep is not None:
        for key in list(notams):
            rec = notams[key]
            end = L.parse_iso(rec.get('end'))
            seen = L.parse_iso(rec.get('last_seen')) or now
            if (end and (now - end).days > keep) or (now - seen).days > keep + 15:
                del notams[key]
        live_ids = set(notams)
        for old_id in list(superseded):
            if old_id not in live_ids and superseded[old_id]['by'] not in live_ids:
                del superseded[old_id]

    state['last_success'] = now_iso
    return state, summary


def health_step(state, ok, now, send=send_ntfy, env=None, detail=''):
    env = os.environ if env is None else env
    h = state.setdefault('health', {'fails': 0, 'alerted': False})
    topic = env.get('NTFY_TOPIC_HEALTH', '').strip()
    if ok:
        if h.get('alerted'):
            send(topic, 'NOTAM Watch recovered', 'NOTAM fetching is working again.', tags='white_check_mark')
        h['fails'] = 0
        h['alerted'] = False
    else:
        h['fails'] = h.get('fails', 0) + 1
        if h['fails'] >= FAILS_BEFORE_HEALTH_ALERT and not h.get('alerted'):
            send(topic, 'NOTAM Watch cannot fetch', f"{h['fails']} failed runs in a row. {detail}"[:300],
                 priority='high', tags='warning')
            h['alerted'] = True


def _public(rec):
    return {k: v for k, v in rec.items() if k != 'alerted'}


def _is_old(rec, now, days):
    st = L.compute_state(rec, now)
    if st not in ('expired', 'replaced', 'cancelled'):
        return False
    ref = L.parse_iso(rec.get('end')) or L.parse_iso(rec.get('last_seen')) or now
    return (now - ref).days > days


def build_public(state, now, cfg=None):
    """Returns (main_dict, {year: archive_dict}). Recent/active NOTAMs go in the main file;
    long-finished ones go into one archive file per year so the page stays quick."""
    cfg = cfg or {}
    days = int(cfg.get('archive_after_days', 30))
    recent, archives = [], {}
    for rec in state['notams'].values():
        if _is_old(rec, now, days):
            ref = L.parse_iso(rec.get('end')) or L.parse_iso(rec.get('last_seen')) or now
            archives.setdefault(str(ref.year), []).append(_public(rec))
        else:
            recent.append(_public(rec))
    recent.sort(key=lambda r: (r.get('start') or '', r['id']))
    for lst in archives.values():
        lst.sort(key=lambda r: (r.get('start') or '', r['id']))
    types = [[r['id'], r['label']] for r in cfg.get('rules', [])] + \
            [[c['id'], c['label']] for c in cfg.get('categories', [])] + [['other', 'Other']]
    main = {
        'generated_at': L.iso(now),
        'last_success': state.get('last_success'),
        'source_ok': state['health']['fails'] == 0,
        'seeded': state['seeded'],
        'count': len(recent),
        'types': types,
        'archive_years': sorted(archives),
        'archive_count': sum(len(v) for v in archives.values()),
        'notams': recent,
    }
    arch = {y: {'year': y, 'count': len(v), 'notams': v} for y, v in archives.items()}
    return main, arch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--raw')
    ap.add_argument('--fetch-failed', action='store_true')
    ap.add_argument('--detail', default='')
    ap.add_argument('--state', default=os.path.join(ROOT, 'data', 'state.json'))
    ap.add_argument('--out', default=os.path.join(ROOT, 'site', 'data', 'notams.json'))
    ap.add_argument('--rules', default=os.path.join(ROOT, 'config', 'rules.json'))
    args = ap.parse_args()

    cfg = load_json(args.rules, None)
    if cfg is None:
        print('rules.json missing or invalid', file=sys.stderr)
        return 2
    state = load_json(args.state, None) or empty_state()
    now = dt.datetime.now(L.UTC).replace(second=0, microsecond=0)
    state['last_run'] = L.iso(now)

    ok = False
    detail = args.detail
    if not args.fetch_failed and args.raw:
        try:
            with open(args.raw, encoding='utf-8', errors='replace') as f:
                raw = f.read()
            state, sent = run_update(raw, state, cfg, now)
            ok = True
            print(f"ok: {len(state['notams'])} NOTAMs, {len(sent)} alerts sent"
                  + ('' if state['seeded'] else ' (not yet seeded)'))
        except ValueError as e:
            detail = str(e)
            print(f'parse problem: {e}', file=sys.stderr)
    health_step(state, ok, now, detail=detail)
    save_json(args.state, state)
    main_pub, arch = build_public(state, now, cfg)
    save_json(args.out, main_pub)
    outdir = os.path.dirname(args.out)
    for year, obj in arch.items():
        save_json(os.path.join(outdir, f'archive-{year}.json'), obj)
    return 0


if __name__ == '__main__':
    sys.exit(main())
