"""
tools.py

Data retrieval tools for the infrastructure query agent.
These do the actual work — query DuckDB over Parquet files.

Keeping this layer separate from the agent means:
  - Easy to test independently
  - Easy to swap data backends (Parquet → InfluxDB → real DCIM API)
  - The agent code stays clean

DuckDB is great for this use case: fast analytical queries on
Parquet without needing to spin up a database server.
"""

import json
import logging
from dataclasses import dataclass, asdict
from typing import Optional, Any
from datetime import datetime, timedelta
import pandas as pd
import duckdb

logger = logging.getLogger(__name__)


@dataclass
class QueryResult:
    data: Optional[Any] = None
    metadata: Optional[dict] = None
    error: Optional[str] = None

    def to_json(self) -> str:
        if self.error:
            return json.dumps({"error": self.error})
        return json.dumps({
            "data": self.data,
            "metadata": self.metadata or {},
        }, default=str)


class InfraQueryTools:

    def __init__(self, data_path: str):
        self.data_path = data_path.rstrip("/")
        self._con = duckdb.connect(":memory:")
        self._register_tables()

    def _register_tables(self):
        """
        Register Parquet files as virtual tables in DuckDB.
        Lazy loading — DuckDB only reads what the query needs.
        """
        import os
        tables = {
            "power": "power.parquet",
            "thermal": "thermal.parquet",
        }
        for tbl, fname in tables.items():
            path = f"{self.data_path}/{fname}"
            if os.path.exists(path):
                self._con.execute(f"CREATE VIEW {tbl} AS SELECT * FROM read_parquet('{path}')")
                logger.info(f"Registered table '{tbl}' from {path}")
            else:
                logger.warning(f"Data file not found: {path} — queries on {tbl} will fail")

    def _parse_scope(self, scope: str) -> tuple[str, str]:
        """
        Parse scope string like 'row:ROW-07' or 'rack:R07-12' into (level, id).
        Returns ('facility', '') for facility-level queries.
        """
        if ":" in scope:
            level, entity_id = scope.split(":", 1)
            return level.lower(), entity_id
        return "facility", ""

    def query_thermal(
        self,
        time_start: str,
        time_end: str,
        scope: str,
        metric: str = "inlet_temp_c",
        aggregation: str = "avg",
    ) -> QueryResult:
        agg_fn = {"avg": "AVG", "max": "MAX", "min": "MIN", "p95": "PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY"}.get(aggregation, "AVG")
        level, entity_id = self._parse_scope(scope)

        where_clauses = [
            f"timestamp >= '{time_start}'",
            f"timestamp <= '{time_end}'",
        ]
        if level == "row" and entity_id:
            where_clauses.append(f"row_id = '{entity_id}'")
        elif level == "rack" and entity_id:
            where_clauses.append(f"rack_id = '{entity_id}'")

        where_str = " AND ".join(where_clauses)

        # p95 needs special syntax in DuckDB
        if aggregation == "p95":
            agg_expr = f"PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY {metric})"
        else:
            agg_expr = f"{agg_fn}({metric})"

        group_by = ""
        select_extra = ""
        if level == "row":
            select_extra = "rack_id,"
            group_by = "GROUP BY rack_id"

        query = f"""
            SELECT {select_extra}
                   {agg_expr} AS value,
                   COUNT(*) AS n_readings
            FROM thermal
            WHERE {where_str}
            {group_by}
            ORDER BY value DESC
            LIMIT 20
        """

        try:
            result = self._con.execute(query).df()
            if result.empty:
                return QueryResult(error="No data found for the specified scope and time range")

            data = result.to_dict(orient="records")
            meta = {
                "metric": metric,
                "aggregation": aggregation,
                "scope": scope,
                "time_start": time_start,
                "time_end": time_end,
                "n_results": len(data),
            }
            return QueryResult(data=data, metadata=meta)

        except Exception as e:
            logger.error(f"Thermal query failed: {e}\nQuery: {query}")
            return QueryResult(error=str(e))

    def query_power(
        self,
        time_start: str,
        time_end: str,
        scope: str,
        metric: str = "pdu_kw",
        aggregation: str = "avg",
    ) -> QueryResult:
        level, entity_id = self._parse_scope(scope)

        where_clauses = [
            f"timestamp >= '{time_start}'",
            f"timestamp <= '{time_end}'",
        ]
        if level == "row" and entity_id:
            where_clauses.append(f"row_id = '{entity_id}'")
        elif level == "rack" and entity_id:
            where_clauses.append(f"rack_id = '{entity_id}'")

        where_str = " AND ".join(where_clauses)

        if aggregation == "timeseries":
            query = f"""
                SELECT DATE_TRUNC('hour', timestamp::TIMESTAMP) AS hour,
                       {'SUM' if metric != 'pue' else 'AVG'}({metric}) AS value
                FROM power
                WHERE {where_str}
                GROUP BY 1
                ORDER BY 1
            """
        elif aggregation == "sum":
            query = f"SELECT SUM({metric}) AS value FROM power WHERE {where_str}"
        else:
            agg_fn = {"avg": "AVG", "max": "MAX", "min": "MIN"}.get(aggregation, "AVG")
            query = f"SELECT {agg_fn}({metric}) AS value FROM power WHERE {where_str}"

        try:
            result = self._con.execute(query).df()
            return QueryResult(
                data=result.to_dict(orient="records"),
                metadata={"metric": metric, "aggregation": aggregation, "scope": scope},
            )
        except Exception as e:
            logger.error(f"Power query failed: {e}")
            return QueryResult(error=str(e))

    def query_anomalies(
        self,
        time_start: str,
        time_end: str,
        severity: str = "all",
        scope: str = "facility",
    ) -> QueryResult:
        """
        Detect anomalies via simple threshold rules.
        Real anomaly detection would use the forecaster residuals,
        but this is a quick heuristic for the prototype.

        ASHRAE A2 inlet limit: 35°C. Warning at 28°C.
        """
        query = f"""
            SELECT rack_id, row_id,
                   MAX(inlet_temp_c) AS max_inlet_c,
                   MAX(outlet_temp_c) AS max_outlet_c,
                   CASE
                       WHEN MAX(inlet_temp_c) >= 32 THEN 'critical'
                       WHEN MAX(inlet_temp_c) >= 28 THEN 'warning'
                       ELSE 'ok'
                   END AS severity
            FROM thermal
            WHERE timestamp >= '{time_start}'
              AND timestamp <= '{time_end}'
            GROUP BY rack_id, row_id
            HAVING MAX(inlet_temp_c) >= 28
            ORDER BY max_inlet_c DESC
            LIMIT 50
        """
        try:
            result = self._con.execute(query).df()
            if severity != "all":
                result = result[result["severity"] == severity]

            return QueryResult(
                data=result.to_dict(orient="records"),
                metadata={"scope": scope, "threshold_warning_c": 28, "threshold_critical_c": 32},
            )
        except Exception as e:
            return QueryResult(error=str(e))

    def resolve_time_reference(self, reference: str) -> QueryResult:
        """
        Convert relative time references to ISO timestamps.
        This runs locally — no LLM needed for this.

        Handles the most common cases. For anything complex,
        just ask the user to specify the time range directly.
        """
        now = datetime.utcnow()
        ref = reference.lower().strip()

        # common patterns
        mappings = {
            "today": (now.replace(hour=0, minute=0, second=0), now),
            "yesterday": (now.replace(hour=0, minute=0, second=0) - timedelta(days=1),
                          now.replace(hour=0, minute=0, second=0)),
            "this morning": (now.replace(hour=6, minute=0, second=0), now.replace(hour=12, minute=0, second=0)),
            "this afternoon": (now.replace(hour=12, minute=0, second=0), now.replace(hour=18, minute=0, second=0)),
            "last week": (now - timedelta(weeks=1), now),
            "past 24 hours": (now - timedelta(hours=24), now),
            "past 4 hours": (now - timedelta(hours=4), now),
            "past hour": (now - timedelta(hours=1), now),
        }

        for pattern, (start, end) in mappings.items():
            if pattern in ref:
                return QueryResult(data={
                    "time_start": start.isoformat(),
                    "time_end": end.isoformat(),
                    "resolved_from": reference,
                })

        # fallback: try to parse as "past N hours"
        import re
        m = re.search(r"past (\d+) hours?", ref)
        if m:
            hours = int(m.group(1))
            return QueryResult(data={
                "time_start": (now - timedelta(hours=hours)).isoformat(),
                "time_end": now.isoformat(),
                "resolved_from": reference,
            })

        return QueryResult(error=f"Could not resolve time reference: '{reference}'. Please provide an explicit date/time range.")
