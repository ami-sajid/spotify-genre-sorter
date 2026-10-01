#!/usr/bin/env python3
"""
spotify_genre_sorter.py - sort your Spotify Liked Songs into per-genre playlists.

Commands
    auth    One-time, browser-on-any-device authorization. Prints a refresh token.
    check   Sanity-check auth, playlist access and whether Spotify returns artist genres.
    sync    Fetch NEW liked songs, resolve genres, add songs to genre playlists.
            Flags: --dry-run (change nothing), --full (ignore the watermark, re-scan all likes).

Runtime credentials (environment variables)
    SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET   your Spotify app
    SPOTIFY_REFRESH_TOKEN                      printed by `auth`
    SPOTIPY_REDIRECT_URI                       only used by `auth` (default http://127.0.0.1:8888/callback)
    LASTFM_API_KEY                             optional genre fallback (see README)

Optional tuning (all have defaults)
    SORTER_STATE_PATH          state/state.json
    SORTER_PLAYLIST_PREFIX     ""     e.g. "Genre - "
    SORTER_MAX_GENRES          3      max genre playlists per song
    SORTER_ALL_ARTISTS         false  use every artist on a song, not just the first
    SORTER_GENRE_ALLOWLIST     ""     comma-separated; empty = every genre
    SORTER_MAX_ARTIST_LOOKUPS  300    max new artist lookups per run (backfill spreads over runs)
    SORTER_GENRE_CACHE_DAYS    60
    SORTER_REQUEST_DELAY       0.25   seconds between artist lookups
    SORTER_PUBLIC              false  make created playlists public
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import spotipy
from spotipy.cache_handler import CacheHandler, MemoryCacheHandler
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth

SCOPE = (
    "user-library-read playlist-read-private "
    "playlist-modify-private playlist-modify-public"
)
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"
TOKEN_URL = "https://accounts.spotify.com/api/token"
STATE_VERSION = 1
PLAYLIST_DESCRIPTION = "Auto-managed: liked songs sorted by genre."

log = logging.getLogger("genre-sorter")


# --------------------------------------------------------------------------- config
def _env(name: str, default: str = "") -> str:
    return os.getenv(name) or default  # treats empty string as unset (GitHub vars)


def _flag(name: str) -> bool:
    return _env(name).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    state_path: Path
    playlist_prefix: str
    max_genres: int
    all_artists: bool
    allowlist: set[str]
    max_artist_lookups: int
    genre_cache_days: int
    request_delay: float
    public: bool
    lastfm_key: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            state_path=Path(_env("SORTER_STATE_PATH", "state/state.json")),
            playlist_prefix=_env("SORTER_PLAYLIST_PREFIX"),
            max_genres=int(_env("SORTER_MAX_GENRES", "3")),
            all_artists=_flag("SORTER_ALL_ARTISTS"),
            allowlist={g.strip().lower() for g in _env("SORTER_GENRE_ALLOWLIST").split(",") if g.strip()},
            max_artist_lookups=int(_env("SORTER_MAX_ARTIST_LOOKUPS", "300")),
            genre_cache_days=int(_env("SORTER_GENRE_CACHE_DAYS", "60")),
            request_delay=float(_env("SORTER_REQUEST_DELAY", "0.25")),
            public=_flag("SORTER_PUBLIC"),
            lastfm_key=_env("LASTFM_API_KEY"),
        )


# --------------------------------------------------------------------------- auth
class EnvRefreshTokenCache(CacheHandler):
    """Token cache seeded from a long-lived refresh token: no browser, no disk."""

    def __init__(self, refresh_token: str):
        self._seed = refresh_token
        self._token = {
            "access_token": "",
            "token_type": "Bearer",
            "expires_in": 0,
            "expires_at": 0,  # forces an immediate refresh
            "refresh_token": refresh_token,
            "scope": SCOPE,
        }

    def get_cached_token(self):
        return dict(self._token)

    def save_token_to_cache(self, token_info):
        new_refresh = token_info.get("refresh_token")
        if new_refresh and new_refresh != self._seed:
            log.warning(
                "Spotify issued a rotated refresh token. If a later run fails with "
                "invalid_grant, re-run `auth` and update SPOTIFY_REFRESH_TOKEN."
            )
        self._token = dict(token_info)


def make_client() -> spotipy.Spotify:
    refresh = (os.getenv("SPOTIFY_REFRESH_TOKEN") or "").strip().strip("\"'")
    if refresh.startswith("SPOTIFY_REFRESH_TOKEN="):
        refresh = refresh.split("=", 1)[1]
    refresh = "".join(refresh.split())  # drop hidden spaces/newlines
    if not refresh:
        sys.exit("SPOTIFY_REFRESH_TOKEN is not set. Run `python spotify_genre_sorter.py auth` once.")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{80,250}", refresh):
        sys.exit(
            f"SPOTIFY_REFRESH_TOKEN looks wrong (length {len(refresh)}; a real one is ~130 "
            "characters of letters, digits, - and _ only). Re-copy it from refresh_token.txt."
        )
    log.info("Refresh token format OK (length %d).", len(refresh))
    auth = SpotifyOAuth(
        scope=SCOPE,
        redirect_uri=_env("SPOTIPY_REDIRECT_URI", DEFAULT_REDIRECT_URI),
        cache_handler=EnvRefreshTokenCache(refresh),
        open_browser=False,
        requests_timeout=20,
    )
    # spotipy retries 429/5xx and honours Retry-After; the workflow timeout is the backstop.
    return spotipy.Spotify(
        auth_manager=auth, requests_timeout=20, retries=5, status_retries=5, backoff_factor=1.0
    )


def cmd_auth() -> int:
    client_id = os.getenv("SPOTIPY_CLIENT_ID")
    client_secret = os.getenv("SPOTIPY_CLIENT_SECRET")
    redirect = _env("SPOTIPY_REDIRECT_URI", DEFAULT_REDIRECT_URI)
    if not (client_id and client_secret):
        sys.exit("Set SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET first.")

    oauth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect,
        scope=SCOPE,
        open_browser=False,
        cache_handler=MemoryCacheHandler(),
    )
    print("\n1) Open this URL in any browser (any device) and approve access:\n")
    print(oauth.get_authorize_url())
    print(
        "\n2) You'll be redirected to a page that fails to load. That's expected.\n"
        "   Copy the FULL URL from the address bar (it contains ?code=...).\n"
    )
    redirected = input("Paste the full redirected URL here: ").strip()
    code = oauth.parse_response_code(redirected)
    if not code or code == redirected:
        sys.exit("Couldn't find a `code` in that URL. Did you approve access? Try again.")

    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect},
        auth=(client_id, client_secret),
        timeout=20,
    )
    if resp.status_code != 200:
        sys.exit(f"Token exchange failed ({resp.status_code}): {resp.text}")
    refresh = resp.json().get("refresh_token")
    if not refresh:
        sys.exit("Spotify returned no refresh token.")
    out = Path("refresh_token.txt")
    out.write_text(refresh + "\n", encoding="utf-8")
    print(f"\nSuccess. Your refresh token was saved to:\n  {out.resolve()}\n")
    print("Open that file in Notepad, press Ctrl+A then Ctrl+C, and paste it as the")
    print("SPOTIFY_REFRESH_TOKEN secret. Treat it like a password and delete the file afterwards.\n")
    return 0


# --------------------------------------------------------------------------- state
def _empty_state() -> dict:
    return {"version": STATE_VERSION, "watermark": None, "watermark_ids": [], "artist_genres": {}}


def load_state(path: Path) -> dict:
    if not path.exists():
        return _empty_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("version") == STATE_VERSION:
            return state
        log.warning("State version mismatch; starting fresh.")
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Couldn't read state (%s); starting fresh.", exc)
    return _empty_state()


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


# --------------------------------------------------------------------------- genres
NON_GENRE_TAGS = {
    "seen live", "favorites", "favourites", "favorite", "albums i own", "female vocalists",
    "male vocalists", "female vocalist", "male vocalist", "singer-songwriter", "british",
    "american", "usa", "uk", "under 2000 listeners", "spotify", "love", "awesome",
}
DECADE_RE = re.compile(r"^(\d{2}|\d{4})s$")


def normalize_genre(g: str) -> str:
    return re.sub(r"\s+", " ", g.strip().lower())


def lastfm_genres(session: requests.Session, api_key: str, artist_name: str) -> list[str]:
    """Top Last.fm tags for an artist, filtered down to plausible genres."""
    try:
        r = session.get(
            "https://ws.audioscrobbler.com/2.0/",
            params={
                "method": "artist.gettoptags", "artist": artist_name, "autocorrect": 1,
                "api_key": api_key, "format": "json",
            },
            timeout=15,
        )
        r.raise_for_status()
        tags = r.json().get("toptags", {}).get("tag", [])
    except (requests.RequestException, ValueError) as exc:
        log.warning("Last.fm lookup failed for %r: %s", artist_name, exc)
        return []
    out = []
    for tag in tags:
        name = normalize_genre(tag.get("name", ""))
        if not name or name in NON_GENRE_TAGS or DECADE_RE.match(name):
            continue
        if int(tag.get("count", 0) or 0) < 50:  # Last.fm weights tags 0-100
            continue
        out.append(name)
        if len(out) >= 3:
            break
    return out


class GenreResolver:
    """Artist -> genres, cached in state so each artist costs at most one call per TTL."""

    def __init__(self, sp: spotipy.Spotify, cfg: Config, cache: dict):
        self.sp, self.cfg, self.cache = sp, cfg, cache
        self.lookups = 0
        self.session = requests.Session()
        self._warned_missing = False

    def _fresh(self, entry: dict) -> bool:
        try:
            fetched = datetime.fromisoformat(entry["fetched"])
        except (KeyError, ValueError):
            return False
        return datetime.now(timezone.utc) - fetched < timedelta(days=self.cfg.genre_cache_days)

    def get(self, artist_id: str, artist_name: str) -> list[str] | None:
        """[] = looked up, no genres. None = lookup budget exhausted (defer the track)."""
        entry = self.cache.get(artist_id)
        if entry and self._fresh(entry):
            return entry["genres"]
        if self.lookups >= self.cfg.max_artist_lookups:
            return None
        self.lookups += 1
        time.sleep(self.cfg.request_delay)
        genres, source = self._lookup(artist_id, artist_name)
        self.cache[artist_id] = {
            "name": artist_name, "genres": genres, "source": source,
            "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        return genres

    def _lookup(self, artist_id: str, artist_name: str) -> tuple[list[str], str]:
        genres, source = [], "spotify"
        try:
            artist = self.sp.artist(artist_id)  # single-artist endpoint; the batch one is gone
        except SpotifyException as exc:
            if exc.http_status != 404:
                raise
            artist = {}
        if "genres" in artist:
            genres = [normalize_genre(g) for g in artist["genres"] if g.strip()]
        elif artist and not self._warned_missing:
            self._warned_missing = True
            log.warning(
                "Spotify's artist response has no `genres` field for this app. "
                "Set LASTFM_API_KEY to enable the fallback (see README)."
            )
        if not genres and self.cfg.lastfm_key:
            time.sleep(0.2)
            fallback = lastfm_genres(self.session, self.cfg.lastfm_key, artist_name)
            if fallback:
                return fallback, "lastfm"
        return genres, source


# --------------------------------------------------------------------------- liked songs
def fetch_new_likes(sp: spotipy.Spotify, state: dict, full: bool) -> list[dict]:
    """Page through liked songs (newest first) and stop at the watermark."""
    watermark = None if full else state.get("watermark")
    seen_at_wm = set(state.get("watermark_ids", []))
    new: list[dict] = []
    results = sp.current_user_saved_tracks(limit=50)
    pages = 0
    while results:
        pages += 1
        reached_old = False
        for item in results.get("items", []):
            track = item.get("track") or {}
            added = item.get("added_at")
            if not track.get("id"):
                continue
            if watermark and added:
                if added < watermark:
                    reached_old = True
                    break
                if added == watermark and track["id"] in seen_at_wm:
                    continue
            new.append({
                "id": track["id"],
                "name": track.get("name", ""),
                "added_at": added,
                "artists": [
                    {"id": a["id"], "name": a.get("name", "")}
                    for a in track.get("artists", []) if a.get("id")
                ],
            })
        if reached_old or not results.get("next"):
            break
        results = sp.next(results)
    log.info("Fetched %d new liked song(s) in %d page(s).", len(new), pages)
    return new


# --------------------------------------------------------------------------- playlists
class PlaylistIndex:
    """Owned playlists by name, plus lazily-loaded track-ID sets for the ones we touch."""

    def __init__(self, sp: spotipy.Spotify, cfg: Config, dry_run: bool):
        self.sp, self.cfg, self.dry_run = sp, cfg, dry_run
        self.me = sp.current_user()["id"]
        self.by_name: dict[str, str] = {}
        self._contents: dict[str, set[str]] = {}
        results = sp.current_user_playlists(limit=50)
        while results:
            for pl in results.get("items", []):
                if pl and (pl.get("owner") or {}).get("id") == self.me:
                    self.by_name.setdefault(pl["name"].casefold(), pl["id"])
            results = sp.next(results) if results.get("next") else None
        log.info("Found %d playlist(s) owned by you.", len(self.by_name))

    def display_name(self, genre: str) -> str:
        return f"{self.cfg.playlist_prefix}{genre.title()}"

    def ensure(self, genre: str) -> str:
        name = self.display_name(genre)
        key = name.casefold()
        if key in self.by_name:
            return self.by_name[key]
        if self.dry_run:
            log.info("[dry-run] would create playlist %r", name)
            self.by_name[key] = f"dry-run:{key}"
        else:
            created = self.sp.current_user_playlist_create(
                name, public=self.cfg.public, description=PLAYLIST_DESCRIPTION
            )
            log.info("Created playlist %r", name)
            self.by_name[key] = created["id"]
        self._contents[self.by_name[key]] = set()
        return self.by_name[key]

    def contents(self, playlist_id: str) -> set[str]:
        if playlist_id not in self._contents:
            ids: set[str] = set()
            results = self.sp.playlist_items(playlist_id, limit=50, additional_types=("track",))
            while results:
                for row in results.get("items", []):
                    # Feb-2026 API renamed `track` -> `item`; `track` may now be a boolean.
                    obj = row.get("item") or row.get("track")
                    if isinstance(obj, dict) and obj.get("id"):
                        ids.add(obj["id"])
                results = self.sp.next(results) if results.get("next") else None
            self._contents[playlist_id] = ids
        return self._contents[playlist_id]

    def add(self, playlist_id: str, track_ids: list[str]) -> int:
        have = self.contents(playlist_id)
        fresh = [t for t in dict.fromkeys(track_ids) if t not in have]
        if not fresh:
            return 0
        if self.dry_run:
            have.update(fresh)
            return len(fresh)
        for i in range(0, len(fresh), 100):  # API max 100 per call
            chunk = fresh[i:i + 100]
            self.sp.playlist_add_items(playlist_id, [f"spotify:track:{t}" for t in chunk])
            have.update(chunk)
        return len(fresh)


# --------------------------------------------------------------------------- sync
def genres_for_track(track: dict, resolver: GenreResolver, cfg: Config) -> list[str] | None:
    artists = track["artists"] if cfg.all_artists else track["artists"][:1]
    genres: list[str] = []
    for artist in artists:
        found = resolver.get(artist["id"], artist["name"])
        if found is None:
            return None
        genres.extend(g for g in found if g not in genres)
    if cfg.allowlist:
        genres = [g for g in genres if g in cfg.allowlist]
    return genres[: cfg.max_genres]


def sync(cfg: Config, dry_run: bool, full: bool) -> int:
    state = load_state(cfg.state_path)
    before = json.dumps(state, sort_keys=True)
    errors = 0      # real failures -> non-zero exit
    deferred = False  # songs postponed by the lookup budget -> normal, retried next run
    try:
        sp = make_client()
        tracks = fetch_new_likes(sp, state, full)
        if not tracks:
            log.info("Nothing new. Done.")
            return 0

        resolver = GenreResolver(sp, cfg, state["artist_genres"])
        plan: dict[str, list[str]] = defaultdict(list)
        unsorted = 0
        for track in tracks:
            try:
                genres = genres_for_track(track, resolver, cfg)
            except SpotifyException as exc:
                log.error("Genre lookup failed for %r: %s", track["name"], exc)
                errors += 1
                continue
            if genres is None:  # lookup budget exhausted
                deferred = True
                continue
            if not genres:
                unsorted += 1
                continue
            for g in genres:
                plan[g].append(track["id"])
        if deferred:
            log.info(
                "Artist-lookup budget (%d) reached; remaining songs will be picked up next run.",
                cfg.max_artist_lookups,
            )

        index = PlaylistIndex(sp, cfg, dry_run)
        added = 0
        for genre, ids in sorted(plan.items()):
            try:
                added += index.add(index.ensure(genre), ids)
            except SpotifyException as exc:
                log.error("Failed updating %r playlist: %s", genre, exc)
                errors += 1
        log.info(
            "Added %d playlist entries across %d genre(s); %d song(s) had no genre; "
            "%d artist lookup(s).",
            added, len(plan), unsorted, resolver.lookups,
        )

        # Advance the watermark only if every fetched song was fully handled.
        if not (errors or deferred or dry_run):
            stamped = [t for t in tracks if t["added_at"]]
            if stamped:
                newest = max(t["added_at"] for t in stamped)
                ids = {t["id"] for t in stamped if t["added_at"] == newest}
                old = state.get("watermark")
                if old is None or newest > old:
                    state["watermark"], state["watermark_ids"] = newest, sorted(ids)
                elif newest == old:
                    state["watermark_ids"] = sorted(ids | set(state.get("watermark_ids", [])))
        return 1 if errors else 0
    finally:
        # Always persist the genre cache, even after a crash, so progress isn't lost.
        if not dry_run and json.dumps(state, sort_keys=True) != before:
            save_state(cfg.state_path, state)
            log.info("State saved to %s", cfg.state_path)


# --------------------------------------------------------------------------- check
def cmd_check(cfg: Config) -> int:
    sp = make_client()
    me = sp.current_user()
    print(f"Authenticated as: {me['id']}")
    page = sp.current_user_saved_tracks(limit=5)
    print(f"Liked songs total: {page.get('total')}")
    for item in page.get("items", [])[:3]:
        artist = item["track"]["artists"][0]
        data = sp.artist(artist["id"])
        has_field = "genres" in data
        print(f"  {artist['name']}: `genres` field present={has_field} value={data.get('genres')}")
    owned = sum(
        1 for pl in sp.current_user_playlists(limit=50).get("items", [])
        if pl and (pl.get("owner") or {}).get("id") == me["id"]
    )
    print(f"Playlists owned by you (first page): {owned}")
    print(f"Last.fm fallback: {'enabled' if cfg.lastfm_key else 'not configured'}")
    return 0


# --------------------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth", help="one-time authorization; prints a refresh token")
    sub.add_parser("check", help="verify auth and genre availability")
    p_sync = sub.add_parser("sync", help="sort new liked songs into genre playlists")
    p_sync.add_argument("--dry-run", action="store_true", help="log what would happen; change nothing")
    p_sync.add_argument("--full", action="store_true", help="ignore the watermark and re-scan all likes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    cfg = Config.from_env()
    try:
        if args.command == "auth":
            return cmd_auth()
        if args.command == "check":
            return cmd_check(cfg)
        return sync(cfg, dry_run=args.dry_run, full=args.full)
    except SpotifyException as exc:
        log.error("Spotify API error: %s", exc)
        return 1
    except requests.RequestException as exc:
        log.error("Network error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
