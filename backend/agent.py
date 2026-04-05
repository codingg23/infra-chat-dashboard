"""
agent.py

Query understanding and response synthesis using Claude.

Design philosophy: Claude is the reasoning layer, NOT the data layer.
  1. Claude extracts structured intent from the natural language query
  2. We execute the query using real tools (DuckDB, etc.)
  3. Claude synthesises the results into a readable response

This means every number in the response is grounded in actual data.
No hallucinated statistics.

Tool calling approach: define a set of infrastructure query tools,
let Claude pick the right one(s) and fill in the parameters.
"""

import json
import logging
from typing import Optional
import anthropic

from .tools import InfraQueryTools, QueryResult

logger = logging.getLogger(__name__)

# The system prompt defines the assistant's role and constraints.
# Keeping it concise  -  too much in the system prompt and the model
# starts ignoring parts of it.
SYSTEM_PROMPT = """You are an infrastructure operations assistant for a data centre.
You help engineers query and understand power, thermal, and operational metrics.

When answering queries:
- Always use the available tools to retrieve real data
- Present numbers with appropriate units and context
- Flag anomalies or values approaching thresholds (e.g. ASHRAE limits)
- Be concise  -  operators are busy, they want the answer fast
- If something looks concerning, say so directly

You have access to tools that query real telemetry data. Use them.
Do not make up or estimate numbers if a tool can provide them.
"""

# Tool definitions for the API call
TOOLS = [
    {
        "name": "query_thermal_metrics",
        "description": "Query thermal sensor data (inlet/outlet temps, delta-T) for racks or rows. Use for questions about temperature, cooling, hot spots.",
        "input_schema": {
            "type": "object",
            "properties": {
                "time_start": {"type": "string", "description": "ISO 8601 start time, e.g. '2024-03-15T14:00:00'"},
                "time_end": {"type": "string", "description": "ISO 8601 end time"},
                "scope": {"type": "string", "description": "Scope: 'facility', 'row:ROW-07', 'rack:R07-12', etc."},
                "metric": {"type": "string", "enum": ["inlet_temp_c", "outlet_temp_c", "delta_t_c"], "description": "Which thermal metric"},
                "aggregation": {"type": "string", "enum": ["avg", "max", "min", "p95"], "default": "avg"},
            },
            "required": ["time_start", "time_end", "scope", "metric"],
        },
    },
    {
        "name": "query_power_metrics",
        "description": "Query power draw data (kW, PUE) for racks, rows, or facility level.",
        "input_schema": {
            "type": "object",
            "properties": {
                "time_start": {"type": "string"},
                "time_end": {"type": "string"},
                "scope": {"type": "string", "description": "Scope: 'facility', 'row:ROW-03', 'rack:R03-07'"},
                "metric": {"type": "string", "enum": ["pdu_kw", "facility_kw", "pue"], "default": "pdu_kw"},
                "aggregation": {"type": "string", "enum": ["avg", "sum", "max", "timeseries"], "default": "avg"},
            },
            "required": ["time_start", "time_end", "scope", "metric"],
        },
    },
    {
        "name": "query_anomalies",
        "description": "Retrieve recently detected anomalies or threshold breaches. Use for 'what's wrong', 'any alerts', 'what's causing X'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "time_start": {"type": "string"},
                "time_end": {"type": "string"},
                "severity": {"type": "string", "enum": ["all", "warning", "critical"], "default": "all"},
                "scope": {"type": "string", "default": "facility"},
            },
            "required": ["time_start", "time_end"],
        },
    },
    {
        "name": "resolve_time_reference",
        "description": "Convert relative time references to absolute timestamps. Use this first if the query has relative times like 'today', 'last week', 'this afternoon', 'yesterday'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reference": {"type": "string", "description": "The relative time expression, e.g. 'this afternoon', 'last week', 'past 4 hours'"},
            },
            "required": ["reference"],
        },
    },
]


class InfraAgent:
    """
    Orchestrates the query understanding → data retrieval → response synthesis pipeline.

    Stateless per query for now  -  no session memory. That's a TODO.
    """

    def __init__(self, data_path: str, model: str = "claude-3-5-sonnet-20240620"):
        self.client = anthropic.Anthropic()
        self.model = model
        self.query_tools = InfraQueryTools(data_path)

    def _execute_tool(self, tool_name: str, tool_input: dict) -> QueryResult:
        """Route tool call to the appropriate query function."""
        dispatch = {
            "query_thermal_metrics": self.query_tools.query_thermal,
            "query_power_metrics": self.query_tools.query_power,
            "query_anomalies": self.query_tools.query_anomalies,
            "resolve_time_reference": self.query_tools.resolve_time_reference,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return QueryResult(error=f"Unknown tool: {tool_name}")
        try:
            return fn(**tool_input)
        except Exception as e:
            logger.exception(f"Tool {tool_name} failed with input {tool_input}")
            return QueryResult(error=str(e))

    def query(self, user_message: str, max_tool_rounds: int = 4) -> str:
        """
        Process a natural language query and return a response.

        Runs the agentic loop: Claude calls tools until it has enough
        information, then synthesises the response.

        max_tool_rounds: safety limit to prevent infinite loops.
        In practice it's almost always 1-2 rounds.
        """
        messages = [{"role": "user", "content": user_message}]

        for round_num in range(max_tool_rounds):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            logger.debug(f"Round {round_num + 1}: stop_reason={response.stop_reason}")

            if response.stop_reason == "end_turn":
                # Claude is done  -  extract the text response
                text_blocks = [b.text for b in response.content if hasattr(b, "text")]
                return "\n".join(text_blocks)

            if response.stop_reason != "tool_use":
                # unexpected stop reason
                logger.warning(f"Unexpected stop_reason: {response.stop_reason}")
                break

            # execute all tool calls in this round
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                logger.info(f"Executing tool: {block.name}({json.dumps(block.input)[:120]})")
                result = self._execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result.to_json(),
                })

            # append assistant response + tool results to conversation
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        return "Sorry, I couldn't complete that query. Please try rephrasing."
