Job Tracker
I built this because I kept missing jobs. I'd sign up for a company's "talent community," and weeks later I'd find a posting on their actual website that I never got an email about. Checking 30+ career pages by hand every day wasn't realistic either, so I built something to do it for me.
Every morning it checks a list of companies I care about, looks for new postings that match what I'm actually looking for (keywords + location), and emails me if there's anything new. If there's nothing new, it stays quiet. Runs automatically through GitHub Actions, so I don't have to do anything once it's set up.
How it actually works
Most companies don't build their own career site from scratch. They run on top of a handful of platforms (Workday, Greenhouse, Lever, iCIMS, Ashby, Workable). A few of those platforms expose their job listings as clean, public data if you know where to look, which means I don't have to scrape or parse a rendered webpage at all. I can just ask for the data directly.
Right now this covers 35 companies:
Workday (25 companies)
Greenhouse (8 companies)
Lever (1 company)
Ashby (1 company)
Every run does the same thing, company by company:
Pull the current job list.
Check the job title against my keyword list (title only; checking full descriptions caused false matches early on, more on that below).
Check the location against my location list.
If it's a match, tag it as likely entry-level, likely experienced, or unclear, based on scanning for phrases like "senior" or "entry level."
Drop anything I've already been emailed about before.
Email me whatever's left.
The limitations
This only works for companies running on a platform I've actually built support for. If a company runs something else (I ran into Avature, Eightfold, Phenom, SAP SuccessFactors, and a handful of fully custom-built sites), this tool is completely blind to it. Not partially, not slower, just doesn't see it at all. Some of those platforms don't expose any public data no matter how you ask; JavaScript loads the job listings into the page after the fact, so there's nothing there to grab until a real browser opens it.
iCIMS is a partial case worth calling out specifically: it does have a workaround (it auto-generates a sitemap file with every job title baked into the URL), but every company I actually tried it against blocked the automated request anyway. So the code for it exists in job_tracker.py, but nothing in config.json currently uses it.
If a company matters to you and it's not on this list, the honest answer is either it's not supported yet, or it can't be. Check the note below on adding your own companies to figure out which.
Using this for your own job search
Everything you'd want to change lives in config.json. You shouldn't need to touch job_tracker.py unless you're changing how it behaves, not what it's looking for.
companies: swap this out for your own target list.
keywords: what has to appear in a job title for it to count as a match. Mine are analyst/data-flavored; change these to whatever fits your field.
entry_level_signals / experienced_signals: phrases used to tag a posting by seniority. This is just a label, not a filter, unless you turn on exclude_experienced (see below).
exclude_experienced: set this to true to stop getting emailed about anything tagged "likely experienced." I turned this on once I trusted the tagging enough.
Per-company location_filter: each company entry has its own list of locations to match against. Change these to your city/region.
Want to add a company that's not on the list? Check their careers page URL. If it contains greenhouse.io, jobs.lever.co, myworkdayjobs.com, jobs.ashbyhq.com, or apply.workable.com, it's probably supported. Open job_tracker.py and look at how the existing companies for that platform are set up in config.json, then copy the pattern. If it's none of those, it's likely not supported without more work.
One-time setup
Push this to your own GitHub repo.
Create a Gmail App Password (regular passwords won't work here): go to https://myaccount.google.com/apppasswords (needs 2-Step Verification on), create one, save the 16-character code.
Add three GitHub Secrets, under Settings, then Secrets and variables, then Actions:
EMAIL_ADDRESS: your Gmail address
EMAIL_APP_PASSWORD: the code from step 2
EMAIL_TO: where you want the digest sent
Test it manually: Actions tab, then "Job Tracker," then "Run workflow." There's also a "Full report" checkbox on that same screen, which ignores your history and shows every current match. Useful the first time you run it, or any time you want a full snapshot instead of just what's new.
Let it run: it's scheduled for 12:00 UTC (8am Eastern) daily, set in .github/workflows/job-check.yml if you want a different time.

