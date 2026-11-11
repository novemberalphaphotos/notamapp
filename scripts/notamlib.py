"""NOTAM parsing, classification and state helpers. Standard library only."""
import re
import datetime as dt

UTC = dt.timezone.utc

HEADER = re.compile(
    r'(?<![A-Z0-9])([A-Z]\d{4}/\d{2})\s+NOTAM([NRC])(?:\s+([A-Z]\d{4}/\d{2}))?')


def _field_start(text, letter, pos, line_start=False):
    pat = (r'(?m)^[ \t]*' if line_start else r'(?:(?<=\s)|^)') + letter + r'\)\s?'
    m = re.compile(pat).search(text, pos)
    return m


def parse_time(s):
    """'2611201000' (YYMMDDHHMM) or 12-digit YYYYMMDDHHMM -> aware UTC datetime."""
    s = re.sub(r'\D', '', s or '')
    try:
        if len(s) == 10:
            return dt.datetime.strptime(s, '%y%m%d%H%M').replace(tzinfo=UTC)
        if len(s) == 12:
            return dt.datetime.strptime(s, '%Y%m%d%H%M').replace(tzinfo=UTC)
    except ValueError:
        return None
    return None


def iso(d):
    return d.strftime('%Y-%m-%dT%H:%MZ') if d else None


def parse_iso(s):
    if not s:
        return None
    return dt.datetime.strptime(s, '%Y-%m-%dT%H:%MZ').replace(tzinfo=UTC)


def split_blocks(text):
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n')
    heads = list(HEADER.finditer(text))
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append((m, text[m.start():end].rstrip()))
    return out


def parse_block(m, block):
    """Parse one NOTAM block into a record dict (no classification yet)."""
    nid, kind, ref = m.group(1), m.group(2), m.group(3)
    body = block[m.end() - m.start():]
    # find fields in order
    pos = 0
    marks = {}
    qm = _field_start(body, 'Q', 0)
    if qm:
        marks['Q'] = qm
        pos = qm.end()
    for letter in 'ABC':
        fm = _field_start(body, letter, pos)
        if not fm:
            break
        marks[letter] = fm
        pos = fm.end()
    for letter in 'DEFG':
        fm = _field_start(body, letter, pos, line_start=True)
        if not fm and letter in 'EG':
            fm = _field_start(body, letter, pos)
        if fm:
            marks[letter] = fm
            pos = fm.end()
    ordered = sorted(marks.items(), key=lambda kv: kv[1].start())
    fields = {}
    for i, (letter, fm) in enumerate(ordered):
        end = ordered[i + 1][1].start() if i + 1 < len(ordered) else len(body)
        fields[letter] = body[fm.end():end].strip()

    fir = qcode = None
    q = fields.get('Q', '')
    if q:
        parts = [p.strip() for p in q.split('/')]
        if parts and re.fullmatch(r'Y[A-Z]{3}', parts[0]):
            fir = parts[0]
        if len(parts) > 1:
            qcode = parts[1]
    a = fields.get('A', '')
    if not fir:
        for token in re.findall(r'[A-Z]{4}', a):
            if token in ('YMMM', 'YBBB'):
                fir = token
                break

    b = parse_time(fields.get('B', '')[:14])
    craw = fields.get('C', '')
    perm = bool(re.match(r'\s*PERM', craw, re.I))
    c = None if perm else parse_time(re.sub(r'\s*EST\s*$', '', craw.strip())[:14])
    estimated = bool(re.search(r'EST\s*$', craw.strip()))

    return {
        'key': nid,
        'id': nid,
        'kind': kind,
        'ref': ref,
        'fir': fir,
        'qcode': qcode,
        'location': re.sub(r'\s+', ' ', a),
        'start': iso(b),
        'end': iso(c),
        'perm': perm,
        'estimated': estimated,
        'schedule': re.sub(r'\s+', ' ', fields.get('D', '')) or None,
        'text': fields.get('E', '').strip(),
        'lower': fields.get('F', '').strip() or None,
        'upper': fields.get('G', '').strip() or None,
        'raw': block.strip(),
    }


def parse_briefing(text, default_fir=None):
    """Return list of record dicts from a briefing text. Duplicates (same id) collapse."""
    seen = {}
    for m, block in split_blocks(text):
        rec = parse_block(m, block)
        if not rec['fir']:
            rec['fir'] = default_fir
        if rec['key'] not in seen:
            seen[rec['key']] = rec
    return list(seen.values())


def compile_rules(cfg):
    rules = []
    for r in cfg['rules']:
        kws = [k for k in r.get('keywords', [])]
        rx = re.compile(r'(?<![A-Z0-9])(?:' + '|'.join(kws) + r')(?![A-Z0-9])') if kws else None
        rules.append({**r, '_rx': rx})
    return rules


def classify(rec, rules):
    hay = ' '.join(filter(None, [rec.get('text'), rec.get('schedule')])).upper()
    subj = (rec.get('qcode') or '')[1:3]
    for r in rules:
        if r['_rx'] and r['_rx'].search(hay):
            return r['id']
        if subj and subj in r.get('q_subjects', []):
            return r['id']
    return 'other'


def compute_state(rec, now):
    if rec.get('cancelled_by'):
        return 'cancelled'
    if rec.get('replaced_by'):
        return 'replaced'
    start, end = parse_iso(rec.get('start')), parse_iso(rec.get('end'))
    if end and now > end:
        return 'expired'
    if start and now < start:
        return 'upcoming'
    return 'active'
