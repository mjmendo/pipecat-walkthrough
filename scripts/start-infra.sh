#!/bin/bash
# Start Jaeger + Prometheus + Grafana for M5 observability

set -e

cd "$(dirname "$0")/.."

echo "Starting observability infrastructure..."
docker compose -f infra/docker-compose.yml up -d

echo ""
echo "Services:"
echo "  Jaeger UI:     http://localhost:16686"
echo "  Prometheus UI: http://localhost:9090"
echo "  Grafana:       http://localhost:3000 (admin/admin)"
echo "  OTLP endpoint: localhost:4317"
echo ""
echo "Pipecat will expose metrics at: http://localhost:8000/metrics"
echo "Run: python bots/tech-support/observability_server.py"
