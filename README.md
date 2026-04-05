# infra-chat-dashboard

A natural language interface for querying data centre infrastructure metrics. Ask questions in plain English, get answers backed by real telemetry data.

This started as a prototype for what will eventually become part of Veltora's operator interface. The idea is that infrastructure engineers should be able to ask "which rows are running hot right now?" without writing SQL or navigating a dashboarding tool.

## Demo

```
> What's the average inlet temperature across Row 7 this afternoon?

  Row 7 average inlet temp (12:00-17:00): 23.1°C
  Hottest rack: R07-12 at 26.4°C (83% of ASHRAE A2 limit)
  R07-12 has been trending up since 14:30 - worth a look

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
    |
    v
Query Understanding (LLM API)
    |  extracts: intent, time range, entities (rows, racks, metrics)
    v
Tool Router
    |  dispatches to the right data retrieval function
    v
Data Layer (DuckDB over Parquet files)
    |  runs the actual query
    v
Response Synthesis (LLM API)
    |  formats result as natural language with context
    v
Response + Optional Chart
```

The LLM handles understanding and synthesis but never touches the data directly. It generates structured query parameters, the tool layer runs the actual query, then the LLM writes the response. Every number in the response is traceable to a real query, not generated from memory.

## Features

- Natural language queries over power, thermal, and PUE metrics
- Time range extraction from relative references ("this afternoon", "last week", "since the maintenance window")
- Entity resolution (rack IDs, row names, floor maps)
- Anomaly highlighting in responses
- Streaming responses via FastAPI + SSE
- React frontend (TypeScript)

## Tech Stack

**Backend**
- Python 3.11, FastAPI
- Anthropic API (claude-3-5-sonnet for query understanding)
- DuckDB for analytical queries over Parquet
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
export DATA_PATH=/path/to/parquet/data/

uvicorn api:app --reload --port 8000

# frontend (separate terminal)
cd frontend
npm install
npm run dev
```

Both servers need to be running. The frontend runs locally on port 5173, the backend on port 8000. Nothing is deployed, this is a local dev setup only.

To generate test data to query against, run the synthetic-infra-datasets generator first and point DATA_PATH at the output folder.

## Results / Learnings

The hardest part was entity resolution, not the LLM bit. Operators say things like "the hot row" or "that rack near the broken CRAC" and you need to map that to R07-12. The system currently asks for clarification when it can't resolve an entity, which is annoying. Probably need a proper entity index that operators can add aliases to.

Keeping the LLM out of the data path has been worth the extra complexity. Early versions had the model writing SQL directly, which worked okay but occasionally gave plausible-looking wrong answers. Tool-based approach means everything is auditable.

Latency is around 800ms median end-to-end. Fine for a prototype, would need caching before this could handle real query volume.

## Known Gaps

- No auth - this is a prototype
- Data source is file-based only, no live DCIM integration
- Entity resolution is basic (exact match + fuzzy fallback)
- No session memory, each query is independent
- Frontend needs a lot of work
