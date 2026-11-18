# Shorts Autopilot

A free-forever machine that finds footage you are allowed to publish, clips it,
captions it, adds a hook and schedules two YouTube Shorts every day, with no
clicking once it is set up.

Each run it tops up a folder of footage from public-domain and Creative Commons
libraries (no account, no key, no payment), picks an unused moment, cuts it,
transcribes the speech, re-cuts tight to the talking, burns word-timed captions
plus a gold hook at the top, writes a title and description with the credit the
licence requires, and uploads it to YouTube scheduled for your two daily slots.

Everything in it is free with no time limit:

| Job | What does it | Cost |
|---|---|---|
| Cutting, cropping, captions | ffmpeg | $0, open source |
| Speech to text | faster-whisper running on the runner | $0, open source |
| Caption font | DejaVu Sans, downloaded once and cached | $0 |
| Uploading and scheduling | YouTube Data API v3 | $0, no paid tier, 100 uploads/day |
| The daily trigger | GitHub Actions scheduled workflow | $0 on public repos |
| Storage of clip history | a small file committed to your repo | $0 |

No subscriptions, no trials, no credit card, nothing that expires.

---

## Where the clips come from

Automatically, from libraries that are free to publish from, forever. Each run
tops up `sources/` with footage whose licence allows commercial use and edits:

