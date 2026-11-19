# GTA 6 Auto-Clipper

Every 20 min it pulls the most-viewed new GTA 6 Twitch clips from your streamer list, cuts the dead air, transcribes the audio, and renders a 9:16 short with word-by-word captions, a hook at the top, and streamer credit. Then it schedules each short to TikTok, Reels and Shorts, spaced 45 min apart.

```
Twitch clips API ──► yt-dlp ──► Whisper captions ──► ffmpeg 9:16 render ──► [you approve] ──► Upload-Post ──► TikTok / IG / YT
 (proven moments)                (word timestamps)    (hook + captions)       (10 sec each)     (scheduled)
```

---

## 1. How you actually make money (read this first)

**Get paid per view through clipping campaigns.** Streamers and brands post campaigns on **Content Rewards** (contentrewards.com, paid out via Whop) and **Vyro**. You post their clips on your own accounts, submit the links, and get paid per 1,000 verified views (median ≈ US$1, roughly C$1.40). One clip at 500K views earns about C$700.
- Check both sites **daily**. GTA 6 campaigns will open over launch week. Join one *before* you clip that streamer, because only clips posted under a campaign get paid.
- Put the streamers from your campaigns in `config.yaml → streamers`. That keeps you legal (you have permission) and paid.

**Platform payouts (TikTok Creator Rewards, YouTube Partner Program) are a bonus, not the plan.** They require "original" content, and raw reposted clips usually don't qualify. The edits this tool makes (captions, hook, crop) help, but don't count on these payouts.

**Don't post clips from streamers you don't have permission for.** The streamer can file copyright takedowns, and 3 strikes kills the account. Use `discover` to spot who's trending, then find or request their campaign.

---

## 2. Setup on your MacBook (~20 min)

```bash
# 1. tools
brew install ffmpeg python@3.12
cd ~/Downloads/gta6-clipper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. config
cp config.example.yaml config.yaml
cp .env.example .env
open -e .env        # paste your keys (see below)

# 3. verify
python clipper.py check
```

**Keys you need:**

| Key | Where | Cost |
|---|---|---|
| `TWITCH_CLIENT_ID` / `SECRET` | dev.twitch.tv/console/apps → Register app → redirect `http://localhost` | Free |
| `UPLOAD_POST_API_KEY` / `USER` | upload-post.com → create a profile → connect TikTok, Instagram (must be a Creator/Business account), YouTube → API key | Free tier, then paid |
| `ANTHROPIC_API_KEY` (optional) | console.anthropic.com. Writes much better hooks than the fallback | Cents per day |

**Why Upload-Post instead of each platform's own API?** TikTok's API only allows private posts until your app passes an audit (days to weeks). YouTube does the same for unverified projects. You don't have that time at launch, so this uses a service that's already approved.

---

## 3. Daily workflow

```bash
source .venv/bin/activate

python clipper.py discover --hours 6   # who's getting the most GTA 6 clip views right now
python clipper.py run --loop 20        # leave running in a Terminal tab, renders new clips every 20 min

# in a second Terminal tab, a few times a day:
python clipper.py approve              # each clip opens; y = approve, n = trash, c = edit caption
python clipper.py post                 # schedules everything approved, 45 min apart
python clipper.py stats
```

**Fully hands-off:** set `posting.auto_post: true` in `config.yaml`, and `run --loop` will render and post with no review. Turn this on only after a day of checking that the output is consistently good. One bad clip (spoiler, slur, dead moment) can hurt an account's reach.

Rendered files land in `work/review → work/approved → work/posted` (or `work/rejected`).

**Facecam layout:** by default the gameplay sits centred on a blurred background, which works for every streamer. For a streamer with a fixed webcam, pause their VOD and estimate where the cam sits as fractions of the screen (e.g. top-left, 28% wide, 32% tall). Then set:
```yaml
- login: kaicenat
  display: Kai Cenat
  layout: { mode: stack, facecam: [0.0, 0.0, 0.28, 0.32] }
```
Their face goes on top and the gameplay on the bottom. That layout performs best for reaction-heavy streamers.

---

## 4. What makes clips get views (built into the tool, plus what you control)

**Already automated:**
- **Picks moments that are already proven.** It ranks clips by Twitch views per hour, so you only render moments viewers already clipped and rewatched.
- **Freshness.** It only uses clips from the last 12 hours, so you're early on a moment before 50 other clippers post it.
- **No dead air.** It cuts the silence before the first word, so the clip starts on the action.
- **Hook on screen from frame 1.** It writes curiosity text and never reveals the punchline.
- **Word-by-word captions.** Most people scroll with the sound off, so the captions carry the clip.
- **Loudness normalized** to −14 LUFS (platform standard).
- **Duplicate filter.** It skips overlapping clips of the same moment.
- **Posts spaced 45 min apart** instead of 10 at once, which reads as spam.

**You control:**
1. **Pick a lane.** Have one account per streamer, or one "GTA 6 funny moments" account, and stick to it. A focused account gets pushed to the right audience faster.
2. **Volume.** Aim for 3–6 posts a day per account during launch week. The window is short.
3. **First 2 hours after launch matter most.** Early reactions ("first time in Vice City", first police chase, first glitch) are the biggest search spike of the year.
4. **No story spoilers in hooks or captions.** People report them and the comments get toxic.
5. **Reply to the first comments** on every post within the first hour.
6. **Fresh accounts:** spend 2–3 days scrolling and liking GTA content before you post, so the algorithm knows your niche. Don't use a VPN, and don't run multiple accounts from one phone.
7. **Read your numbers daily.** If a clip passes 50% average watch time, find more clips like it.

---

## 5. Tweaks

| Want | Change in `config.yaml` |
|---|---|
| More/less zoom on gameplay | `layout.game_aspect` (1.0 = tight, 1.78 = full frame) |
| Bigger captions | `style.caption_size` |
| Hook only for the first 3s | `style.hook_seconds: 3` |
| Faster on old Mac | `whisper_model: base` |
| More clips per run | `fetch.max_new_per_run`, lower `min_views` |
| Only TikTok | `posting.platforms: [tiktok]` |

**Keep it running when the lid is closed:** run `caffeinate -i python clipper.py run --loop 20`.

**Troubleshooting**
- `check` says the category isn't found: Twitch may list the game under a different name. Search it on twitch.tv and paste the exact name into `game_name`.
- yt-dlp download errors: run `pip install -U yt-dlp`. Twitch changes often.
- Captions show in the wrong font: change `style.font` to a font installed on your Mac (e.g. `"Impact"`).
