# Architecture: where things are

A map for when you want to look at the code after the [tour](LEARN.md). It is short on purpose.

## One question, end to end

```
browser ──POST /support──► gateway ──POST /ask──► agents (LangGraph)
                                                    │
                          supervisor ◄──────────────┤   decides the next worker, up to 5 times
                              │                     │
              ┌───────────────┴───────────────┐     │
          order_agent                    policy_agent
          (LangChain agent)              (looks up policy text, asks the model)
              │ tool: get_order
              ▼
        orders ──► inventory          each worker may call the model (real Gemini or the mock)
          │            │
       orders.db   inventory.db       then: writer ──► final answer ──► back up the chain
```

Every hop is an HTTP call. The trace id and request id travel in headers (`traceparent`, `X-Request-ID`),
which is what makes four programs show up as one trace.

## The code

| Path | What it does |
|---|---|
| `services/gateway/main.py` | Public entry point (`/support`), the X-ray page's backend (live event stream, past requests), `/api/config`, `/api/traffic`. Also serves the page. |
| `services/gateway/store.py` | In-memory store of the last 50 requests' events, fanned out live to the browser |
| `services/gateway/traffic.py` | The "Generate sample traffic" runner (bounded, mock-only) |
| `services/agents/graph.py` | **The multi-agent graph**: supervisor, order agent, policy agent, writer, and the guardrail. `_ask()` is the single door to the model. |
| `services/agents/main.py` | HTTP wrapper around the graph (`/ask`, `/feedback`, `/config`) |
| `services/agents/prompts.py` | Every prompt in one place, with an automatic version fingerprint |
| `services/agents/tools.py` | The `get_order` tool (calls the orders service) |
| `services/agents/rag.py`, `kb/policies.md` | Tiny retrieval over the policy text (word overlap, not embeddings) |
| `services/agents/models.py` | Mock or live? Decided per request |
| `services/agents/mock_llm.py` | The built-in fake model (rules, delay, token counts) and the span that records each call |
| `services/agents/callbacks.py` | Records the model calls that happen *inside* the LangChain order agent |
| `services/orders/`, `services/inventory/` | Two small services backed by SQLite |
| `services/common/telemetry.py` | **OpenTelemetry setup**: traces, metrics, logs, auto-instrumentation, the span feed for the X-ray page |
| `services/common/metrics.py` | The metrics: HTTP (RED), model calls, tokens, cost, tool calls, supervisor decisions |
| `services/common/events.py` | The X-ray page's extra details (prompts, tool arguments) and the request-id header |
| `services/common/chaos.py` | The three injectable problems |
| `services/common/pricing.py`, `model_prices.json` | Token counts to estimated cost |
| `ui/` | The X-ray page (plain JavaScript modules, no build step) |
| `infra/` | Collector, Tempo, Loki, Prometheus config; alert, recording and SLO rules; the four Grafana dashboards |
| `evals/` | The 20 test questions, the scoring checks, the AI judges |
| `run.py`, `Dockerfile`, `docker-compose.yml` | One-command start/stop, and the containers |
| `scripts/` | `run_all.ps1`/`.cmd` (Windows, services from source), `load.py` (command-line traffic generator) |
| `tests/` | Offline tests (no Docker, no key) |

## The three signals, and where each leaves the code

| Signal | Created in | Sent to | Stored in | Viewed in |
|---|---|---|---|---|
| Traces | auto: FastAPI, httpx, SQLite instrumentation; manual: `@traced`, `get_tracer()` in `graph.py` | Collector (OTLP, port 4318) | Tempo | Grafana → Explore, and the X-ray page |
| Metrics | `services/common/metrics.py` helpers | Collector, every 5 s | Prometheus (scrapes the Collector) | Grafana dashboards |
| Logs | Python's `logging`, bridged to OpenTelemetry in `telemetry.py` | Collector | Loki | Grafana, and the X-ray page's "logs" link |

## Things that travel with a request

| Header | Set by | Meaning |
|---|---|---|
| `traceparent`, `baggage` | OpenTelemetry (automatic) | Which trace this is; whether the X-ray page is watching it |
| `X-Request-ID` | gateway | Groups the X-ray page's events |
| `X-LLM-Mode` | gateway, from the page's switch | `mock` or `live` for this question |
| `X-Chaos` | gateway, from the page's menu | Which problem to inject (ignored unless `CHAOS_ENABLED`) |

## Small things worth knowing

- **Where to add a metric:** define it in `services/common/metrics.py`, record it where the thing happens.
- **Where to add a span:** wrap the code in `get_tracer().start_as_current_span("name")` (or the `@traced` decorator) and put facts
  on it with `set_span_attributes`. Names starting `gen_ai.` follow OpenTelemetry's AI conventions.
- **Never put unbounded values in metric labels** (ids, question text). Put them on spans or in logs.
- **Observability must never break the app:** exporters batch and drop data if the Collector is down; the page's event
  delivery swallows errors. Keep that property when you add code.
