# Spotify Liked Songs -> genre playlists

Runs on a schedule, finds songs you've liked since the last run, looks up each artist's genres,
and drops every song into a matching genre playlist (creating the playlist if it doesn't exist).
Private playlists by default. State lives in `state/state.json` in your repo.

## Read this first: Spotify's 2026 rules

- **The app owner needs Spotify Premium.** Since February 2026 a Development Mode app stops working without it.
- Development Mode allows 5 users per app. Fine for personal use.
- Batch artist lookups (`GET /artists?ids=`) were removed, so this script fetches artists one at a time and caches them. Playlist calls use the new `/items` endpoints (spotipy 2.26+ does this for you).
- **Artist `genres` is the one uncertain field.** Spotify's changelog doesn't list it as removed, but some developers report it missing for Development Mode apps. Step 4 below tests your app. If it's missing, the script falls back to Last.fm tags (free key, step 4).

## Setup (about 15 minutes)

### 1. Create the Spotify app
1. Go to https://developer.spotify.com/dashboard -> **Create app**.
2. Redirect URI: `http://127.0.0.1:8888/callback` (use the IP, not `localhost`).
3. Tick **Web API**. Save, then copy the **Client ID** and **Client secret** (Settings).

### 2. Authorize once (headless flow)
Do this on any machine with Python 3.10+:

```bash
pip install -r requirements.txt
export SPOTIPY_CLIENT_ID=...        # Windows PowerShell: $env:SPOTIPY_CLIENT_ID="..."
export SPOTIPY_CLIENT_SECRET=...
python spotify_genre_sorter.py auth
```

Open the printed URL in any browser and approve. You'll land on a page that fails to load; that's expected.
Copy the full address-bar URL, paste it into the terminal, and it prints `SPOTIFY_REFRESH_TOKEN=...`.
That token never expires until you revoke it, so the cloud job never needs a browser again. Treat it like a password.

### 3. Push to a private GitHub repo
Create a **private** repo and upload everything in this folder, keeping `.github/workflows/sync.yml` at that exact path.

### 4. Verify genres work for your app (optional but recommended)
```bash
export SPOTIFY_REFRESH_TOKEN=...
python spotify_genre_sorter.py check
```
It prints whether Spotify returns a `genres` field for your liked artists. If it says `present=False` (or values are mostly empty), get a free key at https://www.last.fm/api/account/create and add it as `LASTFM_API_KEY` in step 5.

### 5. Add secrets in GitHub
Repo -> **Settings -> Secrets and variables -> Actions -> New repository secret**:

| Secret | Value |
|---|---|
| `SPOTIPY_CLIENT_ID` | from step 1 |
| `SPOTIPY_CLIENT_SECRET` | from step 1 |
| `SPOTIFY_REFRESH_TOKEN` | from step 2 |
| `LASTFM_API_KEY` | optional fallback |

### 6. First run
**Actions -> Spotify genre sync -> Run workflow**, tick **Dry run** first and read the log.
Then run it again without dry run. After that it runs every 6 hours (edit the `cron` line to change it).

If **Settings -> Actions -> General -> Workflow permissions** is set to read-only and the state commit fails, switch it to *Read and write*.

## Backfilling a big library
Each run does at most 100 new artist lookups (`SORTER_MAX_ARTIST_LOOKUPS`), one per second, to stay under Spotify's rate limits. A big library backfills over several runs. Progress is cached, and the watermark only advances once every song in a batch is handled, so nothing is skipped. Click **Run workflow** again after each run finishes to speed it up; don't start two at once. A dry run saves its genre lookups too, so they aren't wasted.

## Tuning (Settings -> Secrets and variables -> Actions -> **Variables**)

| Variable | Default | Effect |
|---|---|---|
| `SORTER_PLAYLIST_PREFIX` | empty | e.g. `Genre - ` so auto playlists don't collide with your own |
| `SORTER_MAX_GENRES` | 3 | max playlists per song |
| `SORTER_GENRE_ALLOWLIST` | empty (all) | e.g. `rock,pop,hip hop,electronic` to avoid hundreds of micro-genre playlists |
| `SORTER_ALL_ARTISTS` | false | use every artist on a song, not just the first |
| `SORTER_PUBLIC` | false | create public playlists |
| `SORTER_MAX_ARTIST_LOOKUPS` | 100 | per-run lookup budget |
| `SORTER_REQUEST_DELAY` | 1.0 | seconds between artist lookups (raise it if you hit rate limits) |

Existing playlists match by name (case-insensitive) and only if you own them. With no prefix, a playlist you already named "Rock" will start receiving songs.
Spotify's genres are very granular ("bangla indie", "dark trap"), so set an allowlist or a low `SORTER_MAX_GENRES` if you'd rather have a handful of big playlists.

## Troubleshooting

- **`invalid_grant` on refresh:** the token was revoked or rotated. Re-run `auth`, update the secret.
- **403 on playlist calls:** old spotipy. Make sure `requirements.txt` resolves to 2.26+.
- **403 on everything:** the app owner's Premium lapsed.
- **Most songs "had no genre":** set `LASTFM_API_KEY`, then run once with **Full re-scan** ticked (already-sorted songs are skipped automatically).
- **Log says "Spotify rate limit hit (Retry-After about N h)":** Spotify temporarily blocked the app for sending too many requests. The script stops immediately and saves its progress. Wait out the time shown (it can be many hours), don't run anything else with the same Client ID meanwhile, then run again. If it keeps happening, raise `SORTER_REQUEST_DELAY` to 2 and lower `SORTER_MAX_ARTIST_LOOKUPS` to 50.
- To wipe progress, delete `state/state.json` in the repo.

## Not on GitHub Actions?
Any box with Python works: export the same environment variables and run `python spotify_genre_sorter.py sync` from cron or a systemd timer. Keep `SORTER_STATE_PATH` on persistent disk.

## Security
Never commit the refresh token or client secret. Revoke access anytime at https://www.spotify.com/account/apps/.
