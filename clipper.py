#!/usr/bin/env python3
"""
GTA 6 Auto-Clipper
------------------
Finds the most-viewed GTA 6 moments from streamers you're allowed to clip,
turns them into vertical 9:16 shorts with animated captions + a hook,
and posts them to TikTok, Instagram Reels and YouTube Shorts.

Pipeline:  Twitch clips API -> yt-dlp -> faster-whisper -> ffmpeg -> Upload-Post API

Commands:
  python clipper.py check              # verify setup
  python clipper.py discover           # who's blowing up on GTA 6 right now (all streamers)
  python clipper.py run                # fetch + render once
  python clipper.py run --loop 20      # keep running every 20 min
  python clipper.py approve            # watch each rendered clip, y/n to approve
  python clipper.py post               # schedule approved clips (spaced out)
  python clipper.py stats              # counts by status
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent


def _bin(name):
    for d in ("/opt/homebrew/opt/ffmpeg-full/bin", "/usr/local/opt/ffmpeg-full/bin"):
        if Path(d, name).exists():
            return str(Path(d, name))
    return name


FFMPEG, FFPROBE = _bin("ffmpeg"), _bin("ffprobe")
UTC = dt.timezone.utc


# ───────────────────────── config / env ─────────────────────────
def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config() -> dict:
    load_env(ROOT / ".env")
    cfg_path = ROOT / "config.yaml"
    if not cfg_path.exists():
        sys.exit("config.yaml not found. Copy config.example.yaml to config.yaml and fill it in.")
    cfg = yaml.safe_load(cfg_path.read_text())
    for k in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "BUFFER_API_KEY", "SUPABASE_URL", "SUPABASE_KEY",
              "UPLOAD_POST_API_KEY", "UPLOAD_POST_USER", "ANTHROPIC_API_KEY"):
        if k in os.environ:
            os.environ[k] = os.environ[k].strip()
    cfg.setdefault("work_dir", "work")
    CAMPAIGNS.update({k.lower(): v for k, v in (cfg.get("campaigns") or {}).items()})
    # cloud overrides (set in the GitHub workflow)
    if os.getenv("WHISPER_MODEL"):
        cfg["whisper_model"] = os.getenv("WHISPER_MODEL")
    if os.getenv("CAPTION_FONT"):
        cfg["style"]["font"] = os.getenv("CAPTION_FONT")
    if os.getenv("AUTO_POST") == "1":
        cfg["posting"]["auto_post"] = True
    for sub in ("raw", "review", "approved", "posted", "rejected"):
        (ROOT / cfg["work_dir"] / sub).mkdir(parents=True, exist_ok=True)
    return cfg


CAMPAIGNS = {}  # twitch login -> required hashtag (filled from config.yaml "campaigns")


_alerted = set()


def alert(title: str, msg: str, key: str = "") -> None:
    """Push a problem alert to Jacob's phone (ntfy). Sent at most once per run per key."""
    k = key or title
    if k in _alerted:
        return
    _alerted.add(k)
    try:
        cfgp = ROOT / "config.yaml"
        topic = (yaml.safe_load(cfgp.read_text()).get("posting") or {}).get("ntfy_topic")
        if topic:
            requests.post(f"https://ntfy.sh/{topic}", data=msg.encode(), timeout=20,
                          headers={"Title": title.encode("ascii", "ignore").decode().strip(), "Tags": "warning", "Priority": "high"})
    except Exception:
        pass


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ───────────────────────── state db ─────────────────────────
def db(cfg) -> sqlite3.Connection:
    con = sqlite3.connect(ROOT / cfg["work_dir"] / "state.db")
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE IF NOT EXISTS clips(
            id TEXT PRIMARY KEY, streamer TEXT, title TEXT, url TEXT, views INT,
            created_at TEXT, status TEXT, video TEXT, meta TEXT,
            scheduled_at TEXT, posted_at TEXT, error TEXT)"""
    )
    try:
        con.execute("ALTER TABLE clips ADD COLUMN platforms TEXT")
    except sqlite3.OperationalError:
        pass
    return con


# ───────────────────────── Twitch ─────────────────────────
class Twitch:
    API = "https://api.twitch.tv/helix"

    def __init__(self):
        cid, secret = os.getenv("TWITCH_CLIENT_ID"), os.getenv("TWITCH_CLIENT_SECRET")
        if not cid or not secret:
            sys.exit("Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env")
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            params={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"},
            timeout=20,
        )
        r.raise_for_status()
        self.h = {"Client-Id": cid, "Authorization": f"Bearer {r.json()['access_token']}"}

    def get(self, path, **params):
        r = requests.get(f"{self.API}/{path}", headers=self.h, params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def game_id(self, name: str) -> str:
        data = self.get("games", name=name)["data"]
        if not data:
            data = self.get("search/categories", query=name, first=5)["data"]
        if not data:
            sys.exit(f"Twitch category '{name}' not found. Check game_name in config.yaml.")
        return data[0]["id"]

    def user_ids(self, logins: list[str]) -> dict[str, str]:
        out = {}
        for i in range(0, len(logins), 100):
            chunk = logins[i : i + 100]
            for u in self.get("users", login=chunk)["data"]:
                out[u["login"].lower()] = u["id"]
        return out

    def clips(self, since: dt.datetime, **filt) -> list[dict]:
        out, cursor = [], None
        for _ in range(5):  # up to 500 clips
            params = dict(filt, started_at=since.strftime("%Y-%m-%dT%H:%M:%SZ"), first=100)
            if cursor:
                params["after"] = cursor
            js = self.get("clips", **params)
            out += js["data"]
            cursor = js.get("pagination", {}).get("cursor")
            if not cursor:
                break
        return out


BLOCK = re.compile(
    r"strip ?club|stripper|strip ?tease|lap ?dance|private dance|pole ?danc|horny|\bsex|nsfw|nude|naked|porn|onlyfans|"
    r"boob|tits|titty|booty|\bbutt\b|butt ?plug|\bplug\b|panties|undies|thong|dildo|vibrator|condom|bend over|"
    r"\bthicc?\b|gyat|\bcheeks\b|bikini|lingerie|\bfreaky\b|\bsexy\b|\bstrip\b|"
    r"twerk|hooker|prostitut|brothel|escort|\brape|thirst|\bnut\b|gooning|\bhot ?tub|"
    r"n[i1]gg|f[a@]gg?[o0]?t|\bretard|tranny|\bkys\b|kill yourself", re.I)
SWEARS = re.compile(r"\b(f+u+c+k\w*|sh[i1]t\w*|b[i1]tch\w*|ass(hole)?|dick\w*|pussy|c+u+n+t\w*|damn)\b", re.I)


def is_inappropriate(*texts) -> bool:
    return any(BLOCK.search(t or "") for t in texts)


def censor(word: str) -> str:
    return SWEARS.sub(lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1), word)


_nude = None
NSFW_HARD = {"FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED",
             "BUTTOCKS_EXPOSED", "ANUS_EXPOSED"}
NSFW_SOFT = {"FEMALE_BREAST_COVERED", "BUTTOCKS_COVERED", "FEMALE_GENITALIA_COVERED", "BELLY_EXPOSED"}


def nsfw_check(video: Path, start: float = 0, length: float = 600) -> str:
    """Scan ~1 frame/second with NudeNet. Returns a reason string if the clip should be blocked."""
    global _nude
    try:
        from nudenet import NudeDetector
    except ImportError:
        return ""
    if _nude is None:
        _nude = NudeDetector()
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        subprocess.run([FFMPEG, "-v", "error", "-ss", f"{start:.2f}", "-i", str(video), "-t", f"{length:.2f}",
                        "-vf", "fps=1,scale=640:-2", f"{td}/f%03d.jpg"], check=True)
        soft_hits = 0
        for f in sorted(Path(td).glob("*.jpg")):
            for d in _nude.detect(str(f)):
                if d["class"] in NSFW_HARD and d["score"] >= 0.35:
                    return f"nudity ({d['class'].lower()})"
                if d["class"] in NSFW_SOFT and d["score"] >= 0.40:
                    soft_hits += 1
        if soft_hits >= 2:
            return f"sexual/suggestive visuals ({soft_hits} frames)"
    return ""


def views_per_hour(c: dict) -> float:
    age_h = (dt.datetime.now(UTC) - dt.datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))).total_seconds() / 3600
    return c["view_count"] / max(age_h, 0.25)


# ───────────────────────── download ─────────────────────────
def download(url: str, dest: Path) -> Path:
    out = dest.with_suffix(".mp4")
    if out.exists():
        return out
    subprocess.run(
        ["yt-dlp", "-q", "--no-warnings", "-f", "best[ext=mp4]/best", "-o", str(out), url],
        check=True,
    )
    return out


def duration(path: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(r.stdout.strip())


def best_window(path: Path, total: float, win: float) -> float:
    """Start time of the `win`-second stretch with the most audio energy (the hype part)."""
    if total <= win:
        return 0.0
    import numpy as np
    pcm = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
                         capture_output=True, check=True).stdout
    a = np.frombuffer(pcm, np.int16).astype(np.float32)
    step = 4000  # 0.5s
    n = len(a) // step
    if n == 0:
        return 0.0
    rms = np.sqrt((a[: n * step].reshape(n, step) ** 2).mean(axis=1))
    w = int(win * 2)
    if n <= w:
        return 0.0
    sums = np.convolve(rms, np.ones(w), "valid")
    return float(np.argmax(sums)) / 2


# ───────────────────────── transcription ─────────────────────────
_whisper = None


def transcribe(path: Path, model_size: str) -> list[dict]:
    """Returns [{'w': word, 's': start, 'e': end}, ...]"""
    global _whisper
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log("faster-whisper not installed -> no captions")
        return []
    if _whisper is None:
        log(f"loading whisper model '{model_size}' (first run downloads it)…")
        _whisper = WhisperModel(model_size, device="auto", compute_type="int8")
    import numpy as np
    pcm = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, check=True,
    ).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    segs, _ = _whisper.transcribe(audio, word_timestamps=True, vad_filter=True)
    words = []
    for seg in segs:
        for w in seg.words or []:
            t = w.word.strip()
            if t:
                words.append({"w": t, "s": float(w.start), "e": float(w.end)})
    return words


# ───────────────────────── hook / caption writing ─────────────────────────
def write_copy(cfg, streamer: str, clip_title: str, words: list[dict]) -> dict:
    transcript = " ".join(w["w"] for w in words)[:2500]
    tags = cfg["posting"]["hashtags"]
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        prompt = f"""You write viral short-form copy for GTA 6 streamer clips.
Streamer: {streamer}
Twitch clip title: {clip_title}
Transcript: {transcript or "(no speech)"}

