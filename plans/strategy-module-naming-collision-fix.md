# Plan: Fix Strategy-Level Module Naming Collisions

**Status: DONE (2026-08-07).** All four strategies (Iris, Artemis, Athena,
Apollo) renamed. Discovered 2026-08-06 while adding a REST-fallback rate
limiter to Iris; confirmed pre-existing (reproduces on a clean checkout via
`git stash`), not introduced by that change. Approved 2026-08-07 to fix now,
starting with Iris. Nothing committed yet — see §Outcome at the bottom for the
final state and what to review.

**Iris — DONE (2026-08-07).** `iris_production/functions.py` → `iris_functions.py`,
`state.py` → `iris_state.py`, `configs.py` → `iris_configs.py`,
`logger_setup.py` → `iris_logger_setup.py` (all via `git mv`, history preserved).
Every internal importer within `iris_production/` updated to match (`iris.py`,
plus the renamed files' own cross-references — `iris_state.py`→`iris_configs`,
`iris_functions.py`→`iris_configs`+`iris_logger_setup`,
`iris_logger_setup.py`→`iris_configs`). Verified: Iris imports cleanly standalone,
and the exact cross-strategy collision simulation from §1 (Athena's modules
imported first, then Iris, matching real `leto.py` sys.path order) now succeeds
— previously crashed with `ImportError: cannot import name 'IrisState' from
'state'`. Full test suite re-run shows identical results to before (44 passed,
12 failed, 1 collection error) — Iris's rename didn't touch or regress anything
in the still-unfixed Apollo/Athena/Artemis collision chain, as expected.

**Root cause of the `test_strike_search.py` failures — confirmed, same bug
class.** Not a separate issue. `tests/test_strike_search.py:44-76` defends
itself against exactly this collision by manually injecting mock modules
(`sys.modules['configs'] = _m_conf`, plus mocks for `logger_setup` and
`functions`) before importing `credit_spread.py` from `artemis_production` —
the test's own comment (lines 86-90) explicitly describes this as a workaround
for cross-test contamination via the shared `configs` module name. But the
mock is itself vulnerable to the same bare-name collision it's working around:
depending on pytest's collection order, another test file's *real* import of a
same-named module (`configs`, `functions`, or `logger_setup` from Apollo,
Athena, or Artemis) can load before or interleave with this test's mock
injection, leaving `strike_iteration_interval` (or another mocked attribute)
resolved from the wrong module — surfacing as `range() arg 3 must not be zero`
when a step value comes through as unset/zero instead of the mocked 100. This
is the silent-wrong-value failure mode flagged as a risk in the original
scoping, now confirmed to actually occur, not just be theoretically possible.
The permanent fix is the same rename fix as everywhere else — once
`artemis_production`'s files are uniquely named, this test's mock only needs to
own a name nothing else can collide with.

**Artemis — DONE (2026-08-07).** `artemis_production/functions.py` →
`artemis_functions.py`, `configs.py` → `artemis_configs.py`,
`logger_setup.py` → `artemis_logger_setup.py` (via `git mv`). Artemis has no
`state.py` (uses CSVs directly, per existing convention). Updated importers:
`artemis.py`, `iron_condor.py`, `credit_spread.py`, plus each renamed file's
own cross-references. No external (outside `artemis_production/`) references
found. `leto.py`'s `_run_artemis()` needed no changes — it imports Artemis by
its own module name (`import artemis`), unaffected by the rename.

Renaming alone fixed the cross-strategy half of the problem (confirmed: Apollo,
previously failing collection with `ImportError: cannot import name
'_increment_rms_poll' from 'functions'`, now collects and passes cleanly —
64 passed / 0 errors, up from 44 passed / 1 error). But `test_strike_search.py`'s
12 failures **did not** go away from the rename alone — root-caused precisely:
it was never a cross-*strategy* collision at all. Both `test_artemis_strike_math.py`
and `test_strike_search.py` mock the same (now-unique) `artemis_configs` name to
test the same `credit_spread.py` singleton; pytest's alphabetical collection
runs `test_artemis_strike_math.py` first, whose mock zeroes
`strike_iteration_interval`/`strike_values_iterator` (irrelevant to *its* own
SL-math tests) and freezes those onto the cached `credit_spread` module at
import time. `test_strike_search.py` already had a partial workaround for this
exact class of contamination (a manual "rebind onto the cached module" block)
but it only covered `sl_*_dte`/`index_sl_offset` — whoever wrote it didn't
realize `strike_iteration_interval`/`strike_values_iterator` needed the same
treatment, since those two are the actual `range()` bounds/step the strike-search
tests exercise. Added the two missing rebind lines. Confirmed: plain
`python -m pytest tests/`, no flags, now passes **76/76, zero collection
errors, zero failures**.

