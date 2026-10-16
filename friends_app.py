"""
Music Discovery for Friends (Last.fm edition)

Tell it some songs you like and it finds similar ones using Last.fm's listening data.
No logins and no streaming accounts: you get a list you can copy or download and
move to Spotify, YouTube Music, or Apple Music with a free playlist-transfer site.

Needs a free Last.fm API key (https://www.last.fm/api/account/create).
Keep the key in Streamlit "secrets" (never in your code or on GitHub):

    LASTFM_API_KEY = "your-key-here"

Run it on your own computer:   streamlit run friends_lastfm.py
"""

import csv
import hashlib
import io
import json
import os
import random
import re
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
import streamlit as st

# ---------------------------------------------------------------
# Settings (kept small so one person can't overload the app)
# ---------------------------------------------------------------
API_URL = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "MusicDiscoveryForFriends/1.0"

EXAMPLE_SONGS = """Blinding Lights - The Weeknd
Levitating - Dua Lipa
Heat Waves - Glass Animals"""

MAX_SEEDS = 15           # most seed songs per run
SIMILAR_LIMIT = 50       # similar songs requested per seed
FALLBACK_LIMIT = 4       # how many seeds per run may use the slower artist-based fallback
MAX_PLAYLIST_SIZE = 60
MIN_MS_PLAYED = 30_000   # a Spotify play only counts if you listened 30+ seconds
RANGE_DAYS = {
    "All time": None,
    "Last 12 months": 365,
    "Last 6 months": 183,
    "Last 3 months": 92,
}

# ---------------------------------------------------------------
# Text helpers and filters
# ---------------------------------------------------------------
LIVE_PATTERN = re.compile(
    r"[\(\[][^\)\]]*\b(live|en vivo|en directo|ao vivo|unplugged|concert)\b[^\)\]]*[\)\]]"
    r"|\blive (at|from|in|on|session|version)\b"
    r"|\s-\s*live\b"
    r"|\b(en vivo|en directo|ao vivo)\b",
    re.IGNORECASE,
)
REMIX_PATTERN = re.compile(
    r"\b(remix|sped up|speed up|slowed|nightcore|mashup|bootleg|8d audio)\b",
    re.IGNORECASE,
)
INSTRUMENTAL_PATTERN = re.compile(
    r"\b(instrumental|karaoke|backing track|tribute)\b", re.IGNORECASE
)

SPANISH_WORDS = set("""
el la los las de del que mi tu te se un una con por para como pero mas más yo es y en lo al muy
sin sobre amor corazón corazon vida noche mundo siempre nunca quiero eres soy tengo cuando donde
todo todos nada hoy ya aquí aqui ahora tú vez dame voy si fue era esta este ese esa mis tus su
sus nos les ella él ellos nuestro nuestra tiene tienes hay estoy estás estas está
""".split())
ENGLISH_WORDS = set("""
the and you your my of to in it is on for with i i'm im don't dont that this we be are was not but
all so what when like just baby love heart night world never always want need feel know got get go
back down up out over one two time girl boy man life way home he she her his they them our
""".split())


def normalize(text):
    text = re.sub(r"[\(\[].*?[\)\]]", " ", str(text).casefold())
    return re.sub(r"[\W_]+", "", text)


def detect_language(text):
    if re.search(r"[\uac00-\ud7af\u1100-\u11ff]", text):
        return "korean"
    if re.search(r"[\u3040-\u30ff]", text):
        return "japanese"
    if re.search(r"[\u0400-\u04ff\u0600-\u06ff\u0900-\u097f\u0e00-\u0e7f\u4e00-\u9fff]", text):
        return "other"
    lowered = text.lower()
    words = re.findall(r"[a-záéíóúüñ']+", lowered)
    spanish = sum(word in SPANISH_WORDS for word in words)
    english = sum(word in ENGLISH_WORDS for word in words)
    if re.search(r"[áéíóúñ¿¡]", lowered):
        spanish += 2
    if spanish > english:
        return "spanish"
    if english > spanish:
        return "english"
    return "unknown"


def language_ok(choice, text):
    if choice == "Any":
        return True
    guess = detect_language(text)
    wanted = choice.lower()
    if wanted in ("english", "spanish"):
        return guess in (wanted, "unknown")
    return guess == wanted