Return ONLY JSON with:
"hook": on-screen text, max 7 words, creates curiosity or shock, no hashtags, no emojis, never reveal the punchline
"caption": 1 short line for TikTok/IG, may include 1 emoji, mention the streamer
"yt_title": YouTube Shorts title under 70 chars, include "{streamer}" and "GTA 6"
Do not invent events that aren't in the transcript/title."""
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": cfg.get("llm_model", "claude-haiku-5-5"), "max_tokens": 300,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=60,
            )
            r.raise_for_status()
            text = r.json()["content"][0]["text"]
            js = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
            js["hashtags"] = tags
            return js
        except Exception as e:  # fall back to template
            log(f"LLM copy failed ({e}); using template")
    t = re.sub(r"\s+", " ", clip_title).strip()
    clean = re.fullmatch(r"[A-Za-z0-9 ,.!?'\-]{6,70}", t) and not is_inappropriate(t)
    if clean:
        nice = t[0].upper() + t[1:]
        return {"hook": nice, "caption": f"{nice} 😂", "hashtags": tags + [f"#{re.sub(r'[^a-z0-9]', '', streamer.lower())}"],
                "yt_title": (f"{streamer} GTA 6: {nice}" if streamer.lower() not in t.lower() else nice)[:70]}
    return {
        "hook": (t[:42] + "…") if len(t) > 43 else t,
        "caption": f"{streamer} in GTA 6 💀",  # fallback
        "yt_title": f"{streamer} - {t}"[:70] if "gta" in t.lower() else f"{streamer} GTA 6: {t}"[:70],
        "hashtags": tags,
    }


# ───────────────────────── subtitles (ASS) ─────────────────────────
def ass_time(t: float) -> str:
    t = max(t, 0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(words, hook, credit, clip_len, style, path: Path, offset: float) -> None:
    font = style.get("font", "Arial Black")
    size = style.get("caption_size", 86)
    hi = style.get("highlight_color", "&H0000F2FF")  # ASS = &HAABBGGRR (yellow)
    cap_y = style.get("caption_y", 0.70)
    margin_v = int(1920 * (1 - cap_y))
    hook_secs = style.get("hook_seconds", 0) or clip_len

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,2,60,60,{margin_v},1
Style: Hook,{font},{style.get('hook_size', 64)},&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,8,90,90,{style.get('hook_margin', 170)},1
Style: Credit,{font},38,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,0,2,40,40,70,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    if hook:
        ev.append(f"Dialogue: 2,{ass_time(0)},{ass_time(hook_secs)},Hook,,0,0,0,,{ass_escape(hook.upper())}")
    if credit:
        ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(clip_len)},Credit,,0,0,0,,{ass_escape(credit)}")

    # word-by-word karaoke captions, chunks of N words
    n = style.get("words_per_line", 3)
    ws = [dict(w, s=w["s"] - offset, e=w["e"] - offset) for w in words if w["e"] - offset > 0]
    for i in range(0, len(ws), n):
        chunk = ws[i : i + n]
        for j, w in enumerate(chunk):
            start = max(w["s"], 0)
            end = chunk[j + 1]["s"] if j + 1 < len(chunk) else w["e"] + 0.15
            if i + n < len(ws):
                end = min(end, ws[i + n]["s"])
            if end <= start:
                end = start + 0.08
            parts = []
            for k, x in enumerate(chunk):
                txt = ass_escape(censor(x["w"]).upper().strip(".,"))
                parts.append(f"{{\\c{hi}\\fscx112\\fscy112}}{txt}{{\\r}}" if k == j else txt)
            ev.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Cap,,0,0,0,,{' '.join(parts)}")
    path.write_text(head + "\n".join(ev) + "\n", encoding="utf-8")


# ───────────────────────── burned-in caption detection ─────────────────────────
_STOP = set("that this with have what just like they them then there their your youre about would could should "
            "were been from into because yeah okay gonna wanna really right know think going here where when "
            "dont cant will well were what's that's it's i'm".split())


