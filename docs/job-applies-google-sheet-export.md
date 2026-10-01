# Job Applies → Google Sheet export: setup

The "Export to Google Sheet" button on a Job Applies run appends one row per job × profile to today's tab of a
shared Google Sheet. Each row has a **Download** link for the resume to send. The app writes through a Google
**service account**, so nobody signs in with Google.

## One-time setup (Google Cloud)

1. Open <https://console.cloud.google.com/> and pick the project the app already uses for Gmail (or any project).
2. **APIs & Services → Library →** search **Google Sheets API → Enable**.
3. **IAM & Admin → Service Accounts → Create service account**.
   - Name: `job-applies-export`.
   - Skip the optional role and user steps, then click **Done**.
4. Open the new service account and go to **Keys → Add key → Create new key → JSON**. A `.json` file downloads.
   Keep it secret.
5. Set the backend environment variable `GOOGLE_SERVICE_ACCOUNT_JSON` to the **contents** of that file (the
   whole JSON on one line), or to a path to the file on the server.
   - On Railway: Variables → New variable → paste the JSON.
   - Locally: add it to `engineers_backend/.env`.
6. Set `APP_BASE_URL` to the backend's **public** URL, for example `https://api.example.com`. The Download links
   in the sheet point there.
7. Restart the backend.

## Each sheet (once)

1. Create a Google Sheet (or use an existing one) and **Share** it with the service account's email, for example
   `job-applies-export@your-project.iam.gserviceaccount.com`, as **Editor**.
2. In Job Applies, click **Export to Google Sheet** and paste the sheet link. The app remembers it for the next
   time.

## What gets written

| Date | Profile | Company | Title | Job URL | Resume | Resume type | Score | Posted | Applied |
|---|---|---|---|---|---|---|---|---|---|

- **Tabs:** one tab per day, named like `2026-10-02`. It is created on first use, with a bold frozen header and
  an **Applied** checkbox column. Later exports that day append to the same tab.
- **Rows:** one per job still to apply to × profile the job is open to.
- **Resume:** the profile's tailored resume when it's ready. Otherwise its uploaded resume that meets the
  threshold, which has to have its original PDF stored in S3.
- **Rows left out:** jobs still tailoring, with nothing to apply with, or already exported. The dialog shows how
  many of each.
- **Download links:** they work without logging in and expire after 30 days. Each opens only that one PDF.
- **Applied ticks:** ticking Applied in the sheet does not sync back into the app.
