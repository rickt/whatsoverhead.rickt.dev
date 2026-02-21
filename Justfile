set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

default:
  @just --list

env:
  @. ./.env && echo "ENDPOINT=${ENDPOINT} PROJECT=${GCP_PROJECT_ID} REGION=${GCP_REGION}"

build:
  ./scripts/build.sh

push:
  ./scripts/push.sh

deploy:
  ./scripts/deploy.sh

release:
  ./scripts/build.sh && ./scripts/push.sh && ./scripts/deploy.sh

run-backend:
  . ./.env && python -m uvicorn whatsoverhead:app --host 0.0.0.0 --port "${PORT}" --reload

get-secret:
  ./scripts/getsecret.sh

poll base='https://whatsoverhead.rickt.dev':
  curl -sS -X POST "{{base}}/poll" -H "X-Poll-Secret: $(./scripts/getsecret.sh)" | jq .

cached code='code=lax' base='https://whatsoverhead.rickt.dev':
  curl -sS "{{base}}/cached?{{code}}" | jq .

check:
  python -m py_compile whatsoverhead.py
  python -m json.tool config/airports.json >/dev/null
  bash -n scripts/deploy.sh
