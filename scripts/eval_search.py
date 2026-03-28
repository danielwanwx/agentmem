#!/usr/bin/env python3
"""Search quality evaluation script.

Loads test documents, runs queries, measures Precision@5.
Usage: python scripts/eval_search.py
Output: P@5: 0.XX (single line, parseable by autoresearch)
"""
import json
import sys
import tempfile
from pathlib import Path

from agent_memory.store import MemoryStore


# Test corpus: 20 documents across different domains
DOCUMENTS = [
    # Kafka cluster
    {"title": "Kafka consumer rebalance timeout", "content": "- **consumer.timeout**: 300s default\n- **rebalance.backoff**: 5s\nDecision: Use 600s for large clusters", "source": "architectural_decision", "project": "data-pipeline"},
    {"title": "Kafka producer batching config", "content": "- **batch.size**: 16384 bytes\n- **linger.ms**: 5ms\n- **compression**: snappy", "source": "technical_insight", "project": "data-pipeline"},
    {"title": "Kafka topic retention policy", "content": "- **retention.ms**: 604800000 (7 days)\n- **cleanup.policy**: delete\nDecision: Use compact for changelog topics", "source": "architectural_decision", "project": "data-pipeline"},

    # YARN/Spark
    {"title": "YARN executor memory hard limit", "content": "- **executor_memory_hard**: 4GB\n- **driver_memory**: 2GB\nDecision: Use hard limits, YARN ignores soft limits silently", "source": "architectural_decision", "project": "data-pipeline"},
    {"title": "Spark shuffle service configuration", "content": "- **spark.shuffle.service.enabled**: true\n- **spark.dynamicAllocation**: enabled", "source": "technical_insight", "project": "data-pipeline"},
    {"title": "YARN container OOM debugging", "content": "Container killed by YARN without error. Root cause: soft memory limit. Fix: use executor_memory_hard.", "source": "debug_solution", "project": "data-pipeline"},

    # Database
    {"title": "PostgreSQL connection pool settings", "content": "- **pool_size**: 20\n- **max_overflow**: 10\n- **pool_timeout**: 30s\nDecision: Use SQLAlchemy pool with pre-ping", "source": "architectural_decision", "project": "backend"},
    {"title": "PostgreSQL vacuum tuning", "content": "- **autovacuum_vacuum_cost_delay**: 2ms\n- **autovacuum_vacuum_scale_factor**: 0.1", "source": "technical_insight", "project": "backend"},
    {"title": "Redis cache invalidation strategy", "content": "- **TTL**: 3600s default\n- **pub/sub channel**: cache-invalidate\nDecision: TTL-based over event-driven for simplicity", "source": "architectural_decision", "project": "backend"},

    # API/Auth
    {"title": "JWT token rotation policy", "content": "- **access_token_ttl**: 15 minutes\n- **refresh_token_ttl**: 7 days\nDecision: Short-lived access + long refresh", "source": "architectural_decision", "project": "auth-service"},
    {"title": "Rate limiting configuration", "content": "- **rate_limit**: 100 req/min per user\n- **burst**: 20\n- **algorithm**: sliding window", "source": "technical_insight", "project": "auth-service"},
    {"title": "OAuth2 PKCE flow debugging", "content": "Code verifier was not URL-safe base64. Fix: use base64url encoding without padding.", "source": "debug_solution", "project": "auth-service"},

    # Frontend
    {"title": "React query cache configuration", "content": "- **staleTime**: 5 minutes\n- **gcTime**: 30 minutes\nDecision: Use staleTime to reduce refetches", "source": "technical_insight", "project": "frontend"},
    {"title": "CSS grid dashboard layout", "content": "- **grid-template-columns**: repeat(auto-fit, minmax(300px, 1fr))\n- **gap**: 16px", "source": "session_note", "project": "frontend"},
    {"title": "Webpack bundle size optimization", "content": "- **splitChunks**: vendor + async\n- **tree-shaking**: enabled\nReduced bundle from 2.4MB to 890KB", "source": "debug_solution", "project": "frontend"},

    # DevOps
    {"title": "Kubernetes pod memory limits", "content": "- **requests.memory**: 512Mi\n- **limits.memory**: 1Gi\nDecision: Always set both requests and limits", "source": "architectural_decision", "project": "infra"},
    {"title": "CI/CD pipeline caching strategy", "content": "- **cache key**: hash of lock file\n- **cache paths**: node_modules, .gradle\nReduced build time from 12min to 4min", "source": "technical_insight", "project": "infra"},
    {"title": "Docker layer optimization", "content": "- **multi-stage build**: yes\n- **COPY order**: deps first, code last\nReduced image from 1.2GB to 340MB", "source": "debug_solution", "project": "infra"},

    # Misc
    {"title": "Python logging with structlog", "content": "- **log_level**: INFO in prod\n- **format**: JSON\n- **processors**: add_log_level, TimeStamper, JSONRenderer", "source": "routine", "project": "backend"},
    {"title": "Git hooks pre-commit setup", "content": "- **hooks**: black, ruff, mypy\n- **config**: .pre-commit-config.yaml", "source": "session_note", "project": None},
]

