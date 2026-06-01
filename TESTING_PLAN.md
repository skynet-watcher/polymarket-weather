# Testing Plan — Polymarket Weather

Owner: Chad  
Status: **Active — 26/29 passing, 0 failures, 3 skipped (network)**

---

## The Drift Problem This Plan Solves

Before this plan, testing drifted in three ways:

1. **Spec without implementation** — tests described in PREPRODUCTION_TESTS.md but
   never added to run_tests.py (5 tests were missing).
2. **Implementation without spec** — schema columns added to init_db.py without
   corresponding PLAN.md updates, or vice versa.
3. **No enforcement** — nothing prevented these gaps from silently growing.

This plan establishes single sources of truth, anti-drift rules, and a sync check
that runs with every test suite execution.

---

## Single Sources of Truth

| What | Authority | All other references must match it |
|---|---|---|
| Database schema | `scripts/init_db.py` | PLAN.md schema section is documentation — init_db.py is law |
| Pre-production tests | `scripts/run_tests.py` | PREPRODUCTION_TESTS.md describes what's in run_tests.py — not the other way around |
| Analytical tests | `TESTS_TO_RUN.md` | Analysis scripts live in `scripts/analysis/` once written |
| Architecture | `PLAN.md` | All other docs reference PLAN.md; CHAD_*.md are archived |
| Cron schedule | `crontab -l` + `scripts/heartbeat.sh` | Schedule lives in cron; heartbeat.sh is the runner |

### Anti-drift rules

1. **Schema change**: edit `scripts/init_db.py` first. Then update PLAN.md to match.
   Run P-01 before committing. If P-01 fails, the change is incomplete.

2. **New pre-production test**: add the test function to `scripts/run_tests.py` AND
   add the prose description to `PREPRODUCTION_TESTS.md` in the same commit.
   The SYNC self-check will fail if they diverge.

3. **New analytical test**: add it to `TESTS_TO_RUN.md`. When data is available and
   the script is written, add it to `scripts/analysis/` and link it from TESTS_TO_RUN.md.

4. **Plan change**: update PLAN.md. If the change affects schema, also update init_db.py.
   If it affects test logic, also update run_tests.py or TESTS_TO_RUN.md.

5. **Never commit a run_tests.py that has failures** (FAIL status). SKIP is acceptable
   for network tests when running offline. FAIL is not acceptable for any test at any time.

---

## Test Pyramid

```
         ┌─────────────────────────┐
    L4   │   End-to-end (P-28)    │  Full Seoul pipeline, network + live
         ├─────────────────────────┤
    L3   │   API / Network         │  Live METAR, Discovery (P-05, P-09, P-10)
         ├─────────────────────────┤
    L2   │   Integration (DB)      │  Schema, settlement, scanner, alerts
         ├─────────────────────────┤
    L1   │   Unit (pure logic)     │  Timestamps, bucket parser, gaps, labels
         └─────────────────────────┘
```

- **L1 + L2** (offline): run in <10 seconds. Must always pass. Run daily by cron.
- **L3** (network): run 4× daily. Must pass consistently once collection is live.
- **L4** (end-to-end): run manually or weekly. Requires live data + full pipeline up.

---

## Test Files and Their Roles

| File | Role | Owner |
|---|---|---|
| `scripts/run_tests.py` | **Executable test suite** — the only thing that determines pass/fail | Must match PREPRODUCTION_TESTS.md |
| `PREPRODUCTION_TESTS.md` | Prose descriptions of every test in run_tests.py | Updated when run_tests.py changes |
| `TESTS_TO_RUN.md` | Analytical test descriptions for Phase 2/3 research | Updated as analysis scripts are written |
| `scripts/heartbeat.sh` | Cron runner — calls run_tests.py on schedule | Updated when schedule changes |
| `PLAN.md` | Architecture and schema reference | Updated when design decisions change |

### Files that are now archived (not authoritative)

- `CHAD_FULL_AUDIT_2026-06-01.md` — superseded by PLAN.md
- `CHAD_PLAN_REVIEW_2026-05-31.md` — superseded by PLAN.md

---

## Current Test Status

**Run**: `python3 scripts/run_tests.py`  
**As of**: 2026-06-01

| Group | Tests | Passing | Skipped | Failing |
|---|---|---|---|---|
| 1 — Schema | P-01 to P-04 | 4 | 0 | 0 |
| 2 — Discovery | P-05 to P-08 | 3 | 1 (network) | 0 |
| 3 — METAR | P-09 to P-13 | 4 | 1 (network) | 0 |
| 4 — Order Books | P-14 to P-16 | 3 | 0 | 0 |
| 5 — Settlement | P-17 to P-21 | 5 | 0 | 0 |
| 6 — Scanner | P-22 to P-25 | 4 | 0 | 0 |
| 7 — Reliability | P-26 to P-27 | 2 | 0 | 0 |
| 8 — End-to-end | P-28 | 0 | 1 (network) | 0 |
| Sync check | SYNC | 1 | 0 | 0 |
| **Total** | **29** | **26** | **3** | **0** |

**Target state**: 29/29 passing, 0 skipped. The 3 skipped tests require live APIs
(P-05 discovery, P-09 METAR, P-28 end-to-end). They will move from SKIP to PASS
once collection is running and `--network` flag is passed.

---

## Heartbeat Schedule

