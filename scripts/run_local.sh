#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

# Preserve caller-provided overrides before sourcing .env.
ORIG_HOST="${HOST-}"
ORIG_PORT="${PORT-}"
ORIG_RELOAD="${RELOAD-}"
ORIG_LOG_LEVEL="${LOG_LEVEL-}"
ORIG_PYTHON_BIN="${PYTHON_BIN-}"
ORIG_GCP_PROJECT_ID="${GCP_PROJECT_ID-}"
ORIG_GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT-}"
ORIG_GCLOUD_PROJECT="${GCLOUD_PROJECT-}"
ORIG_POLL_SHARED_SECRET="${POLL_SHARED_SECRET-}"
ORIG_POLL_SECRET_NAME="${POLL_SECRET_NAME-}"

if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . ".env"
  set +a
fi

# Re-apply caller-provided overrides.
[[ -n "${ORIG_HOST}" ]] && HOST="${ORIG_HOST}"
[[ -n "${ORIG_PORT}" ]] && PORT="${ORIG_PORT}"
[[ -n "${ORIG_RELOAD}" ]] && RELOAD="${ORIG_RELOAD}"
[[ -n "${ORIG_LOG_LEVEL}" ]] && LOG_LEVEL="${ORIG_LOG_LEVEL}"
[[ -n "${ORIG_PYTHON_BIN}" ]] && PYTHON_BIN="${ORIG_PYTHON_BIN}"
[[ -n "${ORIG_GCP_PROJECT_ID}" ]] && GCP_PROJECT_ID="${ORIG_GCP_PROJECT_ID}"
[[ -n "${ORIG_GOOGLE_CLOUD_PROJECT}" ]] && GOOGLE_CLOUD_PROJECT="${ORIG_GOOGLE_CLOUD_PROJECT}"
[[ -n "${ORIG_GCLOUD_PROJECT}" ]] && GCLOUD_PROJECT="${ORIG_GCLOUD_PROJECT}"
[[ -n "${ORIG_POLL_SHARED_SECRET}" ]] && POLL_SHARED_SECRET="${ORIG_POLL_SHARED_SECRET}"
[[ -n "${ORIG_POLL_SECRET_NAME}" ]] && POLL_SECRET_NAME="${ORIG_POLL_SECRET_NAME}"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8080}"
RELOAD="${RELOAD:-1}"
LOG_LEVEL="${LOG_LEVEL:-info}"

if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
  PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

if [[ -n "${GCP_PROJECT_ID:-}" ]]; then
  export GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-${GCP_PROJECT_ID}}"
  export GCLOUD_PROJECT="${GCLOUD_PROJECT:-${GCP_PROJECT_ID}}"
fi

resolve_secret_name_and_project() {
  local raw_name="$1"
  local fallback_project="$2"
  local secret_name="$raw_name"
  local project_id="$fallback_project"

  if [[ "${raw_name}" == projects/*/secrets/* ]]; then
    # Accept:
    #   projects/<project>/secrets/<secret>
    #   projects/<project>/secrets/<secret>/versions/<version>
    project_id="$(echo "${raw_name}" | cut -d'/' -f2)"
    secret_name="$(echo "${raw_name}" | awk -F'/' '{for(i=1;i<=NF;i++) if($i=="secrets"){print $(i+1); exit}}')"
  fi

  echo "${secret_name}|${project_id}"
}

if [[ -z "${POLL_SHARED_SECRET:-}" && -n "${POLL_SECRET_NAME:-}" ]]; then
  if command -v gcloud >/dev/null 2>&1; then
    pair="$(resolve_secret_name_and_project "${POLL_SECRET_NAME}" "${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}")"
    secret_name="${pair%%|*}"
    project_id="${pair##*|}"

    if [[ -z "${secret_name}" || -z "${project_id}" ]]; then
      echo "error: POLL_SECRET_NAME is set, but project/secret resolution failed." >&2
      exit 1
    fi

    echo "loading POLL_SHARED_SECRET from Secret Manager (${project_id}/${secret_name})..."
    POLL_SHARED_SECRET="$(gcloud secrets versions access latest --secret="${secret_name}" --project="${project_id}")"
    export POLL_SHARED_SECRET
  else
    echo "warning: gcloud not found; POLL_SECRET_NAME was set but POLL_SHARED_SECRET could not be loaded." >&2
  fi
fi

echo "starting ${APP_NAME:-whatsoverhead} locally on http://${HOST}:${PORT}"
echo "python: ${PYTHON_BIN}"
echo "project: ${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-unset}}"
echo "firestore db: ${FIRESTORE_DB:-unset} collection: ${FIRESTORE_COLLECTION:-unset}"

UVICORN_ARGS=(
  "whatsoverhead:app"
  "--host" "${HOST}"
  "--port" "${PORT}"
  "--log-level" "${LOG_LEVEL}"
)

if [[ "${RELOAD}" == "1" ]]; then
  UVICORN_ARGS+=("--reload")
fi

exec "${PYTHON_BIN}" -m uvicorn "${UVICORN_ARGS[@]}"