def has_burned_captions(src: Path, start: float, length: float, words: list) -> bool:
    """True if the stream already shows its own captions (on-screen text matches what's being said)."""
    try:
        import pytesseract
        from PIL import Image, ImageOps
    except ImportError:
        return False
    spoken = {re.sub(r"[^a-z']", "", w["w"].lower()) for w in words}
    spoken = {w for w in spoken if len(w) >= 4 and w not in _STOP}
    if len(spoken) < 4:
        return False
    import tempfile
    seen = set()
    try:
        with tempfile.TemporaryDirectory() as td:
            subprocess.run([FFMPEG, "-v", "error", "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{length:.2f}",
                            "-vf", "fps=2/3,scale=1920:-2", f"{td}/f%03d.png"], check=True)
            for f in sorted(Path(td).glob("*.png")):
                g = ImageOps.grayscale(Image.open(f))
                bright = ImageOps.invert(g.point(lambda p: 255 if p > 190 else 0))  # white caption text
                for im in (g, bright):
                    txt = pytesseract.image_to_string(im, config="--psm 11")
                    seen |= {re.sub(r"[^a-z']", "", t.lower()) for t in txt.split()}
    except Exception as e:
        log(f"   caption check skipped: {e}")
        return False
    hit = spoken & seen
    ratio = len(hit) / len(spoken)
    if os.getenv("CAPTION_DEBUG"):
        log(f"   caption check: {len(hit)}/{len(spoken)} spoken words seen on screen {sorted(hit)[:12]}")
    return len(hit) >= 4 and ratio >= 0.15


# ───────────────────────── webcam detection ─────────────────────────
def detect_cam(src: Path, start: float, length: float):
    """Find the streamer's webcam: a face that stays in the same spot across frames.
    Returns (x, y, w, h) as fractions of the frame, or None. Never raises."""
    try:
        return _detect_cam(src, start, length)
    except Exception as e:
        log(f"   webcam detection skipped: {e}")
        return None


def _detect_cam(src: Path, start: float, length: float):
    import cv2
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        subprocess.run([FFMPEG, "-v", "error", "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{length:.2f}",
                        "-vf", "fps=1/2,scale=960:-2", f"{td}/f%03d.jpg"], check=True)
        frames = sorted(Path(td).glob("*.jpg"))
        if not frames:
            return None
        casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        hits, W, H = [], 960, 540
        for f in frames:
            img = cv2.imread(str(f))
            H, W = img.shape[:2]
            g = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
            for (x, y, w, h) in casc.detectMultiScale(g, 1.1, 6, minSize=(int(W * 0.04), int(W * 0.04))):
                hits.append((x + w / 2, y + h / 2, w))
        if not hits:
            return None
        # webcam = the face position that repeats across many frames (game faces move around)
        best, best_n = None, 0
        for (cx, cy, w) in hits:
            n = sum(1 for (x2, y2, _) in hits if abs(x2 - cx) < W * 0.05 and abs(y2 - cy) < H * 0.06)
            if n > best_n:
                best, best_n = (cx, cy, w), n
        if best_n < max(2, len(frames) * 0.4):
            return None
        group = [h for h in hits if abs(h[0] - best[0]) < W * 0.05 and abs(h[1] - best[1]) < H * 0.06]
        cx = sum(h[0] for h in group) / len(group)
        cy = sum(h[1] for h in group) / len(group)
        fw = sorted(h[2] for h in group)[len(group) // 2]
        # webcam box ≈ face × 3.2 wide, 16:9-ish, face slightly above centre
        bw = min(W, fw * 3.2)
        bh = min(H, bw * 0.75)
        x0 = min(max(0, cx - bw / 2), W - bw)
        y0 = min(max(0, cy - bh * 0.45), H - bh)
        return (x0 / W, y0 / H, bw / W, bh / H)


# ───────────────────────── render ─────────────────────────
def render(src: Path, out: Path, ass: Path, layout: dict, start: float, length: float) -> None:
    ass_f = ass.name  # ffmpeg runs inside the subtitle's folder, so a plain filename is enough
    mode = layout.get("mode", "blur")
    if mode == "bubble":
        cam = layout.get("_cam")
        game = "[g]crop=ih*9/16:ih,scale=1080:1920:flags=lanczos,unsharp=5:5:0.7:5:5:0.0,setsar=1[game]"
        if cam:
            x, y, w, h = cam
            bw, bh = layout.get("bubble_w", 460), int(layout.get("bubble_w", 460) * 0.8)
            fc = (
                f"[0:v]split=2[g][c];{game};"
                f"[c]crop=iw*{w:.4f}:ih*{h:.4f}:iw*{x:.4f}:ih*{y:.4f},"
                f"scale={bw}:{bh}:force_original_aspect_ratio=increase:flags=lanczos,crop={bw}:{bh},"
                "pad=iw+12:ih+12:6:6:white[cam];"
                f"[game][cam]overlay=40:{layout.get('bubble_y', 170)},"
                f"ass=filename={ass_f}[v]"
            )
        else:
            fc = f"[0:v]null[g];{game};[game]ass=filename={ass_f}[v]"
    elif mode == "full":
        cam = layout.get("_cam")
        if cam:
            x, y, w, h = cam
            fc = (
                "[0:v]split=3[a][b][c];"
                "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:6,eq=brightness=-0.15[bg];"
                f"[b]crop=iw*{w:.4f}:ih*{h:.4f}:iw*{x:.4f}:ih*{y:.4f},scale=1080:720:force_original_aspect_ratio=increase,crop=1080:720[cam];"
                "[c]scale=1080:-2[game];"
                "[bg][cam]overlay=0:150[t1];[t1][game]overlay=0:890,setsar=1,"
                f"ass=filename={ass_f}[v]"
            )
        else:
            fc = (
                "[0:v]split=2[a][b];"
                "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:6,eq=brightness=-0.15[bg];"
                "[b]scale=1080:-2[game];"
                "[bg][game]overlay=0:(H-h)/2-80,setsar=1,"
                f"ass=filename={ass_f}[v]"
            )
    elif mode == "stack" and layout.get("facecam"):
        x, y, w, h = layout["facecam"]  # fractions of source frame
        top_h = layout.get("facecam_height", 760)
        bot_h = 1920 - top_h
        game_aspect = 1080 / bot_h
        fc = (
            f"[0:v]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},scale=1080:{top_h}:force_original_aspect_ratio=increase,"
            f"crop=1080:{top_h}[cam];"
            f"[0:v]crop=ih*{game_aspect:.4f}:ih,scale=1080:{bot_h}[game];"
            f"[cam][game]vstack=inputs=2,setsar=1,ass=filename={ass_f}[v]"
        )
    else:
        zoom = layout.get("game_aspect", 1.25)  # width/height of the centre gameplay crop
        fc = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
            "boxblur=25:6,eq=brightness=-0.12[bg];"
            f"[0:v]crop=min(iw\\,ih*{zoom}):ih,scale=1080:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2-{layout.get('lift', 60)},setsar=1,ass=filename={ass_f}[v]"
        )
    cmd = [
        FFMPEG, "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{length:.2f}",
        "-filter_complex", fc, "-map", "[v]", "-map", "0:a?",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-c:v", "libx264", "-preset", "medium", "-profile:v", "high", "-crf", "18", "-maxrate", "3.5M", "-bufsize", "7M", "-r", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(out.resolve()),
    ]
    cmd[cmd.index("-i") + 1] = str(src.resolve())
    try:
        subprocess.run(cmd, check=True, cwd=str(ass.parent))
    except Exception:
        out.unlink(missing_ok=True)
        raise


