"""Prometheus metrics: what to graph and alert on. Each process serves its own /metrics.

    METRICS_PORT=9100   # the bot and every worker expose http://<host>:9100/metrics (0 = off)

Counters are cheap and never raise; label values are bounded (task names, models, Bot API
methods), never chat ids.
"""

from __future__ import annotations

import logging

from prometheus_client import Counter, Histogram, start_http_server

log = logging.getLogger(__name__)

JOBS = Counter("english_agent_jobs_total", "Jobs finished, by task and outcome", ["task", "outcome"])
JOB_SECONDS = Histogram(
    "english_agent_job_duration_seconds",
    "Job run time",
    ["task"],
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1200),
)
LLM_TOKENS = Counter("english_agent_llm_tokens_total", "Claude API tokens", ["model", "kind"])
LLM_COST = Counter("english_agent_llm_cost_usd_total", "Estimated Claude API cost in USD", ["model"])
LLM_ERRORS = Counter("english_agent_llm_errors_total", "Claude API calls that failed after SDK retries", ["kind"])
TELEGRAM_REQUESTS = Counter(
    "english_agent_telegram_requests_total", "Bot API requests, by method and outcome", ["method", "outcome"]
)
TELEGRAM_FLOOD_WAIT = Counter(
    "english_agent_telegram_retry_after_seconds_total", "Seconds waited because Telegram answered 429"
)
LESSONS_SENT = Counter("english_agent_lessons_sent_total", "Lessons delivered to a learner", ["cefr"])
LESSONS_FAILED = Counter("english_agent_lessons_failed_total", "Lesson attempts that failed", ["final"])
ALERTS = Counter("english_agent_alerts_total", "Operational alerts raised", ["kind"])


def serve_metrics(port: int) -> None:
    """Start the /metrics endpoint in a background thread of this process (no-op for port 0)."""
    if port <= 0:
        return
    start_http_server(port)
    log.info("metrics on :%d/metrics", port)
