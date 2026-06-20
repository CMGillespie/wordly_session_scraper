# Deploy: wordly-session-scraper
# Project: support-467322

## FILES REQUIRED IN THIS FOLDER BEFORE BUILDING
- wordly_session_scraper_v1_0_CLOUD.py  ✅
- Dockerfile                             ✅
- requirements.txt                       ✅
- wordly_creds.txt                       ← add this (not in git)
- slack_webhook.txt                      ← add this (not in git)

## STEP 1 — Build and push container image
cd into this folder, then:

    gcloud builds submit \
      --tag gcr.io/support-467322/wordly-session-scraper \
      --project support-467322

## STEP 2 — Create the Cloud Run job

    gcloud run jobs create wordly-session-scraper \
      --image gcr.io/support-467322/wordly-session-scraper \
      --project support-467322 \
      --region us-central1 \
      --task-timeout 7200 \
      --memory 2Gi \
      --cpu 2 \
      --max-retries 0

## STEP 3 — Trigger the March 1 → today backfill

    gcloud run jobs execute wordly-session-scraper \
      --region us-central1 \
      --project support-467322 \
      --update-env-vars START_DATE=03/01/2026,END_DATE=05/05/2026

## STEP 4 — Schedule nightly (runs 2am UTC = 7pm PDT)
Replace YOUR_SERVICE_ACCOUNT with the service account from the
wordly-usage-sync job (same project, reuse it).

    gcloud scheduler jobs create http wordly-session-scraper-nightly \
      --schedule "0 2 * * *" \
      --uri "https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/support-467322/jobs/wordly-session-scraper:run" \
      --oauth-service-account-email YOUR_SERVICE_ACCOUNT@support-467322.iam.gserviceaccount.com \
      --location us-central1 \
      --project support-467322

## NIGHTLY RUN (no env vars needed — uses defaults)
Once scheduled, runs automatically. To trigger manually:

    gcloud run jobs execute wordly-session-scraper \
      --region us-central1 \
      --project support-467322

## SECRETS CHECKLIST
- wordly_creds.txt  : Wordly portal admin email + password
- slack_webhook.txt : Slack incoming webhook URL
