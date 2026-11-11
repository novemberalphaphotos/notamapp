#!/usr/bin/env python3
"""Send one clearly-labelled test message to each configured ntfy topic."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from update import send_ntfy  # noqa: E402

for var, label in (('NTFY_TOPIC_FIREWORKS', 'Fireworks'), ('NTFY_TOPIC_MILITARY', 'Military'), ('NTFY_TOPIC_HEALTH', 'Health')):
    topic = os.environ.get(var, '').strip()
    if not topic:
        print(f'{label}: secret {var} is empty, skipped')
        continue
    ok = send_ntfy(topic, f'TEST {label} topic', f'This is a test of the {label} topic. Nothing is wrong.', tags='white_check_mark')
    print(f'{label}: {"sent" if ok else "FAILED"}')
