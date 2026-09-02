#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🎵 موزیک‌یاب — بات تلگرام پیدا کردن آهنگ (کاملاً رایگان)

منابع رایگان و بدون کلید API:
  - تشخیص آهنگ از صدا: shazamio (سرویس عمومی Shazam)
  - جستجوی آهنگ:      iTunes Search API
  - متن آهنگ:         lrclib.net
  - دانلود MP3:       yt-dlp + ffmpeg
  - آهنگ‌های ترند:     فید رسمی Apple Music
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import aiohttp
import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from shazamio import Shazam

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("muzikyab")

BASE_DIR = Path(__file__).resolve().parent
MAX_TELEGRAM_DOWNLOAD = 20 * 1024 * 1024   # محدودیت دانلود بات تلگرام
MAX_MP3_BYTES = 48 * 1024 * 1024          # سقف ارسال فایل بات (۵۰ مگ)
MAX_TRACK_SECONDS = 1500                  # سقف طول آهنگ برای دانلود (۲۵ دقیقه)

# ---------------------------------------------------------------- ffmpeg / deno

def find_ffmpeg() -> str:
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:  # نسخه‌ی همراه پکیج imageio-ffmpeg به‌عنوان جایگزین
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"


FFMPEG = find_ffmpeg()

# deno برای استخراج پایدار yt-dlp از یوتیوب
_deno = Path.home() / ".deno" / "bin"
if _deno.is_dir():
    os.environ["PATH"] = f"{_deno}{os.pathsep}{os.environ.get('PATH', '')}"

shazam = Shazam()

# ---------------------------------------------------------------- ابزار عمومی

def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


async def safe_answer(cb, *args, **kwargs) -> None:
    """پاسخ به callback — اگر کوئری قدیمی/نامعتبر بود، بی‌صدا رد شو"""
    try:
        await cb.answer(*args, **kwargs)
    except Exception:
        pass


STORE: dict[str, dict] = {}
STORE_PATH = BASE_DIR / "store.json"
_save_task: asyncio.Task | None = None


def _load_store() -> None:
    """دکمه‌ها بعد از ری‌استارت هم کار کنند — ذخیره‌سازی روی دیسک"""
    try:
        if STORE_PATH.exists():
            data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                STORE.update(data)
    except Exception as e:
        log.warning("load store failed: %s", e)


def _save_store() -> None:
    try:
        tmp = STORE_PATH.parent / (STORE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(STORE, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STORE_PATH)
    except Exception as e:
        log.warning("save store failed: %s", e)


def _schedule_save() -> None:
    global _save_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _save_store()
        return
    if _save_task is not None and not _save_task.done():
        return

    async def _delayed() -> None:
        await asyncio.sleep(2.0)
        _save_store()

    _save_task = loop.create_task(_delayed())


def store_put(data: dict) -> str:
    """ذخیره‌ی اطلاعات هر نتیجه برای دکمه‌های inline — ماندگار روی دیسک"""
    if len(STORE) > 5000:
        for k in list(STORE)[:2500]:
            STORE.pop(k, None)
    key = uuid.uuid4().hex[:10]
    STORE[key] = data
    _schedule_save()
    return key


_load_store()


def run_ffmpeg(args: list[str]) -> None:
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args],
        check=True, capture_output=True, timeout=180,
    )


def convert_to_wav(src: Path, dst: Path, seconds: int = 55) -> None:
    """تبدیل هر فرمت صوتی/ویدیویی به WAV مونو برای تشخیص"""
    run_ffmpeg(["-i", str(src), "-t", str(seconds),
                "-vn", "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", str(dst)])


def clean_filename(name: str) -> str:
    name = re.sub(r"[^\w\s.\-–—]", "", name or "audio").strip()
    return (name[:80] or "audio") + ".mp3"


def split_artist_title(yt_title: str, uploader: str | None) -> tuple[str, str]:
    """استخراج «خواننده» و «عنوان» از عنوان ویدیوی یوتیوب"""
    t = re.sub(r"\s*[\(\[【].*?[\)\]】]\s*", " ", yt_title or "").strip()
    for sep in (" - ", " – ", " — ", " | "):
        if sep in t:
            left, right = t.split(sep, 1)
            if 1 < len(left) < 60 and len(right) > 1:
                return right.strip(), left.strip()
    return (t or yt_title or "Unknown"), (uploader or "").strip()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9\u0600-\u06FF]", "", (s or "").lower())


