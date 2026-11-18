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