def passes_filters(title, artist, settings):
    if settings["skip_live"] and LIVE_PATTERN.search(title):
        return False
    if settings["skip_remix"] and REMIX_PATTERN.search(title):
        return False
    if settings["skip_instrumental"] and INSTRUMENTAL_PATTERN.search(title):
        return False
    return language_ok(settings["language"], f"{title} {artist}")


def escape_markdown(text):
    return re.sub(r"([*_\[\]`~])", r"\\\1", str(text))


# ---------------------------------------------------------------
# Spotify data export (read in memory only, never saved)
# ---------------------------------------------------------------
def parse_timestamp(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def read_history_entries(uploads):
    entries = []

    def handle_json(raw):
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except Exception:
            return
        if not isinstance(data, list):
            return
        for item in data:
            if not isinstance(item, dict):
                continue
            track = item.get("master_metadata_track_name") or item.get("trackName")
            artist = item.get("master_metadata_album_artist_name") or item.get("artistName")
            if not track or not artist:
                continue
            ms = item.get("ms_played", item.get("msPlayed", 0)) or 0
            when = parse_timestamp(item.get("ts") or item.get("endTime"))
            entries.append({"track": track, "artist": artist, "ms": ms, "when": when})

    for name, raw in uploads:
        lower = name.lower()
        if lower.endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    for member in archive.namelist():
                        base = member.lower().rsplit("/", 1)[-1]
                        if base.endswith(".json") and "streaminghistory" in base.replace("_", ""):
                            handle_json(archive.read(member))
            except zipfile.BadZipFile:
                pass
        elif lower.endswith(".json"):
            handle_json(raw)
    return entries


def analyze_spotify(uploads, time_range):
    """Returns (top songs, every song played, total plays counted)."""
    entries = [e for e in read_history_entries(list(uploads)) if e["ms"] >= MIN_MS_PLAYED]

    known = set()
    for e in entries:
        known.add((normalize(e["track"]), normalize(e["artist"])))

    cutoff = None
    days = RANGE_DAYS.get(time_range)
    dates = [e["when"] for e in entries if e["when"]]
    if days and dates:
        cutoff = max(dates) - timedelta(days=days)

    counts = Counter()
    display = {}
    for e in entries:
        if cutoff and e["when"] and e["when"] < cutoff:
            continue
        key = (normalize(e["track"]), normalize(e["artist"]))
        counts[key] += 1
        display.setdefault(key, (e["track"], e["artist"]))

    top = [(display[key], plays) for key, plays in counts.most_common(200)]
    return top, known, len(entries)


def analyze_spotify_for_session(uploads, time_range):
    """Remember the result for THIS visitor only (it disappears when they leave)."""
    digest = hashlib.sha256()
    for name, raw in uploads:
        digest.update(name.encode("utf-8"))
        digest.update(raw)
    key = f"{digest.hexdigest()}|{time_range}"
    cached = st.session_state.get("spotify_result")
    if cached and cached[0] == key:
        return cached[1]
    result = analyze_spotify(uploads, time_range)
    st.session_state["spotify_result"] = (key, result)
    return result


# ---------------------------------------------------------------
# Last.fm
# ---------------------------------------------------------------
class LastFmError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def get_api_key():
    try:
        key = st.secrets.get("LASTFM_API_KEY")
    except Exception:
        key = None
    return key or os.environ.get("LASTFM_API_KEY")


def as_list(value):
    if not value:
        return []
    return value if isinstance(value, list) else [value]


def name_of(value):
    """Last.fm gives an artist as text in some replies and as an object in others."""
    if isinstance(value, dict):
        return value.get("name") or value.get("#text") or ""
    return value or ""


def lastfm_call(method, **params):
    key = get_api_key()
    if not key:
        raise LastFmError(10, "No Last.fm API key is set up")
    query = {"method": method, "api_key": key, "format": "json", **params}
    try:
        response = requests.get(
            API_URL, params=query, timeout=15, headers={"User-Agent": USER_AGENT}
        )
    except requests.RequestException as error:
        # Don't print the error text itself: it can contain the web address with the key in it.
        raise LastFmError(0, f"Network problem ({type(error).__name__})")
    try:
        data = response.json()
    except ValueError:
        raise LastFmError(0, f"Unexpected reply from Last.fm (HTTP {response.status_code})")
    if isinstance(data, dict) and "error" in data:
        raise LastFmError(int(data["error"]), data.get("message", "Last.fm error"))
    return data


@st.cache_data(ttl=3600, show_spinner=False)
def search_track(text):
    """Turn free text like 'Song Artist' into (track, artist) using Last.fm search."""
    data = lastfm_call("track.search", track=text, limit=1)
    matches = as_list(data.get("results", {}).get("trackmatches", {}).get("track"))
    if not matches:
        return None
    return matches[0].get("name"), name_of(matches[0].get("artist"))


@st.cache_data(ttl=3600, show_spinner=False)
def similar_tracks(artist, track):
    data = lastfm_call(
        "track.getSimilar", artist=artist, track=track, limit=SIMILAR_LIMIT, autocorrect=1
    )
    results = []
    for item in as_list(data.get("similartracks", {}).get("track")):
        title, who = item.get("name"), name_of(item.get("artist"))
        if title and who:
            try:
                match = float(item.get("match") or 0)
            except (TypeError, ValueError):
                match = 0.0
            results.append({"title": title, "artist": who, "match": match})
    return results


@st.cache_data(ttl=3600, show_spinner=False)
def similar_from_artist(artist):
    """Fallback for songs Last.fm has no 'similar tracks' for: top songs of similar artists."""
    data = lastfm_call("artist.getSimilar", artist=artist, limit=8, autocorrect=1)
    results = []
    for item in as_list(data.get("similarartists", {}).get("artist")):
        who = item.get("name")
        if not who:
            continue
        try:
            match = float(item.get("match") or 0) * 0.6
        except (TypeError, ValueError):
            match = 0.0
        try:
            top = lastfm_call("artist.getTopTracks", artist=who, limit=2)
        except LastFmError as error:
            if error.code in (10, 26, 29):
                raise
            continue
        for track in as_list(top.get("toptracks", {}).get("track")):
            if track.get("name"):
                results.append({"title": track["name"], "artist": who, "match": match})
    return results


def parse_seed_line(line):
    """'Song - Artist' or 'Song by Artist' -> (track, artist). Otherwise None."""
    if " - " in line:
        track, artist = line.rsplit(" - ", 1)
    else:
        found = re.match(r"^(.*)\s+by\s+(.+)$", line, re.IGNORECASE)
        if not found:
            return None
        track, artist = found.group(1), found.group(2)
    track, artist = track.strip(), artist.strip()
    return (track, artist) if track and artist else None


def generate(seeds, known_keys, settings, progress):
    """seeds: list of dicts: {"label", "track", "artist"} (artist None = needs a search)."""
    counts = Counter()
    scores = Counter()
    info = {}
    seed_keys = set()
    missed = []
    fallback_used = 0
    total = len(seeds)

    for i, seed in enumerate(seeds, start=1):
        label = seed["label"]
        progress.progress((i - 1) / total, text=f"Looking up: {label}")

        track, artist = seed["track"], seed["artist"]
        try:
            if artist is None:
                found = search_track(label)
                if not found:
                    missed.append(f"{label} (not found - try the format 'Song - Artist')")
                    continue
                track, artist = found
            seed_keys.add((normalize(track), normalize(artist)))

            similar = similar_tracks(artist, track)
            if not similar and fallback_used < FALLBACK_LIMIT:
                fallback_used += 1
                similar = similar_from_artist(artist)
        except LastFmError as error:
            if error.code in (10, 26, 29, 0):
                raise  # key problems, rate limit, or network trouble affect everything
            missed.append(f"{label} (not found on Last.fm)")
            continue

        if not similar:
            missed.append(f"{label} (Last.fm has no similar songs for this one)")
            continue

        seen = set()
        for item in similar:
            key = (normalize(item["title"]), normalize(item["artist"]))
            if key in seen:
                continue
            seen.add(key)
            counts[key] += 1
            scores[key] += item["match"]
            info.setdefault(key, item)

    progress.progress(1.0, text="Ranking songs...")

    candidates = []
    for key, matches in counts.items():
        item = info[key]
        if key in seed_keys:
            continue
        if settings["skip_known"] and key in known_keys:
            continue
        if not passes_filters(item["title"], item["artist"], settings):
            continue
        # Songs similar to more of your picks rank higher; a little randomness varies each run
        rank = matches + 0.5 * scores[key] + random.uniform(0, 0.25)
        candidates.append((rank, key))

    candidates.sort(key=lambda pair: pair[0], reverse=True)

    rows = []
    per_artist = Counter()
    for _, key in candidates:
        item = info[key]
        if per_artist[key[1]] >= settings["per_artist"]:
            continue
        per_artist[key[1]] += 1
        rows.append(
            {
                "Keep": True,
                "Song": item["title"],
                "Artist": item["artist"],
                "Matches": counts[key],
            }
        )
        if len(rows) >= settings["size"]:
            break

    return rows, missed, {"found": len(counts), "kept": len(candidates)}


# ---------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------
def make_text_list(kept):
    return "\n".join(f"{row['Song']} - {row['Artist']}" for row in kept)


def make_csv(kept):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Title", "Artist"])
    for row in kept:
        writer.writerow([row["Song"], row["Artist"]])
    return buffer.getvalue()


def search_links(song, artist):
    text = f"{song} {artist}"
    return (
        "https://music.youtube.com/search?q=" + quote(text),
        "https://open.spotify.com/search/" + quote(text),
    )


# ---------------------------------------------------------------
# The page
# ---------------------------------------------------------------
def main():
    st.set_page_config(page_title="Music Discovery", page_icon="🎧")
    st.title("🎧 Music Discovery")
    st.write(
        "Tell me some songs you like and I'll find new ones. "
        "No login needed - you get a list to take to YouTube Music, Spotify, or Apple Music."
    )

    if not get_api_key():
        st.error(
            "This app isn't set up yet: it needs a Last.fm API key. "
            "(Owner: add LASTFM_API_KEY in the app's secrets.)"
        )
        st.stop()

    # ---- Step 1
    st.header("1. Songs you like")
    manual_text = st.text_area(
        "One per line, like this: Song - Artist",
        value=EXAMPLE_SONGS,
        height=140,
    )

    files = st.file_uploader(
        "Optional: upload your Spotify data export (.zip, or the Streaming_History .json files)",
        type=["zip", "json"],
        accept_multiple_files=True,
    )
    st.caption(
        "Your files are read in memory just to find your top songs. They aren't saved, "
        "and your history is forgotten when you close the page. "
        "Song lookups are briefly remembered to keep the app fast. "
        "Get your export at spotify.com/account/privacy."
    )

    spotify_top, known_keys = [], set()
    use_spotify, n_spotify, shuffle_seeds = False, 8, True
    if files:
        time_range = st.selectbox("Use Spotify plays from", list(RANGE_DAYS))
        uploads = tuple((f.name, f.getvalue()) for f in files)
        spotify_top, known_keys, total_plays = analyze_spotify_for_session(uploads, time_range)
        if spotify_top:
            st.success(f"Read {total_plays:,} plays and found {len(spotify_top)} top songs.")
            use_spotify = st.checkbox("Use my top Spotify songs as seeds", value=True)
            n_spotify = st.slider("How many Spotify songs to use", 3, MAX_SEEDS, 8)
            shuffle_seeds = st.checkbox(
                "Pick randomly from my top 50 (more variety each time)", value=True
            )
        else:
            st.warning(
                "I couldn't find listening history in those files. Look for files named "
                "Streaming_History_Audio_...json (or StreamingHistory_music_...json)."
            )

    # ---- Step 2
    st.header("2. Filters")
    col1, col2 = st.columns(2)
    skip_live = col1.checkbox("Skip live versions", value=True)
    skip_remix = col1.checkbox("Skip remixes / sped up / slowed", value=True)
    skip_instrumental = col2.checkbox("Skip instrumentals & karaoke", value=True)
    skip_known = col2.checkbox(
        "Skip songs I've already played on Spotify", value=True, disabled=not known_keys
    )
    language = st.selectbox("Language", ["Any", "English", "Spanish", "Korean", "Japanese"])
    if language != "Any":
        st.caption(
            "Language is a rough guess from titles and artist names only. "
            "If a title is unclear, the song is kept."
        )

    size = st.slider("How many songs", 10, MAX_PLAYLIST_SIZE, 30, step=5)
    per_artist = st.slider("Max songs per artist", 1, 6, 3)

    settings = {
        "skip_live": skip_live,
        "skip_remix": skip_remix,
        "skip_instrumental": skip_instrumental,
        "skip_known": skip_known and bool(known_keys),
        "language": language,
        "size": size,
        "per_artist": per_artist,
    }

    # ---- Find songs
    if st.button("🎵 Find songs", type="primary"):
        seeds = []
        for line in manual_text.splitlines():
            line = line.strip()
            if not line:
                continue
            pair = parse_seed_line(line)
            if pair:
                seeds.append({"label": line, "track": pair[0], "artist": pair[1]})
            else:
                seeds.append({"label": line, "track": None, "artist": None})

        if use_spotify and spotify_top:
            if shuffle_seeds:
                pool = spotify_top[:50]
                chosen = random.sample(pool, min(n_spotify, len(pool)))
            else:
                chosen = spotify_top[:n_spotify]
            for (track, artist), _ in chosen:
                seeds.append({"label": f"{track} - {artist}", "track": track, "artist": artist})

        unique, lowered = [], set()
        for seed in seeds:
            if seed["label"].lower() not in lowered:
                lowered.add(seed["label"].lower())
                unique.append(seed)
        if len(unique) > MAX_SEEDS:
            st.info(f"Using the first {MAX_SEEDS} songs to keep things quick.")
            unique = unique[:MAX_SEEDS]

        if not unique:
            st.error("Add at least one song first.")
        else:
            progress = st.progress(0.0, text="Starting...")
            try:
                rows, missed, stats = generate(unique, known_keys, settings, progress)
                st.session_state["results"] = rows
                st.session_state["missed"] = missed
                st.session_state["stats"] = stats
                st.session_state["run_id"] = st.session_state.get("run_id", 0) + 1
            except LastFmError as error:
                if error.code in (10, 26):
                    st.error("The app's Last.fm key isn't working. Please tell the person who runs this app.")
                elif error.code == 29:
                    st.error("Last.fm says it's too busy right now. Please try again in a minute.")
                else:
                    st.error(f"Couldn't reach Last.fm. Please try again in a minute. ({error})")
            progress.empty()

    # ---- Results
    if "results" in st.session_state:
        rows = st.session_state["results"]
        st.header("Your recommendations")

        if st.session_state["missed"]:
            with st.expander(f"{len(st.session_state['missed'])} song(s) couldn't be used"):
                for item in st.session_state["missed"]:
                    st.write("- " + item)

        if not rows:
            st.warning(
                "Nothing made it through. Try turning some filters off, "
                "or adding more songs."
            )
        else:
            stats = st.session_state["stats"]
            st.caption(
                f"Looked at {stats['found']} similar songs; {stats['kept']} passed your filters. "
                "'Matches' = how many of your songs each one is similar to. "
                "Uncheck anything you don't want."
            )
            edited = st.data_editor(
                rows,
                column_config={"Keep": st.column_config.CheckboxColumn("Keep")},
                disabled=["Song", "Artist", "Matches"],
                hide_index=True,
                key=f"editor_{st.session_state.get('run_id', 0)}",
            )
            records = edited.to_dict("records") if hasattr(edited, "to_dict") else list(edited)
            kept = [row for row in records if row["Keep"]]
            st.write(f"{len(kept)} songs selected.")

            if kept:
                st.header("Take your list with you")

                st.subheader("Option A: Move it to Spotify, YouTube Music, or Apple Music")
                st.write(
                    "Download the list, then import it with a free playlist-transfer site such as "
                    "TuneMyMusic or Soundiiz. Check which file or text format they accept."
                )
                dl1, dl2 = st.columns(2)
                dl1.download_button(
                    "⬇️ Download as CSV", make_csv(kept), file_name="my_discoveries.csv"
                )
                dl2.download_button(
                    "⬇️ Download as text", make_text_list(kept), file_name="my_discoveries.txt"
                )
                with st.expander("Or copy the list"):
                    st.code(make_text_list(kept), language=None)

                st.subheader("Option B: Look songs up one at a time")
                with st.expander("Search links for each song"):
                    for number, row in enumerate(kept, start=1):
                        yt_link, sp_link = search_links(row["Song"], row["Artist"])
                        st.markdown(
                            f"{number}. {escape_markdown(row['Song'])} - "
                            f"{escape_markdown(row['Artist'])}  "
                            f"[YouTube Music]({yt_link}) · [Spotify]({sp_link})"
                        )

    st.divider()
    st.caption(
        "A hobby project. Not affiliated with Spotify, YouTube, Apple, or Last.fm. "
        "Recommendations are powered by Last.fm listening data."
    )


if __name__ == "__main__":
    main()