def process_clip(cfg, con, clip: dict, streamer_cfg: dict) -> None:
    wd = ROOT / cfg["work_dir"]
    cid = clip["id"]
    streamer = streamer_cfg.get("display", clip["broadcaster_name"])
    try:
        raw = download(clip["url"], wd / "raw" / cid)
        total = duration(raw)
        words = transcribe(raw, cfg.get("whisper_model", "small"))
        if is_inappropriate(clip["title"], " ".join(w["w"] for w in words)):
            con.execute("UPDATE clips SET status='skipped', error='inappropriate content' WHERE id=?", (cid,))
            con.commit()
            log(f"🚫 skipped {streamer}: inappropriate content")
            return

        # trim dead air at the start: begin just before the first spoken word (max 4s trimmed)
        target = cfg.get("max_seconds", 30)
        start = best_window(raw, total, target)
        if words and cfg["style"].get("trim_start", True):
            inside = [w for w in words if start <= w["s"] < start + target]
            if inside and inside[0]["s"] - 0.35 > start:
                start = min(inside[0]["s"] - 0.35, start + 3.0)
        length = min(total - start, target)

        layout = {**cfg.get("layout", {}), **streamer_cfg.get("layout", {})}
        if layout.get("mode") == "bubble":
            layout["_cam"] = detect_cam(raw, start, length)
            cfg["style"]["caption_y"] = 0.70
            log(f"   layout: full-screen gameplay{' + face box' if layout['_cam'] else ''}")
        elif layout.get("mode") == "full":
            layout["_cam"] = detect_cam(raw, start, length)
            # captions sit below the stream when there's a webcam zoom, otherwise under the centred stream
            cfg["style"]["caption_y"] = 0.86 if layout["_cam"] else 0.78
            log(f"   layout: full stream{' + webcam zoom' if layout['_cam'] else ''}")
        bad = nsfw_check(raw, start, length)
        if bad:
            con.execute("UPDATE clips SET status='skipped', error=? WHERE id=?", (f"inappropriate: {bad}", cid))
            con.commit()
            log(f"🚫 skipped {streamer}: {bad}")
            return
        copy = write_copy(cfg, streamer, clip["title"], words)
        credit = streamer_cfg.get("credit") or cfg["style"].get("credit_template", "{streamer} • twitch.tv/{login}").format(
            streamer=streamer, login=clip["broadcaster_name"].lower())
        ass = wd / "raw" / f"{cid}.ass"
        mode_c = cfg["style"].get("burn_captions", True)
        add_caps = bool(mode_c)
        if mode_c == "auto":
            add_caps = not has_burned_captions(raw, start, length, words)
            log(f"   captions: {'adding mine' if add_caps else 'stream already has captions — keeping theirs only'}")
        build_ass(words if add_caps else [], copy["hook"] if cfg["style"].get("show_hook", True) else "", credit, length, cfg["style"], ass, start)

        target = "approved" if cfg["posting"].get("auto_post") else "review"
        out = wd / target / f"{re.sub(r'[^a-z0-9]+', '_', streamer.lower())}_{cid[:24]}.mp4"
        render(raw, out, ass, layout, start, length)

        meta = dict(copy, streamer=streamer, login=clip["broadcaster_name"].lower(),
                    clip_url=clip["url"], views=clip["view_count"])
        con.execute("UPDATE clips SET status=?, video=?, meta=? WHERE id=?",
                    (target, str(out), json.dumps(meta), cid))
        con.commit()
        log(f"✅ rendered [{target}] {streamer}: {copy['hook']}  -> {out.name}")
    except Exception as e:
        con.execute("UPDATE clips SET status='error', error=? WHERE id=?", (str(e)[:500], cid))
        con.commit()
        log(f"❌ {cid}: {e}")


# ───────────────────────── commands ─────────────────────────
def cmd_discover(cfg, args):
    tw = Twitch()
    gid = tw.game_id(cfg["game_name"])
    since = dt.datetime.now(UTC) - dt.timedelta(hours=args.hours)
    clips = tw.clips(since, game_id=gid)
    by = {}
    for c in clips:
        s = by.setdefault(c["broadcaster_name"], {"views": 0, "clips": 0, "best": c})
        s["views"] += c["view_count"]
        s["clips"] += 1
        if c["view_count"] > s["best"]["view_count"]:
            s["best"] = c
    print(f"\nTop GTA 6 streamers by clip views, last {args.hours}h (category id {gid}):\n")
    for name, s in sorted(by.items(), key=lambda kv: -kv[1]["views"])[:25]:
        print(f"{name:<22} {s['views']:>9,} views  {s['clips']:>3} clips   best: {s['best']['title'][:50]}")
    print("\nAdd the ones you have permission / a paid campaign for to config.yaml -> streamers.")


def cmd_hot(cfg, args):
    tw = Twitch()
    gid = tw.game_id(cfg["game_name"])
    since = dt.datetime.now(UTC) - dt.timedelta(hours=args.hours)
    clips = [c for c in tw.clips(since, game_id=gid) if c.get("language", "").startswith("en")]
    clips.sort(key=lambda c: -c["view_count"])
    print(f"HOT GTA 6 clips (English), last {args.hours}h:")
    for c in clips[:20]:
        age = (dt.datetime.now(UTC) - dt.datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))).total_seconds() / 3600
        print(f"HOT | {c['view_count']:>7,} | {c['broadcaster_name']:<16} | {age:4.1f}h ago | {c['title'][:60]}")


def cmd_run_once(cfg, con):
    tw = Twitch()
    gid = tw.game_id(cfg["game_name"])
    auto = cfg.get("streamers") == "auto"
    streamers = {} if auto else {s["login"].lower(): s for s in cfg["streamers"]}
    ids = {} if auto else tw.user_ids(list(streamers))
    since = dt.datetime.now(UTC) - dt.timedelta(hours=cfg["fetch"].get("lookback_hours", 24))
    f = cfg["fetch"]

    candidates = []
    if auto and not cfg.get("_any"):  # pick the N biggest streamers by GTA 6 clip views
        a = cfg.get("auto", {})
        block = {b.lower() for b in a.get("block_streamers", [])}
        pool = [c for c in tw.clips(since, game_id=gid)
                if (not a.get("language") or c.get("language", "").startswith(a["language"]))
                and c["broadcaster_name"].lower() not in block]
        allow = {b.lower() for b in a.get("only_streamers", [])}
        viral_any = cfg["fetch"].get("viral_any_views", 50000)
        if allow:
            pool = [c for c in pool if c["broadcaster_name"].lower() in allow or c["view_count"] >= viral_any]
        tot = {}
        for c in pool:
            tot[c["broadcaster_name"]] = tot.get(c["broadcaster_name"], 0) + c["view_count"]
        top = sorted(tot, key=lambda k: -tot[k])[: a.get("top_n", 5)]
        top += [c["broadcaster_name"] for c in pool if c["view_count"] >= viral_any and c["broadcaster_name"] not in top]
        log("auto-picked streamers: " + ", ".join(f"{t} ({tot[t]:,} views)" for t in top))
        block = {b.lower() for b in a.get("block_streamers", [])}
        pool = [c for c in pool if c["broadcaster_name"] in top]
        for c in pool:
            streamers.setdefault(c["broadcaster_name"].lower(), {"login": c["broadcaster_name"].lower(), "display": c["broadcaster_name"]})
        ids = {"_any": None}
    if cfg.get("_any"):  # test mode: top clips in the category from any streamer
        pool = tw.clips(since, game_id=gid)
        for c in pool:
            lg = c["broadcaster_name"].lower()
            streamers.setdefault(lg, {"login": lg, "display": c["broadcaster_name"]})
        ids = {"_any": None}
    for login, uid in ids.items():
        for c in (pool if login == "_any" else tw.clips(since, broadcaster_id=uid)):
            if login == "_any":
                login = c["broadcaster_name"].lower()
            if c["game_id"] != gid or c["view_count"] < f.get("min_views", 300):
                continue
            if c["duration"] < f.get("min_seconds", 12):
                continue
            if is_inappropriate(c["title"]):
                continue
            c["_score"] = c["view_count"] if cfg["fetch"].get("rank_by") == "views" else views_per_hour(c)
            if c["broadcaster_name"].lower() in CAMPAIGNS:
                c["_score"] *= cfg.get("campaign_boost", 4)  # paid campaigns first
            c["_login"] = c["broadcaster_name"].lower()
            candidates.append(c)

    # tiered quality bar: if any clip clears the premium bar, only use those
    premium = f.get("premium_views", 0)
    if premium and any(c["view_count"] >= premium for c in candidates):
        candidates = [c for c in candidates if c["view_count"] >= premium
                      or c["broadcaster_name"].lower() in CAMPAIGNS]
        log(f"premium mode: {len(candidates)} clips with {premium:,}+ views")
    # dedupe overlapping clips of the same moment (same VOD, offsets within 45s)
    candidates.sort(key=lambda c: -c["_score"])
    picked, seen_moments = [], []
    for c in candidates:
        key = (c.get("video_id") or c["broadcaster_id"], c.get("vod_offset") or 0)
        if any(k[0] == key[0] and key[1] and abs(k[1] - key[1]) < 45 for k in seen_moments):
            continue
        if con.execute("SELECT 1 FROM clips WHERE id=? AND NOT ((status='error' AND error='video file missing') OR (status='removed' AND error='cancelled (old layout)'))",
                       (c["id"],)).fetchone():
            continue
        seen_moments.append(key)
        picked.append(c)

    per = {}
    varied = []
    for c in picked:
        k = c["broadcaster_name"].lower()
        if per.get(k, 0) < f.get("max_per_streamer", 2):
            per[k] = per.get(k, 0) + 1
            varied.append(c)
    picked = varied
    backlog = con.execute("SELECT COUNT(*) n FROM clips WHERE status='approved'").fetchone()["n"]
    queued = con.execute("SELECT COUNT(*) n FROM clips WHERE status='scheduled' AND scheduled_at > ?",
                         (dt.datetime.now(UTC).isoformat(),)).fetchone()["n"]
    if cfg["posting"].get("auto_post") and (backlog >= 10 or queued >= 9):
        picked = []
        log(f"Buffer queue full ({queued} waiting) — not rendering more this round")
    elif cfg["posting"].get("auto_post"):
        picked = picked[: max(1, 9 - queued)]  # never render more than we can post
    picked = picked[: f.get("max_new_per_run", 6)]
    log(f"{len(candidates)} candidate clips, {len(picked)} new to render")
    for c in picked:
        con.execute("INSERT OR REPLACE INTO clips(id,streamer,title,url,views,created_at,status) VALUES(?,?,?,?,?,?,?)",
                    (c["id"], c["broadcaster_name"], c["title"], c["url"], c["view_count"], c["created_at"], "new"))
        con.commit()
        process_clip(cfg, con, c, streamers[c["_login"]])

    if cfg["posting"].get("auto_post"):
        cmd_post(cfg, con)


