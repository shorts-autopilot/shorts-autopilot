# Privacy Policy — shorts-autopilot

`shorts-autopilot` is a private automation tool that runs only inside the
repository owner's own GitHub Actions workflow. It is not a public service, it
has no users other than the person who set it up, and it does not collect, store
or share information about anyone.

## What it accesses

- **YouTube Data API v3** — with the `youtube.upload` scope only. It is used to
  upload videos to the authorising user's own YouTube channel and to schedule
  them. It cannot read other channels, comments, subscribers or analytics.
- **Google account** — only the identity needed to perform that upload.
- **Public footage libraries** (Internet Archive, Wikimedia Commons, NASA) —
  read-only requests for public-domain content. No account or personal data is
  involved.

## What it stores

- Nothing on any server belonging to this project. There is no backend, no
  database and no cloud account.
- A small record of which clips have already been published, kept in the
  repository's `state/` folder so the same clip is not used twice. It contains
  no personal data — only file names and public-domain credit information.
- The OAuth credential (client ID, client secret, refresh token) is stored as
  encrypted GitHub repository secrets, readable only by this repository's own
  workflow.

## What it shares

Nothing. No analytics, no tracking, no advertising, no third parties beyond the
Google APIs named above. No data is sold or transferred to anyone.

## Removing your data

Delete the repository's GitHub secrets and the `state/` folder, and revoke the
app's access at [myaccount.google.com/permissions](https://myaccount.google.com/permissions)
(or delete the OAuth client or the Google Cloud project). All stored data goes
with those deletions.

## Contact

Open an issue in this repository.