def itunes_match(t_title: str, t_artist: str, r: dict) -> bool:
    """آیا نتیجه‌ی iTunes همان آهنگ تشخیص‌داده‌شده است؟ (تطبیق شُل)"""
    a, b = _norm(t_artist), _norm(r.get("artistName") or "")
    if not (a and b) or not (a in b or b in a):
        return False
    x, y = _norm(t_title), _norm(r.get("trackName") or "")
    return bool(x and y) and (x[:8] in y or y[:8] in x)

# ---------------------------------------------------------------- سرویس‌های رایگان

async def itunes_search(term: str, limit: int = 5, attribute: str | None = None) -> list[dict]:
    import urllib.parse as up
    params = {"term": term, "media": "music", "entity": "song", "limit": limit}
    if attribute:
        params["attribute"] = attribute
    url = "https://itunes.apple.com/search?" + up.urlencode(params)
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json(content_type=None)
    return [x for x in data.get("results", []) if x.get("kind") == "song" or "previewUrl" in x]


async def lrclib_lyrics(artist: str, title: str) -> dict | None:
    """متن آهنگ از lrclib.net — رایگان و بدون کلید"""
    import urllib.parse as up
    headers = {"User-Agent": "MuzikYab Telegram Bot (free open-source bot)"}
    async with aiohttp.ClientSession(headers=headers) as s:
        # تلاش اول: تطبیق دقیق
        u = f"https://lrclib.net/api/get?{up.urlencode({'artist_name': artist, 'track_name': title})}"
        async with s.get(u, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 200:
                return await r.json(content_type=None)
        # تلاش دوم: جستجو
        u = f"https://lrclib.net/api/search?{up.urlencode({'q': f'{artist} {title}'})}"
        async with s.get(u, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                if isinstance(data, list) and data:
                    return data[0]
    return None


async def apple_top_songs(limit: int = 10, country: str = "us") -> list[dict]:
    """۱۰ آهنگ پرشنونده — فید رسمی و رایگان Apple Music (با تلاش مجدد؛ این فید گاهی کند است)"""
    url = f"https://rss.applemarketingtools.com/api/v2/{country}/music/most-played/{limit}/songs.json"
    last: Exception | None = None
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    data = await r.json(content_type=None)
            return data["feed"]["results"]
        except Exception as e:  # noqa: BLE001
            last = e
            await asyncio.sleep(1 + attempt)
    raise last or RuntimeError("apple top songs unavailable")


async def deezer_json(url: str, params: dict | None = None) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
            return await r.json(content_type=None)


async def deezer_similar(artist: str, title: str, count: int = 10) -> list[dict]:
    """آهنگ‌های هم‌سبک از خواننده‌های مرتبط — Deezer API رایگان و بدون کلید"""
    d = await deezer_json("https://api.deezer.com/search/track",
                          {"q": f'artist:"{artist}" track:"{title}"', "limit": 1})
    data = d.get("data") or []
    if not data:  # جستجوی شُل‌تر
        d = await deezer_json("https://api.deezer.com/search/track",
                              {"q": f"{artist} {title}", "limit": 1})
        data = d.get("data") or []
    if not data:
        return []
    aid = data[0]["artist"]["id"]
    rel = (await deezer_json(f"https://api.deezer.com/artist/{aid}/related", {"limit": 5})).get("data") or []
    items: list[dict] = []
    seen = {_norm(title)}
    per = max(2, count // max(1, len(rel)))
    for a in rel:
        top = (await deezer_json(f"https://api.deezer.com/artist/{a['id']}/top", {"limit": per})).get("data") or []
        for x in top:
            key = _norm(x.get("title") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            album = x.get("album") or {}
            items.append({
                "title": x.get("title") or "", "artist": (x.get("artist") or {}).get("name") or a.get("name") or "",
                "album": album.get("title") or "", "year": "",
                "query": f"{(x.get('artist') or {}).get('name') or a.get('name')} {x.get('title')} official audio",
                "preview": x.get("preview"),
                "artwork": album.get("cover_big") or album.get("cover_medium") or "",
                "list": None,
            })
            if len(items) >= count:
                return items
    return items


class TrackTooLong(Exception):
    pass


def yt_download(query: str, tmp: Path) -> tuple[Path, dict]:
    """جستجو در یوتیوب و دانلود صدا به‌صورت MP3 (در ترد جدا اجرا می‌شود)"""
    base = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "outtmpl": str(tmp / "%(id)s.%(ext)s"),
        "ffmpeg_location": FFMPEG,
    }
    with yt_dlp.YoutubeDL(base) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        # نتیجه‌ی جستجو به‌شکل پلی‌لیست برمی‌گردد؛ ویدیوی واقعی را باز کن
        if info.get("entries"):
            info = info["entries"][0]
        dur = info.get("duration") or 0
        if dur > MAX_TRACK_SECONDS:
            raise TrackTooLong(f"آهنگ پیدا شده {dur // 60} دقیقه است (سقف {MAX_TRACK_SECONDS // 60} دقیقه)")
        ydl_opts = dict(base)
        ydl_opts["writethumbnail"] = True  # دانلود کاور برای تلگرام
        ydl_opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
        ]
        with yt_dlp.YoutubeDL(ydl_opts) as ydl2:
            ydl2.download([info["webpage_url"]])
    mp3s = sorted(tmp.glob("*.mp3"))
    if not mp3s:
        raise RuntimeError("فایل MP3 ساخته نشد")
    return mp3s[0], info


def make_thumb(tmp: Path) -> Path | None:
    """کاور یوتیوب را به JPEG مربعی کوچک برای تلگرام تبدیل می‌کند"""
    srcs = [p for ext in ("*.webp", "*.jpg", "*.jpeg", "*.png") for p in tmp.glob(ext)]
    if not srcs:
        return None
    try:
        out = tmp / "cover.jpg"
        run_ffmpeg(["-i", str(srcs[0]), "-vf", "scale=320:320:force_original_aspect_ratio=increase,"
                    "crop=320:320", "-q:v", "4", str(out)])
        return out if out.exists() and out.stat().st_size < 200_000 else None
    except Exception:
        return None


def track_meta(t: dict) -> dict:
    """استخراج آلبوم/ژانر/تاریخ از خروجی Shazam"""
    meta: dict[str, str] = {}
    for sec in t.get("sections", []) or []:
        for m in sec.get("metadata", []) or []:
            key = (m.get("title") or "").strip().upper()
            val = m.get("text")
            if key and val:
                meta[key] = val
    return meta

# ---------------------------------------------------------------- متن‌های بات

WELCOME = """🎵 <b>موزیک‌یاب</b> — پیدا کردن و دانلود آهنگ، کاملاً رایگان!

<b>فقط دو راه داری:</b>
🎙 یه <b>ویس از آهنگ</b> بفرست → اسمش رو می‌گم
🔍 یا <b>اسم آهنگ/خواننده</b> رو بنویس

بعدش کافیه روی اسم آهنگ بزنی و با یه دکمه <b>MP3 کامل</b> رو بگیری 📥

آماده‌ام — یه ویس بفرست یا اسم آهنگ رو بنویس 👇"""

HELP = """❓ <b>راهنمای موزیک‌یاب</b>

1️⃣ <b>از روی صدا</b> — ویس/فایل صوتی/ویدیو از آهنگ بفرست (زیر ۲۰ مگ، ۱۰ تا ۳۰ ثانیه از خود آهنگ بهترین نتیجه رو می‌ده)
2️⃣ <b>با اسم</b> — اسم آهنگ یا خواننده رو بنویس (انگلیسی دقیق‌تره)
3️⃣ روی نتیجه بزن → <b>📥 دانلود</b> • <b>▶️ پیش‌نمایش</b> • <b>🎼 مشابه</b> • <b>🎤 هم‌خواننده</b> • <b>📝 متن</b>

🛠 دستورها:
/dl اسم آهنگ — دانلود مستقیم
/top — آهنگ‌های ترند
/help — همین راهنما"""

NO_RESULT = ("🤷 چیزی پیدا نکردم.\n\n"
             "💡 اسم رو کوتاه‌تر یا انگلیسی بنویس، "
             "یا یه تیکه از آهنگ رو ویس بفرست تا از روش صدا پیداش کنم.")

EXPIRED = "این نتیجه منقضی شده؛ دوباره جستجو کن 🙂"

# ---------------------------------------------------------------- رابط کاربری

dp = Dispatcher()

REPLY_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🔥 آهنگ‌های ترند"), KeyboardButton(text="❓ راهنما")]],
    resize_keyboard=True,
)


def item_from_itunes(r: dict) -> dict:
    title = r.get("trackName") or ""
    artist = r.get("artistName") or ""
    year = (r.get("releaseDate") or "")[:4]
    return {"title": title, "artist": artist, "year": year,
            "album": r.get("collectionName") or "",
            "query": f"{artist} {title} official audio",
            "preview": r.get("previewUrl"),
            "artwork": (r.get("artworkUrl100") or "").replace("100x100", "400x400"),
            "list": None}


PAGE_SIZE = 5


def make_list(items: list[dict], header: str) -> str:
    """ساخت فهرست صفحه‌بندی‌شده؛ sid هر آهنگ یک‌بار ساخته می‌شود"""
    lid = store_put({"kind": "list", "header": header, "sids": []})
    sids = [store_put({**it, "list": lid}) for it in items]
    STORE[lid]["sids"] = sids
    _schedule_save()
    return lid


def render_page(lid: str, page: int = 0) -> tuple[str, InlineKeyboardMarkup] | None:
    """یک صفحه از فهرست — فقط دکمه‌ها + دکمه‌های قبلی/بعدی"""
    lst = STORE.get(lid)
    if not lst or lst.get("kind") != "list":
        return None
    sids = lst.get("sids") or []
    pages = max(1, -(-len(sids) // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    rows = []
    for sid in sids[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        it = STORE.get(sid)
        if not it:
            continue
        label = f"{it['title']} — {it['artist']}"
        if len(label) > 52:
            label = label[:51] + "…"
        rows.append([InlineKeyboardButton(text=f"🎵 {label}", callback_data=f"sel:{sid}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"page:{lid}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"page:{lid}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="✖️ بستن", callback_data="close")])
    text = f"{lst.get('header', '')}\n\n📄 صفحه‌ی {page + 1} از {pages} — <b>روی آهنگ بزن:</b>"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def show_panel(bot: Bot, chat_id: int, sid: str) -> bool:
    """پنل عملیات یک آهنگ: کاور + اطلاعات + دکمه‌های دانلود/پیش‌نمایش/متن"""
    item = STORE.get(sid)
    if not item:
        return False
    cap = [f"🎵 <b>{esc(item['title'])}</b>", f"🎤 {esc(item['artist'])}"]
    extra = []
    if item.get("album"):
        extra.append(f"💿 {esc(item['album'])}")
    if item.get("year"):
        extra.append(f"📅 {esc(item['year'])}")
    if extra:
        cap.append(" • ".join(extra))
    cap.append("\nچی کار کنم؟ 👇")

    row1 = [InlineKeyboardButton(text="📥 دانلود MP3", callback_data=f"dl:{sid}")]
    if item.get("preview"):
        row1.append(InlineKeyboardButton(text="▶️ پیش‌نمایش ۳۰ ثانیه", callback_data=f"prev:{sid}"))
    kb = [row1,
          [InlineKeyboardButton(text="🎼 آهنگ‌های مشابه", callback_data=f"sim:{sid}"),
           InlineKeyboardButton(text="🎤 هم‌خواننده", callback_data=f"art:{sid}")],
          [InlineKeyboardButton(text="📝 متن آهنگ", callback_data=f"lyr:{sid}")]]
    nav = []
    if item.get("list"):
        nav.append(InlineKeyboardButton(text="↩️ بازگشت به فهرست", callback_data=f"back:{item['list']}"))
    nav.append(InlineKeyboardButton(text="✖️ بستن", callback_data="close"))
    kb.append(nav)
    markup = InlineKeyboardMarkup(inline_keyboard=kb)

    text = "\n".join(cap)
    if item.get("artwork"):
        await bot.send_photo(chat_id, item["artwork"], caption=text, reply_markup=markup)
    else:
        await bot.send_message(chat_id, text, reply_markup=markup)
    return True

# ---------------------------------------------------------------- هندلرها

@dp.message(CommandStart())
async def cmd_start(msg: Message) -> None:
    await msg.answer(WELCOME, reply_markup=REPLY_KB)


@dp.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    await msg.answer(HELP, reply_markup=REPLY_KB)


@dp.message(F.text == "❓ راهنما")
async def btn_help(msg: Message) -> None:
    await cmd_help(msg)


@dp.message(Command("top"))
async def cmd_top(msg: Message) -> None:
    status = await msg.answer("⏳ یه لحظه، فهرست ترندها رو میارم...")
    try:
        songs = await apple_top_songs(10)
        if not songs:
            await status.edit_text("😕 فهرستی پیدا نشد، بعداً دوباره تلاش کن.")
            return
        items = []
        for t in songs:
            title, artist = t.get("name") or "", t.get("artistName") or ""
            items.append({"title": title, "artist": artist, "year": "", "album": "",
                          "query": f"{artist} {title} official audio", "preview": None,
                          "artwork": (t.get("artworkUrl100") or "").replace("100x100", "400x400"),
                          "list": None})
        header = "🔥 <b>۱۰ آهنگ پرشنونده‌ی جهان</b>"
        lid = make_list(items, header)
        rendered = render_page(lid, 0)
        if not rendered:
            await status.edit_text("😕 فهرستی پیدا نشد.")
            return
        try:
            await status.edit_text(rendered[0], reply_markup=rendered[1])
        except Exception:  # شبکه‌ی تلگرام لحظه‌ای قطع شد
            await msg.answer(rendered[0], reply_markup=rendered[1])
    except Exception as e:
        log.exception("top failed")
        try:
            await status.edit_text("⚠️ خطا در دریافت ترندها، دوباره تلاش کن.")
        except Exception:
            pass


@dp.message(F.text == "🔥 آهنگ‌های ترند")
async def btn_top(msg: Message) -> None:
    await cmd_top(msg)


@dp.message(Command("dl"))
async def cmd_dl(msg: Message, command: CommandObject) -> None:
    query = (command.args or "").strip()
    if not query:
        await msg.answer("اسم آهنگ رو هم بنویس. مثلاً:\n<code>/dl adele hello</code>")
        return
    await send_mp3(msg.bot, msg.chat.id, query, reply_msg=msg)


def media_ref(msg: Message) -> tuple[str | None, int]:
    """file_id و سایز صدای ارسالی کاربر (ویس/آهنگ/ویدیو)"""
    for attr in ("voice", "audio", "video", "video_note"):
        m = getattr(msg, attr, None)
        if m:
            return m.file_id, getattr(m, "file_size", 0) or 0
    if msg.document and (msg.document.mime_type or "").startswith("audio"):
        return msg.document.file_id, msg.document.file_size or 0
    return None, 0


@dp.message(lambda m: media_ref(m)[0] is not None)
async def handle_recognize(msg: Message) -> None:
    file_id, size = media_ref(msg)
    if size > MAX_TELEGRAM_DOWNLOAD:
        await msg.answer("⚠️ فایل بزرگ‌تر از ۲۰ مگابایت است؛ لطفاً تیکه‌ی کوتاه‌تری بفرست.")
        return
    status = await msg.answer("🎧 دارم گوش می‌دم...")
    tmp = Path(tempfile.mkdtemp(prefix="muz_rec_"))
    try:
        await msg.bot.download(file_id, destination=tmp / "input")
        await asyncio.to_thread(convert_to_wav, tmp / "input", tmp / "clip.wav")
        try:
            out = await shazam.recognize(str(tmp / "clip.wav"))
        except Exception as e:
            log.warning("shazam error: %s", e)
            await status.edit_text("⚠️ سرویس تشخیص موقتاً جواب نداد، دوباره امتحان کن.")
            return
        track = out.get("track")
        if not track:
            await status.edit_text("🤷 این صدا رو نشناختم!\n\n💡 سعی کن ۱۰ تا ۳۰ ثانیه از "
                                   "<b>خودِ آهنگ</b> (بدون صحبت و نویز) رو بفرستی.")
            return
        await status.edit_text("✅ شناختمش! یه لحظه...")
        await send_shazam_result(msg.bot, msg.chat.id, track)
        await status.delete()
    except Exception as e:
        log.exception("recognize failed")
        try:
            await msg.answer(f"⚠️ خطا در پردازش فایل: <code>{esc(e)[:150]}</code>")
        except Exception:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@dp.message(F.text)
async def handle_search(msg: Message) -> None:
    term = (msg.text or "").strip()
    if len(term) < 2:
        await msg.answer(WELCOME, reply_markup=REPLY_KB)
        return
    await msg.bot.send_chat_action(msg.chat.id, ChatAction.TYPING)
    try:
        results = await itunes_search(term, limit=25)
    except Exception as e:
        log.exception("itunes search failed")
        await msg.answer(f"⚠️ خطا در جستجو: <code>{esc(e)[:150]}</code>")
        return
    if not results:
        await msg.answer(NO_RESULT)
        return
    items = [item_from_itunes(r) for r in results]
    header = f"🔍 <b>نتایج برای «{esc(term)}»</b>"
    lid = make_list(items, header)
    rendered = render_page(lid, 0)
    if not rendered:
        await msg.answer(NO_RESULT)
        return
    await msg.answer(rendered[0], reply_markup=rendered[1])


async def send_shazam_result(bot: Bot, chat_id: int, t: dict) -> None:
    """نتیجه‌ی تشخیص Shazam → پنل عملیات (با غنی‌سازی از iTunes برای پیش‌نمایش/کاور)"""
    title = t.get("title") or "نامشخص"
    artist = t.get("subtitle") or "نامشخص"
    meta = track_meta(t)

    item = None
    try:  # تلاش برای گرفتن پیش‌نمایش و کاور باکیفیت از iTunes
        for r in await itunes_search(f"{artist} {title}", limit=3):
            if itunes_match(title, artist, r):
                item = item_from_itunes(r)
                item["title"], item["artist"] = title, artist  # عنوان دقیق Shazam
                break
    except Exception:
        item = None
    if item is None:
        item = {"title": title, "artist": artist,
                "year": (meta.get("RELEASED") or meta.get("RELEASE DATE") or "")[:4],
                "album": meta.get("ALBUM") or "",
                "query": f"{artist} {title} official audio", "preview": None,
                "artwork": (t.get("images") or {}).get("coverarthq"),
                "list": None}
    if not item.get("album") and meta.get("ALBUM"):
        item["album"] = meta["ALBUM"]
    if not item.get("year"):
        item["year"] = (meta.get("RELEASED") or meta.get("RELEASE DATE") or "")[:4]

    sid = store_put(item)
    await show_panel(bot, chat_id, sid)


@dp.callback_query(F.data == "close")
async def cb_close(cb: CallbackQuery) -> None:
    await safe_answer(cb, )
    try:
        await cb.message.delete()
    except Exception:
        pass


@dp.callback_query(F.data.startswith("sel:"))
async def cb_select(cb: CallbackQuery) -> None:
    sid = cb.data[4:]
    await safe_answer(cb, )
    if not await show_panel(cb.bot, cb.message.chat.id, sid):
        try:
            await cb.message.answer(f"⚠️ {EXPIRED}")
        except Exception:
            pass


@dp.callback_query(F.data.startswith("page:"))
async def cb_page(cb: CallbackQuery) -> None:
    try:
        _, lid, p = cb.data.split(":")
        rendered = render_page(lid, int(p))
    except Exception:
        rendered = None
    if not rendered:
        await safe_answer(cb, EXPIRED, show_alert=True)
        return
    await safe_answer(cb, )
    try:
        await cb.message.edit_text(rendered[0], reply_markup=rendered[1])
    except Exception:
        await cb.message.answer(rendered[0], reply_markup=rendered[1])


@dp.callback_query(F.data.startswith("back:"))
async def cb_back(cb: CallbackQuery) -> None:
    rendered = render_page(cb.data[5:], 0)
    if not rendered:
        await safe_answer(cb, EXPIRED, show_alert=True)
        return
    await safe_answer(cb, )
    await cb.message.answer(rendered[0], reply_markup=rendered[1])
    try:
        await cb.message.delete()
    except Exception:
        pass


@dp.callback_query(F.data.startswith("dl:"))
async def cb_download(cb: CallbackQuery) -> None:
    item = STORE.get(cb.data[3:])
    if not item:
        await safe_answer(cb, EXPIRED, show_alert=True)
        return
    await safe_answer(cb, "⏳ دانلود شروع شد...")
    await send_mp3(cb.bot, cb.message.chat.id, item["query"], item=item, reply_msg=cb.message)


@dp.callback_query(F.data.startswith("lyr:"))
async def cb_lyrics(cb: CallbackQuery) -> None:
    item = STORE.get(cb.data[4:])
    if not item:
        await safe_answer(cb, EXPIRED, show_alert=True)
        return
    await safe_answer(cb, )
    try:
        data = await lrclib_lyrics(item["artist"], item["title"])
    except Exception as e:
        await cb.message.answer(f"⚠️ خطا در دریافت متن: <code>{esc(e)[:120]}</code>")
        return
    lyrics = (data or {}).get("plainLyrics") or (data or {}).get("syncedLyrics") or ""
    if not lyrics:
        await cb.message.answer(f"📝 متنی برای «{esc(item['title'])}» پیدا نشد.")
        return
    lyrics = re.sub(r"^\[\d{2}:\d{2}\.\d{2,3}\]\s*", "", lyrics, flags=re.M)  # حذف تایم‌کد
    header = f"📝 <b>{esc(item['title'])}</b> — {esc(item['artist'])}\n{'─' * 20}\n"
    body = esc(lyrics)
    for i in range(0, len(body), 3800):
        await cb.message.answer(header + body[i:i + 3800])


@dp.callback_query(F.data.startswith("prev:"))
async def cb_preview(cb: CallbackQuery) -> None:
    item = STORE.get(cb.data[5:])
    if not item or not item.get("preview"):
        await safe_answer(cb, "پیش‌نمایشی موجود نیست 🙂", show_alert=True)
        return
    await safe_answer(cb, "▶️ در حال ارسال پیش‌نمایش...")
    try:
        await cb.message.answer_audio(item["preview"], title=item["title"],
                                      performer=item["artist"],
                                      caption="▶️ پیش‌نمایش ۳۰ ثانیه‌ای (iTunes)")
    except Exception as e:
        await cb.message.answer(f"⚠️ ارسال پیش‌نمایش ناموفق: <code>{esc(e)[:120]}</code>")


@dp.callback_query(F.data.startswith("sim:"))
async def cb_similar(cb: CallbackQuery) -> None:
    """آهنگ‌های مشابه — هم‌سبک ولی آهنگ/خواننده‌ی دیگر (از هنرمندان مرتبط Deezer)"""
    item = STORE.get(cb.data[4:])
    if not item:
        await safe_answer(cb, EXPIRED, show_alert=True)
        return
    await safe_answer(cb, "🎼 در حال پیدا کردن مشابه‌ها...")
    similar: list[dict] = []
    try:
        similar = await deezer_similar(item["artist"], item["title"], count=10)
    except Exception:
        log.warning("deezer similar failed", exc_info=True)
    if similar:
        header = f"🎼 <b>مشابه‌های «{esc(item['title'])}»</b> — هم‌سبک، از خواننده‌های دیگر"
        items = similar
    else:  # جایگزین: آهنگ‌های خود خواننده
        try:
            results = await itunes_search(item["artist"], limit=10, attribute="artistTerm")
        except Exception:
            results = []
        if not results:
            await cb.message.answer("🎼 مشابهی پیدا نشد.")
            return
        header = f"🎤 <b>آهنگ‌های {esc(item['artist'])}</b>"
        items = [item_from_itunes(r) for r in results]
    lid = make_list(items, header)
    rendered = render_page(lid, 0)
    if not rendered:
        await cb.message.answer("🎼 مشابهی پیدا نشد.")
        return
    await cb.message.answer(rendered[0], reply_markup=rendered[1])


@dp.callback_query(F.data.startswith("art:"))
async def cb_artist(cb: CallbackQuery) -> None:
    """آهنگ‌های همین خواننده"""
    item = STORE.get(cb.data[4:])
    if not item:
        await safe_answer(cb, EXPIRED, show_alert=True)
        return
    await safe_answer(cb, )
    try:
        results = await itunes_search(item["artist"], limit=10, attribute="artistTerm")
    except Exception:
        results = []
    if not results:
        await cb.message.answer(f"🎤 آهنگی از «{esc(item['artist'])}» پیدا نشد.")
        return
    items = [item_from_itunes(r) for r in results]
    header = f"🎤 <b>آهنگ‌های {esc(item['artist'])}</b>"
    lid = make_list(items, header)
    rendered = render_page(lid, 0)
    if not rendered:
        return
    await cb.message.answer(rendered[0], reply_markup=rendered[1])


async def send_mp3(bot: Bot, chat_id: int, query: str, item: dict | None = None,
                   reply_msg: Message | None = None) -> None:
    """دانلود از یوتیوب، تبدیل به MP3 و ارسال در چت"""
    name = f"{item['title']} — {item['artist']}" if item else query
    status = await (reply_msg.answer(f"⏳ <b>{esc(name)}</b>\n⏬ در حال دانلود از یوتیوب...")
                    if reply_msg else bot.send_message(chat_id, f"⏳ <b>{esc(name)}</b>\n⏬ در حال دانلود..."))
    tmp = Path(tempfile.mkdtemp(prefix="muz_dl_"))
    try:
        mp3, info = await asyncio.to_thread(yt_download, query, tmp)
        if mp3.stat().st_size > MAX_MP3_BYTES:
            await status.edit_text("⚠️ فایل بزرگ‌تر از حد مجاز تلگرام (۵۰ مگ) شد. آهنگ کوتاه‌تری انتخاب کن.")
            return
        title, performer = split_artist_title(info.get("title") or query, info.get("uploader"))
        if item:  # عنوان دقیق‌تر از نتیجه‌ی جستجو
            title, performer = item["title"], item["artist"]
        thumb = await asyncio.to_thread(make_thumb, tmp)
        try:
            await status.edit_text(f"⏳ <b>{esc(name)}</b>\n📤 در حال ارسال...")
        except Exception:
            pass
        await bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)
        audio_kwargs = dict(
            title=title[:64], performer=performer[:64],
            duration=int(info.get("duration") or 0) or None,
            caption=f"🔗 <a href=\"{esc(info.get('webpage_url', ''))}\">منبع در یوتیوب</a>",
            request_timeout=120,  # آپلود فایل بزرگ ممکن است طول بکشد
        )
        if thumb:
            audio_kwargs["thumbnail"] = FSInputFile(thumb)
        # ارسال با تلاش مجدد — شبکه‌ی تلگرام گاهی لحظه‌ای قطع می‌شود
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                await bot.send_audio(chat_id, FSInputFile(mp3, filename=clean_filename(f"{performer} - {title}")),
                                     **audio_kwargs)
                last_err = None
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.warning("send_audio attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(2 * (attempt + 1))
        if last_err:
            raise last_err
        await status.delete()
    except TrackTooLong as e:
        await status.edit_text(f"⚠️ {esc(e)}")
    except Exception as e:
        log.exception("download failed for %r", query)
        try:
            await status.edit_text("⚠️ دانلود ناموفق بود 😕\n\n💡 عبارت جستجو رو عوض کن "
                                   f"(مثلاً اسم دقیق‌تر).\n<code>{esc(e)[:150]}</code>")
        except Exception:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- اجرا

def load_token() -> str | None:
    tok = (os.environ.get("BOT_TOKEN") or "").strip()
    if tok:
        return tok
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return None


async def start_keepalive() -> None:
    """اندپوینت سلامت — روی Hugging Face Space بات را بیدار نگه می‌دارد
    (با یک پینگ‌کننده‌ی رایگان مثل cron-job.org هر ۳۰ دقیقه)"""
    port = int(os.environ.get("PORT", "7860"))
    try:
        from aiohttp import web

        async def health(_):
            return web.Response(text="OK — Music finder bot is running 🎵")

        app = web.Application()
        app.router.add_get("/", health)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        log.info("Health endpoint on 0.0.0.0:%s", port)
    except OSError as e:
        log.warning("Health endpoint start نشد (مهم نیست): %s", e)


async def main() -> None:
    token = load_token()
    if not token:
        raise SystemExit(
            "❌ توکن بات پیدا نشد!\n"
            "توکن رو از @BotFather بگیر و داخل فایل .env بنویس:\n"
            "    BOT_TOKEN=123456:ABC-DEF...\n"
            "یا به‌صورت متغیر محیطی: BOT_TOKEN=... python bot.py"
        )
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    log.info("🎵 بات %s (@%s) بالا آمد | ffmpeg=%s", me.full_name, me.username, FFMPEG)
    await start_keepalive()
    try:
        # polling_timeout کوتاه‌تر: اگر اتصال بی‌صدا قطع شد، حداکثر طی ~۴۰ ثانیه
        # تشخیص داده می‌شود و دوباره وصل می‌شود (جلوگیری از قطعی‌های ۲۰ دقیقه‌ای)
        await dp.start_polling(bot, polling_timeout=15)
    finally:
        _save_store()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