def cmd_ytvod(cfg, con, args):
    """Clip the most-replayed moments of a YouTube video/VOD and queue them for posting."""
    import hashlib
    url = args.url
    r = subprocess.run(["yt-dlp", "-J", "--no-warnings", url], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"❌ YouTube refused: {r.stderr.strip()[-400:]}")
        alert("YouTube clip failed", "YouTube blocked the cloud server from downloading that video. Claude needs another way in.", key="yt")
        sys.exit(1)
    info = json.loads(r.stdout)
    title, channel = info.get("title", ""), info.get("channel") or info.get("uploader") or "Streamer"
    dur = float(info.get("duration") or 0)
    log(f"YouTube: '{title}' by {channel} ({dur/3600:.1f} h)")
    heat = info.get("heatmap") or []
    clip_len = cfg.get("max_seconds", 25) + 8
    if heat:
        peaks = sorted(heat, key=lambda h: -h["value"])
        log(f"using YouTube 'most replayed' graph ({len(heat)} points)")
    else:
        log("no 'most replayed' data yet — sampling evenly across the video")
        step = max(dur / (args.n + 1), clip_len)
        peaks = [{"start_time": step * (i + 1), "end_time": step * (i + 1) + 1, "value": 1} for i in range(args.n)]
    chosen = []
    for h in peaks:
        mid = (h["start_time"] + h["end_time"]) / 2
        if mid < 60 or (dur and mid > dur - 30):
            continue
        if all(abs(mid - c) > 120 for c in chosen):
            chosen.append(mid)
        if len(chosen) >= args.n:
            break
    wd = ROOT / cfg["work_dir"]
    scfg = {"display": channel, "credit": f"{channel} • YouTube"}
    for mid in sorted(chosen):
        a, b = max(0, mid - clip_len * 0.45), mid + clip_len * 0.55
        cid = "yt_" + hashlib.md5(f"{url}{int(a)}".encode()).hexdigest()[:16]
        if con.execute("SELECT 1 FROM clips WHERE id=? AND status!='error'", (cid,)).fetchone():
            continue
        raw = wd / "raw" / f"{cid}.mp4"
        r = subprocess.run(["yt-dlp", "-q", "--no-warnings", "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b",
                            "--download-sections", f"*{a:.0f}-{b:.0f}", "--force-keyframes-at-cuts",
                            "--merge-output-format", "mp4", "-o", str(raw), url], capture_output=True, text=True)
        if r.returncode != 0 or not raw.exists():
            log(f"❌ download failed at {a/60:.0f} min: {r.stderr.strip()[-300:]}")
            continue
        clip = {"id": cid, "url": url, "title": f"{channel} GTA 6 moment", "broadcaster_name": channel,
                "view_count": args.views, "created_at": dt.datetime.now(UTC).isoformat()}
        con.execute("INSERT OR REPLACE INTO clips(id,streamer,title,url,views,created_at,status) VALUES(?,?,?,?,?,?,?)",
                    (cid, channel, clip["title"], url, args.views, clip["created_at"], "new"))
        con.commit()
        log(f"✂️  clipping {channel} at {int(a//3600)}:{int(a%3600//60):02d}:{int(a%60):02d}")
        process_clip(cfg, con, clip, scfg)
    if cfg["posting"].get("auto_post"):
        cmd_post(cfg, con)


def cmd_approve(cfg, con):
    rows = con.execute("SELECT * FROM clips WHERE status='review' ORDER BY views DESC").fetchall()
    if not rows:
        print("Nothing to review.")
        return
    wd = ROOT / cfg["work_dir"]
    for r in rows:
        meta = json.loads(r["meta"])
        print(f"\n▶ {meta['streamer']} | {r['views']:,} views on Twitch\n  HOOK: {meta['hook']}\n  CAPTION: {meta['caption']}")
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run([opener, r["video"]], check=False)
        a = input("  [y]es / [n]o / [c]hange caption / [q]uit > ").strip().lower()
        if a == "q":
            break
        if a == "c":
            meta["caption"] = input("  new caption > ").strip() or meta["caption"]
            a = "y"
        dest = "approved" if a == "y" else "rejected"
        newp = wd / dest / Path(r["video"]).name
        shutil.move(r["video"], newp)
        con.execute("UPDATE clips SET status=?, video=?, meta=? WHERE id=?", (dest, str(newp), json.dumps(meta), r["id"]))
        con.commit()


# ───────────────────────── Buffer (free) posting ─────────────────────────
BUFFER_GQL = "https://api.buffer.com"
CREATE_POST = """mutation($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id dueAt } }
    ... on MutationError { message }
  }
}"""


def buffer_q(query, variables=None):
    r = requests.post(BUFFER_GQL, timeout=60, json={"query": query, "variables": variables or {}},
                      headers={"Authorization": f"Bearer {os.getenv('BUFFER_API_KEY')}", "Content-Type": "application/json"})
    js = r.json()
    if r.status_code >= 300 or js.get("errors"):
        raise RuntimeError(f"Buffer {r.status_code}: {json.dumps(js.get('errors') or js)[:400]}")
    return js["data"]