# Queries with expected relevant doc titles (ground truth)
# Each query should match 1-3 specific docs
QUERIES = [
    {
        "query": "Kafka consumer timeout rebalance",
        "expected": ["Kafka consumer rebalance timeout"],
        "project": "data-pipeline",
    },
    {
        "query": "YARN memory OOM container killed",
        "expected": ["YARN container OOM debugging"],
        "project": "data-pipeline",
    },
    {
        "query": "PostgreSQL connection pool",
        "expected": ["PostgreSQL connection pool settings"],
        "project": "backend",
    },
    {
        "query": "cache invalidation TTL",
        "expected": ["Redis cache invalidation strategy"],
        "project": "backend",
    },
    {
        "query": "JWT token refresh access",
        "expected": ["JWT token rotation policy"],
        "project": "auth-service",
    },
    {
        "query": "rate limit sliding window",
        "expected": ["Rate limiting configuration"],
        "project": "auth-service",
    },
    {
        "query": "OAuth PKCE code verifier",
        "expected": ["OAuth2 PKCE flow debugging"],
        "project": "auth-service",
    },
    {
        "query": "bundle size webpack optimization",
        "expected": ["Webpack bundle size optimization"],
        "project": "frontend",
    },
    {
        "query": "Kubernetes pod memory requests limits",
        "expected": ["Kubernetes pod memory limits"],
        "project": "infra",
    },
    {
        "query": "Docker image size multi-stage",
        "expected": ["Docker layer optimization"],
        "project": "infra",
    },
    {
        "query": "CI build time caching",
        "expected": ["CI/CD pipeline caching strategy"],
        "project": "infra",
    },
    {
        "query": "Spark shuffle dynamic allocation",
        "expected": ["Spark shuffle service configuration"],
        "project": "data-pipeline",
    },
    {
        "query": "executor memory hard soft limit",
        "expected": ["YARN executor memory hard limit", "YARN container OOM debugging"],
        "project": "data-pipeline",
    },
    {
        "query": "Kafka topic retention compact",
        "expected": ["Kafka topic retention policy"],
        "project": "data-pipeline",
    },
    {
        "query": "React query staleTime cache",
        "expected": ["React query cache configuration"],
        "project": "frontend",
    },
    {
        "query": "PostgreSQL autovacuum scale",
        "expected": ["PostgreSQL vacuum tuning"],
        "project": "backend",
    },
    {
        "query": "Python structlog JSON logging",
        "expected": ["Python logging with structlog"],
        "project": "backend",
    },
    {
        "query": "CSS grid dashboard layout",
        "expected": ["CSS grid dashboard layout"],
        "project": "frontend",
    },
    {
        "query": "pre-commit hooks black ruff",
        "expected": ["Git hooks pre-commit setup"],
        "project": None,
    },
    {
        "query": "Kafka batching compression producer",
        "expected": ["Kafka producer batching config"],
        "project": "data-pipeline",
    },
]


def evaluate():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "eval.db")
        store = MemoryStore(db_path=db_path)

        # Load corpus
        for doc in DOCUMENTS:
            store.save(
                title=doc["title"],
                content=doc["content"],
                source=doc["source"],
                project=doc["project"],
            )

        # Run queries and measure P@5
        total_precision = 0.0
        total_queries = len(QUERIES)
        hits = 0
        misses = 0

        for q in QUERIES:
            results = store.search(q["query"], max_results=5, project=q["project"])
            result_titles = [r.l1 for r in results]

            # Check how many expected docs are in the top-5 results
            found = 0
            for expected_title in q["expected"]:
                if any(expected_title in rt for rt in result_titles):
                    found += 1

            precision = found / len(q["expected"]) if q["expected"] else 0
            total_precision += precision
            if found == len(q["expected"]):
                hits += 1
            else:
                misses += 1

        avg_precision = total_precision / total_queries
        hit_rate = hits / total_queries

        store.close()

    # Output format parseable by autoresearch
    print(f"P@5: {avg_precision:.3f}")
    print(f"Hit_rate: {hit_rate:.3f}")
    print(f"Hits: {hits}/{total_queries}")
    return avg_precision


if __name__ == "__main__":
    score = evaluate()
    sys.exit(0 if score >= 0.5 else 1)