Cross-strategy import order re-verified matching `leto.py`'s real sequence
(including its `os.chdir(ARTEMIS_DIR)` before importing Artemis, which the
first pass of this check missed and briefly looked like a new bug before
being traced to the test harness, not the rename): Athena → Artemis → Iris,
all three import cleanly in one process now.

**Athena — DONE (2026-08-07).** `athena_production/functions.py` →
`athena_functions.py`, `configs_live.py` → `athena_configs.py` (dropped
`_live` — kept the `strategy_configs.py` pattern consistent across all four,
per the instruction to keep naming consistent throughout, rather than
preserving Athena's old, already-non-unique `_live` suffix), `state.py` →
`athena_state.py`, `logger_setup.py` → `athena_logger_setup.py`. Updated
importers: `athena_engine.py`, plus each renamed file's own
cross-references.

Two things this pass caught that the earlier ones didn't need to worry about:

1. **A live, external dependency in `leto.py` itself** — `leto.py:403`
   (inside `_route()`'s Friday-routing branch) does
   `from configs_live import FORCE_ENTRY as ATHENA_FORCE`, reaching directly
   into Athena's config. This wasn't caught by the grep pattern used for
   Iris/Artemis's external-reference checks (`grep "iris_production.*functions"`
   etc.), because the line doesn't mention `athena_production` — it relies on
   a separate `sys.path.insert(0, ATHENA_DIR)` two lines above. **Worse: this
   import sits inside a bare `try/except Exception: pass`** — had this been
   missed, Athena's Friday `FORCE_ENTRY` override would have silently stopped
   working the moment the rename landed, with no error, no log line, nothing.
   Fixed: now `from athena_configs import FORCE_ENTRY as ATHENA_FORCE`.
   Re-swept the whole repo root and `research/` for any other same-shaped
   blind spot (bare import following a separate `sys.path.insert`, not
   caught by a directory-name grep) — none found. `research/mtm_equity/`'s
   own `from configs import ...` is a local, self-contained `configs.py` in
   that same folder, unrelated to any strategy directory — left alone.

2. **`tests/test_state_roundtrip.py`** loads `state.py`/`configs_live.py` via
   `importlib.util.spec_from_file_location` with hardcoded filenames — a
   *different*, already-collision-aware loading mechanism (its own docstring:
   "so Apollo and Athena modules never share sys.modules entries") that broke
   the moment Athena's files were renamed out from under it. Generalized
   `_load_state_module()` to take the configs filename, the bare name the
   state file imports it as, and the state filename as parameters, rather
   than hardcoding `'configs_live.py'`/`'state.py'`. Apollo's call site keeps
   the old names for now (unrenamed); Athena's call site uses the new ones.
   No further changes to this function needed when Apollo is renamed next —
   just update its call site's arguments.

`python -m pytest tests/` after both fixes: 76/76, same as after Artemis.
Cross-strategy simulation extended to **all four** strategies importing in
one process (Athena → Artemis → Iris → Apollo) — all succeed. Apollo hasn't
been renamed yet, but it no longer collides with anything either, since it's
now the only one left using generic names and nothing else claims them. The
live re-routing-loop risk this plan exists to close is now effectively
resolved for any session that mixes Apollo with a renamed strategy; Apollo's
own rename (next) is for consistency/completeness rather than closing a
remaining live risk.

**Apollo — DONE (2026-08-07). All four strategies complete.**
`apollo_production/functions.py` → `apollo_functions.py`,
`configs_live.py` → `apollo_configs.py`, `state.py` → `apollo_state.py`,
`logger_setup.py` → `apollo_logger_setup.py`. Updated importers: `apollo.py`,
`supertrend.py`, plus each renamed file's own cross-references.
`technical_indicators.py` and `supertrend.py` are unique filenames (checked —
`apollo_backtest/technical_indicators.py` also exists but backtests never run
in the same process as production strategies, no live collision risk there;
left alone).

Blind-spot sweep (the same class of check that caught `leto.py:403` during
Athena's pass) re-run against the whole repo root, `research/`, and
`slack_listener.py` — nothing else reaches into Apollo's old bare names.
`tests/test_strike_math.py` had its own direct (non-mocked) `from configs_live
import STRIKE_STEP, BUY_LEG_OFFSET, HEDGE_POINTS` — fixed. `test_state_roundtrip.py`'s
Apollo call site updated to the new filenames (the loader function itself
needed no further changes, per the generalization done during Athena's pass).

Final verification: `python -m pytest tests/` — **76/76, zero flags, zero
collection errors.** Cross-strategy simulation re-run with apollo-first import
order (previously untested combination) — all four import cleanly regardless
of order now: apollo → athena → artemis → iris.

## Outcome

All five colliding module names from §2 are now uniquely prefixed per
strategy. `leto.py`'s re-routing loop can import any combination of
apollo/athena/artemis/iris modules, in any order, within one process, without
one strategy's `sys.modules` entry silently shadowing another's. Two real bugs
were found and fixed as a direct result of doing this properly instead of
patching around it: `leto.py`'s silently-broken Athena `FORCE_ENTRY` check
(§Athena), and `test_strike_search.py`'s incomplete rebind list (§Artemis) —
neither would have surfaced without actually tracing the collision through to
its full live blast radius rather than stopping at "tests pass now."

**Regression test added (2026-08-07) — `tests/test_cross_strategy_imports.py`.**
Two independent checks: a static glob-based scan asserting no two
`*_production/` directories share a `.py` basename (fast, no import needed to
trigger), and a dynamic check that imports real strategy modules — never
mocks — in a fresh subprocess per case (isolated from the rest of the test
process, since these are the real production modules and running them inline
risks `sys.modules` interaction with `test_state_roundtrip.py`'s own temporary
aliasing). Three sequences covered: the exact historically-crashing one
(Athena → Iris), all four in the original problem order, and all four
reverse/apollo-first (previously the least-tested combination). Validated the
static check actually fails on a real regression, not just tautologically —
temporarily dropped a duplicate `_regression_check_temp.py` into two strategy
directories, confirmed the test fails with a clear message naming the exact
file and strategies, removed the temp files. `python -m pytest tests/`: 80/80
(76 + 4 new).

Item 7 from §5 (deploy to Delos as one coordinated change) is still open —
nothing has been pushed or committed anywhere. Everything in this plan is
local, uncommitted changes in the working tree.

**Today's live Iris session is not affected.** Verified precisely: with the Slack
manual-override routing to Iris, `leto.py`'s priority-1 checks
(`_apollo_trade_open()`, `_iris_trade_open()`, `_athena_trade_open()`,
`_artemis_trade_open()`) are all plain CSV reads — no strategy module gets
imported during them. The manual-override Iris branch
(`leto.py:384-386`) discards Iris's own handoff value and unconditionally
`return`s `should_reroute=False`, so `main()`'s re-routing `while True` loop
(`leto.py:624`) breaks immediately after Iris runs. No other strategy's modules
are ever imported in this session. This plan is about the next time routing is
back on auto/VIX mode, or any future session where a handoff-driven re-route
actually occurs.

---

## 1. The bug

Four production strategy directories each contain files with **identical bare
module names**. Every strategy does `sys.path.insert(0, its_own_dir)` then a bare
`from functions import ...` / `from state import ...` / `from configs import ...`.
Python caches imports in `sys.modules` by module name — the *first* strategy
whose file gets imported in a process wins that name; every other strategy's
same-named import silently resolves to the same cached module instead of loading
its own file.

`leto.py`'s `main()` is a **single long-running process for the whole session**
(`while True` at line 624), and its re-routing loop genuinely can import more
than one strategy's modules in that one process: `athena_engine.py:990-994`
hands control back to Leto (`return True, self._summary`) whenever Athena is
idle, it's a valid entry day/time, and VIX has drifted outside its 16-25 band at
that exact entry check — an ordinary occurrence, not a rare edge case. Leto then
re-evaluates VIX and can route to a *different* strategy in the same process.

**Confirmed empirically**, not just reasoned about — simulating the exact
sequence `leto.py` supports (Athena's modules imported first, then Iris's,
matching real `sys.path` insertion order) crashes immediately:

```
Athena imported. sys.modules["functions"] now points to: athena_production/functions.py
IRIS IMPORT FAILED: cannot import name 'IrisState' from 'state' (athena_production/state.py)
```

It fails on `state.py` before even reaching the `functions.py` collision — there
are multiple colliding names stacked on top of each other, not just one.

Separately, `python -m pytest tests/` is currently broken by the same mechanism:
alphabetical test-file collection imports Artemis's `functions.py` first, caches
it as `functions`, then `test_strike_math.py` (which imports Apollo) fails with
`ImportError: cannot import name '_increment_rms_poll' from 'functions'`. With
`--continue-on-collection-errors`, 12 tests in `test_strike_search.py` also fail
with `ValueError: range() arg 3 must not be zero` — a zero-step value, consistent
with (not yet confirmed as) a wrong config silently pulled from the wrong cached
module rather than a loud import crash. This is worth root-causing during the
fix, since it's the more dangerous failure mode: a collision that doesn't crash,
it just quietly uses the wrong strategy's data.

## 2. Full scope — every colliding name

| Bare module name | Colliding directories | Collision type |
|---|---|---|
| `functions.py` | apollo, athena, artemis, iris | 4-way |
| `state.py` | apollo, athena, iris | 3-way (artemis has none — uses CSVs directly) |
| `configs.py` | artemis, iris | 2-way |
| `configs_live.py` | apollo, athena | 2-way |
| `logger_setup.py` | apollo, athena, artemis, iris | 4-way |

`configs_live.py` (Apollo/Athena) was apparently already a deliberate rename
away from bare `configs.py`, per the convention documented in `CLAUDE.md`
("use `leto_config.py` not `configs_live.py`") — but that convention only
addressed root-vs-strategy collisions. It didn't help here: Apollo and Athena
both independently chose the *same* replacement name, so they still collide
with each other. `logger_setup.py` was missed entirely — it exists in all four
directories under the same name.

### Files that import these bare names (import-audit, not sampled)

| Directory | Files importing `functions`/`state`/`configs`(`_live`)/`logger_setup` |
|---|---|
| `apollo_production/` | `apollo.py`, `state.py`, `functions.py`, `logger_setup.py`, `supertrend.py` |
| `athena_production/` | `athena_engine.py`, `state.py`, `functions.py`, `logger_setup.py` |
| `artemis_production/` | `artemis.py`, `iron_condor.py`, `credit_spread.py`, `functions.py`, `logger_setup.py` |
| `iris_production/` | `iris.py`, `state.py`, `functions.py`, `logger_setup.py` |

Internal cross-dependencies confirmed (not assumed): within each directory,
`state.py` imports `STATE_FILE` from that directory's own `configs`/
`configs_live`, `functions.py` imports several constants from the same, and
`logger_setup.py` imports `LOG_DIR`/`LOG_LEVEL`/`LOGS_DIR` from the same. A fix
has to update all of these consistently within each directory, not just the
external call sites.

## 3. What we don't know yet

- Whether this has already fired in a live session (no access to Delos logs from
  here — local logs stop 2026-06-01, before Iris went live). The mechanism is
  proven reproducible; historical occurrence is not confirmed either way.
- Whether the 12 `test_strike_search.py` failures are actually caused by this
  collision or are an unrelated pre-existing bug. They're suspicious (a
  zero-step value is exactly what a wrong cached config would produce) but
  untraced. Root-cause this before or during the fix — if it *is* the collision,
  it's evidence of the silent-wrong-value failure mode, not just import crashes.

## 4. Fix options

**A. Rename each strategy's shared-name files to strategy-prefixed unique
names** (e.g. `iris_functions.py`, `iris_state.py`, `athena_functions.py`,
`athena_configs_live.py` → or fold into a single `athena_config.py`-style
rename, `apollo_functions.py`, `apollo_state.py`, `artemis_functions.py`,
`artemis_configs.py`, plus `*_logger_setup.py` for all four), and update every
importer within that directory to match.
- Matches the existing repo precedent (`leto_config.py` over `configs_live.py`
  at the root level) and the naming convention `CLAUDE.md` already documents.
- Mechanical, low-conceptual-risk: same file, same content, new name +
  updated imports. No behavior change if done correctly.
- Touches every file listed in §2's table — a real but bounded amount of
  find-and-replace across 4 directories, ~17 files.
- **Recommended.**

**B. Purge `sys.modules` in `leto.py` between strategy switches** — before
each `_run_X()` call, explicitly `del sys.modules['functions']`,
`['state']`, `['configs']`, `['configs_live']`, `['logger_setup']` if present,
forcing a fresh import every time.
- Much smaller patch — only touches `leto.py`.
- Fragile: relies on remembering every colliding name, breaks silently if
  anyone adds a new same-named file later, and doesn't fix the same collision
  for anything run outside `leto.py` (the test suite, `research/` scripts, ad
  hoc manual runs of two strategies in one shell session).
- Doesn't fix the test-suite breakage from §1.
- Fallback if Option A's blast radius is judged too large to do all at once.

**C. Convert each strategy directory into a real Python package** (`__init__.py`
+ package-qualified imports, e.g. `from iris_production.functions import X`,
dropping the `sys.path.insert` pattern entirely).
- The structurally correct long-term fix — eliminates this entire class of bug
  permanently, for any future file added by anyone.
- Highest effort and highest risk: touches every internal import in every
  strategy directory (not just the 5 colliding names — genuinely all
  cross-file imports within each directory would need to become
  package-relative), and changes how `leto.py`, `research/` scripts, and the
  backtest suites reach into production code.
- Not recommended for now — worth revisiting only if the repo keeps growing
  and this class of collision recurs.

## 5. Recommended plan (Option A)

1. Root-cause the `test_strike_search.py` `range() arg 3 must not be zero`
   failures first — confirm or rule out that they're a symptom of this same
   collision before touching anything, since that changes how urgently this
   needs fixing.
2. Rename, one strategy directory at a time, in this order: **Iris first**
   (it's the one currently live and the one this session's work already
   touched), then Artemis, then Athena, then Apollo (lowest live-relevance —
   resume-only).
3. For each directory: rename the colliding file(s), update every importer
   found in §2's table, grep the whole directory afterward for any remaining
   bare `from functions import` / `from state import` / `from configs import`
   / `from configs_live import` / `from logger_setup import` to catch anything
   missed.
4. After each directory's rename, run that strategy's existing test file
   (`test_strike_math.py` / `test_artemis_strike_math.py` /
   `test_athena_strike_math.py`) in isolation to confirm no regression, *and*
   re-run the cross-strategy import simulation from §1 (import two strategies
   in sequence in one process) to confirm the specific collision is gone.
5. Once all four are done, confirm `python -m pytest tests/` passes clean with
   no `--continue-on-collection-errors` flag needed.
6. Add a regression test that imports at least two strategies' modules
   sequentially in one process (mirroring the actual `leto.py` re-routing
   sequence) and asserts each resolves its own module — so this can't silently
   regress if a new same-named file is added later.
7. Deploy to Delos as one coordinated change, not gradually — a partial rename
   (e.g. only Iris renamed) doesn't reduce risk for the remaining strategies
   still sharing names with each other, and could change *which* strategy wins
   a given collision without fixing it.

## 6. Open questions — resolved

- **Naming scheme**: strategy-prefixed, `iris_functions.py`/`iris_configs.py`/
  `iris_state.py`/`iris_logger_setup.py` style, kept consistent across all
  four — including dropping Athena's old `_live` suffix (`configs_live.py` →
  `athena_configs.py`, not `athena_configs_live.py`) so all four use the same
  `<strategy>_configs.py` pattern rather than preserving an inconsistency.
- **Ordering/urgency**: fix now, starting with Iris — confirmed and done, in
  the order Iris → Artemis → Athena → Apollo (see each strategy's section
  above).
- **`test_strike_search.py` root-cause**: done as part of this fix, and it
  turned out more precise than the original hypothesis — not a cross-strategy
  collision at all, but a same-strategy, cross-test-file contamination between
  `test_artemis_strike_math.py` and `test_strike_search.py` sharing the
  `credit_spread` singleton. Full trace and fix are under §Artemis above.
