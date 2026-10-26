# Submission pack: "Fix First"

Track: **Reasoning Architecture** · Open Agent Hackathon 2026 · Solo team DMax
Repository:(https://github.com/dom49in/fix-first.git)

A maintenance-triage agent that decides which open work orders crews should take first, and shows its reasoning. Three agents (Planner, Executor, Evaluator) work in a feedback loop over **four Zetaris sources** (work orders with current and historical records, an SLA policy table derived from the history, and two web APIs registered as Zetaris REST tables: public holidays and weather). Model calls go through Meterless Nightshift, with the NVIDIA lane configured there. Meterless Markovian and H-MEM run inside the loop. Setup, usage and dependencies are in the [README](README.md). Data: DC Cityworks Workorders (District of Columbia), CC BY 4.0; credit and changes are in the README.

## 1. Requirements and evidence

| Requirement | Evidence |
|---|---|
| Zetaris is the data layer, with a real role | Every number is a SQL result via `app/adapters/zetaris.py`. Four sources in one container: `fixfirst.work_orders` (2,041 active + 289 closed historical rows), `fixfirst.sla_policy` (cross-source JOIN in the main query), `fixfirst.us_holidays` (Nager.Date API) and `fixfirst.weather_daily` (Open-Meteo API), the last two registered as Zetaris REST tables (`scripts/zetaris_register_apis.sql`). Loader: `scripts/load_to_zetaris.py`. |
| Data confirmed before agents run | `app/preflight.py` runs `SELECT COUNT(1)` per table before the Planner is created; not loaded gives HTTP 503 `data_not_ready` and no model call. Tests: `tests/test_preflight.py`, `tests/test_live_paths.py::test_data_not_loaded_means_no_model_call_and_no_agent`. |
| At least one Meterless Engine in a working feature, with a test | Markovian (triage loop) and H-MEM (memory loop). Tests: `tests/test_markovian.py`, `tests/test_hmem.py`, and the pipeline test that checks prompt size stays flat. Both are Python ports of the specs. |
| Nightshift is the primary AI provider | All model calls in `app/llm.py` go to `{METERLESS_BASE_URL}/chat/completions`, model `meterless/auto`, headers `meterless-app` and `meterless-scope`, bearer runtime token. |
| NVIDIA technology where it contributes | The NVIDIA key is configured in Nightshift's Providers tab; the model that handled each call is logged (`models_used`, every trace line). See the live outputs in `output_examples/live/` for the models that actually ran. |
| At least two agents with distinct roles | Planner, Executor, Evaluator. |
| A real feedback loop, with critique and revision | The Evaluator recomputes numbers two ways, re-scores the ranking and re-reads the calendar sources; it sends drafts back (up to 2 revisions). Tests inject an ungrounded number and a tampered hot spot and show the loop catching both. |
| Logs showing how the agents worked, including which model handled each request | JSONL traces in `output_examples/logs/` (sample mode) and `output_examples/live/` (live: four runs on the final build); same lines on stdout. |
| At least three inputs and outputs | `input_examples/` (4 files), `output_examples/` (4 sample-mode runs), `output_examples/live/`. |
| No prewritten answers | `tests/test_hygiene.py` checks no business facts are hard-coded in `app/` and `output_examples/` is never read at runtime. |
| No keys committed | `.env` is git-ignored, `.env.example` has blank secrets, `tests/test_hygiene.py` scans for key shapes. |
| Demo video (1 to 4 minutes) and slide deck | Provided with the HackOS submission. |

## 2. Sponsor technology explanation

**Zetaris: the data layer.** Triage needs *joined, aggregated, current* data from different places: work orders (current and historical), the policy that sets each order's target and weight, the holiday calendar and the weather. Zetaris serves all four through one SQL endpoint: two as tables loaded through its REST API, two as web APIs registered as REST tables, so the agent code has one data path and no website calls. The main query JOINs work orders to the policy table inside Zetaris; breakdowns are `GROUP BY` queries; the Evaluator re-queries Zetaris to confirm. SQL is built from a whitelist, escaped, and rejected unless it is one `SELECT` (the model never writes SQL). A pre-flight `COUNT(1)` on every table gates the run, and row caps are detected rather than silently analysed.

**Meterless Markovian: the triage loop.** Finding where backlog concentrates is a drill-down: probe each dimension, descend into the concentrated one, probe again inside it. Each step's prompt is the system prompt, a bounded carryover (`COMPLETED / REMAINING / DECISIONS / CONTEXT`) and only the latest observation, so prompt size stays flat while the investigation deepens. The state transition is code; the model picks the next action.

**Meterless H-MEM: compounding knowledge.** Verified hot spots and ruled-out dimensions are stored with provenance. The next related question starts with those as hints, the Planner orders its hypotheses accordingly, and the Evaluator's verdict feeds back as helpful or not helpful. Memory is advisory: numbers always come from fresh queries.

**Meterless Nightshift: the model provider.** Planner hypotheses, step decisions, drafts and a model critic go through Nightshift (`meterless/auto`) using only the Runtime token. `response.model` is recorded on every log line and in `models_used`, which is how NVIDIA contribution is shown rather than asserted. The NVIDIA key lives in Nightshift's Providers tab, never in this app.

**Not used:** World Model, Scout Intent.

### Privacy design
The model sees aggregates and a short list of order attributes (id, work type, priority, age), never the full table. Staff-name columns were dropped before loading and are never shown. Memory is a local file; secrets only via environment.

## 3. Declaration of pre-existing components

* **Meterless engine specifications and reference implementations** (Apache-2.0, `github.com/Meterless/Meterless`). Not copied; `app/engines/markovian.py` and `app/engines/hmem.py` are Python ports written during the build window, with numeric tests against vectors generated from the TypeScript reference.
* **My own earlier submission in this hackathon, "Why did it move?" (Track 2).** This project reuses its generic framework, written inside the build window: the Nightshift client, tracing, Zetaris adapter and SQL guard, H-MEM glue, the engine ports and the Planner/Executor/Evaluator pattern. The triage domain (SQL builders, scoring, SLA and weather logic, pre-flight gate, agents, prompts, UI, data loader, tests) is new for this submission.
* **Data:** DC Cityworks Workorders (a current extract and a sample of closed historical orders; District of Columbia; CC BY 4.0, https://catalog.data.gov/dataset/cityworks-workorders) is a public dataset downloaded from the open-data catalog; the column selection, cleaning and loading code are new. The SLA targets were derived from the historical sample by `scripts/derive_sla.py`; the priority weights and the synthetic weather test file were created for this project. Holidays and weather are read live from the Nager.Date (MIT) and Open-Meteo (CC BY 4.0, non-commercial free tier, attribution given in the README) public APIs. Full sources and licences are in the README section "Sources and licences".
* **Licence of this project's code:** MIT (`LICENSE`).
* **Open-source libraries:** the Python standard library, `uvicorn` and `requests`.
