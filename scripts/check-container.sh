#!/bin/sh
set -eu
container_id=$(docker run --detach --publish 127.0.0.1::8000 scopecheck:local)
trap 'docker rm --force "$container_id" >/dev/null' EXIT
test "$(docker exec "$container_id" id -u)" = 10001
docker exec "$container_id" python -c 'from scopecheck.datasets.splits import frozen_dataset; assert len(frozen_dataset("dev").cases) == 24; assert len(frozen_dataset("holdout").cases) == 12'
address=$(docker port "$container_id" 8000/tcp)
attempt=0
until response=$(curl --fail --silent "http://$address/health/live"); do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        docker logs "$container_id"
        exit 1
    fi
    sleep 1
done
test "$response" = '{"status":"ok"}'
printf 'Container UID 10001; GET /health/live: %s\n' "$response"