| Library | What is in it | Licence |
|---|---|---|
| [Internet Archive](https://archive.org) | public-domain newsreels, educational films, classic TV — all narrated, so they caption well | public domain / CC |
| [Wikimedia Commons](https://commons.wikimedia.org) | CC BY, CC0 and public-domain video | CC BY, CC0, public domain |
| [NASA](https://images.nasa.gov) | space footage (off by default: mostly no speech to caption) | public domain |

None of them needs an account, an API key or payment, and all three are permanent
public institutions — that is what makes "free forever" real rather than a free
tier that expires.

Three rules are enforced in code rather than left to trust:

- An item is only used if its **own metadata declares a licence**.
- **Non-commercial, no-derivatives and share-alike** licences are refused by
  default, because the channel earns money and every Short is an edit.
- The credit the licence requires is written into the description of every video
  automatically, read from the `.license.json` file saved beside each clip, and a
  permanent record of what came from where is kept in `state/feed.json`.

Footage is only taken from vetted public-domain collections that contain speech
(public-domain TV, educational films, newsreels, features), because a generic
search pulls in junk. Each window is then checked for how much talking there is
and how clearly it transcribed: a muddier window is swapped for another part of
the same film, and a film with no usable speech at all is skipped rather than
published as a caption-less Short — the next run simply picks another one.

This is also why nothing here points at other people's YouTube channels:
downloading and re-uploading someone else's video is infringement, and even with
the creator's permission YouTube's reused-content policy blocks monetisation of
lightly-edited clips. Public-domain and openly licensed footage has neither
problem.

Your own footage still works exactly as before: drop files into `sources/` and
they join the rotation, with no licence file needed.

---

## Setup

Four steps, about 20 minutes, once.

### 1. Footage: already handled

Nothing to do. The first run fills `sources/` from the libraries above, and each
download is deleted again once it has had time to be used, so the folder never
grows and there is nothing to top up by hand.

If you ever want to publish your own filming or footage you have permission for,
drop those files (mp4, mov, mkv, webm, m4v, avi, ogv, mpg) into `sources/` and
they are used alongside the automatic ones. To keep the repo small you can put
them in a Google Drive folder instead: share it as "anyone with the link", then
in the repo go to Settings → Secrets and variables → Actions → **Variables** and
add `DRIVE_FOLDER_ID` with the folder's id (the long string in its URL).

### 2. Get YouTube credentials (one time)

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create
   a project (any name).
2. **APIs & Services → Library** → search "YouTube Data API v3" → **Enable**.
3. **APIs & Services → Google Auth Platform** — Google renamed the old "OAuth
   consent screen" page. If it says *not configured yet*, click **Get started**:
   app name, your email as the support contact, choose **External** as the
   audience, then your email again as the developer contact.
4. **Branding** tab: fill in everything marked required — app name, user support
   email, developer contact email, **Application home page** and **Application
   privacy policy link**. Both addresses can point at your GitHub repository:
   `https://github.com/<your-name>/shorts-autopilot` and that same address plus
   `/blob/main/PRIVACY.md` (that file is in this folder). If an **Authorised
   domains** box appears, add `github.com`. Some accounts only demand these two
   links, so create the repository first if you are asked for them.
5. **Audience** tab: add your own Google account under **Test users**, then click
   **Publish app** → **Confirm** so the status reads **In production**. An
   unverified app is fine for your own channel; publishing is what stops your
   login expiring after a week.
6. **APIs & Services → Credentials → Create credentials → OAuth client ID →
   Desktop app** → **Download JSON**.
7. Put that file next to `setup_auth.py` and rename it `client_secrets.json`.
8. On your own computer:

   ```bash
   pip install google-auth-oauthlib
   python setup_auth.py
   ```

   A browser opens. Sign in with the Google account that owns the channel and
   click **Allow**. The script prints three values: `YT_CLIENT_ID`,
   `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`.

If you ever want AI-written hooks and titles instead of the built-in wording,
get a free Gemini API key from [aistudio.google.com](https://aistudio.google.com)
and keep it as `GEMINI_API_KEY` in step 3. Optional — without it, hooks and
titles are built from what is actually said in the clip.

### 3. Put it on GitHub

Create a new **public** repository (public = unlimited free automation minutes),
upload this folder's contents, then add the secrets:

Settings → Secrets and variables → Actions → **New repository secret**:

| Name | Value |
|---|---|
| `YT_CLIENT_ID` | from setup_auth.py |
| `YT_CLIENT_SECRET` | from setup_auth.py |
| `YT_REFRESH_TOKEN` | from setup_auth.py |
| `GEMINI_API_KEY` | optional |

Then open the **Actions** tab, pick `shorts-autopilot`, and click
**Run workflow** once to prove it works. You should get two Shorts in your
channel's Studio, sitting as scheduled (private until their slot).

### 4. Walk away

The workflow runs daily at 05:00 UTC and schedules the day's two Shorts at the
times in `config.yml`. Nothing else to do.

---

## Settings you might change

`config.yml`:

| Setting | Meaning |
|---|---|
| `posts_per_day` | how many Shorts per day (default 2) |
| `publish_times` | the daily slots, 24h clock (default 09:15 and 18:45) |
| `timezone` | your timezone, e.g. `Europe/London` |
| `clip_seconds` | length of each Short (default 32) |
| `whisper_model` | `base` (default), `tiny` (faster, worse), `small` (more accurate, slower) |
| `min_speech_ratio` | reject a window with less talking than this (0.35) |
| `min_speech_confidence` | reject a window whose captions would be garbled (0.55) |
| `source_attempts` | how many different files to try before giving up on a slot (3) |
| `credit` | extra attribution added to every description (fetched footage already carries its own) |
| `category_id` | 24 Entertainment, 22 People & Blogs, 20 Gaming |
| `feed.enabled` | fetch free-to-publish footage automatically (default on) |
| `feed.queries` | what the Internet Archive search looks for — change these to change the channel's subject |
| `feed.providers` | `internet_archive`, `wikimedia`, and/or `nasa` |
| `feed.max_download_mb` | size cap per downloaded clip (default 150) |
| `feed.keep_hours` | how long a download is kept before it is deleted (default 6) |

---

## What can stop it, and the fix

- **GitHub pauses schedules after 60 days with no repo activity.** Each run
  commits its clip history back to the repo, which normally counts as activity.
  If GitHub ever emails you that a workflow was disabled, open the Actions tab
  and click **Enable workflow** once.
- **The upload quota** is 100 uploads per day per Google project, shared by every
  channel you connect. Two a day will never reach it.
- **Google login expires** if the OAuth app is left in "Testing" — that is the
  7-day expiry. Publishing the app in step 2.3 fixes it permanently.
- **A run can fail** (network, YouTube hiccup). That day's Shorts are simply not
  scheduled; the next run carries on with fresh footage. There is nothing to top
  up by hand.
- **Slow runs**: free runners get about 2 CPU cores. Two Shorts take roughly
  3-5 minutes total. The workflow's limit is 55 minutes.

---

## Run it on your own machine

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python pipeline.py --dry-run --count 1
```

`--dry-run` renders everything into `output/` without uploading, which is the
safe way to see what the captions and hooks look like before anything goes
public. Drop `--dry-run` once you are happy.

---

## Files

| File | What it is |
|---|---|
| `pipeline.py` | the whole machine |
| `feed.py` | fetches free-to-publish footage and records its licence |
| `config.yml` | your settings |
| `setup_auth.py` | one-time YouTube login helper |
| `.github/workflows/publish.yml` | the daily trigger |
| `state/used.json` | which moments have already been used |
| `state/feed.json` | where every downloaded clip came from, and its licence |
| `PRIVACY.md` | the privacy policy Google asks for when publishing the OAuth app |

Never commit `client_secrets.json` or `refresh_token.txt`; `.gitignore` already
covers them. If a refresh token leaks, delete the OAuth client in Google Cloud
and make a new one.
