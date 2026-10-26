# Fix First: a maintenance triage agent

**Open Agent Hackathon 2026, Track 3: Reasoning Architecture.** An agent that answers *"which open work orders should crews take first, and why?"* from the open work backlog of a city's asset-maintenance system. Planner, Executor and Evaluator reason over **four Zetaris sources**, use the **Meterless Markovian and H-MEM engines**, run on **Meterless Nightshift** (with the **NVIDIA** lane configured in Nightshift), and every number is re-checked a second way before it is shown.

Before any agent starts, the app asks Zetaris `SELECT COUNT(1)` on every table it needs. If the data is not loaded the run is refused with `503 data_not_ready` and no model is called.

## Data credit (CC BY 4.0)

Work orders: **Cityworks Workorders**, published by the **District of Columbia** (data.gov catalog: https://catalog.data.gov/dataset/cityworks-workorders), licensed **CC BY 4.0** (https://creativecommons.org/licenses/by/4.0).
Changes made: selected and renamed 20 of 61 columns, dropped staff-name and empty columns, kept two extracts tagged by a `record_set` column: the **active FY2026 extract** (2,041 work orders raised 3 Aug to 8 Sep 2026) and a **sample of 289 closed historical work orders** (FY2023-24). **This project is not endorsed by the District of Columbia.**
Public holidays come from the Nager.Date API and weather from the Open-Meteo API (both read through Zetaris); see "Sources and licences" below. The SLA targets (`data/sla_policy.csv`) are **derived from the historical sample by `scripts/derive_sla.py` and are indicative, not DC policy**; the priority weights and the targets for asset groups without enough history are assumptions.

## Sources and licences

| What | Source | Licence / terms |
|---|---|---|
| Work orders (`data/work_orders.csv`) | Cityworks Workorders, District of Columbia, https://catalog.data.gov/dataset/cityworks-workorders | CC BY 4.0 (https://creativecommons.org/licenses/by/4.0). Credit above; changes listed above; not endorsed by DC. |
| Public holidays | Nager.Date API, https://date.nager.at | MIT-licensed open-source project; read live through a Zetaris REST table. |
| Weather | Open-Meteo API, https://open-meteo.com | Open-Meteo data is CC BY 4.0; the free API is for non-commercial use and requires attribution, given here. Read live through a Zetaris REST table. `data/weather_synthetic_for_tests.csv` is synthetic, made for tests and sample mode only. |
| SLA policy (`data/sla_policy.csv`) | Derived by `scripts/derive_sla.py` from the historical sample | My own derivation; indicative, not DC policy. |
| Meterless Markovian and H-MEM | Specifications and reference implementations, https://github.com/Meterless/Meterless | Apache-2.0. Not copied; Python ports written for this project. |
| Libraries | Python standard library, `uvicorn` (BSD-3-Clause), `requests` (Apache-2.0) | As listed. |
| This project's code | This repository | MIT (see `LICENSE`). |

## The four Zetaris sources (one new container, `fixfirst`)

| # | Zetaris table | What it is | How it gets there |
|---|---|---|---|
| 1 | `fixfirst.work_orders` | 2,330 real DC work orders: 2,041 active + 289 closed history (`record_set`) | `scripts/load_to_zetaris.py` (REST, views from inline `VALUES`) |
| 2 | `fixfirst.sla_policy` | target working days (from history) and weight per asset group and priority | same script, built by `scripts/derive_sla.py` |
| 3 | `fixfirst.us_holidays` | US public holidays 2023-2026, **web API** (Nager.Date) | registered as Zetaris REST tables |
| 4 | `fixfirst.weather_daily` | daily rain, wind, temperature for Washington DC 2023-2026, **web API** (Open-Meteo) | registered as a Zetaris REST table |

The agent never calls a website itself. Sources 3 and 4 are Zetaris REST tables, queried through the same adapter as the work orders. The main work-set query is a cross-source JOIN of tables 1 and 2; the history baseline is a second query on table 1 (`record_set = 'history_sample'`). The earlier `maint` container, if you created it, is not used.

### How the history is used
1. **SLA targets.** `scripts/derive_sla.py` takes the closed, non-cancelled historical orders, measures time to close in working days, drops closures slower than 60 working days (mostly bulk or administrative closures: one unprioritised order closed 473 days after it was raised), and sets each target to the 75th percentile for the asset-group and priority cell (or the whole asset group when the cell has fewer than 8 closures; a cell with too little data keeps an assumed target, labelled in `target_basis`). Targets never get longer for higher priorities.
2. **Time-to-close baseline.** At run time the Executor reads the closed rows from Zetaris, reports the typical (median) time to close per asset group, and the Evaluator re-reads and recomputes it.
3. **Exploratory weather comparison.** Closures raised on weather-impacted days versus other days, with counts. It is descriptive only and reports "too small to tell" unless both groups have at least 20 orders.
Open backlog counts only ever use the active extract; history rows are all closed, so they cannot change them.

## Quick start (no credentials needed)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
SAMPLE_MODE=true python run.py --preflight                       # the data-loaded check
SAMPLE_MODE=true python run.py --input input_examples/example_1.json
SAMPLE_MODE=true python run.py                                   # web UI at http://localhost:8000
python -m unittest discover -s tests -t .                        # the test suite
```

### What "sample mode" is, honestly
`SAMPLE_MODE=true` reads the same tables from `data/*.csv` into in-memory SQLite and, only if no Nightshift credentials are set, replaces the model with deterministic rules (`app/offline.py`, logged as `offline-rules`). The work orders are **real**; `data/weather_synthetic_for_tests.csv` is **synthetic** and every output says so. Sample-mode runs are not live runs and are labelled `sample_data`.

## Load the data into Zetaris (step by step)

1. `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
2. `cp .env.example .env` and fill in `ZETARIS_REST_URL`, `ZETARIS_ORG_ID`, `ZETARIS_API_KEY` (and the Nightshift values for live runs). `.env` may sit in the project folder or in `scripts/`.
3. `python run.py --load` (same as `python scripts/load_to_zetaris.py`). It loads the 2,330 work orders (active + history) in 250-row batches plus the policy table into the new `fixfirst` container, registers the holidays and weather REST tables, skips anything already loaded, and ends with a row count per table. Useful flags: `--probe` (one-row test), `--dry-run` (write `scripts/generated/zetaris_load.sql` and send nothing), `--only sla_policy`, `--register-apis`.
4. `python run.py --preflight` must print `"ready": true` with 2330 work-order rows and 289 history rows. The agent will not run until it does.
5. If the API refuses `CREATE VIEW`, paste `scripts/generated/zetaris_load.sql` and then `scripts/zetaris_register_apis.sql` into the Zetaris SQL Editor, one statement at a time.

### The pre-flight check (`app/preflight.py`)
| Table | Rule |
|---|---|
| work orders | **required**: `COUNT(1) > 0` and equal to `data/manifest.json` (catches a half-loaded UNION view; the manifest is rewritten by the loader) |
| SLA policy | **required** |
| history (289 rows with `record_set = history_sample`) | optional: without it there is no time-to-close baseline and the run says so |
| holidays, weather | optional: if missing the run continues, **degraded**, and the answer says holidays or weather were not applied (`PREFLIGHT_REQUIRE_ALL=true` makes them required) |

It runs in `POST /run` before the Planner is created, in `GET /health/data`, in `python run.py --preflight`, and in the UI banner.

## Live mode

```bash
cp .env.example .env     # SAMPLE_MODE=false is the default; set the Zetaris and Nightshift values
python run.py --input input_examples/example_1.json --output output_examples/live/output_1.json
```
Nightshift: set `METERLESS_BASE_URL` (Nightshift Runtime URL ending `/v1`) and `METERLESS_RUNTIME_TOKEN`; model `meterless/auto`; headers `meterless-app: fix-first`, `meterless-scope: user`. **The NVIDIA key is entered only in Nightshift's Providers tab; this app never sees it.** A live run takes several minutes because every model call is a Nightshift round trip.

## How it works

```
 pre-flight COUNT(1) ─▶ PlannerAgent ──plan + H-MEM hints──▶ ExecutorAgent ──draft + evidence──▶ EvaluatorAgent
   (503 if not loaded)      ▲ recall / store                    │ work set (JOIN) + holidays + weather   │ recomputes 2 ways
                            │                                   │ Markovian loop: probe → descend        │
                            │                                   ▼ Zetaris SQL (whitelisted, read-only) │
                            └──────── critique ◀── revise ◀──────────────────────────────────────────┘
                                       (up to 2 revisions; unresolved issues are reported, never hidden)
```

* **Measure.** Each open order carries the policy weight of its asset group and priority; the *weighted backlog* is the sum. The Markovian loop probes dimensions (asset group, ward, category, priority level), descends into the one where the weighted backlog concentrates (top member ≥ 30%), and stops when the hot spots cover enough of it.
* **Scoring.** Per order: `score = weight × (1 + min(age ÷ target, 3))`. Age counts **working days** (Mon-Fri minus public holidays). An order is *overdue* when its age exceeds its target; *overdue net of weather* gives back working days with heavy rain (≥ 10 mm), high wind (≥ 50 km/h), extreme heat (≥ 35 °C) or hard freeze (≤ -5 °C), so the answer separates delay that weather can explain from delay it cannot.
* **Data quality is a finding.** 35% of open orders carry no priority. The agent says so, scores them as medium urgency and ranks them for triage instead of guessing.
* **The model never writes SQL.** `app/schema.py` builds every statement from whitelisted dimensions and escaped literals, and `app/sqlguard.py` rejects anything but one `SELECT`.

### What the Evaluator verifies (deterministically, whichever model wrote the draft)
work set equals Zetaris's own control total · every Zetaris `GROUP BY` equals the same grouping recomputed in Python · each hot spot recomputed from the work set and from a **fresh Zetaris query** · every ranked order re-scored from raw fields and in descending order · holidays and weather re-read from Zetaris · no truncated result sets · every number in the prose is a rounding of an evidence number · every hot spot cited · missing priorities disclosed · a model critic for unsupported causes. Failed checks produce a critique, a revision and a re-review; anything left after two revisions is returned in `collaboration.unresolved_issues`.

## AI and model usage
* **Models:** all product model calls go through Meterless Nightshift (`meterless/auto`); the model that handled each call is written to every log line and to `models_used`. Roles: Planner hypotheses, Executor step decisions and drafts, Evaluator critic. No embedding model (H-MEM uses a hashed bag-of-words vector).
* **NVIDIA / Nemotron:** the NVIDIA key is configured in Nightshift's Providers tab. The app has no NVIDIA-specific code; NVIDIA models contribute only if Nightshift routes to them, so read `models_used` in a live output before claiming it.
* **Agent framework:** none; a small custom stack. **Libraries:** Python standard library, `uvicorn` (BSD-3), `requests` (Apache-2.0).
* **Coding tool:** built with Claude Code. All code was run and tested.

## Meterless engines
* **Markovian** (`app/engines/markovian.py`), load-bearing: every investigation step sees only a bounded carryover block (COMPLETED / REMAINING / DECISIONS / CONTEXT) plus the latest observation, never the full history, so prompt size stays flat. Config bounds, the compression cascade, run efficiency and the cost model follow the spec; the numeric functions are checked against vectors produced by the TypeScript reference (`tests/fixtures/markovian_reference_vectors.json`). The Python port is not run through the TypeScript conformance suite, so conformance is not claimed.
* **H-MEM** (`app/engines/hmem.py`): the Planner recalls earlier hot spots and ruled-out dimensions as *hints*; after each run it scores them against today's data (helpful / not helpful), then stores new verified findings. Retrieval threshold 0.25 (spec default 0.35, tuned for short machine-written memories). `GET /memory` shows memories and ledger integrity.

## API
`POST /run` `{request_id, user_query, context}` → `{request_id, trace_id, status, preflight, agents_used, result, collaboration, engines, models_used, reasoning_mode, data_source, log_path}`. `GET /health`, `GET /health/data`, `GET /memory`, `POST /memory/feedback`, `POST /memory/sleep`, `GET /engine/markovian`, web UI at `/`. Errors are structured `{type, message, stage, details}`; `data_not_ready` is HTTP 503.

## Examples
`input_examples/` holds four requests (city-wide, one ward, unprioritised work, TOA with weather). `output_examples/output_*.json` are **sample-mode** runs (real work orders, synthetic weather, offline rules) with their JSONL traces in `output_examples/logs/`. The four **live** runs (real Zetaris and Nightshift, final build) are in `output_examples/live/`, each with its JSONL trace.

## Logs
Every agent action, handoff and model call is one JSON line in `logs/<trace_id>.jsonl` (and stdout): timestamp, trace_id, agent_name, action, input/output summary, target_agent, **model**, confidence, retry_count, status.

## Verified vs. not verified
* **Verified live (Zetaris + Nightshift), final build, four runs** (`output_examples/live/`, each with its JSONL trace): all four were approved by the Evaluator with every deterministic check passing, and all four were routed by Nightshift to `nvidia/nemotron-3.5-lightning-30b-a3b` (`models_used`).

  | Example | Trace | Confidence | Revisions |
  |---|---|---|---|
  | 1 city-wide | `trace-20261026-6653beca` | 0.95 | 0 |
  | 2 Ward 7 | `trace-20261026-e0056338` | 0.95 | 0 |
  | 3 unprioritised work | `trace-20261026-322119b4` | 0.95 | 0 |
  | 4 TOA and weather | `trace-20261026-90767538` | 0.87 | 1 (critique, revise, approve) |

  Also live: the five-table `COUNT(1)` gate, the REST load, both web-API tables (holidays 44, weather 1,349), the work-order/policy JOIN and every GROUP BY (the Evaluator recomputed them all and they matched), and H-MEM recall and feedback (example 1 recalled 5 memories and scored 4).
* **Known limitation:** Nightshift can stop a long model call with HTTP 504 `first_token_timeout` (the model reasons silently before its first token). A run takes 10 to 25 minutes. If a *revision* call hits this, the app keeps the last reviewed draft, lists the open issues, lowers confidence and adds a caveat instead of failing the request; a failed critic call is handled the same way. This happened in some of my earlier runs and is why `feedback_loop_completed` is reported honestly rather than assumed.
* **Verified by tests only (local stand-ins):** request headers, retries, truncation handling, model logging (`tests/test_live_paths.py`, `tests/test_live_findings.py`).
* **Not verified:** conformance of the Python Markovian port against the TypeScript suite.

## Deviations and limits
* SLA targets come from a small historical sample (289 closed orders; 20 to 30 per cell at best) and the cap that separates responsive from bulk closures is my choice; weights and weather thresholds are assumptions; the priority scale is undocumented in the source (5 is treated as most urgent because EMERGENCY orders carry it).
* The extract covers five weeks, so ages are short; "as of" is the last work order raised, not today.
* Costs are mostly empty in the source and not used. Staff-name columns were dropped and are never shown.
* The ranking says where weighted backlog and overdue work sit. It cannot see crew capacity, contracts or budgets.

## Layout
`app/` agents, scoring, schema, preflight, engines · `scripts/` loader, SLA derivation, REST-table SQL · `data/` extracts, derived policy, sample files · `tests/` · `input_examples/` · `output_examples/` · `Dockerfile`
