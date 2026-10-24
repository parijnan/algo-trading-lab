"""
Build the Hestia Compounding Simulation page from the tables simulate.py wrote. The look is the Hestia Dashboard's (its stylesheet is read from hestia_performance/tracker_template.html).

    python hestia_simulation/build_page.py --out hestia_compounding.html
"""

import argparse
import json
import os
import re
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim_config as cfg  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TABLES = ['metrics', 'equity', 'monthly', 'yearly', 'engines', 'trades', 'skipped']
ENGINE_COLOURS = ('--e0:#c2453a;--e1:#b8861f;--e2:#2f7fb8;--e3:#1f7a5c', '--e0:#ef7468;--e1:#e0b14d;--e2:#6bb2e8;--e3:#3fbf94')   # the Integrated Backtest's four, light and dark

EXTRA_CSS = """
:root { %s; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { %s; } }
:root[data-theme="dark"] { %s; }
.notes { max-width: none; }
.stack { display: grid; gap: 16px; }
.neg { color: var(--neg); }
.mgrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%%, 19rem), 1fr)); gap: 0 20px; }
.mrow { display: flex; justify-content: space-between; gap: 12px; padding: 6px 0; border-bottom: 1px solid var(--grid); }
.mrow span:first-child { color: var(--ink2); }
.mrow span:last-child { font-variant-numeric: tabular-nums; text-align: right; }
.mh { font-size: 0.72rem; letter-spacing: 0.04em; text-transform: uppercase; color: var(--ink2); margin: 0 0 4px; }
.tile .v { font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.scroll { max-height: 34rem; overflow: auto; }
.scroll th { position: sticky; top: 0; background: var(--surface); z-index: 1; }
th button { font: inherit; color: inherit; background: none; border: 0; padding: 0; cursor: pointer; }
th button:hover { color: var(--ink); }
input[type=search] { font: inherit; font-size: 0.82rem; padding: 5px 8px; border-radius: 6px; border: 1px solid var(--border); background: var(--surface); color: var(--ink); }
.formulas td { white-space: normal; }
details summary { cursor: pointer; font-size: 0.95rem; font-weight: 600; }
.chart .bar.pos { fill: var(--pos); } .chart .bar.neg { fill: var(--neg); }
""" % (ENGINE_COLOURS[0], ENGINE_COLOURS[1], ENGINE_COLOURS[1])


def formula_rows(first, mult):
    rows = [(first, f'price × 10 ÷ 3 × {mult}'), ('Selene', 'price × 1 ÷ 8 × 4'), ('Helios', 'price × (4 × 1,380 ÷ 14,893) × 20 lots'), ('Typhon', 'price × 250 × (4 × 14,000 ÷ (299.7 × 250)) × 2 lots')]
    return ''.join(f'<tr><td>{a}</td><td>{b}</td></tr>' for a, b in rows)


def slip_rows(slip_table, slip, janus):
    out = ''
    for _, r in slip_table.iterrows():
        if r['engine'] == 'Prometheus' and janus:
            out += f'<tr><td>Janus</td><td class="n">none yet</td><td class="n">Rs {slip["Janus"]:.0f}</td><td class="n">assumed</td><td class="n">—</td></tr>'
        else:
            out += (f'<tr><td>{r["engine"]}</td><td class="n">{int(r["n"])}</td><td class="n">Rs {max(0, r["mean"]):.0f}</td><td class="n">± Rs {r["se"]:.0f}</td>'
                    f'<td class="n">Rs {r["mean"]:.0f}</td></tr>')
    return out


def build():
    css = re.search(r'<style>(.*?)</style>', open(cfg.TRACKER_TEMPLATE).read(), re.S).group(1)
    end = pd.Timestamp(json.load(open(os.path.join(cfg.OUT_DIR, 'meta.json')))['end_date'])
    tokens = {'{{END_LONG}}': f'{end.day} {end:%b %Y}', '{{END_FULL}}': f'{end.day} {end:%B %Y}'}
    body, js = open(os.path.join(HERE, 'page_body.html')).read(), open(os.path.join(HERE, 'page_script.js')).read()
    first_card = '<div class="two">\n    <div class="card">\n      <div class="cardhead"><h2>By year</h2></div>'
    assert body.count(first_card) == 1
    body = body.replace(first_card, first_card.replace('class="two"', 'class="stack"'))      # the year and engine tables sit full width, one above the other
    for k, v in tokens.items():
        body, js = body.replace(k, v), js.replace(k, v)
    slip_table = pd.read_csv(os.path.join(cfg.OUT_DIR, 'slip_table.csv'))
    slip = json.load(open(os.path.join(cfg.OUT_DIR, 'slip.json')))
    meta = {}
    for key, sc in cfg.SCENARIOS.items():
        mult = int(sc['janus_mult'])
        meta[key] = {'engines': ['Janus' if sc['swap'] else 'Prometheus', 'Selene', 'Helios', 'Typhon'], 'formulas': formula_rows('Janus' if sc['swap'] else 'Prometheus', mult),
                     'slip': slip_rows(slip_table, slip, sc['swap'])}
    data = f'<script type="application/json" id="d-meta">{json.dumps(meta)}</script>\n'
    for key in cfg.SCENARIOS:
        for t in TABLES:
            text = open(os.path.join(cfg.OUT_DIR, key, t + '.csv')).read()
            assert '</script' not in text
            data += f'<script type="text/plain" id="d-{key}-{t}">{text}</script>\n'
    return ('<title>Hestia Compounding Simulation</title>\n<style>' + css + EXTRA_CSS + '</style>\n' + body + data
            + '<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>\n<script>\n' + js + '</script>\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    html = build()
    open(a.out, 'w').write(html)
    print(f'wrote {a.out} ({len(html):,} bytes)')


if __name__ == '__main__':
    main()
