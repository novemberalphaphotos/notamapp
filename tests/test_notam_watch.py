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
        self.assertEqual(t['A0103/26'], 'other')
        self.assertEqual(t['A0104/26'], 'fireworks')
        self.assertEqual(t['A0105/26'], 'other')
        self.assertEqual(t['A0106/26'], 'military')
        self.assertEqual(t['A0108/26'], 'military')

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
        pub = U.build_public(st, NOW)
        self.assertEqual(pub['count'], 6)
        self.assertNotIn('alerted', pub['notams'][0])
        blob = json.dumps(pub)
        for secret in ENV.values():
            self.assertNotIn(secret, blob)

    def test_ntfy_title_is_ascii(self):
        self.assertEqual(U.ascii_only('Fireworks – test'), 'Fireworks ? test')


if __name__ == '__main__':
    unittest.main()
