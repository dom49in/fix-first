# Demo video script (target 3:15, limit 4:00)

Record the screen. Keep the web UI at `http://localhost:8000` open and the `output_examples/live/` folder ready.

| Time | Show | Say |
|---|---|---|
| 0:00 | Title slide | "Fix First answers one question every crew lead asks in the morning: which open work orders should we take first, and why?" |
| 0:20 | Problem slide | "The extract has 2,041 open orders. 283 have no priority. Priority, age against an SLA target, holidays and weather all matter, so the agent scores them together." |
| 0:45 | Architecture slide | "Three agents: Planner, Executor, Evaluator. Zetaris is the only data path. Nightshift routes every model call. The model never writes SQL." |
| 1:10 | Terminal: `python run.py --preflight` | "Before any agent starts, a COUNT(1) on each of the four Zetaris tables confirms the data is loaded. If not, the request stops with a 503 and no model call." |
| 1:35 | Web UI: ask the city-wide question (use the saved live output if a run takes too long, and say so) | "I ask which orders crews should fix first. The answer leads with order 1969441: priority 5, 8 working days old against a 1-day target, 7 days overdue." |
| 2:00 | Open `output_examples/live/output_4.json`, scroll to `collaboration` and `evaluator_findings` | "This is the feedback loop. The critic flagged the first draft, the Executor revised it, and the Evaluator approved it after re-checking the numbers itself. One revision, confidence 0.87." |
| 2:30 | Open `output_4.trace.jsonl`, point at the `model` field | "Every line logs the model. Nightshift routed all four live runs to NVIDIA Nemotron. That is how NVIDIA's contribution is shown, not just claimed." |
| 2:50 | Sponsors slide | "Zetaris joins and aggregates, Markovian keeps the prompt flat during the drill-down, and H-MEM recalls verified hot spots from earlier questions." |
| 3:05 | Limits slide | "It states what it cannot know: the history is a small sample, weights are assumptions, and it cannot see crew capacity. Thank you." |

Notes
* Real runs take 10 to 25 minutes. Use the saved live outputs for the on-screen answer, and say "from a live run" out loud.
* Do not show `.env`, the Nightshift token or the Zetaris URL.
