# Job Tracker

Checks career pages for the companies in `config.json`, finds postings that
match your keywords, tags each as likely entry-level or not, and emails you
anything new since the last check. Runs automatically every morning via
GitHub Actions — no server needed.

## How it actually works

Almost every big company's "careers" page is a skin on top of one of a
handful of applicant tracking systems (ATS): **Greenhouse**, **Lever**, or
**Workday** are the three most common. Each of those platforms exposes the
job list as clean JSON data if you know the URL pattern — you don't need to
scrape/parse HTML at all. So instead of writing 25 custom scrapers, this
script has 3 fetcher functions (one per platform), and `config.json` just
tells it which platform + company ID to use for each company.

Every run:
1. Pull the current job list for each configured company.
2. Filter to jobs matching your keywords AND your location filters.
3. Drop anything already seen in a previous run (tracked in `seen_jobs.json`).
4. Tag what's left as "likely entry-level" / "likely experienced" / "unclear"
   based on title and description signals — nothing gets dropped, just labeled.
5. Email you a digest of what's new.
6. Save the updated seen-jobs list so tomorrow's run only flags genuinely new stuff.

## One-time setup

### 1. Create a GitHub repo
Push these files to a new **private** repo (Settings → make it private,
since your job search details don't need to be public).

### 2. Gmail App Password (so the script can send email as you)
Regular Gmail passwords won't work for this — you need an "app password":
1. Go to https://myaccount.google.com/apppasswords (requires 2-Step Verification turned on)
2. Create a new app password, name it "job-tracker"
3. Copy the 16-character password it gives you

### 3. Add GitHub Secrets
In your repo: Settings → Secrets and variables → Actions → New repository secret.
Add three:
- `EMAIL_ADDRESS` — your Gmail address
- `EMAIL_APP_PASSWORD` — the 16-character app password from step 2
- `EMAIL_TO` — where you want the digest sent (can be same as EMAIL_ADDRESS)

### 4. Test it manually
Go to the Actions tab in your repo → "Job Tracker" workflow → "Run workflow"
button. This runs it once immediately instead of waiting for tomorrow's
schedule, so you can confirm it works.

### 5. Let it run
It's scheduled for 12:00 UTC (8am Eastern) daily via the cron line in
`.github/workflows/job-check.yml`. Change that if you want a different time.

## Filling in the remaining companies

I've already confirmed and wired up 4 companies (Fidelity, Truist, Pendo,
Bandwidth) as working examples. The rest are marked `"ats": "TODO"` in
`config.json` — the script will skip them and print a warning until you fill
them in. Here's how to find each one, takes about a minute per company:

**Check if it's Greenhouse:**
Go to the company's "careers" or "join us" page and click through to the
actual job listings. If the URL becomes something like
`job-boards.greenhouse.io/companyname` or `boards.greenhouse.io/companyname`,
that's Greenhouse. Grab the slug after `.io/` — that's your `token`.
```json
{ "name": "Example Co", "ats": "greenhouse", "token": "companyname" }
```

**Check if it's Lever:**
Same idea — if the URL becomes `jobs.lever.co/companyname`, that's Lever.
```json
{ "name": "Example Co", "ats": "lever", "slug": "companyname" }
```

**Check if it's Ashby:**
Look for a URL like `jobs.ashbyhq.com/companyname`. Grab the slug after the last `/`.
```json
{ "name": "Example Co", "ats": "ashby", "slug": "companyname" }
```

**Check if it's Workable:**
Look for a URL like `apply.workable.com/companyname` or `companyname.workable.com`. Grab the slug.
```json
{ "name": "Example Co", "ats": "workable", "slug": "companyname" }
```

**Check if it's Workday:**
If the URL looks like `companyname.wd1.myworkdayjobs.com/SiteName` (the
`wd` number varies: wd1, wd3, wd5...), that's Workday. You need three pieces:
```json
{
  "name": "Example Co",
  "ats": "workday",
  "tenant": "companyname",
  "wd_host": "wd1",
  "site": "SiteName"
}
```
(`tenant` = the part before `.wdN`, `site` = the part after the domain)

**If it's none of these:** some companies (Deloitte, PwC, EY, KPMG, IBM,
Cisco, SAS are likely candidates from your list) run custom-built career
sites or other ATS platforms (iCIMS, SmartRecruiters, Phenom, Eightfold).
Those need a bit more digging, or a dedicated scraper. Leave those as
`"ats": "TODO"` for now and send me the careers URL — I can check the page
structure and add a fetcher for whichever platform it turns out to be.

## Adjusting keywords / signals

Everything's in `config.json`:
- `keywords` — broad terms that trigger a match (loose on purpose)
- `entry_level_signals` — phrases that tag a posting "likely entry-level"
- `experienced_signals` — phrases that tag a posting "likely experienced"
- Per-company `location_filter` — only match postings whose location text
  contains one of these strings

Nothing here filters postings *out* — it only adds keyword matching (which
jobs get emailed at all) and tagging (a label in the email). If you want it
looser, just add more keywords.
