# infra-chat-dashboard

A natural language interface for querying data centre infrastructure metrics. Ask questions in plain English, get answers backed by real telemetry data.

This is an early prototype of what will eventually become part of Veltora's operator-facing interface. The core idea: infrastructure engineers should be able to ask "which rows are running hot right now?" or "what's driving the PUE spike on floor 3?" without writing SQL or navigating a dashboarding tool.

## Demo

```
> What's the average inlet temperature across Row 7 this afternoon?

  Row 7 average inlet temp (12:00–17:00): 23.1°C
  Hottest rack: R07-12 at 26.4°C (83% of ASHRAE A2 limit)
  ⚠ R07-12 has been trending up since 14:30 — worth a look

> Compare PUE today vs same day last week

  Today (so far):    PUE = 1.52
  Last Tuesday:      PUE = 1.48
  Delta: +0.04 (+2.7%)

  Main driver: chiller CH-02 running at 94% capacity vs 81% last week.
  CH-02 is due for service in 12 days.
```

## Architecture

```
User Query
    │
    ▼
Query Understanding (Claude API)
    │  extracts: intent, time range, entities (rows, racks, metrics)
    ▼
Tool Router
    │  dispatches to appropriate data retrieval function
    ▼
Data Layer (DuckDB over Parquet files / InfluxDB)
    │  executes the actual query
    ▼
Response Synthesis (Claude API)
    │  formats result as natural language with context
    ▼
Response + Optional Chart
```

Using Claude as the LLM backbone for now (easy to swap). The key design decision was to keep the LLM out of the data path — it generates structured query parameters, the tool layer executes, then the LLM synthesises the response. This makes it auditable and avoids hallucinated numbers.

## Features

- Natural language queries over power, thermal, and PUE metrics
- Time range extraction from relative references ("this afternoon", "last week", "since the maintenance window")
- Entity resolution (rack IDs, row names, floor maps)
- Anomaly highlighting in responses
- Streaming responses via FastAPI + SSE
- Thin React frontend (TypeScript)

## Tech Stack

**Backend**
- Python 3.11, FastAPI
- Anthropic Claude API (claude-3-5-sonnet for query understanding)
- DuckDB for fast analytical queries over Parquet
- Pandas for result post-processing

**Frontend**
- React + TypeScript
- Tailwind CSS
- Recharts for inline sparklines

## How to Run

```bash
# backend
cd backend
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your_key_here
export DATA_PATH=/path/to/your/parquet/data/

uvicorn api:app --reload --port 8000

# frontend
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 and start querying.

## Results / Learnings

The hardest part wasn't the LLM piece — it was entity resolution. Operators say things like "the hot row" or "that rack near the broken CRAC" and you need to map that to R07-12. For now the system asks for clarification when it can't resolve an entity, but that's annoying. The right solution is probably to build an entity index with aliases.

The "don't let the LLM touch the data" principle has paid off. Early versions had the model writing SQL directly, which worked surprisingly well but occasionally produced plausible-looking wrong answers. The tool-based approach means every number in the response is traceable to a real query.

Latency is okay: ~800ms median end-to-end including the Claude API call. Would need caching for frequently repeated queries if this were production.

## Known Gaps

- No auth — this is a prototype
- Data source is currently file-based only, no live DCIM integration
- Entity resolution is basic (exact match + fuzzy)
- No session memory — each query is stateless
- Frontend is rough
