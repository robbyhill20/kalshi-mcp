#!/usr/bin/env sh
set -eu
umask 077
# Exactly one worker. A persistent process lock also enforces this.
exec uvicorn src.server:create_app --factory --host 0.0.0.0 --port "${PORT:-8000}" \
  --workers 1 --no-access-log --limit-concurrency 40 --timeout-keep-alive 5
