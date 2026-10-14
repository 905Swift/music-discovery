"""
Music Discovery for Friends  -  find new songs and take the list to YouTube Music, Spotify, or Apple Music.

No logins and no accounts: it never touches anyone's streaming account.
You get a list you can copy, download, or turn into a YouTube playlist link.

Run it on your own computer:   streamlit run friends_app.py
"""

import csv
import hashlib
import io
import json
import random
import re
import zipfile
from collections import Counter
from datetime import datetime, timedelta

import streamlit as st
from ytmusicapi import YTMusic

# ---------------------------------------------------------------
# Settings (kept small so one person can't overload the app)
# ---------------------------------------------------------------
EXAMPLE_SONGS = """Blinding Lights The Weeknd
Levitating Dua Lipa
Heat Waves Glass Animals"""

MAX_SEEDS = 15           # most seed songs per run
RADIO_SIZE = 30          # similar songs fetched per seed song
MAX_PLAYLIST_SIZE = 60
LINK_CHUNK = 20          # songs per "open as YouTube playlist" link
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


def passes_filters(title, artists_text, video_type, settings):
    if settings["skip_live"] and LIVE_PATTERN.search(title):
        return False
    if settings["skip_remix"] and REMIX_PATTERN.search(title):
        return False
    if settings["skip_instrumental"] and INSTRUMENTAL_PATTERN.search(title):
        return False
    if settings["skip_ugc"] and video_type and "UGC" in str(video_type):
        return False
    return language_ok(settings["language"], f"{title} {artists_text}")


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
# YouTube Music (public search, no login)
# ---------------------------------------------------------------
@st.cache_resource
def get_yt():
    return YTMusic()


def artist_names(track):
    artists = track.get("artists") or []
    names = [a.get("name", "") for a in artists if isinstance(a, dict) and a.get("name")]
    return names or ["Unknown artist"]


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_radio(video_id):
    """Similar songs for one song. Cached for an hour so repeat lookups are free.
    (Only public song IDs are used as the cache key. Failures are not cached.)"""
    radio = get_yt().get_watch_playlist(videoId=video_id, radio=True, limit=RADIO_SIZE)
    tracks = []
    for track in radio.get("tracks", []):
        vid = track.get("videoId")
        if vid:
            tracks.append(
                {
                    "videoId": vid,
                    "title": track.get("title", "Unknown"),
                    "artists": artist_names(track),
                    "type": track.get("videoType"),
                }
            )
    return tracks


