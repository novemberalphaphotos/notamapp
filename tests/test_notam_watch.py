import copy
import datetime as dt
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import notamlib as L  # noqa: E402
import update as U  # noqa: E402

FX = os.path.join(ROOT, 'tests', 'fixtures')
CFG = json.load(open(os.path.join(ROOT, 'config', 'rules.json')))
NOW = dt.datetime(2026, 11, 12, 0, 0, tzinfo=L.UTC)
ENV = {'NTFY_TOPIC_FIREWORKS': 'fw-secret', 'NTFY_TOPIC_MILITARY': 'mil-secret', 'NTFY_TOPIC_HEALTH': 'hl-secret'}


def raw(name):
    return open(os.path.join(FX, name), encoding='utf-8').read()


class Recorder:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, topic, title, body, **kw):
        self.calls.append((topic, title, body))
        return self.ok


class ParserTests(unittest.TestCase):
    def test_parse_fields(self):
        recs = {r['id']: r for r in L.parse_briefing(raw('briefing_1.txt'))}
        self.assertEqual(len(recs), 6)
        a = recs['A0102/26']
        self.assertEqual((a['fir'], a['qcode'], a['start'], a['end']), ('YMMM', 'QWELW', '2026-11-15T00:00Z', '2026-11-20T08:00Z'))
        self.assertEqual(a['schedule'], 'DAILY 0000-0800')
        self.assertTrue(a['text'].startswith('MIL EXERCISE'))
        self.assertEqual(a['lower'], 'SFC')
        self.assertEqual(recs['A0101/26']['text'].count('FIREWORKS'), 2)

    def test_perm_and_est(self):
        recs = {r['id']: r for r in L.parse_briefing(raw('briefing_1.txt'))}
        self.assertTrue(recs['A0105/26']['perm'])
        self.assertIsNone(recs['A0105/26']['end'])
        self.assertTrue(recs['A0106/26']['estimated'])
        self.assertEqual(recs['A0106/26']['end'], '2026-11-10T00:00Z')

    def test_replace_cancel_refs(self):
        recs = {r['id']: r for r in L.parse_briefing(raw('briefing_2.txt'))}
        self.assertEqual((recs['A0110/26']['kind'], recs['A0110/26']['ref']), ('R', 'A0102/26'))
        self.assertEqual((recs['A0109/26']['kind'], recs['A0109/26']['ref']), ('C', 'A0104/26'))

    def test_classify(self):
        rules = L.compile_rules(CFG)
        t = {r['id']: L.classify(r, rules) for r in L.parse_briefing(raw('briefing_1.txt') + raw('briefing_2.txt'))}
        self.assertEqual(t['A0101/26'], 'fireworks')
        self.assertEqual(t['A0102/26'], 'military')
        self.assertEqual(t['A0104/26'], 'fireworks')
        self.assertEqual(t['A0106/26'], 'military')
        self.assertEqual(t['A0108/26'], 'military')

    def test_categories_from_qcode(self):
        cats = CFG['categories']
        mk = lambda q: {'qcode': q}
        self.assertEqual(L.categorize(mk('QMRLC'), cats), 'aerodrome')
        self.assertEqual(L.categorize(mk('QNVAS'), cats), 'navaids')
        self.assertEqual(L.categorize(mk('QRTCA'), cats), 'airspace')
        self.assertEqual(L.categorize(mk('QOBCE'), cats), 'obstacles')
        self.assertEqual(L.categorize(mk('QPICH'), cats), 'procedures')
        self.assertEqual(L.categorize(mk('QWULW'), cats), 'warnings')
        self.assertEqual(L.categorize(mk('QXXXX'), cats), 'other')
        self.assertEqual(L.categorize({'qcode': None}, cats), 'other')

    def test_keyword_needs_whole_word(self):
        rules = L.compile_rules(CFG)
        rec = {'text': 'RUNWAY LIGHTING ARMYWORM TERMINAL', 'schedule': None, 'qcode': 'QMRLC'}
        self.assertEqual(L.classify(rec, rules), 'other')

    def test_garbage_yields_nothing(self):
        self.assertEqual(L.parse_briefing('hello world\nno notams here'), [])

    def test_state_logic(self):
        mk = lambda **k: {'start': '2026-11-10T00:00Z', 'end': '2026-11-14T00:00Z', **k}
        self.assertEqual(L.compute_state(mk(), NOW), 'active')
        self.assertEqual(L.compute_state(mk(start='2026-11-13T00:00Z'), NOW), 'upcoming')
        self.assertEqual(L.compute_state(mk(end='2026-11-11T00:00Z'), NOW), 'expired')
        self.assertEqual(L.compute_state(mk(replaced_by='X'), NOW), 'replaced')
        self.assertEqual(L.compute_state(mk(cancelled_by='X', replaced_by='Y'), NOW), 'cancelled')
        self.assertEqual(L.compute_state(mk(end=None), NOW), 'active')