| When | Mode | What runs | Pass criterion |
|---|---|---|---|
| Daily 06:00 UTC | `offline` | All offline tests (L1 + L2) | 26/26 PASS |
| 00:30/06:30/12:30/18:30 UTC | `network` | Full suite including live APIs | 29/29 PASS |
| Mondays 07:00 UTC | `report` | Analytical test readiness summary | See unlock criteria below |

All output appended to `logs/cron.log`.

**Run manually**:
```bash
# Offline only (fast, no network)
python3 scripts/run_tests.py

# Full suite with live APIs
python3 scripts/run_tests.py --network

# Weekly readiness report
bash scripts/heartbeat.sh report

# Single group
python3 scripts/run_tests.py --group 5
```

---

## Path to Steady State

### Phase 0 — Logic verified (complete)
- [x] All 28 pre-production tests implemented in run_tests.py
- [x] SYNC self-check passes (spec and implementation match)
- [x] 26/29 offline tests passing, 0 failures
- [x] Heartbeat cron installed
- [x] Stale CHAD_*.md archived

### Phase 1 — Collection live (unblocked when data flows)
- [ ] `discover_markets.py` running daily → P-05 SKIP → PASS
- [ ] `fetch_weather.py` loop running → P-09 SKIP → PASS
- [ ] `log_orderbooks.py` running → P-28 partially testable
- [ ] Network heartbeat (4×/day) consistently passing 29/29

**Completion criterion**: heartbeat network run shows 29/29 PASS for 3 consecutive days.

### Phase 2 — Settlement pipeline live
- [ ] WU adapter built (`wunderground_daily`) — unblocks ~82% of settlement
- [ ] `settle_markets.py` trigger running automatically
- [ ] Monday report shows first analytical tests unlocking:
  - Tests 8, 12, 14, 10 unlock after 500+ observations
  - Test 1 (stale observation) unlocks after obs + first settlement
  - Test 6 (neg-risk gaps) unlocks after 500+ order book snapshots

**Completion criterion**: Monday report shows ≥4 analytical tests ✅ Ready.

### Phase 3 — Research begins
- [ ] ≥3 forecast models collecting (ICON, MF, GEM) — unblocks Tests 3, 4, 5
- [ ] ≥14 days of data — unlocks Tests 4, 11
- [ ] ≥100 settled markets — unlocks Tests 5, 13
- [ ] VHHH-HKO offset calibrated — HK obs_mismatch alerts trusted

**Completion criterion**: Monday report shows ≥10 analytical tests ✅ Ready.

---

## Analytical Test Unlock Criteria

These unlock thresholds are checked automatically by `heartbeat.sh report`.

| Test | Unlocks when |
|---|---|
| Test 8 — Liquidity filter | `ob_snapshots > 500` |
| Test 12 — Already priced in | `wx_observations > 500` |
| Test 14 — Source latency | `wx_observations > 200` |
| Test 10 — Local-time path | `wx_observations > 500` |
| Test 1 — Stale observation | `obs > 500 AND settled_markets > 0` |
| Test 6 — Neg-risk gaps | `ob_snapshots > 500` |
| Test 7 — Resolution mismatch | `settled_markets ≥ 50` |
| Test 2 — Market-open accuracy | `forecast_models ≥ 1 AND days_obs ≥ 3` |
| Test 9 — Forecast revision | `forecast_models ≥ 1 AND days_obs ≥ 7` |
| Test 3 — Forecast consensus | `forecast_models ≥ 3 AND days_obs ≥ 7` |
| Test 4 — Best timing | `forecast_models ≥ 3 AND days_obs ≥ 14` |
| Test 5 — Dynamic rebalancing | `settled_markets ≥ 100` |
| Test 11 — Station reliability | `days_obs ≥ 14 AND settled_markets > 50` |
| Test 13 — Bucket adjacency | `settled_markets ≥ 50` |

---

## How to Add a New Test

1. Write the test function in `scripts/run_tests.py`:
   ```python
   def test_pNN_descriptive_name():
       """One-line description."""
       # ... test logic ...
       report("P-NN", "Human readable name", PASS_or_FAIL, "detail")
   ```

2. Add it to the correct group's call block in `main()`.

3. Add the prose description to `PREPRODUCTION_TESTS.md` under the correct group
   heading, using the same P-NN identifier.

4. Run `python3 scripts/run_tests.py` and confirm:
   - SYNC check passes (counts match)
   - New test passes
   - All existing tests still pass

5. Commit both files together. Never commit one without the other.

---

## How to Change the Schema

1. Edit `scripts/init_db.py`:
   - Add the column to the `CREATE TABLE IF NOT EXISTS` DDL
   - Add it to `_add_missing_columns()` in `_migrate_existing_tables()`

2. Run P-01: `python3 scripts/run_tests.py --group 1`
   - P-01 will fail if the column is missing from either place

3. Update `PLAN.md` schema section to match.

4. Commit `init_db.py` and `PLAN.md` together.

---

## Definition of Done

The project is at **steady state** when all of the following are true:

- [ ] `python3 scripts/run_tests.py --network` exits 0 (29/29 PASS, 0 FAIL, 0 SKIP)
- [ ] Heartbeat network runs are consistently passing for 7 consecutive days
- [ ] Monday report shows ≥4 analytical tests Ready
- [ ] No open alerts of type `fetch_failure`, `discovery_slug_failure`, or
      `settlement_integrity_error` in `weather.db`
- [ ] SYNC check passes (28 spec tests = 28 implemented tests)
- [ ] PLAN.md schema section and `scripts/init_db.py` are in sync
      (verified by P-01 passing)
