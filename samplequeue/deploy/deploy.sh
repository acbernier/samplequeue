#!/usr/bin/env bash
#
# Deploy SampleQueue to Cloud Run with Cloud SQL and Google sign-in.
#
#   PROJECT=your-project-id ./deploy/deploy.sh
#
# Safe to re-run: every step either creates a thing or notices it exists.
#
# IAP attaches directly to the Cloud Run service (`--iap`), so there is no
# load balancer to build and no serverless NEG to wire up. Google terminates
# sign-in in front of the container; the app verifies what it is handed.
#
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${PROJECT:?set PROJECT to your GCP project id}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-samplequeue}"
DB_INSTANCE="${DB_INSTANCE:-samplequeue-db}"
DB_NAME="${DB_NAME:-samplequeue}"
DB_USER="${DB_USER:-samplequeue}"
DB_TIER="${DB_TIER:-db-f1-micro}"
ALLOWED_DOMAINS="${ALLOWED_DOMAINS:-broadinstitute.org}"
# Who may sign in. A whole domain, or repeat with user:someone@... instead.
IAP_MEMBER="${IAP_MEMBER:-domain:${ALLOWED_DOMAINS%%,*}}"

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

say "Project $PROJECT, region $REGION"
gcloud config set project "$PROJECT" >/dev/null
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"

say "Enabling APIs (slow the first time)"
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iap.googleapis.com

say "Artifact Registry repository"
# `run deploy --source` otherwise stops to ask whether it may create this,
# which blocks the script partway through.
gcloud artifacts repositories describe cloud-run-source-deploy \
  --location="$REGION" >/dev/null 2>&1 \
  || gcloud artifacts repositories create cloud-run-source-deploy \
       --repository-format=docker --location="$REGION" \
       --description="Images built by gcloud run deploy --source"

say "Cloud SQL instance ($DB_TIER) -- this takes several minutes to create"
if gcloud sql instances describe "$DB_INSTANCE" >/dev/null 2>&1; then
  echo "already exists"
else
  gcloud sql instances create "$DB_INSTANCE" \
    --database-version=POSTGRES_16 \
    --tier="$DB_TIER" \
    --region="$REGION" \
    --storage-auto-increase
fi

say "Database password"
if gcloud secrets describe samplequeue-db-password >/dev/null 2>&1; then
  echo "secret exists; reusing it"
  DB_PASSWORD="$(gcloud secrets versions access latest --secret=samplequeue-db-password)"
else
  DB_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  printf '%s' "$DB_PASSWORD" | gcloud secrets create samplequeue-db-password --data-file=-
fi

# Create or reset the role, then hand it a database it OWNS. Without
# ownership, schema creation fails with "must be owner of table samples".
if gcloud sql users list --instance="$DB_INSTANCE" --format='value(name)' | grep -qx "$DB_USER"; then
  gcloud sql users set-password "$DB_USER" --instance="$DB_INSTANCE" --password="$DB_PASSWORD"
else
  gcloud sql users create "$DB_USER" --instance="$DB_INSTANCE" --password="$DB_PASSWORD"
fi
gcloud sql databases describe "$DB_NAME" --instance="$DB_INSTANCE" >/dev/null 2>&1 \
  || gcloud sql databases create "$DB_NAME" --instance="$DB_INSTANCE"

CONNECTION_NAME="$(gcloud sql instances describe "$DB_INSTANCE" --format='value(connectionName)')"
# Cloud Run mounts the Cloud SQL socket under /cloudsql.
DATABASE_URL="postgresql://${DB_USER}:${DB_PASSWORD}@/${DB_NAME}?host=/cloudsql/${CONNECTION_NAME}"

say "Storing the connection string as a secret"
# Keeping it out of the env-var config means the password is not visible in
# the Cloud Run console, and sidesteps gcloud's comma-delimited parsing.
if gcloud secrets describe samplequeue-database-url >/dev/null 2>&1; then
  printf '%s' "$DATABASE_URL" | gcloud secrets versions add samplequeue-database-url --data-file=-
else
  printf '%s' "$DATABASE_URL" | gcloud secrets create samplequeue-database-url --data-file=-
fi
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
gcloud secrets add-iam-policy-binding samplequeue-database-url \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role=roles/secretmanager.secretAccessor >/dev/null

say "Building and deploying"
# Note: one --set-env-vars flag only. Passing it twice makes gcloud keep the
# last one and silently drop the rest.
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --platform managed \
  --add-cloudsql-instances "$CONNECTION_NAME" \
  --set-env-vars "ALLOWED_DOMAINS=${ALLOWED_DOMAINS},TRUST_IAP_HEADER=1" \
  --set-secrets "DATABASE_URL=samplequeue-database-url:latest" \
  --iap \
  --ingress all \
  --min-instances 0 \
  --max-instances 4 \
  --memory 1Gi \
  --cpu 1 \
  --timeout 900

say "Granting IAP access"
# IAP itself must be allowed to invoke the service...
gcloud run services add-iam-policy-binding "$SERVICE" --region "$REGION" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com" \
  --role=roles/run.invoker >/dev/null
# ...and these are the humans IAP will let through.
gcloud iap web add-iam-policy-binding \
  --resource-type=cloud-run --service="$SERVICE" --region="$REGION" \
  --member="$IAP_MEMBER" \
  --role=roles/iap.httpsResourceAccessor >/dev/null

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"

say "Deployed"
cat <<EOF

  $URL

Open it in a browser and sign in with your Google account. Two things left:

  1. Click "Refresh data" once. The app starts with no data and no columns,
     so the rule builder has nothing to offer until the first ingest runs.

  2. The app is trusting IAP's email header rather than verifying its
     signature -- safe only because nothing but IAP can reach it. The page
     shows a yellow banner with the exact audience string. Copy it and:

       gcloud run services update ${SERVICE} --region ${REGION} \\
         --set-env-vars "ALLOWED_DOMAINS=${ALLOWED_DOMAINS},IAP_AUDIENCE=<the string>" \\
         --remove-env-vars TRUST_IAP_HEADER

     The banner disappears once signatures are being checked.

Optional: load the three example processes so there is something to look at.

  gcloud run jobs create ${SERVICE}-seed --region ${REGION} \\
    --image "\$(gcloud run services describe ${SERVICE} --region ${REGION} --format='value(spec.template.spec.containers[0].image)')" \\
    --add-cloudsql-instances ${CONNECTION_NAME} \\
    --set-secrets DATABASE_URL=samplequeue-database-url:latest \\
    --command python --args seed_demo.py
  gcloud run jobs execute ${SERVICE}-seed --region ${REGION} --wait

EOF
