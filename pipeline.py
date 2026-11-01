"""YouTube URL + prompt -> grounded, screenshot-rich guide on disk.

CLI:  uv run python pipeline.py <youtube-url> "<what you want>"
Each job lives in library/<video_id>-<timestamp>/:
  meta.json   url, title, prompt, current step, error
  guide.json  raw structured output from Gemini (+ per-step `verified`)
  guide.md    the readable guide (what the app renders and exports)
  qa.json     questions asked about the video
  images/     screenshots
"""

import difflib
import json
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import yt_dlp
from google import genai
from pydantic import BaseModel, Field

LIBRARY = Path(__file__).parent / "library"
# Compared on a 12-min and a 3-h video: Argon wrote ~6x more detail (exact commands, prompts, URLs) at ~3x the cost.
# Q&A answers are short and Flash got them right, so it stays on the cheaper model.
GUIDE_MODEL = "gemini-4-argon"
ASK_MODEL = "gemini-3.8-flash"
AGENTIC_AFTER_S = 600  # docs: agentic mode wins on videos longer than ~5-10 min
QUOTE_WINDOW_S = 90  # look for a quote this many seconds either side of its timestamp (Argon can be ~70s off on 3h videos)
MATCH_THRESHOLD = 0.6  # difflib ratio; auto-captions mis-hear words, so not 1.0

_client = None
_client_lock = threading.Lock()


def client():
    # one shared client: the SDK closes a Client when it's garbage-collected, even mid-request
    global _client
    with _client_lock:
        _client = _client or genai.Client()
    return _client


# ---------- what we ask Gemini for ----------


# Timestamps are MM:SS strings: asked for integer seconds, Gemini writes 4:28 as 428.
TIME_DESC = "Timestamp in the video as MM:SS (or H:MM:SS past an hour), e.g. 04:28."


def to_seconds(stamp):
    parts = [int(p) for p in re.findall(r"\d+", stamp)][-3:] or [0]
    return sum(v * 60**i for i, v in enumerate(reversed(parts)))


def add_seconds(obj):
    """Walk Gemini's output and add t_s / start_s / end_s ints next to the MM:SS strings."""
    if isinstance(obj, list):
        for x in obj:
            add_seconds(x)
    elif isinstance(obj, dict):
        for key, out in (("time", "t_s"), ("start", "start_s"), ("end", "end_s")):
            if isinstance(obj.get(key), str):
                obj[out] = to_seconds(obj[key])
        for v in obj.values():
            add_seconds(v)
    return obj


class Step(BaseModel):
    text_md: str = Field(description="One step, claim or fact, in Markdown. Keep exact tool names, settings, numbers and prompts the speaker gives.")
    time: str = Field(description=TIME_DESC)
    quote: str = Field(description="The speaker's exact words at that time that support this step, copied verbatim (8-30 words). Never paraphrase.")


class Screenshot(BaseModel):
    time: str = Field(description="When the screen shows something worth seeing. " + TIME_DESC)
    caption: str = Field(description="What the reader should notice in this frame.")


class Section(BaseModel):
    heading: str
    start: str = Field(description=TIME_DESC)
    end: str = Field(description=TIME_DESC)
    intro_md: str = Field(description="1-3 sentences of context for this section, only from the video.")
    steps: list[Step]
    screenshots: list[Screenshot]


class Guide(BaseModel):
    title: str
    summary: str = Field(description="2-4 sentences: what the video covers and who it is for.")
    sections: list[Section]


class Citation(BaseModel):
    time: str = Field(description=TIME_DESC)
    quote: str = Field(description="The speaker's exact words at that time, copied verbatim (8-30 words).")


class Answer(BaseModel):
    in_video: bool = Field(description="False if the video does not answer the question.")
    answer_md: str = Field(description="The answer in Markdown, using only what the video says or shows.")
    citations: list[Citation]


GUIDE_RULES = """You turn a YouTube video into a reference guide the reader keeps instead of re-watching.
Rules, in priority order:
1. Only include what is actually said or shown in the video. Never add outside knowledge, assumptions or "best practices" the speaker did not state. A missing step is better than an invented one.
2. Every step carries its time (MM:SS) and a verbatim quote of the speaker's words at that moment.
3. Be detailed: keep the concrete tool names, settings, numbers, prompts, formulas and sequences the speaker gives. Do not compress steps into vague summaries.
4. Screenshots: pick moments where the screen shows something useful (a UI, a setting, a slide, a diagram, code, a result). Never pick a talking head or a transition. Use as many as the content needs.
5. Follow the reader's request below for scope and format; if they ask for one part only, cover only that part."""