def buffer_channels():
    orgs = buffer_q("query { account { organizations { id name } } }")["account"]["organizations"]
    chans = []
    for o in orgs:
        q = 'query { channels(input: {organizationId: "%s"}) { id name service isQueuePaused } }' % o["id"]
        for c in buffer_q(q)["channels"]:
            c["service"] = str(c["service"]).lower()
            chans.append(c)
    return chans


def sb_upload(path: Path) -> str:
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    name = path.name
    with open(path, "rb") as fh:
        r = requests.post(f"{url}/storage/v1/object/gta-clips/{name}", data=fh, timeout=600,
                          headers={"Authorization": f"Bearer {key}", "apikey": key,
                                   "Content-Type": "video/mp4", "x-upsert": "true"})
    if r.status_code >= 300:
        raise RuntimeError(f"upload {r.status_code}: {r.text[:300]}")
    return f"{url}/storage/v1/object/public/gta-clips/{name}"


def sb_cleanup(con):
    """Delete hosted clips that already went live more than 6h ago (keeps the free storage empty)."""
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    cutoff = (dt.datetime.now(UTC) - dt.timedelta(hours=6)).isoformat()
    rows = con.execute("SELECT id, video FROM clips WHERE status='scheduled' AND scheduled_at < ? AND (error IS NULL OR error != 'hosted-deleted')", (cutoff,)).fetchall()
    if not rows:
        return
    names = [Path(r["video"]).name for r in rows]
    r = requests.delete(f"{url}/storage/v1/object/gta-clips", json={"prefixes": names}, timeout=60,
                        headers={"Authorization": f"Bearer {key}", "apikey": key})
    if r.status_code < 300:
        con.executemany("UPDATE clips SET error='hosted-deleted' WHERE id=?", [(r_["id"],) for r_ in rows])
        con.commit()


def cmd_audit(cfg, con):
    """Re-check every queued Buffer post with the safety filters; delete the ones that fail."""
    orgs = buffer_q("query { account { organizations { id } } }")["account"]["organizations"]
    posts = []
    for o in orgs:
        after = None
        for _ in range(20):
            a = f', after: "{after}"' if after else ""
            q = ('query { posts(first: 50%s, input: {organizationId: "%s", filter: {status: [scheduled]}}) '
                 '{ edges { node { id dueAt text assets { source } } } pageInfo { hasNextPage endCursor } } }') % (a, o["id"])
            d = buffer_q(q)["posts"]
            posts += [e["node"] for e in d["edges"]]
            if not d["pageInfo"]["hasNextPage"]:
                break
            after = d["pageInfo"]["endCursor"]
    by_url = {}
    for p_ in posts:
        for a_ in p_.get("assets") or []:
            if a_.get("source"):
                by_url.setdefault(a_["source"], []).append(p_)
    log(f"Auditing {len(posts)} queued posts ({len(by_url)} videos)")
    import tempfile
    for url, plist in by_url.items():
        name = url.rsplit("/", 1)[-1]
        row = con.execute("SELECT id, title FROM clips WHERE video LIKE ?", (f"%{name}",)).fetchone()
        reason = "bad title/caption" if is_inappropriate(row["title"] if row else "", plist[0].get("text", "")) else ""
        if not reason:
            with tempfile.TemporaryDirectory() as td:
                f = Path(td) / name
                r = requests.get(url, timeout=120)
                if r.status_code != 200:
                    log(f"   can't download {name} ({r.status_code}) — leaving it")
                    continue
                f.write_bytes(r.content)
                reason = nsfw_check(f)
        if reason:
            for p_ in plist:
                buffer_q("mutation($i: DeletePostInput!) { deletePost(input: $i) { __typename } }", {"i": {"id": p_["id"]}})
            if row:
                con.execute("UPDATE clips SET status='removed', error=? WHERE id=?", (f"audit: {reason}", row["id"]))
                con.commit()
            when = dt.datetime.fromisoformat(plist[0]["dueAt"].replace("Z", "+00:00")).astimezone()
            log(f"🗑️  removed {len(plist)} queued post(s) for {when:%a %I:%M %p} — {reason}")
        else:
            log(f"   ok: {name}")


def m_login(r):
    try:
        return json.loads(r["meta"])["login"].lower()
    except Exception:
        return ""


def cmd_cancel(cfg, con, name):
    """Delete queued Buffer posts whose video file name contains `name`."""
    orgs = buffer_q("query { account { organizations { id } } }")["account"]["organizations"]
    n = 0
    for o in orgs:
        q = ('query { posts(first: 100, input: {organizationId: "%s", filter: {status: [scheduled]}}) '
             '{ edges { node { id dueAt assets { source } } } } }') % o["id"]
        for e in buffer_q(q)["posts"]["edges"]:
            p_ = e["node"]
            if any(name in (a_.get("source") or "") for a_ in p_.get("assets") or []):
                buffer_q("mutation($i: DeletePostInput!) { deletePost(input: $i) { __typename } }", {"i": {"id": p_["id"]}})
                n += 1
    con.execute("UPDATE clips SET status='removed', error='cancelled (old layout)' WHERE video LIKE ? AND status='scheduled'", (f"%{name}%",))
    con.commit()
    log(f"🗑️  cancelled {n} queued post(s) matching {name}")


