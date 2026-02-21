#! /bin/bash

. ./.env

docker build --platform=linux/amd64 -t gcr.io/${GCP_PROJECT_ID}/${ENDPOINT} .

# EOF