ANSWER_RULES = """You answer questions about a YouTube video using only what is said or shown in it.
If the video does not cover the question, set in_video=false and say plainly that the video does not cover it; do not answer from general knowledge.
Cite every claim with its time (MM:SS) and the speaker's verbatim words at that moment."""


def ask_gemini(model, url, duration, text, schema, system, previous_id=None, tries=2):
    """One Interactions call against the YouTube video. Returns (parsed schema, interaction id).
    Retries once: Gemini occasionally fails a long video run with a transient INTERNAL error."""
    for attempt in range(tries):
        try:
            return _ask_gemini_once(model, url, duration, text, schema, system, previous_id)
        except RuntimeError:
            if attempt == tries - 1:
                raise


def _ask_gemini_once(model, url, duration, text, schema, system, previous_id):
    parts = [{"type": "text", "text": text}]
    if not previous_id:  # follow-ups already have the video in their context
        video = {"type": "video", "uri": url}
        if duration > AGENTIC_AFTER_S:
            video["processing"] = "agentic"
        parts.insert(0, video)
    it = client().interactions.create(
        model=model,
        input=parts,
        system_instruction=system,
        response_format={"type": "text", "mime_type": "application/json", "schema": schema.model_json_schema()},
        previous_interaction_id=previous_id,
        background=True,  # long videos can take minutes; poll instead of holding a connection
    )
    while it.status in ("in_progress", "queued"):
        time.sleep(5)
        it = client().interactions.get(it.id)
    if it.status != "completed":
        raise RuntimeError(f"Gemini interaction {it.id} ended with status {it.status}: {it.errors}")
    return schema.model_validate_json(it.output_text), it.id


# ---------- video metadata + captions ----------


YDL_BASE = {
    "quiet": True, "no_warnings": True, "noprogress": True,
    # YouTube guards its streams with a JavaScript challenge. Without a runtime to solve it (plus the yt-dlp-ejs
    # package), yt-dlp can hand back stream URLs that YouTube refuses, and every screenshot fails.
    "js_runtimes": {"deno": {}, "node": {}, "bun": {}},
}


def fetch_video(url, out_dir):
    """Metadata + English captions (manual preferred, auto fallback) as [(start_s, text)]."""
    opts = {
        **YDL_BASE,
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en", "en-US", "en-GB", "en-orig"],
        "subtitlesformat": "json3",
        "outtmpl": str(out_dir / "captions.%(ext)s"),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    cues = []
    for f in sorted(out_dir.glob("captions.*.json3")):
        cues = parse_json3(json.loads(f.read_text()))
        if cues:
            break
    return info, cues


def parse_json3(data):
    cues = []
    for ev in data.get("events", []):
        text = "".join(s.get("utf8", "") for s in ev.get("segs", [])).strip()
        if text:
            cues.append((ev.get("tStartMs", 0) / 1000, text))
    return cues


# ---------- grounding check ----------


def _words(s):
    return re.findall(r"[a-z0-9']+", s.lower())


def check_quote(quote, t_s, cues):
    """Look for `quote` in the captions within QUOTE_WINDOW_S of t_s.
    Returns (verified, t_s): verified is True/False, or None when there are no captions;
    on a match t_s snaps to the caption time where the quote starts.
    Quotes stitched with "..." pass only if every part is found."""
    if not cues:
        return None, t_s
    parts = [p for p in re.split(r"\.\.\.|…", quote) if len(_words(p)) >= 3] or [quote]
    results = [_check_part(p, t_s, cues) for p in parts]
    return all(ok for ok, _ in results), results[0][1]


def _check_part(quote, t_s, cues):
    q = _words(quote)
    near = [(w, start) for start, text in cues if abs(start - t_s) <= QUOTE_WINDOW_S for w in _words(text)]
    words = [w for w, _ in near]
    n = len(q)
    if not q or not near:
        return False, t_s
    ratio, i = max((difflib.SequenceMatcher(None, q, words[i : i + n]).ratio(), i) for i in range(max(1, len(words) - n + 1)))
    return (True, int(near[i][1])) if ratio >= MATCH_THRESHOLD else (False, t_s)


def verify(items, cues):
    """Set `verified` on each item with a quote and snap its t_s to the captions."""
    for it in items:
        it["verified"], it["t_s"] = check_quote(it["quote"], it["t_s"], cues)


# ---------- screenshots ----------


def stream_url(url):
    # ponytail: streams frames over HTTP instead of downloading; fall back to a full download if YouTube throttles range requests
    with yt_dlp.YoutubeDL({**YDL_BASE, "format": "bv*[height<=1080][ext=mp4]/bv*[height<=1080]/b"}) as ydl:
        info = ydl.extract_info(url, download=False)
    return info["url"], info.get("http_headers", {})


def grab_frame(src, headers, t_s, dest):
    """Most representative frame in a 3s window around t_s (ffmpeg's thumbnail filter skips blur/transition frames).
    thumbnail=40 must be <= frames in the window (3s x fps), else ffmpeg exits 0 without writing anything.
    Returns None on success, else what went wrong."""
    hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-headers", hdr, "-ss", str(max(0, t_s - 1.5)), "-i", src,
           "-t", "3", "-vf", "thumbnail=40", "-frames:v", "1", "-q:v", "3", str(dest)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "ffmpeg timed out reading the video stream"
    if r.returncode == 0 and dest.exists():
        return None
    return (r.stderr.strip().splitlines() or ["ffmpeg wrote no frame"])[-1][:300]


def take_screenshots(job, url, shots, duration):
    """Grab every screenshot into job/images and set sh["file"] (None if it couldn't be grabbed).
    Failures are retried with a freshly fetched stream URL: when YouTube refuses a stream URL,
    every grab from it fails, so retrying the same URL never helps. Returns the last error, or None."""
    clamp = lambda t: min(t, max(0, duration - 1))  # noqa: E731
    todo, files, error = sorted({clamp(sh["t_s"]) for sh in shots}), {}, None
    for attempt in range(3):
        if not todo:
            break
        if attempt:
            time.sleep(3 * attempt)
        src, headers = stream_url(url)

        def shoot(t):
            name = f"shot_{t:05d}.jpg"
            return t, name, grab_frame(src, headers, t, job / "images" / name)

        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(shoot, todo))
        files.update({t: name for t, name, err in results if err is None})
        failed = [(t, err) for t, _, err in results if err is not None]
        todo, error = [t for t, _ in failed], (failed[-1][1] if failed else None)
    for sh in shots:
        sh["file"] = files.get(clamp(sh["t_s"]))
    return error