def cmd_post_buffer(cfg, con):
    if not os.getenv("BUFFER_API_KEY") or not os.getenv("SUPABASE_URL"):
        log("BUFFER_API_KEY / SUPABASE_URL missing in .env — skipping posting")
        return
    p = cfg["posting"]
    want = set(p.get("platforms", ["tiktok", "instagram", "youtube"]))
    chans = [c for c in buffer_channels() if c["service"] in want and not c.get("isQueuePaused")]
    if not chans:
        log(f"No Buffer channels found for {sorted(want)} — connect them at buffer.com")
        alert("⚠️ Buffer accounts disconnected", "Buffer has no connected TikTok/Instagram/YouTube channels (or the queue is paused). Open buffer.com and reconnect / unpause.")
        return
    missing = want - {c["service"] for c in chans}
    if missing:
        alert("⚠️ Buffer channel missing", f"{', '.join(sorted(missing))} is disconnected or paused in Buffer. Open buffer.com → reconnect it.", key="missing")
    log("Buffer channels: " + ", ".join(f"{c['service']} ({c['name']})" for c in chans))
    try:
        sb_cleanup(con)
    except Exception as e:
        log(f"cleanup skipped: {e}")

    now = dt.datetime.now(UTC)
    queued = con.execute("SELECT COUNT(*) n FROM clips WHERE status='scheduled' AND scheduled_at > ?", (now.isoformat(),)).fetchone()["n"]
    room = max(0, 9 - queued)  # Buffer free = 10 queued per channel
    rows = con.execute("SELECT * FROM clips WHERE status='approved' ORDER BY views DESC").fetchall()[:room]

    # Peak-hour slot schedule (Toronto time), built from 2026 posting-time studies
    used = [dt.datetime.fromisoformat(sa) for (sa,) in con.execute(
        "SELECT scheduled_at FROM clips WHERE scheduled_at IS NOT NULL AND scheduled_at > ?",
        ((now - dt.timedelta(hours=1)).isoformat(),))]
    ig_slots = set(p.get("instagram_slots", []))

    def free_slots():
        today = now.astimezone().replace(second=0, microsecond=0)
        horizon = now + dt.timedelta(hours=p.get("horizon_hours", 6))
        for d in range(-1, 3):
            day = today + dt.timedelta(days=d)
            key = "slots_weekend" if day.weekday() >= 5 else "slots_weekday"
            for hm in p.get(key, []):
                h, m = map(int, hm.split(":"))
                t = day.replace(hour=h, minute=m)
                if h < 6:  # after-midnight slots belong to the previous evening
                    t += dt.timedelta(days=1)
                t = t.astimezone(UTC)
                if t < now + dt.timedelta(minutes=5) or t > horizon:
                    continue
                if any(abs((t - u).total_seconds()) < 20 * 60 for u in used):
                    continue
                yield t, hm

    slots = sorted(free_slots())
    cap = p.get("daily_cap", 14)
    fast_views = p.get("fast_lane_views", 10000)
    start_h, end_h = p.get("active_hours", [9, 27])
    ig_hours = set(p.get("instagram_hours", [12, 13, 19, 20, 21, 22, 23, 0]))
    per_day = {}
    for (sa,) in con.execute("SELECT scheduled_at FROM clips WHERE scheduled_at IS NOT NULL"):
        d_ = dt.datetime.fromisoformat(sa).astimezone()
        d_ = (d_ - dt.timedelta(hours=6)).date()  # "posting day" runs 6 AM → 6 AM
        per_day[d_] = per_day.get(d_, 0) + 1

    def pday(t):
        return (t.astimezone() - dt.timedelta(hours=6)).date()

    def in_window(t):
        h = t.astimezone().hour
        return start_h <= h < end_h if end_h <= 24 else (h >= start_h or h < end_h - 24)

    def fast_slot():
        t = now + dt.timedelta(minutes=10)
        for _ in range(30):
            if not any(abs((t - u).total_seconds()) < 20 * 60 for u in used):
                return t if in_window(t) else None
            t += dt.timedelta(minutes=5)
        return None
    if os.getenv("POST_NOW", "").lower() in ("1", "true"):  # test mode: post in ~10 min, all platforms
        slots = [(now + dt.timedelta(minutes=10 + 15 * i), "now") for i in range(len(rows))]
        ig_slots = set()
    wd = ROOT / cfg["work_dir"]
    for r in rows:
        if not Path(r["video"]).exists():
            con.execute("UPDATE clips SET status='error', error='video file missing' WHERE id=?", (r["id"],))
            con.commit()
            log(f"skipped {r['streamer']}: video from an earlier run is gone (cloud runs start fresh)")
            continue
        m = json.loads(r["meta"])
        camp = CAMPAIGNS.get(m["login"].lower())
        tags = ([camp] if camp else []) + [t for t in m["hashtags"] if t.lower() != (camp or "").lower()]
        capt = m["caption"] if m["streamer"].lower() in m["caption"].lower() else f"{m['streamer']}: {m['caption']}"
        social = f"{capt}\n\n🎥 {m['streamer']} (twitch.tv/{m['login']})\n{' '.join(tags)}"
        try:
            video_url = sb_upload(Path(r["video"]))
            hm = None
            slot = fast_slot() if r["views"] >= fast_views else None
            if slot:
                hm = "fast"
                slots = [x for x in slots if abs((x[0] - slot).total_seconds()) >= 20 * 60]
            elif slots:
                slot, hm = slots.pop(0)
            else:
                log("No free posting slot in the next few hours — waiting for the next peak window.")
                break
            if per_day.get(pday(slot), 0) >= cap:
                log(f"Daily cap of {cap} reached — skipping {r['streamer']}")
                continue
            # keep the day mostly big viral clips: smaller clips only as occasional filler
            premium_v = cfg["fetch"].get("premium_views", 10000)
            is_camp = m_login(r) in CAMPAIGNS
            if r["views"] < premium_v and not is_camp:
                filler_max = p.get("filler_daily", 4)
                gap_min = p.get("filler_gap_minutes", 90)
                fillers = [dt.datetime.fromisoformat(sa) for (sa, v) in con.execute(
                    "SELECT scheduled_at, views FROM clips WHERE scheduled_at IS NOT NULL AND status='scheduled'")
                    if v is not None and v < premium_v]
                f_today = sum(1 for t in fillers if pday(t) == pday(slot))
                recent = any(abs((slot - u).total_seconds()) < gap_min * 60 for u in used)
                if f_today >= filler_max or recent:
                    con.execute("UPDATE clips SET status='skipped', error='filler limit (saving room for viral clips)' WHERE id=?", (r["id"],))
                    con.commit()
                    log(f"⏭️  held back {r['streamer']} ({r['views']:,} views) — saving room for bigger clips")
                    continue
            ok = []
            yt_limit = p.get("youtube_daily", 2)
            yt_today = sum(1 for (sa, pl) in con.execute(
                "SELECT scheduled_at, platforms FROM clips WHERE platforms LIKE '%youtube%'")
                if pday(dt.datetime.fromisoformat(sa)) == pday(slot))
            for c in chans:
                if c["service"] == "youtube" and yt_today >= yt_limit:
                    continue  # YouTube only allows a few posts/day on this channel — save them for the best clips
                if c["service"] == "instagram" and ig_slots and (
                        (hm == "fast" and slot.astimezone().hour not in ig_hours) or (hm not in ("fast",) and hm not in ig_slots)):
                    continue
                meta = {}
                if c["service"] == "instagram":
                    meta = {"instagram": {"type": "reel", "shouldShareToFeed": True}}
                elif c["service"] == "youtube":
                    meta = {"youtube": {"title": f"{m['yt_title']} #shorts"[:100], "privacy": "public",
                                        "categoryId": "20", "madeForKids": False, "notifySubscribers": True}}
                inp = {"text": social, "channelId": c["id"], "schedulingType": "automatic",
                       "mode": "customScheduled", "dueAt": slot.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                       "assets": [{"video": {"url": video_url, "metadata": {"thumbnailOffset": 1000}}}],
                       "metadata": meta}
                res = buffer_q(CREATE_POST, {"input": inp})["createPost"]
                if res.get("message"):
                    log(f"   ⚠️ {c['service']}: {res['message']}")
                    if "maximum number of posts" not in res["message"]:
                        alert(f"⚠️ {c['service'].title()} post rejected", f"Buffer refused a post to {c['service']}: {res['message'][:200]}. Claude will look at it on the next check-in.", key=c["service"])
                else:
                    ok.append(c["service"])
            if not ok:
                raise RuntimeError("no channel accepted the post")
            newp = wd / "posted" / Path(r["video"]).name
            shutil.move(r["video"], newp)
            con.execute("UPDATE clips SET status='scheduled', video=?, scheduled_at=?, platforms=? WHERE id=?",
                        (str(newp), slot.isoformat(), ",".join(ok), r["id"]))
            con.commit()
            topic = p.get("ntfy_topic")
            if topic:
                if camp:
                    msg = f"{m['streamer']} clip is LIVE on {', '.join(ok)}. Submit the links in Content Rewards now (30-min rule)!"
                    hdr = {"Title": f"💰 Submit {camp} clip now", "Tags": "moneybag", "Priority": "high",
                           "Click": "https://contentrewards.com"}
                else:
                    msg = f"{m['streamer']} ({r['views']:,} Twitch views) just posted on {', '.join(ok)}: {m['caption'][:80]}"
                    hdr = {"Title": "✅ New clip posted", "Tags": "movie_camera", "Priority": "default"}
                hdr["At"] = str(int(slot.timestamp()) + 60)
                hdr["Title"] = hdr["Title"].encode("ascii", "ignore").decode().strip()
                try:
                    requests.post(f"https://ntfy.sh/{topic}", timeout=20, data=msg.encode(), headers=hdr)
                except Exception as e:
                    log(f"   (alert not sent: {e})")
            lane = "⚡ FAST LANE " if hm == "fast" else "📅 "
            log(f"{lane}{m['streamer']} ({r['views']:,} views): '{m['caption']}' → {', '.join(ok)} at {slot.astimezone():%a %I:%M %p}")
            used.append(slot)
            per_day[pday(slot)] = per_day.get(pday(slot), 0) + 1
        except Exception as e:
            log(f"❌ post failed for {r['id']}: {e}")


