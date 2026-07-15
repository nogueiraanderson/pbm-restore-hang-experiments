#!/bin/bash
# End-to-end A/B for the storage-deadline fix (see README, experiment 2).
#
# Builds a FileStat probe from pristine PBM v2.11.0 and from the patched
# tree, runs both against a TCP black hole (accepts, never responds), and,
# when docker is available, against a real MinIO for the no-regression
# check. Fails closed: exits non-zero unless every expected outcome holds.
#
# Env overrides:
#   CUTOFF      seconds before a blocked probe is declared hung (default 300)
#   MINIO_HOST  host where MinIO's published port is reachable (default
#               127.0.0.1; set when DOCKER_HOST points at a remote engine)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly WORK_DIR="${SCRIPT_DIR}/work"
readonly PBM_DIR="${WORK_DIR}/percona-backup-mongodb"
readonly PBM_TAG="v2.11.0"
readonly BLACKHOLE_PORT=19999
readonly MINIO_CONTAINER="pbm-lab-minio"
CUTOFF="${CUTOFF:-300}"

# With a remote docker engine, published ports live on the engine host, not
# on 127.0.0.1. Derive the probe target from DOCKER_HOST unless overridden.
if [[ -z "${MINIO_HOST:-}" && "${DOCKER_HOST:-}" =~ ^tcp://([^:/]+) ]]; then
  MINIO_HOST="${BASH_REMATCH[1]}"
fi
MINIO_HOST="${MINIO_HOST:-127.0.0.1}"

BLACKHOLE_PID=""
FAILURES=0

err() {
  echo "ERROR: $*" >&2
}

check() {
  local name="$1"
  local ok="$2"
  local detail="$3"

  if [[ "${ok}" == "yes" ]]; then
    printf '%-38s %-6s %s\n' "${name}" "PASS" "${detail}"
  else
    printf '%-38s %-6s %s\n' "${name}" "FAIL" "${detail}"
    FAILURES=$((FAILURES + 1))
  fi
}

cleanup() {
  if [[ -n "${BLACKHOLE_PID}" ]]; then
    kill "${BLACKHOLE_PID}" 2>/dev/null || true
  fi

  if command -v docker >/dev/null; then
    docker rm -f "${MINIO_CONTAINER}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

preflight() {
  local tool

  for tool in git go python3; do
    command -v "${tool}" >/dev/null || { err "missing required tool: ${tool}"; exit 1; }
  done
}

prepare_tree_and_probes() {
  mkdir -p "${WORK_DIR}"

  if [[ ! -d "${PBM_DIR}" ]]; then
    echo "Cloning percona-backup-mongodb ${PBM_TAG}..."
    git clone --quiet --depth 1 --branch "${PBM_TAG}" \
      https://github.com/percona/percona-backup-mongodb "${PBM_DIR}"
  else
    echo "Reusing existing clone; resetting to pristine..."
    (cd "${PBM_DIR}" && git checkout -- . && rm -rf probe)
  fi

  cp -r "${SCRIPT_DIR}/probe" "${PBM_DIR}/probe"

  echo "Building pristine probe..."
  (cd "${PBM_DIR}" && go build -o "${WORK_DIR}/probe-pristine" ./probe)

  echo "Applying patches..."
  (cd "${PBM_DIR}" && git apply "${SCRIPT_DIR}"/patches/*.patch)

  echo "Building fixed probe..."
  (cd "${PBM_DIR}" && go build -o "${WORK_DIR}/probe-fixed" ./probe)
}

run_blackhole_test() {
  echo "Black-hole A/B (cutoff ${CUTOFF}s; pristine is expected to hang)..."
  python3 "${SCRIPT_DIR}/blackhole.py" "${BLACKHOLE_PORT}" &
  BLACKHOLE_PID=$!
  sleep 1

  local endpoint="http://127.0.0.1:${BLACKHOLE_PORT}"
  local pristine_rc=0
  local fixed_rc=0

  timeout "${CUTOFF}" "${WORK_DIR}/probe-pristine" "${endpoint}" \
    > "${WORK_DIR}/blackhole-pristine.txt" 2>&1 &
  local pristine_pid=$!

  timeout "${CUTOFF}" "${WORK_DIR}/probe-fixed" "${endpoint}" \
    > "${WORK_DIR}/blackhole-fixed.txt" 2>&1 &
  local fixed_pid=$!

  wait "${pristine_pid}" || pristine_rc=$?
  wait "${fixed_pid}" || fixed_rc=$?

  kill "${BLACKHOLE_PID}" 2>/dev/null || true
  BLACKHOLE_PID=""

  local pristine_ok="no"
  if [[ "${pristine_rc}" -eq 124 ]]; then
    pristine_ok="yes"
  fi
  check "pristine blocks (the bug)" "${pristine_ok}" \
    "rc=${pristine_rc}, still blocked when killed at ${CUTOFF}s"

  local fixed_ok="no"
  if [[ "${fixed_rc}" -eq 0 ]] \
    && grep -q "context deadline exceeded" "${WORK_DIR}/blackhole-fixed.txt"; then
    fixed_ok="yes"
  fi
  check "fixed errors within deadline" "${fixed_ok}" \
    "$(cat "${WORK_DIR}/blackhole-fixed.txt")"
}

run_happy_path() {
  if ! command -v docker >/dev/null; then
    printf '%-38s %-6s %s\n' "happy path vs real MinIO" "SKIP" "docker not available"
    return
  fi

  echo "Happy path against real MinIO..."
  docker rm -f "${MINIO_CONTAINER}" >/dev/null 2>&1 || true
  docker run -d --name "${MINIO_CONTAINER}" -p 9000:9000 \
    -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
    minio/minio server /data >/dev/null

  local attempt
  for attempt in $(seq 1 30); do
    if docker exec "${MINIO_CONTAINER}" \
      curl -sf http://127.0.0.1:9000/minio/health/live >/dev/null 2>&1; then
      break
    fi

    if [[ "${attempt}" -eq 30 ]]; then
      err "MinIO did not become healthy within 30s"
      exit 1
    fi
    sleep 1
  done

  # mc runs on the docker host's network, so it always targets 127.0.0.1;
  # the probes run here and target MINIO_HOST.
  docker run --rm --network host --entrypoint sh minio/mc -c \
    "mc alias set local http://127.0.0.1:9000 minioadmin minioadmin >/dev/null \
     && mc mb --ignore-existing local/probe >/dev/null \
     && echo 0123456789 | mc pipe local/probe/probe-object" >/dev/null

  local endpoint="http://${MINIO_HOST}:9000"
  local variant
  local output

  for variant in pristine fixed; do
    output="$(PROBE_ACCESS_KEY=minioadmin PROBE_SECRET_KEY=minioadmin \
      timeout 60 "${WORK_DIR}/probe-${variant}" "${endpoint}" 2>&1)" || true

    local ok="no"
    if [[ "${output}" == *"err=<nil>"* ]]; then
      ok="yes"
    fi
    check "happy path (${variant})" "${ok}" "${output}"
  done

  docker rm -f "${MINIO_CONTAINER}" >/dev/null 2>&1 || true
}

build_fixed_agent() {
  echo "Building patched pbm-agent..."
  (cd "${PBM_DIR}" && go build -o "${WORK_DIR}/pbm-agent-fixed" ./cmd/pbm-agent)
  echo "Patched agent: ${WORK_DIR}/pbm-agent-fixed"
  echo "Note: patch 0001 also lowers hbFrameSec to 15 for fast testing;"
  echo "drop that hunk before building for anything production-like."
}

main() {
  local mode="${1:-test}"

  preflight
  prepare_tree_and_probes

  if [[ "${mode}" == "build" ]]; then
    build_fixed_agent
    return
  fi

  echo
  printf '%-38s %-6s %s\n' "CHECK" "RESULT" "DETAIL"
  run_blackhole_test
  run_happy_path
  echo

  if [[ "${FAILURES}" -gt 0 ]]; then
    err "${FAILURES} check(s) failed"
    exit 1
  fi

  echo "ALL CHECKS PASSED"
}

main "$@"
