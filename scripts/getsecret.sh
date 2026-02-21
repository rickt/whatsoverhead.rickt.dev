#! /bin/bash

gcloud secrets versions access latest --secret="whatsoverhead_poll_secret" --project="rickts-new-dev-project"

