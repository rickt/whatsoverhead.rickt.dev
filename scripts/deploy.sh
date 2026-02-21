#!/bin/bash
set -euo pipefail

. ./.env

REGION="${CLOUDRUN_REGION:-${GCP_REGION}}"
ENV_VARS="FIRESTORE_DB=${FIRESTORE_DB},FIRESTORE_COLLECTION=${FIRESTORE_COLLECTION},FIRESTORE_LOCK_DOC=${FIRESTORE_LOCK_DOC},POLL_SLEEP_MS=${POLL_SLEEP_MS},ACTIVE_VIEW_WINDOW_SECONDS=${ACTIVE_VIEW_WINDOW_SECONDS},AIRPORTS_CONFIG_PATH=${AIRPORTS_CONFIG_PATH},POLL_SECRET_NAME=${POLL_SECRET_NAME}"

echo "running: gcloud beta run deploy ${ENDPOINT} --region ${REGION} --image gcr.io/${GCP_PROJECT_ID}/${ENDPOINT} --port ${PORT} --cpu ${CLOUDRUN_CPU} --memory ${CLOUDRUN_MEMORY}Gi --max-instances ${CLOUDRUN_MAXINSTANCES} --min-instances ${CLOUDRUN_MININSTANCES} --concurrency ${CLOUDRUN_CONCURRENCY} --service-account ${GCP_SERVICE_ACCOUNT} --update-env-vars ${ENV_VARS} --allow-unauthenticated"
echo ""

gcloud beta run deploy "${ENDPOINT}" \
    --region "${REGION}" \
    --image "gcr.io/${GCP_PROJECT_ID}/${ENDPOINT}" \
    --port "${PORT}" \
    --cpu "${CLOUDRUN_CPU}" \
    --memory "${CLOUDRUN_MEMORY}Gi" \
    --max-instances "${CLOUDRUN_MAXINSTANCES}" \
    --min-instances "${CLOUDRUN_MININSTANCES}" \
    --concurrency "${CLOUDRUN_CONCURRENCY}" \
    --service-account "${GCP_SERVICE_ACCOUNT}" \
    --update-env-vars "${ENV_VARS}" \
    --allow-unauthenticated

# EOF
