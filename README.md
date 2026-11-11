# NOTAM Watch

Watches NOTAMs for the **YMMM** (Melbourne) and **YBBB** (Brisbane) FIRs from Airservices Australia NAIPS,
publishes a filterable web page, and sends ntfy alerts for new **fireworks** and **military** NOTAMs
(one topic per rule). Runs free on GitHub Actions + GitHub Pages every 30 minutes; no computer needed.

Unofficial. Not for flight planning or operational use.

## How it works
1. `scripts/fetch_source.py` logs in to the NAIPS briefing web service (SOAP) with your `NAIPS_USER` / `NAIPS_PASS` secrets and saves the briefing text.
2. `scripts/notamlib.py` splits it into NOTAMs, reads the Q/A/B/C/D/E/F/G fields, and classifies each as Fireworks, Military or Other using `config/rules.json`.
3. `scripts/update.py` merges them into `data/state.json`, works out replaced/cancelled links, writes `site/data/notams.json`, and sends ntfy alerts.
   * The first successful run is **silent** (everything already there is marked as alerted).
   * Each NOTAM alerts **once** per rule. A failed send is retried next run.
   * A fetch/parse failure never wipes data. After 3 failures in a row you get one alert on the health topic (if set), and one when it recovers.
4. The workflow commits the data back to the repo and deploys `site/` to GitHub Pages.

## Secrets (Settings -> Secrets and variables -> Actions -> New repository secret)
| Name | What |
|---|---|
| `NAIPS_USER` | NAIPS username |
| `NAIPS_PASS` | NAIPS password |
| `NTFY_TOPIC_FIREWORKS` | a long random topic name for fireworks alerts |
| `NTFY_TOPIC_MILITARY` | a different long random topic for military alerts |
| `NTFY_TOPIC_HEALTH` | optional, third topic for "can't fetch" warnings |

Topic names are effectively passwords on ntfy.sh: make them long and random, and do not reuse your bushfire topic.

## Changing the rules
Edit `config/rules.json` on GitHub (pencil icon). Keywords are matched as whole words, case-insensitive. Add a rule by copying a block and adding a matching `NTFY_TOPIC_...` secret and a line for it in the workflow's "Update data" step.

## If fetching fails
Run **Actions -> Probe NAIPS (diagnose only) -> Run workflow**. It prints the service's expected inputs and how many NOTAMs each location combination returns, without printing NOTAM text unless you tick the box. Fix `config/source.json` accordingly.

## Tests
`python3 -m unittest discover -s tests` (synthetic NOTAMs only).