class LifecycleTests(unittest.TestCase):
    def test_first_run_is_silent_then_once_only(self):
        st = U.empty_state()
        rec = Recorder()
        st, sent = U.run_update(raw('briefing_1.txt'), st, CFG, NOW, send=rec, env=ENV)
        self.assertEqual(rec.calls, [])
        self.assertTrue(st['seeded'])
        self.assertEqual(len(st['notams']), 6)

        # same data again: still nothing
        st, _ = U.run_update(raw('briefing_1.txt'), st, CFG, NOW, send=rec, env=ENV)
        self.assertEqual(rec.calls, [])

        # second briefing: new fireworks (A0107) and new army (A0108); the R and C are handled
        later = NOW + dt.timedelta(hours=1)
        st, sent = U.run_update(raw('briefing_2.txt'), st, CFG, later, send=rec, env=ENV)
        topics = sorted((c[0], c[1].split(' ')[1]) for c in rec.calls)
        ids = sorted(i for _, i in sent)
        self.assertIn('A0107/26', ids)
        self.assertIn('A0108/26', ids)
        self.assertIn('A0110/26', ids)          # a replacement NOTAM is "new" and matches military
        self.assertEqual(st['notams']['A0102/26']['replaced_by'], 'A0110/26')
        self.assertEqual(L.compute_state(st['notams']['A0102/26'], NOW + dt.timedelta(hours=1)), 'replaced')
        self.assertNotIn('A0109/26', ids)       # cancellations do not alert
        self.assertNotIn('A0101/26', ids)       # already seeded
        by_topic = {c[0] for c in rec.calls}
        self.assertEqual(by_topic, {'fw-secret', 'mil-secret'})   # separate topics per rule

        # the cancel marks the old NOTAM
        self.assertEqual(st['notams']['A0104/26']['cancelled_by'], 'A0109/26')
        self.assertEqual(L.compute_state(st['notams']['A0104/26'], later), 'cancelled')

        # run again: nothing more is sent
        n = len(rec.calls)
        st, _ = U.run_update(raw('briefing_2.txt'), st, CFG, later + dt.timedelta(hours=1), send=rec, env=ENV)
        self.assertEqual(len(rec.calls), n)

    def test_cancelled_type_inherited(self):
        st = U.empty_state()
        st, _ = U.run_update(raw('briefing_1.txt'), st, CFG, NOW, send=Recorder(), env=ENV)
        st, _ = U.run_update(raw('briefing_2.txt'), st, CFG, NOW, send=Recorder(), env=ENV)
        self.assertEqual(st['notams']['A0109/26']['type'], 'fireworks')

    def test_failed_send_is_retried_not_lost(self):
        st = U.empty_state()
        st, _ = U.run_update(raw('briefing_1.txt'), st, CFG, NOW, send=Recorder(), env=ENV)
        bad = Recorder(ok=False)
        st, sent = U.run_update(raw('briefing_2.txt'), st, CFG, NOW, send=bad, env=ENV)
        self.assertEqual(sent, [])
        self.assertTrue(bad.calls)
        good = Recorder()
        st, sent = U.run_update(raw('briefing_2.txt'), st, CFG, NOW, send=good, env=ENV)
        self.assertTrue(sent)                    # retried on the next run
        n = len(good.calls)
        st, _ = U.run_update(raw('briefing_2.txt'), st, CFG, NOW, send=good, env=ENV)
        self.assertEqual(len(good.calls), n)

    def test_already_expired_new_notam_does_not_alert(self):
        st = U.empty_state()
        st, _ = U.run_update(raw('briefing_1.txt'), st, CFG, NOW, send=Recorder(), env=ENV)
        text = raw('briefing_2.txt').replace('C) 2611290100', 'C) 2611010100').replace('B) 2611282000', 'B) 2610310000')
        rec = Recorder()
        U.run_update(text, st, CFG, NOW, send=rec, env=ENV)
        self.assertNotIn('A0107/26', ' '.join(c[1] for c in rec.calls))

    def test_empty_briefing_never_wipes_or_seeds(self):
        st = U.empty_state()
        with self.assertRaises(ValueError):
            U.run_update('Service unavailable', st, CFG, NOW, send=Recorder(), env=ENV)
        self.assertFalse(st['seeded'])
        st, _ = U.run_update(raw('briefing_1.txt'), st, CFG, NOW, send=Recorder(), env=ENV)
        before = copy.deepcopy(st['notams'])
        with self.assertRaises(ValueError):
            U.run_update('', st, CFG, NOW, send=Recorder(), env=ENV)
        self.assertEqual(st['notams'], before)

    def test_missing_topic_secret_does_not_crash_or_flood_later(self):
        st = U.empty_state()
        st, _ = U.run_update(raw('briefing_1.txt'), st, CFG, NOW, send=Recorder(), env={})
        rec = Recorder()
        st, sent = U.run_update(raw('briefing_2.txt'), st, CFG, NOW, send=rec, env={})
        self.assertEqual(rec.calls, [])
        st, sent = U.run_update(raw('briefing_2.txt'), st, CFG, NOW, send=rec, env=ENV)
        self.assertEqual(rec.calls, [])

    def test_alert_cap(self):
        cfg = copy.deepcopy(CFG)
        cfg['max_alerts_per_rule_per_run'] = 1
        st = U.empty_state()
        st, _ = U.run_update(raw('briefing_1.txt'), st, cfg, NOW, send=Recorder(), env=ENV)
        extra = raw('briefing_2.txt') + '\nA0120/26 NOTAMN\nQ) YMMM/QWFLW/IV/BO/W/000/030/3800S14500E005\nA) YMML B) 2611282000 C) 2611290100\nE) FIREWORKS TEST TWO.\n' \
                                       '\nA0121/26 NOTAMN\nQ) YMMM/QWFLW/IV/BO/W/000/030/3800S14500E005\nA) YMML B) 2611282000 C) 2611290100\nE) FIREWORKS TEST THREE.\n'
        rec = Recorder()
        U.run_update(extra, st, cfg, NOW, send=rec, env=ENV)
        fw = [c for c in rec.calls if c[0] == 'fw-secret']
        self.assertEqual(len(fw), 2)             # 1 individual + 1 "more" summary

    def test_health_alert_transitions_only(self):
        st = U.empty_state()
        rec = Recorder()
        for _ in range(2):
            U.health_step(st, False, NOW, send=rec, env=ENV)
        self.assertEqual(rec.calls, [])
        for _ in range(4):
            U.health_step(st, False, NOW, send=rec, env=ENV)
        self.assertEqual(len(rec.calls), 1)       # once, not every run
        U.health_step(st, True, NOW, send=rec, env=ENV)
        self.assertEqual(len(rec.calls), 2)       # recovery
        U.health_step(st, True, NOW, send=rec, env=ENV)
        self.assertEqual(len(rec.calls), 2)

    def test_public_output_has_no_alert_bookkeeping(self):
        st = U.empty_state()
        st, _ = U.run_update(raw('briefing_1.txt'), st, CFG, NOW, send=Recorder(), env=ENV)
        pub, _ = U.build_public(st, NOW, CFG)
        self.assertEqual(pub['count'], 6)
        self.assertNotIn('alerted', pub['notams'][0])
        blob = json.dumps(pub)
        for secret in ENV.values():
            self.assertNotIn(secret, blob)

    def test_categories_do_not_alert_and_types_are_in_output(self):
        st = U.empty_state()
        st, _ = U.run_update(raw('briefing_1.txt'), st, CFG, NOW, send=Recorder(), env=ENV)
        self.assertEqual(st['notams']['A0103/26']['type'], 'aerodrome')
        self.assertEqual(st['notams']['A0105/26']['type'], 'navaids')
        rec = Recorder()
        extra = raw('briefing_2.txt') + '\nA0130/26 NOTAMN\nQ) YMMM/QMRLC/IV/NBO/A/000/999/3400S15100E005\nA) YSSY B) 2611122000 C) 2611300000\nE) RWY 16R CLOSED.\n'
        st, sent = U.run_update(extra, st, CFG, NOW, send=rec, env=ENV)
        self.assertNotIn('A0130/26', [i for _, i in sent])
        pub, _ = U.build_public(st, NOW, CFG)
        ids = [t[0] for t in pub['types']]
        self.assertEqual(ids[:2], ['fireworks', 'military'])
        self.assertIn('aerodrome', ids)
        self.assertEqual(ids[-1], 'other')

    def test_keep_forever_and_archive_split(self):
        st = U.empty_state()
        st, _ = U.run_update(raw('briefing_1.txt'), st, CFG, NOW, send=Recorder(), env=ENV)
        later = NOW + dt.timedelta(days=400)        # years later: nothing is deleted
        st, _ = U.run_update(raw('briefing_2.txt'), st, CFG, later, send=Recorder(), env=ENV)
        self.assertIn('A0103/26', st['notams'])
        self.assertIn('A0106/26', st['notams'])
        pub, arch = U.build_public(st, later, CFG)
        self.assertEqual(pub['archive_count'], sum(a['count'] for a in arch.values()))
        self.assertIn('A0103/26', [r['id'] for a in arch.values() for r in a['notams']])  # ended long ago
        self.assertNotIn('A0103/26', [r['id'] for r in pub['notams']])
        self.assertIn('A0105/26', [r['id'] for r in pub['notams']])   # PERM stays in the main file
        self.assertEqual(sorted(arch), pub['archive_years'])
        self.assertEqual(pub['count'] + pub['archive_count'], len(st['notams']))

    def test_keep_days_prunes_when_configured(self):
        cfg = copy.deepcopy(CFG); cfg['keep_days_after_end'] = 30
        st = U.empty_state()
        st, _ = U.run_update(raw('briefing_1.txt'), st, cfg, NOW, send=Recorder(), env=ENV)
        st, _ = U.run_update(raw('briefing_2.txt'), st, cfg, NOW + dt.timedelta(days=400), send=Recorder(), env=ENV)
        self.assertNotIn('A0103/26', st['notams'])

    def test_ntfy_title_is_ascii(self):
        self.assertEqual(U.ascii_only('Fireworks – test'), 'Fireworks ? test')


class FetchCollectTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, 'scripts'))
        import fetch_source as F
        self.F = F
        self.cfg = {'loc': ['YMMM', 'YBBB'], 'aerodromes': ['YSSY', 'YMML', 'YZZZ', 'YBBN', 'YPPH', 'YPAD'],
                    'chunk_size': 3, 'pause_seconds': 0, 'max_rejected_codes': 5}

    def test_bad_code_is_isolated_and_rest_kept(self):
        calls = []

        def fake(loc):
            calls.append(list(loc))
            if 'YZZZ' in loc:
                raise RuntimeError('unknown location')
            return 'TEXT ' + ','.join(loc)
        text, stats = self.F.collect(fake, self.cfg, sleep=lambda s: None)
        self.assertEqual(stats['rejected'], ['YZZZ'])
        self.assertEqual(stats['aerodromes_ok'], 5)
        for code in ('YMMM', 'YSSY', 'YMML', 'YBBN', 'YPPH', 'YPAD'):
            self.assertIn(code, text)
        self.assertFalse(stats['aborted'])

    def test_fir_failure_is_fatal(self):
        def fake(loc):
            raise RuntimeError('login failed')
        with self.assertRaises(RuntimeError):
            self.F.collect(fake, self.cfg, sleep=lambda s: None)

    def test_gives_up_when_everything_fails(self):
        n = [0]

        def fake(loc):
            n[0] += 1
            if n[0] == 1:
                return 'FIR TEXT'
            raise RuntimeError('service down')
        cfg = dict(self.cfg, aerodromes=['Y%03d' % i for i in range(60)], max_rejected_codes=4)
        text, stats = self.F.collect(fake, cfg, sleep=lambda s: None)
        self.assertTrue(stats['aborted'])
        self.assertLess(n[0], 60)             # did not hammer the service for every code

    def test_other_australian_fir_code_kept(self):
        st = U.empty_state()
        raw_text = raw('briefing_1.txt') + '\nA0140/26 NOTAMN\nQ) YBBB/QMRLC/IV/NBO/A/000/999/1200S13000E005\nA) YPDN B) 2611122000 C) 2611300000\nE) TWY A CLOSED.\n' + \
                   '\nA0141/26 NOTAMN\nQ) NZZC/QMRLC/IV/NBO/A/000/999/3600S17400E005\nA) NZAA B) 2611122000 C) 2611300000\nE) NOT AUSTRALIAN.\n'
        st, _ = U.run_update(raw_text, st, CFG, NOW, send=Recorder(), env=ENV)
        self.assertIn('A0140/26', st['notams'])
        self.assertNotIn('A0141/26', st['notams'])


if __name__ == '__main__':
    unittest.main()