def retake_screenshots(job):
    """Grab the screenshots a finished guide is missing (no Gemini call). Returns how many are still missing."""
    meta, guide = read_json(job / "meta.json"), read_json(job / "guide.json")
    missing = [sh for sec in guide["sections"] for sh in sec["screenshots"] if not sh.get("file")]
    error = take_screenshots(job, meta["url"], missing, meta["duration"])
    write_json(job / "guide.json", guide)
    (job / "guide.md").write_text(guide_markdown(guide, meta))
    still = sum(1 for sh in missing if not sh["file"])
    set_meta(job, missing_shots=still, shot_error=error if still else None)
    return still


# ---------- markdown ----------


def ts(s):
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def at(url, s):
    return f"{url}{'&' if '?' in url else '?'}t={int(s)}s"


MARK = {True: "✓", False: "⚠ quote not found in captions", None: ""}


def guide_markdown(guide, meta):
    url = meta["url"]
    lines = [f"# {guide['title']}", "",
             f"*Source: [{meta['title']}]({url}) · {meta.get('channel', '')} · {ts(meta.get('duration', 0))}*", "",
             f"> Asked for: {meta['prompt']}", "", guide["summary"], ""]
    for sec in guide["sections"]:
        steps = sec["steps"]
        checked = [st["verified"] for st in steps if st["verified"] is not None]
        tally = f" · {sum(checked)}/{len(checked)} checked" if checked else (" · not checked (no captions)" if steps else "")
        lines += [f"## {sec['heading']}", "", f"[▶ {ts(sec['start_s'])}–{ts(sec['end_s'])}]({at(url, sec['start_s'])}){tally}", ""]
        if sec["intro_md"]:
            lines += [sec["intro_md"], ""]
        # attach each screenshot to the last step at or before it, so images sit where they belong
        shots = {}
        for sh in sec["screenshots"]:
            if sh.get("file"):
                idx = max([i for i, st in enumerate(steps) if st["t_s"] <= sh["t_s"]], default=0)
                shots.setdefault(idx, []).append(sh)
        for i, st in enumerate(steps):
            # chip + mark go on the step's first line; any sub-list Gemini wrote is indented to stay inside this item
            first, _, rest = st["text_md"].strip().partition("\n")
            lines.append(f"{i + 1}. {first} [▶ {ts(st['t_s'])}]({at(url, st['t_s'])}) {MARK[st['verified']]}".rstrip())
            if rest.strip():
                lines += ["", *("    " + ln if ln.strip() else "" for ln in rest.splitlines())]
            for sh in shots.get(i, []):
                lines += ["", f"    ![{sh['caption']}](images/{sh['file']})", f"    *{sh['caption']}*"]
            lines.append("")
        if not steps:  # screenshots-only section
            for sh in shots.get(0, []):
                lines += [f"![{sh['caption']}](images/{sh['file']})", f"*{sh['caption']}*", ""]
    return "\n".join(lines)