def cmd_post(cfg, con):
    if cfg["posting"].get("backend") == "buffer":
        return cmd_post_buffer(cfg, con)
    key, user = os.getenv("UPLOAD_POST_API_KEY"), os.getenv("UPLOAD_POST_USER")
    if not key or not user:
        log("UPLOAD_POST_API_KEY / UPLOAD_POST_USER missing in .env — skipping posting")
        return
    p = cfg["posting"]
    spacing = dt.timedelta(minutes=p.get("spacing_minutes", 45))
    last = con.execute("SELECT MAX(scheduled_at) m FROM clips WHERE scheduled_at IS NOT NULL").fetchone()["m"]
    slot = max(dt.datetime.now(UTC) + dt.timedelta(minutes=3),
               (dt.datetime.fromisoformat(last) + spacing) if last else dt.datetime.min.replace(tzinfo=UTC))
    wd = ROOT / cfg["work_dir"]
    for r in con.execute("SELECT * FROM clips WHERE status='approved' ORDER BY views DESC").fetchall():
        m = json.loads(r["meta"])
        tags = " ".join(m["hashtags"])
        social = f"{m['caption']}\n\n🎥 {m['streamer']} (twitch.tv/{m['login']})\n{tags}"
        data = [("user", user), ("title", m["yt_title"]), ("scheduled_date", slot.strftime("%Y-%m-%dT%H:%M:%SZ"))]
        data += [("platform[]", pl) for pl in p.get("platforms", ["tiktok", "instagram", "youtube"])]
        data += [
            ("tiktok_title", social), ("privacy_level", "PUBLIC_TO_EVERYONE"),
            ("instagram_title", social), ("media_type", "REELS"), ("share_to_feed", "true"),
            ("youtube_title", f"{m['yt_title']} #shorts"[:100]),
            ("youtube_description", f"{social}\n\nOriginal stream: {m['clip_url']}"),
            ("privacyStatus", "public"), ("categoryId", "20"),
        ]
        try:
            with open(r["video"], "rb") as fh:
                resp = requests.post(
                    "https://api.upload-post.com/api/upload",
                    headers={"Authorization": f"Apikey {key}", "Idempotency-Key": r["id"]},
                    data=data, files={"video": (Path(r["video"]).name, fh, "video/mp4")}, timeout=600,
                )
            if resp.status_code >= 300:
                raise RuntimeError(f"{resp.status_code}: {resp.text[:300]}")
            newp = wd / "posted" / Path(r["video"]).name
            shutil.move(r["video"], newp)
            con.execute("UPDATE clips SET status='scheduled', video=?, scheduled_at=?, platforms=? WHERE id=?",
                        (str(newp), slot.isoformat(), ",".join(ok), r["id"]))
            con.commit()
            log(f"📅 scheduled {m['streamer']} '{m['hook']}' for {slot.astimezone():%a %H:%M}")
            slot += spacing
        except Exception as e:
            log(f"❌ post failed for {r['id']}: {e}")


def cmd_stats(cfg, con):
    for r in con.execute("SELECT status, COUNT(*) n FROM clips GROUP BY status"):
        print(f"{r['status']:<10} {r['n']}")


def cmd_check(cfg, con):
    ok = True
    for b in ("ffmpeg", "ffprobe", "yt-dlp"):
        found = shutil.which(b)
        print(f"{'✅' if found else '❌'} {b}")
        ok &= bool(found)
    try:
        import faster_whisper  # noqa
        print("✅ faster-whisper")
    except ImportError:
        print("❌ faster-whisper (pip install faster-whisper)")
        ok = False
    for k in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "BUFFER_API_KEY", "SUPABASE_URL", "ANTHROPIC_API_KEY"):
        print(f"{'✅' if os.getenv(k) else ('⚠️ ' if k == 'ANTHROPIC_API_KEY' else '❌')} {k}"
              + ("  (optional: better hooks)" if k == "ANTHROPIC_API_KEY" else ""))
    try:
        tw = Twitch()
        print(f"✅ Twitch category '{cfg['game_name']}' id = {tw.game_id(cfg['game_name'])}")
    except SystemExit as e:
        print(f"❌ {e}")
        ok = False
    if cfg["posting"].get("backend") == "buffer":
        try:
            for c in buffer_channels():
                print(f"✅ Buffer channel: {c['service']} — {c['name']}")
        except Exception as e:
            print(f"❌ Buffer: {e}")
            ok = False
        try:
            import tempfile
            t = Path(tempfile.gettempdir()) / "clipper_check.mp4"
            subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", str(t)], check=True)
            u = sb_upload(t)
            print("✅ Video hosting works" if requests.get(u, timeout=30).status_code == 200 else "❌ Video hosting: can't read back")
            requests.delete(f"{os.getenv('SUPABASE_URL')}/storage/v1/object/gta-clips", json={"prefixes": [t.name]},
                            headers={"Authorization": f"Bearer {os.getenv('SUPABASE_KEY')}", "apikey": os.getenv("SUPABASE_KEY")})
        except Exception as e:
            print(f"❌ Video hosting: {e}")
            ok = False
    print("\nReady." if ok else "\nFix the ❌ items above.")


def main():
    ap = argparse.ArgumentParser(description="GTA 6 auto-clipper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    d = sub.add_parser("discover"); d.add_argument("--hours", type=int, default=12)
    r = sub.add_parser("run"); r.add_argument("--loop", type=int, default=0, help="minutes between runs")
    r.add_argument("--game", help="override game_name (for testing)")
    r.add_argument("--max", type=int, help="max clips this run")
    r.add_argument("--hours", type=int, help="lookback hours")
    r.add_argument("--any", action="store_true", help="test: use top clips from any streamer")
    r.add_argument("--autopost", action="store_true", help="approve + schedule clips that pass the safety filter")
    r.add_argument("--preview", action="store_true", help="render clips only, never post")
    sub.add_parser("approve"); sub.add_parser("post"); sub.add_parser("stats"); sub.add_parser("audit")
    x = sub.add_parser("cancel"); x.add_argument("name")
    hh = sub.add_parser("hot"); hh.add_argument("--hours", type=int, default=6)
    y = sub.add_parser("ytvod"); y.add_argument("url"); y.add_argument("--n", type=int, default=4)
    y.add_argument("--views", type=int, default=50000, help="priority (>=10k = fast lane)")
    y.add_argument("--autopost", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    con = db(cfg)
    if args.cmd == "run":
        if args.game: cfg["game_name"] = args.game
        if args.max: cfg["fetch"]["max_new_per_run"] = args.max
        if args.hours: cfg["fetch"]["lookback_hours"] = args.hours
        if args.any: cfg["_any"] = True; cfg["fetch"]["min_views"] = 20
        if args.autopost: cfg["posting"]["auto_post"] = True
        if args.preview: cfg["posting"]["auto_post"] = False
        while True:
            try:
                cmd_run_once(cfg, con)
            except Exception as e:
                log(f"run error: {e}")
                if not args.loop:
                    sys.exit(1)
            if not args.loop:
                break
            log(f"sleeping {args.loop} min…")
            time.sleep(args.loop * 60)
    elif args.cmd == "hot":
        cmd_hot(cfg, args)
    elif args.cmd == "cancel":
        cmd_cancel(cfg, con, args.name)
    elif args.cmd == "ytvod":
        if args.autopost:
            cfg["posting"]["auto_post"] = True
        cmd_ytvod(cfg, con, args)
    elif args.cmd == "discover":
        cmd_discover(cfg, args)
    else:
        {"check": cmd_check, "approve": cmd_approve, "post": cmd_post, "stats": cmd_stats, "audit": cmd_audit}[args.cmd](cfg, con)


if __name__ == "__main__":
    main()
