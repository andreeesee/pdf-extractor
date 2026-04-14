#!/bin/bash
set -e

echo "?? Stopping and removing old containers..."
docker compose down --remove-orphans

echo "?? Cleaning up old images..."
docker image prune -f

echo "?? Rebuilding without cache..."
docker compose build --no-cache

echo "?? Starting containers..."
docker compose up -d --force-recreate

echo "? Done."
