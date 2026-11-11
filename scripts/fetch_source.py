#!/usr/bin/env python3
"""Fetch the current NOTAM briefing from the NAIPS SOAP briefing service.

  fetch_source.py --out out/briefing.txt            normal run
  fetch_source.py --probe                           diagnose: prints the service signature and
                                                    how many NOTAMs each location combo returns
                                                    (no NOTAM text unless PROBE_SAMPLE=1)
  fetch_source.py --from-file tests/fixtures/x.txt --out out/briefing.txt    offline/testing

Credentials come ONLY from the environment: NAIPS_USER, NAIPS_PASS. They are never printed.

NOTE: written from the published NAIPS WSDL description and public client examples; it has not
been run against the live service by its author. If NAIPS answers differently, --probe shows why.
"""
import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notamlib as L  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_cfg():
    with open(os.path.join(ROOT, 'config', 'source.json'), encoding='utf-8') as f:
        return json.load(f)


def creds():
    user = os.environ.get('NAIPS_USER', '').strip()
    pw = os.environ.get('NAIPS_PASS', '')
    if not user or not pw:
        raise SystemExit('NAIPS_USER / NAIPS_PASS are not set (add them as repository secrets).')
    return user.upper(), pw      # NAIPS expects the username in capitals


def make_client(cfg):
    try:
        import zeep
        from zeep.transports import Transport
    except ImportError:
        raise SystemExit('The "zeep" package is missing: pip install zeep')
    import requests
    s = requests.Session()
    s.headers['User-Agent'] = 'notam-watch/1.0'
    return zeep.Client(wsdl=cfg['wsdl'], transport=Transport(session=s, timeout=60, operation_timeout=90))


def call(client, cfg, loc):
    user, pw = creds()
    req = {
        'requestor': user,
        'password': pw,
        'source': cfg.get('source_label', 'notam-watch'),
        'loc': list(loc),
        'duration': cfg.get('duration', 'PT336H'),
        'flags': cfg.get('flags', {'met': False, 'ntm': True, 'hon': True, 'charts': False}),
    }
    op = client.service[cfg.get('operation', 'loc-brief')]
    resp = op(**req)
    return extract_text(resp)


def extract_text(resp):
    if resp is None:
        return ''
    if isinstance(resp, str):
        return resp
    for attr in ('content', 'Content'):
        v = getattr(resp, attr, None)
        if v is None and isinstance(resp, dict):
            v = resp.get(attr)
        if v:
            return v if isinstance(v, str) else str(v)
    return str(resp)


def scrub(s):
    """Remove credentials from any message before it reaches the logs."""
    for key in ('NAIPS_USER', 'NAIPS_PASS'):
        v = os.environ.get(key)
        if v:
            s = s.replace(v, '***').replace(v.upper(), '***')
    return s


def probe(cfg):
    creds()
    client = make_client(cfg)
    print('--- service signature (operation inputs) ---')
    for svc in client.wsdl.services.values():
        for port in svc.ports.values():
            for name, op in port.binding._operations.items():
                if name in ('loc-brief', 'area-brief'):
                    print(name, '->', op.input.signature())
    print('\n--- trying location combinations ---')
    for loc in cfg.get('probe_alternatives', [cfg['loc']]):
        try:
            text = call(client, cfg, loc)
            n = len(L.parse_briefing(text))
            print(f'loc={loc}: {len(text)} chars, {n} NOTAMs parsed')
            if os.environ.get('PROBE_SAMPLE') == '1':
                print('--- first 1500 chars ---')
                print(text[:1500])
                print('--- end sample ---')
        except Exception as e:
            print(f'loc={loc}: ERROR {type(e).__name__}: {scrub(str(e))[:300]}')
    print('\nDone. Nothing was saved.')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(ROOT, 'out', 'briefing.txt'))
    ap.add_argument('--probe', action='store_true')
    ap.add_argument('--from-file')
    args = ap.parse_args()
    cfg = load_cfg()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    if args.from_file:
        shutil.copyfile(args.from_file, args.out)
        return 0
    if args.probe:
        probe(cfg)
        return 0
    creds()
    try:
        client = make_client(cfg)
        text = call(client, cfg, cfg['loc'])
    except SystemExit:
        raise
    except Exception as e:
        print(f'fetch failed: {type(e).__name__}: {scrub(str(e))[:300]}', file=sys.stderr)
        return 1
    if not text.strip():
        print('fetch failed: empty response', file=sys.stderr)
        return 1
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(text)
    print(f'fetched {len(text)} chars')
    return 0


if __name__ == '__main__':
    sys.exit(main())