def generate(yt, seed_queries, known_keys, settings, progress):
    counts = Counter()
    info = {}
    seed_ids = set()
    seed_keys = set()
    missed = []
    total = len(seed_queries)

    for i, query in enumerate(seed_queries, start=1):
        progress.progress((i - 1) / total, text=f"Looking up: {query}")
        try:
            hits = yt.search(query, filter="songs")
            seed = next((h for h in hits if h.get("videoId")), None)
        except Exception:
            seed = None
        if not seed:
            missed.append(query)
            continue

        seed_artists = artist_names(seed)
        seed_ids.add(seed["videoId"])
        seed_keys.add((normalize(seed.get("title", "")), normalize(seed_artists[0])))

        try:
            radio = fetch_radio(seed["videoId"])
        except Exception:
            missed.append(f"{query} (couldn't get similar songs)")
            continue

        seen = set()
        for track in radio:
            vid = track["videoId"]
            if vid in seen:
                continue
            seen.add(vid)
            counts[vid] += 1
            info.setdefault(vid, track)

    progress.progress(1.0, text="Ranking songs...")

    candidates = []
    for vid, matches in counts.items():
        if vid in seed_ids:
            continue
        item = info[vid]
        key = (normalize(item["title"]), normalize(item["artists"][0]))
        if key in seed_keys:
            continue
        if settings["skip_known"] and key in known_keys:
            continue
        if not passes_filters(item["title"], ", ".join(item["artists"]), item["type"], settings):
            continue
        candidates.append((vid, matches, key))

    random.shuffle(candidates)
    candidates.sort(key=lambda c: c[1], reverse=True)

    rows = []
    per_artist = Counter()
    used_keys = set()
    for vid, matches, key in candidates:
        item = info[vid]
        main_artist = item["artists"][0]
        if key in used_keys or per_artist[main_artist] >= settings["per_artist"]:
            continue
        used_keys.add(key)
        per_artist[main_artist] += 1
        rows.append(
            {
                "Keep": True,
                "Song": item["title"],
                "Artist": ", ".join(item["artists"]),
                "Matches": matches,
                "videoId": vid,
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


def make_youtube_links(kept):
    """YouTube's 'watch_videos' link builds a temporary playlist from video IDs."""
    ids = [row["videoId"] for row in kept]
    links = []
    for start in range(0, len(ids), LINK_CHUNK):
        chunk = ids[start : start + LINK_CHUNK]
        links.append("https://www.youtube.com/watch_videos?video_ids=" + ",".join(chunk))
    return links


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

    # ---- Step 1
    st.header("1. Songs you like")
    manual_text = st.text_area(
        "One per line: song name + artist", value=EXAMPLE_SONGS, height=140
    )

    files = st.file_uploader(
        "Optional: upload your Spotify data export (.zip, or the Streaming_History .json files)",
        type=["zip", "json"],
        accept_multiple_files=True,
    )
    st.caption(
        "Your files are read in memory just to find your top songs. They aren't saved, "
        "and your history is forgotten when you close the page. "
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
    skip_instrumental = col1.checkbox("Skip instrumentals & karaoke", value=True)
    skip_ugc = col2.checkbox("Skip fan uploads (official songs only)", value=True)
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
        "skip_ugc": skip_ugc,
        "skip_known": skip_known and bool(known_keys),
        "language": language,
        "size": size,
        "per_artist": per_artist,
    }

    # ---- Find songs
    if st.button("🎵 Find songs", type="primary"):
        seeds = [line.strip() for line in manual_text.splitlines() if line.strip()]

        if use_spotify and spotify_top:
            if shuffle_seeds:
                pool = spotify_top[:50]
                chosen = random.sample(pool, min(n_spotify, len(pool)))
            else:
                chosen = spotify_top[:n_spotify]
            seeds += [f"{track} {artist}" for (track, artist), _ in chosen]

        unique = []
        lowered = set()
        for seed in seeds:
            if seed.lower() not in lowered:
                lowered.add(seed.lower())
                unique.append(seed)
        if len(unique) > MAX_SEEDS:
            st.info(f"Using the first {MAX_SEEDS} songs to keep things quick.")
            unique = unique[:MAX_SEEDS]

        if not unique:
            st.error("Add at least one song first.")
        else:
            progress = st.progress(0.0, text="Starting...")
            try:
                rows, missed, stats = generate(get_yt(), unique, known_keys, settings, progress)
                st.session_state["results"] = rows
                st.session_state["missed"] = missed
                st.session_state["stats"] = stats
                st.session_state["run_id"] = st.session_state.get("run_id", 0) + 1
            except Exception as error:
                st.error(
                    "Something went wrong talking to YouTube Music. "
                    f"Please try again in a minute. ({error})"
                )
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
                "Nothing made it through your filters. Try turning some off, "
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
                column_config={
                    "Keep": st.column_config.CheckboxColumn("Keep"),
                    "videoId": None,
                },
                disabled=["Song", "Artist", "Matches", "videoId"],
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

                st.subheader("Option B: Open it as a YouTube playlist (experimental)")
                st.write(
                    "These links build a playlist on YouTube from the songs. If you're signed in, "
                    "you can save it to your library, and it should then show up in YouTube Music. "
                    "This is a YouTube trick that can change or stop working, so if a link "
                    "doesn't work, use Option A."
                )
                for number, link in enumerate(make_youtube_links(kept), start=1):
                    st.link_button(f"▶️ Open playlist part {number}", link)

    st.divider()
    st.caption(
        "A hobby project. Not affiliated with Spotify, YouTube, or Apple. "
        "Recommendations come from YouTube Music's public 'similar songs' results."
    )


if __name__ == "__main__":
    main()
