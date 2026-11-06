# Job Applies → Google Sheet export: setup

The "Export to Google Sheet" button on a Job Applies run appends one row per job × profile to today's tab of a
shared Google Sheet. Each row has a **Download** link for the resume to send. The app writes through a Google
**service account**, so nobody signs in with Google.

## One-time setup (Google Cloud)

1. Open <https://console.cloud.google.com/> and pick any project.
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

| Profile | Company Name | Job Title | Job URL | Download Resume | Match Score | Band |
|---|---|---|---|---|---|---|

- **Tabs:** one tab per day, named like `Apply · 2026-11-06` (the date is in your timezone). It is created on first
  use with a bold, frozen header and the sort / filter dropdowns turned on. Later exports that day add to the same
  tab. The run's job checks go to their own `Checks · …` tab instead (**Export checks**).
- **Rows:** one per application with a resume ready to send: a job × a profile it is open to. Applications already
  marked applied are left out.
- **Resume:** the profile's tailored resume when it's ready. Otherwise its uploaded resume that meets the minimum
  score, which has to have its original file stored.
- **Match Score / Band:** the score (0–100) of the resume in that row against the job, and its band: **strong**
  (80+), **good** (65–79), **fair** (50–64) or **weak** (under 50). The score is a number, so sorting or filtering
  on it works numerically. It's blank if the resume couldn't be scored. When a re-tailored resume replaces a row's
  link, its score replaces the old one.
- **Rows left out:** applications still tailoring or needing a tailored resume (export again once they're done),
  uploaded resumes without a stored file, and rows already in this sheet. The dialog shows how many of each. Another
  sheet gets every row again.
- **Download links:** they work without logging in and expire after 30 days. Each downloads `<Company>.zip`
  holding that one resume.
- **Edits in the sheet:** nothing typed or ticked in the sheet syncs back into the app.
- **Tabs from before Match Score:** today's tab, if it already existed, gets the new header but no filter
  dropdowns (an existing tab is never changed beyond its header). Older tabs keep their five columns.
