#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="$root/tests/database/docker-compose.yml"
bundled_sample="$root/tests/fixtures/corpus_ohlcv_ETH-USDT-USDT_1m_sample.csv"
sibling_corpus="$root/../pineforge-engine/corpus/data/ohlcv_ETH-USDT-USDT_1m.csv"
corpus_path="${PINEFORGE_CORPUS_OHLCV:-$sibling_corpus}"
python_bin="${PYTHON:-python}"

if [[ ! -f "$corpus_path" ]]; then
  corpus_path="$bundled_sample"
fi
if [[ -z "${PYTHON:-}" && -x "$root/.venv/bin/python" ]]; then
  python_bin="$root/.venv/bin/python"
fi

cleanup() {
  docker compose --file "$compose_file" down --volumes --remove-orphans
}
trap cleanup EXIT

docker compose --file "$compose_file" up --detach --wait

mysql_port="$(docker compose --file "$compose_file" port mysql 3306 | awk -F: '{print $NF}')"
postgres_port="$(docker compose --file "$compose_file" port postgres 5432 | awk -F: '{print $NF}')"

export PINEFORGE_DATABASE_E2E=1
export PINEFORGE_CORPUS_OHLCV="$corpus_path"
export PINEFORGE_MYSQL_URL="mysql+pymysql://pineforge:pineforge@127.0.0.1:${mysql_port}/pineforge"
export PINEFORGE_POSTGRES_URL="postgresql+psycopg://pineforge:pineforge@127.0.0.1:${postgres_port}/pineforge"

"$python_bin" -m pytest -q tests/test_database_e2e.py