def qa_markdown(qa, url):
    lines = []
    for item in qa:
        lines += [f"### Q: {item['question']}", ""]
        if not item["in_video"]:
            lines += ["*Not covered in this video.*", ""]
        lines += [item["answer_md"], ""]
        for c in item["citations"]:
            lines.append(f"- [▶ {ts(c['t_s'])}]({at(url, c['t_s'])}) “{c['quote']}” {MARK[c['verified']]}".rstrip())
        lines.append("")
    return "\n".join(lines)


# ---------- jobs ----------


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def set_meta(job, **kw):
    meta = read_json(job / "meta.json", {})
    meta.update(kw)
    write_json(job / "meta.json", meta)
    return meta


def new_job(url, prompt):
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{11})", url)
    if not m:
        raise ValueError("That doesn't look like a YouTube video URL")
    vid = m.group(1)
    job = LIBRARY / f"{vid}-{datetime.now():%Y%m%d-%H%M%S}"
    (job / "images").mkdir(parents=True)
    url = f"https://www.youtube.com/watch?v={vid}"  # one canonical form, so timestamp links are predictable
    set_meta(job, url=url, prompt=prompt, title=url, video_id=vid, step="Queued", stage=0, error=None,
             created=datetime.now().isoformat(timespec="seconds"))
    return job


def run(job):
    """Full pipeline for a job folder created by new_job(). Records progress/errors in meta.json."""
    try:
        meta = read_json(job / "meta.json")
        set_meta(job, step="Reading video info and captions", stage=1)
        info, cues = fetch_video(meta["url"], job)
        meta = set_meta(job, title=info.get("title", meta["url"]), channel=info.get("channel", ""),
                        duration=info.get("duration") or 0, video_id=info.get("id"), has_captions=bool(cues))
        write_json(job / "cues.json", cues)

        set_meta(job, step="Gemini is watching the video (long videos take a few minutes)", stage=2)
        guide, _ = ask_gemini(GUIDE_MODEL, meta["url"], meta["duration"], f"Reader's request: {meta['prompt']}", Guide, GUIDE_RULES)
        guide = add_seconds(guide.model_dump())

        set_meta(job, step="Checking every quote against the captions", stage=3)
        for sec in guide["sections"]:
            verify(sec["steps"], cues)

        shots = [sh for sec in guide["sections"] for sh in sec["screenshots"]]
        times = sorted({min(sh["t_s"], max(0, meta["duration"] - 1)) for sh in shots})
        set_meta(job, step=f"Grabbing {len(times)} screenshots", stage=4)
        shot_error = take_screenshots(job, meta["url"], shots, meta["duration"])

        write_json(job / "guide.json", guide)
        (job / "guide.md").write_text(guide_markdown(guide, meta))
        set_meta(job, step="Done", stage=5, missing_shots=sum(1 for sh in shots if not sh["file"]), shot_error=shot_error)
    except Exception as e:  # surface any failure on the page instead of a silent stuck job
        set_meta(job, step="Failed", error=f"{type(e).__name__}: {e}")
        raise


def ask(job, question):
    """Answer a question from the video, with checked citations. Appends to qa.json."""
    meta = read_json(job / "meta.json")
    qa = read_json(job / "qa.json", [])
    cues = [tuple(c) for c in read_json(job / "cues.json", [])]
    prev = qa[-1].get("interaction_id") if qa else None
    try:
        ans, iid = ask_gemini(ASK_MODEL, meta["url"], meta["duration"], question, Answer, ANSWER_RULES, previous_id=prev)
    except Exception:
        if not prev:
            raise
        # follow-up context failed (e.g. expired) -> ask fresh with the video
        ans, iid = ask_gemini(ASK_MODEL, meta["url"], meta["duration"], question, Answer, ANSWER_RULES)
    item = {"question": question, "interaction_id": iid, **add_seconds(ans.model_dump())}
    verify(item["citations"], cues)
    qa.append(item)
    write_json(job / "qa.json", qa)
    return item


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit('usage: uv run python pipeline.py <youtube-url> "<what you want>"')
    j = new_job(sys.argv[1], sys.argv[2])
    print("job:", j)
    run(j)
    print((j / "guide.md").read_text())
