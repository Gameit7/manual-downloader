import os
import re
import json
import time
import shutil
import tempfile
import asyncio
import datetime
import hashlib
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
import email.utils
import base64
import httpx
import sys
import unicodedata

# ─── Environment Configuration ──────────────────────────────────
TURSO_URL = os.environ.get("TURSO_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")
PIXELDRAIN_API_KEY = os.environ.get("PIXELDRAIN_API_KEY", "")

GAS_PROXIES = []
for _k in ["GAS_PROXY_URL", "GAS_PROXY_URL_2", "GAS_PROXY_URL_3", "GAS_PROXY_URL_4"]:
    _v = os.environ.get(_k, "").strip()
    if _v and _v not in GAS_PROXIES:
        GAS_PROXIES.append(_v)

_proxy_idx = 0
def get_ordered_proxies() -> list:
    global _proxy_idx
    if not GAS_PROXIES:
        return []
    n = len(GAS_PROXIES)
    start = _proxy_idx % n
    _proxy_idx += 1
    return [GAS_PROXIES[(start + i) % n] for i in range(n)]

TORRENT_DOWNLOAD_TIMEOUT = int(os.environ.get("TORRENT_DOWNLOAD_TIMEOUT", "7200"))
MIN_TORRENT_SEEDERS = int(os.environ.get("MIN_TORRENT_SEEDERS", "10"))

NYAA_TRACKERS = [
    "http://nyaa.tracker.wf:7777/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
    "udp://tracker.moeking.me:6969/announce",
    "udp://explodie.org:6969/announce",
]

ANILIST_URL = "https://graphql.anilist.co"

# ─── Logging ────────────────────────────────────────────────────
_log_lines = []

def log_message(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    _log_lines.append(line)

def get_log() -> str:
    return "\n".join(_log_lines)

def clear_log():
    _log_lines.clear()

# ─── Turso Database Helpers ─────────────────────────────────────
def _make_turso_args(args: list) -> list:
    turso_args = []
    for arg in (args or []):
        if arg is None:
            turso_args.append({"type": "null"})
        elif isinstance(arg, int):
            turso_args.append({"type": "integer", "value": str(arg)})
        elif isinstance(arg, float):
            turso_args.append({"type": "float", "value": arg})
        else:
            turso_args.append({"type": "text", "value": str(arg)})
    return turso_args

def _parse_turso_result(exec_result: dict) -> list:
    cols = [col["name"] for col in exec_result.get("cols", [])]
    rows = exec_result.get("rows", [])
    parsed_rows = []
    for row in rows:
        row_dict = {}
        for i, cell in enumerate(row):
            val_type = cell.get("type")
            val = cell.get("value")
            if val_type == "null":
                row_dict[cols[i]] = None
            elif val_type == "integer":
                row_dict[cols[i]] = int(val)
            elif val_type == "float":
                row_dict[cols[i]] = float(val)
            else:
                row_dict[cols[i]] = str(val)
        parsed_rows.append(row_dict)
    return parsed_rows

async def execute_sql(sql: str, args: list = None) -> list:
    body = {
        "requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": _make_turso_args(args)}},
            {"type": "close"}
        ]
    }
    headers = {"Authorization": f"Bearer {TURSO_TOKEN}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.post(f"{TURSO_URL}/v2/pipeline", headers=headers, json=body)
            if r.status_code != 200:
                log_message(f"DB error ({r.status_code})")
                return []
            res = r.json()
            first_res = res.get("results", [{}])[0]
            if first_res.get("type") == "error":
                err_msg = first_res.get("error", {}).get("message", "Unknown DB error")
                log_message(f"DB execute error: {err_msg}")
                return []
            exec_result = first_res.get("response", {}).get("result", {})
            return _parse_turso_result(exec_result)
    except Exception as e:
        log_message(f"DB exception: {e}")
        return []

async def execute_sql_batch(statements: list) -> list:
    if not statements:
        return []
    requests = []
    for sql, args in statements:
        requests.append({"type": "execute", "stmt": {"sql": sql, "args": _make_turso_args(args)}})
    requests.append({"type": "close"})

    headers = {"Authorization": f"Bearer {TURSO_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{TURSO_URL}/v2/pipeline", headers=headers, json={"requests": requests})
        if r.status_code != 200:
            log_message(f"DB batch error ({r.status_code})")
            return []
        res = r.json()
        results = []
        for i, result_obj in enumerate(res.get("results", [])):
            resp = result_obj.get("response", {})
            if resp.get("type") == "execute":
                results.append(_parse_turso_result(resp.get("result", {})))
        return results

# ─── Title & Episode Parsing Functions ─────────────────────────
# (Copied from sync_job.py IDENTICALLY to ensure matching results)

def strip_accents(text: str) -> str:
    if not text or not isinstance(text, str):
        return ""
    normalized = unicodedata.normalize('NFKD', text)
    return "".join(c for c in normalized if unicodedata.category(c) != 'Mn')

def clean_title(title: str) -> str:
    if not title or not isinstance(title, str):
        return ""
    title = strip_accents(title)
    title = re.sub(r'\(.*?\)', '', title)
    title = re.sub(r'\[.*?\]', '', title)
    title = re.sub(r'[:\\/*?"<>|]', ' ', title)
    title = re.sub(r"[^a-zA-Z0-9\s\-'\.]", '', title)
    return re.sub(r'\s+', ' ', title).strip()

def clean_and_strip(title: str) -> str:
    t = clean_title(title)
    t = re.sub(r'\b\d{4}\b', ' ', t)
    t = re.sub(r'\b\d+(st|nd|rd|th)\s+season\b', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\bseason\s+\d+\b', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\bcour\s+\d+\b', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\bs\d+\b', '', t, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', t).strip()

def parse_erai_anime_title(filename: str) -> str:
    if not filename or not isinstance(filename, str):
        return ""
    m = re.match(r'^\[Erai-raws\]\s+(.*?)\s+-\s+\d+', filename)
    return m.group(1).strip() if m else ""

def get_part_number(title: str) -> int:
    if not title or not isinstance(title, str):
        return 0
    t_lower = title.lower()
    m = re.search(r'\b(?:part|cour|pt)\s*[-_.: ]*\s*(iv|iii|ii|i)\b', t_lower)
    if m:
        roman_map = {'i': 1, 'ii': 2, 'iii': 3, 'iv': 4}
        return roman_map.get(m.group(1), 0)
    m = re.search(r'\b(\d+)(st|nd|rd|th)\s+(?:part|cour|pt)\b', t_lower)
    if m:
        return int(m.group(1))
    m = re.search(r'\b(?:part|cour|pt)\s*[-_.: ]*\s*0*(\d+)\b', t_lower)
    if m:
        return int(m.group(1))
    return 0

def get_season_number(title: str) -> int:
    if not title or not isinstance(title, str):
        return 1
    title_lower = title.lower()
    title_lower = re.sub(r'^\[erai-raws\]\s+', '', title_lower)
    title_lower = re.split(r'\s+-\s+\d+', title_lower)[0]
    m = re.search(r'\bs(\d+)e(\d+)\b', title_lower)
    if m:
        return int(m.group(1))
    m = re.search(r'\bs(?:eason)?\s*0*(\d+)\b', title_lower)
    if m:
        return int(m.group(1))
    m = re.search(r'\b(\d+)(st|nd|rd|th)(?:\s+season)?\b', title_lower)
    if m:
        return int(m.group(1))
    m = re.search(r'\b(?:part|cour)\s*0*(\d+)\b', title_lower)
    if m:
        return int(m.group(1))
    clean_no_ver = re.sub(r'\bv\d+\b', '', title_lower)
    if re.search(r'\bii\b$', clean_no_ver) or re.search(r'\bii\b(?=\s)', clean_no_ver):
        return 2
    if re.search(r'\biii\b$', clean_no_ver) or re.search(r'\biii\b(?=\s)', clean_no_ver):
        return 3
    if re.search(r'\biv\b$', clean_no_ver) or re.search(r'\biv\b(?=\s)', clean_no_ver):
        return 4
    if (re.search(r'\bv\b$', clean_no_ver) or re.search(r'\bv\b(?=\s)', clean_no_ver)) and not re.search(r'\b(1080p|720p|480p|2160p|mkv|mp4|v)\s+v\b', title_lower):
        return 5
    clean_end = re.sub(r'[^a-z0-9\s]', '', title_lower).strip()
    m = re.search(r'\s+(\d+)$', clean_end)
    if m:
        num = int(m.group(1))
        if num < 10:
            return num
    return 1

def is_blacklisted_platform(title: str) -> bool:
    if not title or not isinstance(title, str):
        return False
    return bool(re.search(r'\b(nf|netflix|iq|iqiyi)\b', title.lower()))

def get_audio_score(title: str) -> int:
    """
    Score hierarchy:
      4: Multi-Audio (e.g. MULTi-Audio / MULTi AAC)
      3: Dual-Audio (e.g. DUAL / Dual-Audio / DUAL AAC)
      2: Explicit Japanese Audio (e.g. (JA), (JP), Japanese Dub, Japanese Audio, WEB-DLJPN)
      1: Default / Standard Japanese (clean anime release with no foreign audio tags)
     -5: Foreign Single Audio Only (e.g. (KA), Korean Audio, (ZH), Chinese Dub, standalone English Dub)
    """
    if not title or not isinstance(title, str):
        return 0
    t_lower = title.lower()

    # 1. Multi-Audio (highest priority)
    if (re.search(r'\bmulti[-_ ]*audio\b|\bmultiaudio\b', t_lower) or
        re.search(r'\bmulti[-_\.\s]*(aac|ddp|ac3|flac|opus|dts)', t_lower) or
        re.search(r'[\.\[\(]multi[\.\]\)](?![-_\.\s]*sub)', t_lower)):
        return 4

    # 2. Dual-Audio
    if (re.search(r'\bdual[-_ ]*audio\b|\bdualaudio\b', t_lower) or
        re.search(r'\bdual[-_\.\s]*(aac|ddp|ac3|flac|opus|dts)', t_lower) or
        re.search(r'[\.\[\(]dual[\.\]\)]|\bdual\b', t_lower)):
        return 3

    # 3. Check for Explicit Foreign Audio Only (Korean, Chinese, English dub, etc. without Dual/Multi)
    is_foreign = bool(re.search(
        r'[\(\[]\s*(ka|ko|kor|zh|cn|chi)\s*[\)\]]|'
        r'\b(korean|kor)\s*[-_ ]*(audio|dub)\b|'
        r'\b(chinese|mandarin)\s*[-_ ]*(audio|dub)\b|'
        r'\b(english|eng)\s*[-_ ]*dub\b|'
        r'web-dl\s*(kor|chi)',
        t_lower
    ))

    # 4. Explicit Japanese Audio
    is_japanese = bool(re.search(
        r'[\(\[]\s*(ja|jp|jpn)\s*[\)\]]|'
        r'\b(japanese|jpn|jap)\s*[-_ ]*(audio|dub)\b|'
        r'web-dl\s*jpn',
        t_lower
    ))

    if is_foreign and not is_japanese:
        return -5

    if is_japanese:
        return 2

    # 5. Default Japanese (standard anime release)
    return 1

def is_multi_audio_torrent(title: str) -> bool:
    return get_audio_score(title) >= 3


def get_platform_score(title: str) -> int:
    if not title or not isinstance(title, str):
        return 0
    t_lower = title.lower()
    if re.search(r'\b(cr|crunchyroll|amzn|amazon|shahid|starzplay|starz|adn)\b', t_lower):
        return 3
    elif re.search(r'\b(nf|netflix)\b', t_lower):
        return 2
    elif re.search(r'\b(bili|bilibili|iq|iqiyi|disney|hulu|abema|baha|bahamut|ani-one|anione|muse|yt|youtube|wetv)\b', t_lower):
        return 1
    return 0

def get_quality_weight(title: str) -> int:
    if not title or not isinstance(title, str):
        return 0
    t_lower = title.lower()
    if "1080" in t_lower:
        return 3
    elif "720" in t_lower:
        return 2
    elif "480" in t_lower:
        return 1
    return 0

def get_source_weight(title: str) -> int:
    if not title or not isinstance(title, str):
        return 0
    t_lower = title.lower()
    if "web-dl" in t_lower or "webdl" in t_lower:
        return 2
    elif "webrip" in t_lower:
        return 1
    return 0

SEASON_STOPWORDS = {
    "1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th", "10th",
    "season", "cour", "part", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9",
    "tv", "bd", "bluray", "blu-ray", "dvd", "web", "web-dl", "webrip", "hdtv",
    "uncensored", "uncut", "censored", "dual", "multi", "audio", "sub", "subs",
    "subtitle", "subtitles", "dub", "dubs", "dubbed", "v0", "v1", "v2", "v3",
    "batch", "reupload", "re-upload", "remux", "hevc", "x264", "x265", "h264", "h265",
    "10bit", "10bits", "8bit", "8bits", "version", "edit", "specials", "special", "mkv", "mp4", "avi", "webm",
    "1080p", "720p", "480p", "1080", "720", "480", "2160p", "2160", "4k", "5k", "8k",
    "aac2", "aac", "aac5", "ddp2", "ddp5", "ddp", "dts", "ac3", "flac", "avc", "av1", "av01",
    "hdr", "hdr10", "hdr10plus", "sdr", "atmos", "hi10p", "hi10",
    "amzn", "cr", "cru", "nf", "nflx", "netflix", "hulu", "dnp", "disney", "bilibili", "bili", "bsite", "yt", "youtube", "adn", "wetv", "iq", "iqiyi", "mgtv", "youku", "abema", "baha", "bahamut",
    "varyg", "subsplease", "erai-raws", "erai", "judas", "ember", "asw", "kaede", "horriblesubs", "horrible", "sirius", "pas", "commie",
    "tsundere", "raws", "rapta", "repack", "vostfr", "dl", "ona", "ova", "movie", "weekly",
    "eng", "english", "jap", "japanese", "ara", "arabic", "multi-subs", "multisubs", "multisub", "multi-sub",
    "gradation"
}

def get_clean_words(title: str) -> list:
    title_lower = title.lower()
    title_no_se = re.sub(r'\b(s\d+e\d+|s\d+|e\d+)\b', ' ', title_lower)
    title_no_num = re.sub(r'\b\d+\b', ' ', title_no_se)
    clean_t = title_no_num.replace('.', ' ').replace('-', ' ').replace("'", "")
    clean_t = re.sub(r'[^a-zA-Z0-9\s]', ' ', clean_t)
    words = clean_t.split()
    if not words:
        clean_with_num = title_no_se.replace('.', ' ').replace('-', ' ').replace("'", "")
        clean_with_num = re.sub(r'[^a-zA-Z0-9\s]', ' ', clean_with_num)
        words = clean_with_num.split()
    particles = {
        "no", "to", "in", "of", "a", "an", "the", "is", "at", "by", "on",
        "and", "or", "for", "with", "wa", "ga", "wo", "ni", "de", "ka", "mo"
    }
    filtered = []
    for w in words:
        w_stripped = w.strip("-'")
        if not w_stripped or w_stripped in SEASON_STOPWORDS or w_stripped in particles:
            continue
        if len(w_stripped) >= 2 or (len(w_stripped) == 1 and w_stripped.isalnum()):
            filtered.append(w_stripped)
    return filtered

def is_matching_torrent(torrent_title: str, romaji: str, english: str, ep: int, synonyms: list = None, is_special: bool = False) -> bool:
    if not torrent_title or not romaji:
        return False
    t_lower = torrent_title.lower()
    synonyms = synonyms or []

    m_ep = re.search(r'\b(?:s\d+)?e(\d+)\b', t_lower)
    if m_ep:
        if int(m_ep.group(1)) != ep:
            return False
    else:
        bypass_ep_check = False
        if is_special and ep == 1:
            clean_title_for_ep = re.sub(r'\b(1080p|720p|480p|2160p|1080|720|480|2160|3d|4k|5k|8k|x264|x265|h264|h265|10bit|8bit|v\d+)\b', '', t_lower)
            other_ep_match = re.search(r'\b(?:ep|episode|ep\.|sp|special)?\s*0*([2-9]|\d{2,})\b', clean_title_for_ep)
            if not other_ep_match:
                bypass_ep_check = True
        if not bypass_ep_check:
            ep_pattern = re.compile(rf'\b0*{ep}\b')
            if not ep_pattern.search(t_lower):
                return False

    torrent_season = get_season_number(torrent_title)
    clean_romaji = clean_title(romaji)
    clean_english = clean_title(english) if english else ""
    target_season = get_season_number(clean_romaji)
    if target_season == 1 and clean_english:
        eng_s = get_season_number(clean_english)
        if eng_s > 1:
            target_season = eng_s
    if torrent_season != target_season:
        return False

    target_part = get_part_number(clean_romaji) or (get_part_number(clean_english) if english else 0)
    torrent_part = get_part_number(torrent_title)
    if torrent_part != target_part:
        return False

    valid_synonyms = []
    for s in synonyms:
        if not s or not isinstance(s, str):
            continue
        if re.search(r'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]', s):
            c_words = get_clean_words(clean_title(s))
            if len(c_words) < 2 or all(len(w) < 3 for w in c_words):
                continue
        valid_synonyms.append(s)

    def is_title_match(anime_title: str, torrent_title_lower: str) -> bool:
        if not anime_title:
            return False
        def check_match(raw_title_str: str) -> bool:
            clean_t = clean_title(raw_title_str)
            words = get_clean_words(clean_t)
            if not words:
                return False

            matching_words = set()
            for w in words:
                if re.search(rf'\b{re.escape(w)}\b', torrent_title_lower):
                    matching_words.add(w)

            # Check adjacent merged words (e.g. "Dogul Wang" -> "Dogulwang", "Chainsaw Man" -> "Chainsawman")
            for i in range(len(words) - 1):
                w1, w2 = words[i], words[i+1]
                if len(w1) >= 2 and len(w2) >= 2:
                    pair = w1 + w2
                    if re.search(rf'\b{re.escape(pair)}\b', torrent_title_lower):
                        matching_words.add(w1)
                        matching_words.add(w2)

            # Check if entire title with no spaces matches
            if len(words) >= 2:
                all_merged = "".join(words)
                if re.search(rf'\b{re.escape(all_merged)}\b', torrent_title_lower):
                    for w in words:
                        matching_words.add(w)

            ratio = len(matching_words) / len(words)
            if len(words) <= 2:
                return len(matching_words) == len(words)
            if len(words) == 3:
                return len(matching_words) >= 2
            return ratio >= 0.75
        if check_match(anime_title):
            return True
        delimiters = [':', '-']
        for delim in delimiters:
            if delim in anime_title:
                parts = anime_title.split(delim)
                for part in parts:
                    part_stripped = part.strip()
                    if len(get_clean_words(clean_title(part_stripped))) >= 2:
                        if check_match(part_stripped):
                            return True
        return False

    romaji_match = is_title_match(romaji, t_lower)
    eng_match = is_title_match(english, t_lower) if english else False
    syn_match = any(is_title_match(syn, t_lower) for syn in valid_synonyms)
    if not romaji_match and not eng_match and not syn_match:
        return False

    clean_matched_words = get_clean_words(romaji)
    is_trusted_group = bool(re.search(r'\[?(erai[-_ ]?raws|toonshub)\]?', t_lower)) and len(clean_matched_words) >= 2

    torrent_clean = clean_title(torrent_title)
    torrent_words = get_clean_words(torrent_clean)
    anime_words = set(get_clean_words(romaji) + (get_clean_words(english) if english else []))
    for syn in valid_synonyms:
        if syn:
            anime_words.update(get_clean_words(syn))
    extra_words = []
    concat_parts = set()
    for i in range(len(torrent_words) - 1):
        pair_word = torrent_words[i] + torrent_words[i+1]
        if pair_word in anime_words:
            concat_parts.add(torrent_words[i])
            concat_parts.add(torrent_words[i+1])
    for w in torrent_words:
        if w in anime_words or w in concat_parts:
            continue
        is_concat = False
        for w1 in anime_words:
            if len(w1) >= 3 and w.startswith(w1) and w[len(w1):] in anime_words:
                is_concat = True
                break
        if not is_concat:
            extra_words.append(w)
    max_extra = 2 if is_trusted_group else 0
    if len(extra_words) > max_extra:
        return False

    is_multi_sub = bool(re.search(
        r'\b(multi|m)\s*[-_:]?\s*subs?\b|'
        r'multisubs?|'
        r'multiple\s+subtitles?|'
        r'multiple\s+subs?\b|'
        r'\[multi[-_ ]?subs?\]|'
        r'\[multiple[-_ ]?subtitles?\]',
        t_lower
    ))
    return is_multi_sub

def _title_segments(title: str) -> list:
    segs = []
    if not title or not isinstance(title, str):
        return segs
    for part in re.split(r':|\s+-\s+', title):
        cleaned = clean_and_strip(part)
        if cleaned and len(cleaned.split()) >= 2 and cleaned not in segs:
            segs.append(cleaned)
    return segs[:4]

def get_search_queries(romaji: str, english: str, ep: int, synonyms: list = None, is_special: bool = False, erai_title: str = None) -> list:
    queries = []
    ep_str = f"{ep:02d}"
    synonyms = synonyms or []
    r_base = clean_and_strip(romaji)
    e_base = clean_and_strip(english) if english else ""
    r_super = re.sub(r'[\-\.]', ' ', r_base).replace("'", "")
    r_super = re.sub(r'\s+', ' ', r_super).strip()
    e_super = ""
    if e_base:
        e_super = re.sub(r'[\-\.]', ' ', e_base).replace("'", "")
        e_super = re.sub(r'\s+', ' ', e_super).strip()
    search_bases = []
    if erai_title:
        search_bases.append(clean_and_strip(erai_title))
    for source_title in (romaji, english):
        for seg in _title_segments(source_title):
            if seg not in search_bases:
                search_bases.append(seg)
    search_bases.extend([r_base, e_base])
    raw_r = re.sub(r'[:\\/*?"<>|\[\]\(\)]', ' ', romaji).strip()
    raw_r = re.sub(r'\s+', ' ', raw_r)
    if raw_r and raw_r != r_base and raw_r not in search_bases:
        search_bases.append(raw_r)
    if r_super and r_super not in search_bases:
        search_bases.append(r_super)
    if e_super and e_super not in search_bases:
        search_bases.append(e_super)

    # Collapsed variations (e.g. "Dogul Wang" -> "Dogulwang", "Chainsaw Man" -> "Chainsawman")
    if len(r_base.split()) >= 2:
        r_collapsed = "".join(r_base.split())
        if len(r_collapsed) >= 3 and r_collapsed not in search_bases:
            search_bases.append(r_collapsed)
    COMMON_SUFFIXES = ["saki", "tabi", "gumi", "jima", "bashi", "mura", "kan", "sou", "ken", "chou"]
    for title_base in [r_base] + synonyms:
        if not title_base:
            continue
        c_words = clean_and_strip(title_base).split()
        for i, w in enumerate(c_words[:3]):
            w_lower = w.lower()
            if "-" in w:
                unhyphen = w.replace("-", "")
                spaced = w.replace("-", " ")
                v1 = " ".join(c_words[:i] + [unhyphen] + c_words[i+1:])
                v2 = " ".join(c_words[:i] + [spaced] + c_words[i+1:])
                for var in (v1, v2):
                    if var and var not in search_bases:
                        search_bases.append(var)
            else:
                for sfx in COMMON_SUFFIXES:
                    if w_lower.endswith(sfx) and len(w_lower) > len(sfx) + 2:
                        pfx = w[:-len(sfx)]
                        hyphen_var = f"{pfx}-{sfx}"
                        space_var = f"{pfx} {sfx}"
                        v1 = " ".join(c_words[:i] + [hyphen_var] + c_words[i+1:])
                        v2 = " ".join(c_words[:i] + [space_var] + c_words[i+1:])
                        for var in (v1, v2):
                            if var and var not in search_bases:
                                search_bases.append(var)
    for syn in synonyms:
        cleaned_syn = clean_and_strip(syn)
        if cleaned_syn and cleaned_syn not in search_bases:
            search_bases.append(cleaned_syn)
    for base in search_bases:
        if not base:
            continue
        queries.append(f'{base} "{ep_str}"')
        queries.append(f'{base} {ep_str}')
        if is_special and ep == 1:
            queries.append(base)
        words = base.split()
        if len(words) > 3:
            short = " ".join(words[:3])
            queries.append(f'{short} "{ep_str}"')
            queries.append(f'{short} {ep_str}')
            if is_special and ep == 1:
                queries.append(short)
    for base_romaji in [r_base] + synonyms:
        if not base_romaji:
            continue
        base_romaji_clean = clean_and_strip(base_romaji)
        if not base_romaji_clean:
            continue
        r_o = re.sub(r'\bwo\b', 'o', base_romaji_clean, flags=re.IGNORECASE)
        r_wo = re.sub(r'\bo\b', 'wo', base_romaji_clean, flags=re.IGNORECASE)
        for var in [r_o, r_wo]:
            if var != base_romaji_clean:
                queries.append(f'{var} "{ep_str}"')
                queries.append(f'{var} {ep_str}')
                words = var.split()
                if len(words) > 3:
                    short_var = " ".join(words[:3])
                    queries.append(f'{short_var} "{ep_str}"')
                    queries.append(f'{short_var} {ep_str}')
        r_words = base_romaji_clean.split()
        if len(r_words) >= 2:
            merged_first_two = r_words[0] + r_words[1]
            rest = " ".join(r_words[2:])
            var_merged = f"{merged_first_two} {rest}".strip()
            queries.append(f'{var_merged} "{ep_str}"')
            queries.append(f'{var_merged} {ep_str}')
            queries.append(f'{merged_first_two} "{ep_str}"')
            queries.append(f'{merged_first_two} {ep_str}')
            var_merged_o = re.sub(r'\bwo\b', 'o', var_merged, flags=re.IGNORECASE)
            if var_merged_o != var_merged:
                queries.append(f'{var_merged_o} "{ep_str}"')
                queries.append(f'{var_merged_o} {ep_str}')

    # Fallback targeted group queries (Erai-raws & ToonsHub) to rescue older episodes buried past Nyaa RSS 75-item limits
    for base in search_bases:
        if not base:
            continue
        if base in (r_base, e_base) or (erai_title and base == clean_and_strip(erai_title)):
            queries.append(f'[Erai-raws] {base} "{ep_str}"')
            queries.append(f'[ToonsHub] {base} "{ep_str}"')
            words = base.split()
            if len(words) > 3:
                short = " ".join(words[:3])
                queries.append(f'[Erai-raws] {short} "{ep_str}"')
                queries.append(f'[ToonsHub] {short} "{ep_str}"')

    return list(dict.fromkeys(queries))

# ─── Torrent Hash Extraction ──────────────────────────────────
def extract_info_hash(payload: bytes) -> str:
    try:
        data = payload
        def read_str(i):
            colon = data.index(b":", i)
            length = int(data[i:colon])
            start = colon + 1
            return start, start + length
        def skip(i):
            c = data[i:i+1]
            if c == b"i":
                end = data.index(b"e", i)
                return end + 1
            if c in (b"d", b"l"):
                i += 1
                is_dict = c == b"d"
                while data[i:i+1] != b"e":
                    if is_dict:
                        _, i = read_str(i)
                    i = skip(i)
                return i + 1
            _, end = read_str(i)
            return end
        if data[:1] != b"d":
            return None
        i = 1
        while data[i:i+1] != b"e":
            ks, ke = read_str(i)
            key = data[ks:ke]
            val_start = ke
            val_end = skip(val_start)
            if key == b"info":
                return hashlib.sha1(data[val_start:val_end]).hexdigest()
            i = val_end
    except Exception:
        return None
    return None

# ─── Nyaa Search & Proxy Integration ──────────────────────────
async def search_nyaa_rss(query: str, romaji: str, english: str, ep: int, synonyms: list = None, is_special: bool = False) -> tuple:
    encoded_query = urllib.parse.quote(query)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    tag = query[:40].replace("\n", " ")
    proxies = get_ordered_proxies()
    if not proxies:
        return [], f"'{tag}' no GAS proxies configured"
    last_err = ""
    transport = httpx.AsyncHTTPTransport(retries=2)
    for proxy_base in proxies:
        url = f"{proxy_base}?q={encoded_query}"
        try:
            async with httpx.AsyncClient(transport=transport, timeout=20.0, headers=headers, follow_redirects=True) as client:
                r = await client.get(url)
                if r.status_code != 200:
                    last_err = f"'{tag}' proxy HTTP {r.status_code}"
                    continue
                raw_items = []
                text = r.text.strip()
                if text.startswith("{"):
                    try:
                        data = r.json()
                    except Exception:
                        last_err = f"'{tag}' invalid JSON body"
                        continue
                    payload = data.get("data")
                    if not isinstance(payload, list):
                        last_err = f"'{tag}' proxy error payload ({data.get('error') or data.get('status')})"
                        continue
                    for item in payload:
                        raw_items.append({
                            "title": item.get("title", ""),
                            "torrent": item.get("torrent", ""),
                            "seeders": int(item.get("seeders") or 0),
                            "pub_date": int(item.get("pub_date") or item.get("timestamp") or 0)
                        })
                elif "<rss" in text or "<item" in text:
                    try:
                        root = ET.fromstring(r.content)
                    except ET.ParseError:
                        last_err = f"'{tag}' unparsable XML body"
                        continue
                    items = root.findall(".//item")
                    for item in items:
                        title_el = item.find("title")
                        link_el = item.find("link")
                        pub_el = item.find("pubDate")
                        title = title_el.text if title_el is not None else ""
                        torrent_url = link_el.text if link_el is not None else ""
                        pub_date_ts = 0
                        if pub_el is not None and pub_el.text:
                            try:
                                pub_date_ts = int(email.utils.parsedate_to_datetime(pub_el.text).timestamp())
                            except Exception:
                                pub_date_ts = 0
                        seeders = 0
                        for child in item:
                            if child.tag.endswith("seeders"):
                                seeders = int(child.text or 0) if child.text and child.text.isdigit() else 0
                                break
                        raw_items.append({
                            "title": title,
                            "torrent": torrent_url,
                            "seeders": seeders,
                            "pub_date": pub_date_ts
                        })
                else:
                    body_head = text.strip()[:50].replace("\n", " ")
                    last_err = f"'{tag}' unexpected body: {body_head!r}"
                    continue
                if not raw_items:
                    return [], f"'{tag}' raw=0"
                results = []
                for item in raw_items:
                    t = item["title"]
                    torrent_url = item["torrent"]
                    seeders = item["seeders"]
                    pub_date = item.get("pub_date", 0)
                    if not t or not torrent_url:
                        continue
                    if is_matching_torrent(t, romaji, english, ep, synonyms=synonyms, is_special=is_special):
                        results.append({
                            "title": t,
                            "magnet": torrent_url,
                            "seeders": seeders,
                            "pub_date": pub_date
                        })
                if results:
                    return results, ""
                return [], f"'{tag}' raw={len(raw_items)} matched=0"
        except Exception as e:
            last_err = f"'{tag}' {type(e).__name__}"
            continue
    return [], last_err or f"'{tag}' all proxies failed"

# ─── aria2c Downloader ─────────────────────────────────────────
def is_valid_torrent_data(data: bytes) -> bool:
    if not data or len(data) < 50:
        return False
    data_start = data[:100].lower()
    if data_start.startswith(b"<!doctype") or b"<html" in data_start or b"<head" in data_start:
        return False
    return data.startswith(b"d") and (b"announce" in data or b"info" in data)

def download_torrent(torrent_source: str, torrent_title: str) -> tuple:
    download_dir = tempfile.mkdtemp(prefix="anime_")
    torrent_file_path = os.path.join(download_dir, "download.torrent")
    raw_payload = None
    torrent_input = torrent_source

    if torrent_source.startswith("http"):
        sync_transport = httpx.HTTPTransport(retries=2)
        for proxy_base in get_ordered_proxies():
            gas_url = f"{proxy_base}?mode=torrent&url={urllib.parse.quote(torrent_source)}"
            try:
                with httpx.Client(transport=sync_transport, timeout=30.0) as client:
                    r = client.get(gas_url)
                    if r.status_code == 200:
                        data = r.json()
                        if data.get("status") == 200 and data.get("data"):
                            raw_bytes = base64.b64decode(data["data"])
                            if is_valid_torrent_data(raw_bytes):
                                with open(torrent_file_path, "wb") as f:
                                    f.write(raw_bytes)
                                raw_payload = raw_bytes
                                torrent_input = torrent_file_path
                                break
            except Exception:
                continue
    else:
        torrent_input = torrent_source

    trackers_arg = ",".join(NYAA_TRACKERS)
    cmd = [
        "aria2c", torrent_input,
        f"--dir={download_dir}",
        "--seed-time=0",
        "--bt-stop-timeout=120",
        "--file-allocation=none",
        "--enable-dht=true",
        "--enable-peer-exchange=true",
        "--bt-enable-lpd=true",
        "--bt-max-peers=100",
        f"--bt-tracker={trackers_arg}",
        "--max-connection-per-server=16",
        "--summary-interval=10",
        "--allow-overwrite=true",
    ]

    log_message(f"Starting download: {torrent_title}")
    proc = subprocess.run(cmd, timeout=TORRENT_DOWNLOAD_TIMEOUT, capture_output=True, text=True)
    if proc.returncode != 0:
        shutil.rmtree(download_dir, ignore_errors=True)
        raise RuntimeError(f"aria2c failed with code {proc.returncode}")

    video_files = []
    for root, _, files in os.walk(download_dir):
        for f in files:
            if f.endswith((".mkv", ".mp4", ".avi", ".webm")) and not f.endswith((".aria2", ".torrent")):
                fp = os.path.join(root, f)
                video_files.append((fp, f, os.path.getsize(fp)))

    if not video_files:
        shutil.rmtree(download_dir, ignore_errors=True)
        raise RuntimeError("No video file found in downloaded torrent!")

    video_files.sort(key=lambda x: x[2], reverse=True)
    best_file = video_files[0]
    info_hash = extract_info_hash(raw_payload) if raw_payload else None
    return download_dir, best_file[0], best_file[1], best_file[2], info_hash

def fetch_torrent_file(torrent_source: str) -> tuple:
    """Download just the .torrent metadata file. Returns (download_dir, torrent_file_path, raw_payload).
    Tries direct download first (works on GitHub Actions), then falls back to GAS proxies."""
    download_dir = tempfile.mkdtemp(prefix="anime_batch_")
    torrent_file_path = os.path.join(download_dir, "download.torrent")
    raw_payload = None

    if torrent_source.startswith("http"):
        # Try 1: Direct download (GitHub Actions can access nyaa.si directly)
        try:
            log_message(f"Trying direct download: {torrent_source}")
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                r = client.get(torrent_source, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200 and is_valid_torrent_data(r.content):
                    with open(torrent_file_path, "wb") as f:
                        f.write(r.content)
                    raw_payload = r.content
                    log_message(f"Direct download OK ({len(r.content)} bytes)")
        except Exception as e:
            log_message(f"Direct download failed: {e}")

        # Try 2: GAS proxy fallback
        if not raw_payload:
            log_message("Falling back to GAS proxies...")
            sync_transport = httpx.HTTPTransport(retries=2)
            for proxy_base in get_ordered_proxies():
                gas_url = f"{proxy_base}?mode=torrent&url={urllib.parse.quote(torrent_source)}"
                try:
                    with httpx.Client(transport=sync_transport, timeout=30.0) as client:
                        r = client.get(gas_url)
                        if r.status_code == 200:
                            data = r.json()
                            if data.get("status") == 200 and data.get("data"):
                                raw_bytes = base64.b64decode(data["data"])
                                if is_valid_torrent_data(raw_bytes):
                                    with open(torrent_file_path, "wb") as f:
                                        f.write(raw_bytes)
                                    raw_payload = raw_bytes
                                    log_message(f"GAS proxy OK ({len(raw_bytes)} bytes)")
                                    break
                except Exception:
                    continue

        # Try 3: aria2c direct download as last resort
        if not raw_payload:
            log_message("Trying aria2c direct download...")
            try:
                cmd = ["aria2c", torrent_source, f"--dir={download_dir}", "-o", "download.torrent", "--timeout=30"]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if proc.returncode == 0 and os.path.exists(torrent_file_path):
                    with open(torrent_file_path, "rb") as f:
                        raw_payload = f.read()
                    if not is_valid_torrent_data(raw_payload):
                        raw_payload = None
                    else:
                        log_message(f"aria2c download OK ({len(raw_payload)} bytes)")
            except Exception as e:
                log_message(f"aria2c download failed: {e}")

    if not raw_payload:
        shutil.rmtree(download_dir, ignore_errors=True)
        raise RuntimeError("Failed to fetch .torrent file from any source")

    return download_dir, torrent_file_path, raw_payload

def _bdecode(data: bytes, idx: int = 0):
    """Minimal bencoding decoder for .torrent files."""
    if data[idx:idx+1] == b'i':
        end = data.index(b'e', idx)
        return int(data[idx+1:end]), end + 1
    elif data[idx:idx+1] == b'l':
        lst = []
        idx += 1
        while data[idx:idx+1] != b'e':
            val, idx = _bdecode(data, idx)
            lst.append(val)
        return lst, idx + 1
    elif data[idx:idx+1] == b'd':
        dct = {}
        idx += 1
        while data[idx:idx+1] != b'e':
            key, idx = _bdecode(data, idx)
            val, idx = _bdecode(data, idx)
            if isinstance(key, bytes):
                key = key.decode('utf-8', errors='replace')
            dct[key] = val
        return dct, idx + 1
    elif data[idx:idx+1].isdigit():
        colon = data.index(b':', idx)
        length = int(data[idx:colon])
        start = colon + 1
        return data[start:start+length], start + length
    else:
        raise ValueError(f"Invalid bencoding at position {idx}: {data[idx:idx+10]}")

def list_torrent_files(torrent_file_path: str) -> list:
    """Parse .torrent file directly to extract file list with indices.
    Returns list of dicts: [{index: int, path: str, filename: str}, ...]
    Index is 1-based to match aria2c --select-file numbering."""
    with open(torrent_file_path, "rb") as f:
        raw = f.read()
    
    meta, _ = _bdecode(raw)
    info = meta.get("info", {})
    
    files = []
    if "files" in info:
        # Multi-file torrent
        for i, file_entry in enumerate(info["files"]):
            path_parts = file_entry.get("path", [])
            # path_parts is list of bytes
            path_str = "/".join(
                p.decode('utf-8', errors='replace') if isinstance(p, bytes) else str(p)
                for p in path_parts
            )
            fname = os.path.basename(path_str)
            files.append({
                "index": i + 1,  # aria2c uses 1-based indexing
                "path": path_str,
                "filename": fname,
            })
    else:
        # Single-file torrent
        name = info.get("name", b"unknown")
        if isinstance(name, bytes):
            name = name.decode('utf-8', errors='replace')
        files.append({
            "index": 1,
            "path": name,
            "filename": name,
        })
    
    return files

def download_selected_files(torrent_file_path: str, download_dir: str, file_indices: list) -> list:
    """Download only specific file indices from a torrent. Returns list of (full_path, filename, file_size)."""
    indices_str = ",".join(str(i) for i in file_indices)
    trackers_arg = ",".join(NYAA_TRACKERS)
    cmd = [
        "aria2c", torrent_file_path,
        f"--dir={download_dir}",
        f"--select-file={indices_str}",
        "--seed-time=0",
        "--bt-stop-timeout=300",
        "--file-allocation=none",
        "--enable-dht=true",
        "--enable-peer-exchange=true",
        "--bt-enable-lpd=true",
        "--bt-max-peers=100",
        f"--bt-tracker={trackers_arg}",
        "--max-connection-per-server=16",
        "--summary-interval=15",
        "--allow-overwrite=true",
    ]

    log_message(f"Downloading files {indices_str} (timeout={TORRENT_DOWNLOAD_TIMEOUT}s)...")
    proc = subprocess.run(cmd, timeout=TORRENT_DOWNLOAD_TIMEOUT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"aria2c selective download failed (code {proc.returncode})")

    video_files = []
    for root, _, files in os.walk(download_dir):
        for f in files:
            if f.endswith((".mkv", ".mp4", ".avi", ".webm")) and not f.endswith((".aria2", ".torrent")):
                fp = os.path.join(root, f)
                sz = os.path.getsize(fp)
                if sz > 1000:  # skip empty/placeholder files
                    video_files.append((fp, f, sz))

    return video_files


def parse_episode_from_filename(filename: str) -> int:
    """Extract episode number from a video filename. Handles various naming conventions."""
    name = os.path.splitext(filename)[0]

    # Pattern 1: Standard " - 01" or " - 001" (Erai-raws, SubsPlease style)
    m = re.search(r'\s-\s(\d{2,4})\b', name)
    if m:
        return int(m.group(1))

    # Pattern 2: "E01" or "EP01" or "Episode 01"
    m = re.search(r'\b(?:e|ep|episode)\s*(\d{1,4})\b', name, re.IGNORECASE)
    if m:
        return int(m.group(1))

    # Pattern 3: "S01E05" style
    m = re.search(r's\d+e(\d+)', name, re.IGNORECASE)
    if m:
        return int(m.group(1))

    # Pattern 4: Standalone number at end before quality tags "[720p]"
    m = re.search(r'[\s_.-](\d{1,4})\s*[\[\(]', name)
    if m:
        return int(m.group(1))

    # Pattern 5: Last number in the filename (fallback)
    nums = re.findall(r'\b(\d{1,4})\b', name)
    # Filter out common non-episode numbers (year, quality)
    for n in nums:
        val = int(n)
        if val < 2000 and val not in (480, 720, 1080, 1440, 2160):
            return val

    return -1

# ─── Media Inspection ──────────────────────────────────────────
def inspect_media_tracks(video_path: str) -> tuple:
    ALLOWED_SUBS = {"Arabic", "English", "French", "Japanese"}
    ALLOWED_AUDIO = {"Japanese", "Arabic", "English", "French", "Chinese", "Korean"}
    LANG_MAP = {
        "ara": "Arabic", "ar": "Arabic", "arabic": "Arabic", "العربية": "Arabic", "عربي": "Arabic",
        "eng": "English", "en": "English", "english": "English",
        "fra": "French", "fre": "French", "fr": "French", "french": "French", "français": "French",
        "jpn": "Japanese", "ja": "Japanese", "japanese": "Japanese", "jp": "Japanese", "日本語": "Japanese",
        "chi": "Chinese", "zho": "Chinese", "zh": "Chinese", "chinese": "Chinese",
        "kor": "Korean", "ko": "Korean", "korean": "Korean",
    }

    def _resolve_lang(lang_tag: str, title_tag: str) -> str:
        tag_str = (lang_tag or "").strip().lower()
        title_str = (title_tag or "").strip().lower()
        if tag_str in LANG_MAP:
            return LANG_MAP[tag_str]
        for key, name in LANG_MAP.items():
            if re.search(rf'\b{re.escape(key)}\b', title_str):
                return name
            if name.lower() in title_str:
                return name
        return None

    found_subs = set()
    found_audio = set()
    duration_sec = 0
    try:
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams",
            "-show_entries", "stream=codec_type,duration:stream_tags=language,title:format=duration",
            video_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            fmt_dur = data.get("format", {}).get("duration")
            if fmt_dur:
                try:
                    duration_sec = int(round(float(fmt_dur)))
                except (ValueError, TypeError):
                    pass

            streams = data.get("streams", [])
            for s in streams:
                if not duration_sec and s.get("duration"):
                    try:
                        duration_sec = int(round(float(s["duration"])))
                    except (ValueError, TypeError):
                        pass

                c_type = s.get("codec_type")
                tags = s.get("tags") or {}
                lang_tag = tags.get("language")
                title_tag = tags.get("title")
                resolved = _resolve_lang(lang_tag, title_tag)
                if c_type == "subtitle":
                    if resolved and resolved in ALLOWED_SUBS:
                        found_subs.add(resolved)
                elif c_type == "audio":
                    if resolved and resolved in ALLOWED_AUDIO:
                        found_audio.add(resolved)
                    elif not resolved and not found_audio:
                        found_audio.add("Japanese")
    except Exception as e:
        log_message(f"Media probe warning: {e}")

    if not found_audio:
        found_audio.add("Japanese")
    ORDER = ["Arabic", "English", "French", "Japanese", "Chinese", "Korean"]
    sorted_subs = sorted(found_subs, key=lambda x: ORDER.index(x) if x in ORDER else 99)
    sorted_audio = sorted(found_audio, key=lambda x: ORDER.index(x) if x in ORDER else 99)
    return ", ".join(sorted_subs), ", ".join(sorted_audio), duration_sec

# ─── Pixeldrain Upload & Delete ────────────────────────────────
def upload_pixeldrain(file_path: str, filename: str) -> dict:
    url = f"https://pixeldrain.com/api/file/{urllib.parse.quote(filename)}"
    auth = ("", PIXELDRAIN_API_KEY) if PIXELDRAIN_API_KEY else None
    file_size_mb = round(os.path.getsize(file_path) / 1048576, 1)
    log_message(f"Uploading {filename} ({file_size_mb} MB)...")
    with open(file_path, "rb") as f:
        with httpx.Client(timeout=600.0) as client:
            r = client.put(url, content=f.read(), auth=auth)
            if r.status_code in [200, 201]:
                file_id = r.json().get("id")
                log_message(f"Upload complete: {file_id}")
                return {"id": file_id, "url": f"https://pixeldrain.com/api/file/{file_id}"}
            raise RuntimeError(f"Pixeldrain upload failed (HTTP {r.status_code}): {r.text}")

def delete_from_pixeldrain(file_id: str) -> bool:
    if not file_id or not PIXELDRAIN_API_KEY:
        return False
    try:
        url = f"https://pixeldrain.com/api/file/{file_id}"
        with httpx.Client(timeout=15.0) as client:
            r = client.delete(url, auth=("", PIXELDRAIN_API_KEY))
            return r.status_code == 200
    except Exception:
        return False

# ═══════════════════════════════════════════════════════════════
#  Content Analysis Pipeline
# ═══════════════════════════════════════════════════════════════

ANILIST_MEDIA_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    title { romaji english native }
    synonyms
    format
    status
    episodes
    coverImage { large }
    bannerImage
    description
    genres
    airingSchedule(notYetAired: false, perPage: 50) {
      nodes { episode airingAt }
    }
  }
}
"""

async def fetch_anime_by_id(anilist_id: int) -> dict:
    """Fetch anime info from AniList by ID with fallback to local database if AniList is down."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(ANILIST_URL, json={
                "query": ANILIST_MEDIA_QUERY,
                "variables": {"id": anilist_id}
            })
            if r.status_code == 200:
                data = r.json()
                media = data.get("data", {}).get("Media")
                if media:
                    return media
    except Exception:
        pass

    # Fallback to local DB if AniList API is disabled or unreachable
    db_rows = await execute_sql("""
        SELECT id, title_romaji, title_english, title_native, synonyms,
               cover_url, banner_url, synopsis, genres, format, status
        FROM anime WHERE anilist_id = ?
    """, [anilist_id])
    if db_rows:
        row = db_rows[0]
        syns = json.loads(row.get("synonyms") or "[]") if isinstance(row.get("synonyms"), str) else []
        genres = json.loads(row.get("genres") or "[]") if isinstance(row.get("genres"), str) else []
        log_message(f"ℹ️ AniList API is offline; loaded '{row.get('title_romaji')}' from local database.")
        return {
            "id": anilist_id,
            "title": {
                "romaji": row.get("title_romaji") or "",
                "english": row.get("title_english") or "",
                "native": row.get("title_native") or ""
            },
            "format": row.get("format") or "TV",
            "status": row.get("status") or "RELEASING",
            "synonyms": syns,
            "coverImage": {"large": row.get("cover_url") or ""},
            "bannerImage": row.get("banner_url") or "",
            "description": row.get("synopsis") or "",
            "genres": genres,
            "airingSchedule": {"nodes": []}
        }

    raise RuntimeError(f"AniList API is temporarily disabled worldwide and anime ID {anilist_id} is not in local database.")

def parse_episodes_input(text: str) -> list:
    """Parse episode input: '1-12', '5,8,10', '1-5,8,10-12'"""
    episodes = set()
    text = text.strip()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            try:
                start = int(bounds[0].strip())
                end = int(bounds[1].strip())
                for ep in range(start, end + 1):
                    episodes.add(ep)
            except ValueError:
                continue
        else:
            try:
                episodes.add(int(part))
            except ValueError:
                continue
    return sorted(episodes)

async def process_single_episode(anime_info: dict, ep_num: int, anime_db_id: int, force: bool = False) -> str:
    """Process a single episode: search -> download -> inspect -> upload -> update DB."""
    romaji = anime_info["title"]["romaji"] or ""
    english = anime_info["title"]["english"] or ""
    synonyms = anime_info.get("synonyms") or []
    format_type = anime_info.get("format") or "TV"
    is_special = format_type in ["SPECIAL", "MOVIE", "OVA", "ONA"]

    # Get airing date for this episode (if available)
    aired_at = 0
    airing_nodes = anime_info.get("airingSchedule", {}).get("nodes", [])
    for node in airing_nodes:
        if node.get("episode") == ep_num:
            aired_at = node.get("airingAt", 0)
            break

    # Check if episode already exists
    existing = await execute_sql(
        "SELECT id, pixeldrain_id, pixeldrain_1080_id, backup_720_id, backup_480_id FROM episodes WHERE anime_id = ? AND episode_number = ?",
        [anime_db_id, ep_num]
    )

    if existing and not force:
        return f"⏭️ Episode {ep_num} already exists (use Force to re-download)"

    # If force, delete old Pixeldrain files first
    if existing and force:
        old = existing[0]
        for key in ["pixeldrain_id", "pixeldrain_1080_id", "backup_720_id", "backup_480_id"]:
            old_id = old.get(key)
            if old_id:
                log_message(f"🗑️ Deleting old Pixeldrain file: {old_id}")
                delete_from_pixeldrain(old_id)
        # Delete the old episode row so we re-insert
        await execute_sql("DELETE FROM episodes WHERE id = ?", [old["id"]])
        log_message(f"🗑️ Deleted old episode {ep_num} from DB")

    # Insert episode as pending
    await execute_sql("""
        INSERT INTO episodes (anime_id, episode_number, status, aired_at)
        VALUES (?, ?, 'pending', ?)
        ON CONFLICT(anime_id, episode_number) DO NOTHING
    """, [anime_db_id, ep_num, aired_at or int(time.time())])

    # Get the ep_id
    ep_row = await execute_sql(
        "SELECT id FROM episodes WHERE anime_id = ? AND episode_number = ?",
        [anime_db_id, ep_num]
    )
    if not ep_row:
        return f"❌ Episode {ep_num}: Failed to create DB entry"
    ep_id = ep_row[0]["id"]

    # Get erai_title if stored
    erai_rows = await execute_sql("SELECT erai_title FROM anime WHERE id = ?", [anime_db_id])
    erai_title = erai_rows[0].get("erai_title") if erai_rows else None

    # Search
    log_message(f"🔍 Searching torrents for: {romaji} (Ep {ep_num})")
    queries = get_search_queries(romaji, english, ep_num, synonyms=synonyms, is_special=is_special, erai_title=erai_title)

    all_results = []
    search_notes = []
    for i in range(0, min(len(queries), 6), 2):
        batch = queries[i:i+2]
        tasks = [
            search_nyaa_rss(q, romaji, english, ep_num, synonyms=synonyms, is_special=is_special)
            for q in batch
        ]
        batch_res = await asyncio.gather(*tasks, return_exceptions=True)
        for res in batch_res:
            if isinstance(res, Exception):
                search_notes.append(f"task {type(res).__name__}")
                continue
            res_list, res_note = res
            if res_note:
                search_notes.append(res_note)
            if res_list:
                all_results.extend(res_list)
        if any(r["seeders"] >= 50 and bool(re.search(r'\[?(erai[-_ ]?raws|toonshub)\]?', r["title"].lower())) for r in all_results):
            break
        if len(all_results) >= 10:
            break

    # Deduplicate
    seen_magnets = set()
    deduped = []
    for r in all_results:
        if r["magnet"] not in seen_magnets:
            seen_magnets.add(r["magnet"])
            deduped.append(r)

    now_ts = int(time.time())

    def get_min_seeders_for_torrent_local(t_title: str) -> int:
        is_erai = bool(re.search(r'\[?erai[-_ ]?raws\]?', t_title.lower()))
        if is_erai and (aired_at > 0) and (now_ts - aired_at < 7200):
            return 1
        elif is_erai:
            return 2
        return max(10, MIN_TORRENT_SEEDERS)

    def is_valid_release_date(t_pub_date: int, ep_aired_at: int) -> bool:
        if not t_pub_date or not ep_aired_at or ep_aired_at <= 0:
            return True
        if t_pub_date < (ep_aired_at - 7 * 86400):
            return False
        return True

    good = [
        r for r in deduped
        if r["seeders"] >= get_min_seeders_for_torrent_local(r["title"])
        and not is_blacklisted_platform(r["title"])
        and is_valid_release_date(r.get("pub_date", 0), aired_at)
    ]

    if not good:
        hint = ""
        if search_notes:
            unique_notes = list(dict.fromkeys(search_notes))
            hint = f" [{'; '.join(unique_notes[:3])}]"
        await execute_sql("UPDATE episodes SET last_checked = ? WHERE id = ?", [int(time.time()), ep_id])
        return f"❌ Episode {ep_num}: No active torrents found{hint}"

    # Arabic subtitle priority check (same as sync_job.py)
    def _has_arabic_variants(text: str) -> bool:
        if not text: return False
        t = text.lower()
        return bool(re.search(r'\barabic\b|\bara\b|(?<!\w)ar(?!\w)|العربية|عربي', t))

    multi_subs_candidates = [r for r in good if bool(re.search(r'\b(multi|m)\s*[-_:]?\s*subs?\b|multisubs?', r["title"].lower()))]
    platforms_in_multi = set()
    for r in multi_subs_candidates:
        if re.search(r'\b(cr|crunchyroll)\b', r["title"].lower()):
            platforms_in_multi.add('cr')
        elif re.search(r'\b(nf|netflix)\b', r["title"].lower()):
            platforms_in_multi.add('nf')
        elif re.search(r'\b(amzn|amazon)\b', r["title"].lower()):
            platforms_in_multi.add('amzn')
        elif re.search(r'\b(bilibili|bili)\b', r["title"].lower()):
            platforms_in_multi.add('bili')
        else:
            platforms_in_multi.add('other')

    # Check detail pages if:
    # 1. Multiple platforms in multi-subs (existing logic)
    # 2. Or presence of a REPACK candidate alongside regular releases (new logic)
    has_repack = any(bool(re.search(r'\b(repack|re-pack|v2)\b', r["title"].lower())) for r in good)
    has_multiple_platforms = (len(multi_subs_candidates) >= 2 and len(platforms_in_multi) >= 2)
    has_repack_check = (has_repack and len(good) >= 2)

    arabic_cache = {}
    if has_multiple_platforms or has_repack_check:
        async def _check_arabic_for_item(item):
            magnet = item.get("magnet", "")
            view_url = None
            if "nyaa.si/download/" in magnet:
                view_url = magnet.replace("/download/", "/view/").split(".torrent")[0]
            elif "nyaa.si/view/" in magnet:
                view_url = magnet
            else:
                view_url = magnet

            # 1. Direct fetch first
            try:
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                    r = await client.get(view_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                    if r.status_code == 200 and "Subtitles Info" in r.text:
                        return _has_arabic_variants(r.text)
            except Exception:
                pass

            # 2. GAS proxy fallback
            for proxy_base in get_ordered_proxies():
                try:
                    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                        gas_url = f"{proxy_base}?mode=torrent&url={urllib.parse.quote(view_url)}"
                        r2 = await client.get(gas_url)
                        if r2.status_code == 200:
                            try:
                                data = r2.json()
                                if data.get("data"):
                                    html = base64.b64decode(data["data"]).decode('utf-8', errors='ignore')
                                    return _has_arabic_variants(html)
                            except Exception:
                                pass
                            return _has_arabic_variants(r2.text)
                except Exception:
                    continue
            return _has_arabic_variants(item.get("title", ""))

        check_tasks = [_check_arabic_for_item(r) for r in good]
        try:
            results = await asyncio.gather(*check_tasks, return_exceptions=True)
            for idx, res in enumerate(results):
                if isinstance(res, Exception):
                    arabic_cache[good[idx]["magnet"]] = False
                else:
                    arabic_cache[good[idx]["magnet"]] = bool(res)
                    if res:
                        log_message(f"Arabic subtitle found in: {good[idx]['title'][:60]}")
        except Exception as e:
            log_message(f"Arabic check failed: {e}")

    # Ranking (identical to sync_job.py)
    def _arabic_score(item):
        return 1 if arabic_cache.get(item["magnet"], False) else 0

    def _repack_score(item):
        return 1 if re.search(r'\b(repack|re-pack|v2)\b', item["title"].lower()) else 0

    any_arabic_found = any(arabic_cache.values()) if arabic_cache else False

    if any_arabic_found:
        good.sort(key=lambda x: (
            _arabic_score(x),
            _repack_score(x),
            get_audio_score(x["title"]),
            1 if ("[erai-raws]" in x["title"].lower() or "[toonshub]" in x["title"].lower()) else 0,
            get_platform_score(x["title"]),
            get_quality_weight(x["title"]),
            get_source_weight(x["title"]),
            x["seeders"]
        ), reverse=True)
    else:
        good.sort(key=lambda x: (
            get_audio_score(x["title"]),
            _repack_score(x),
            1 if ("[erai-raws]" in x["title"].lower() or "[toonshub]" in x["title"].lower()) else 0,
            get_platform_score(x["title"]),
            get_quality_weight(x["title"]),
            get_source_weight(x["title"]),
            x["seeders"]
        ), reverse=True)

    winner = good[0]
    torrent_title = winner["title"]
    audio_score = get_audio_score(torrent_title)
    is_multi_audio = 1 if audio_score >= 3 else 0

    log_message(f"📦 Selected: {torrent_title} (Seeders: {winner['seeders']}, Audio: {audio_score})")

    # Download
    dl_dir = None
    try:
        dl_dir, v_path, v_name, v_size, info_hash = await asyncio.to_thread(download_torrent, winner["magnet"], torrent_title)
        size_mb = round(v_size / 1048576, 2)
        stored_source = (
            f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(torrent_title)}"
            if info_hash else winner["magnet"]
        )

        subs_found, audio_found, duration_found = inspect_media_tracks(v_path)
        log_message(f"🎵 Tracks: Subs=[{subs_found}] Audio=[{audio_found}] Duration={duration_found}s")

        upload = await asyncio.to_thread(upload_pixeldrain, v_path, v_name)
        pd_id = upload["id"]
        pd_url = upload["url"]

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await execute_sql("""
            UPDATE episodes
            SET status = 'ready',
                stream_url = ?,
                pixeldrain_id = ?,
                pixeldrain_1080_url = ?,
                pixeldrain_1080_id = ?,
                file_size_mb = ?,
                magnet_link = ?,
                is_multi_audio = ?,
                audio_score = ?,
                subtitles = ?,
                audio_tracks = ?,
                subtitles_1080 = ?,
                audio_tracks_1080 = ?,
                duration = CASE WHEN ? > 0 THEN ? ELSE duration END,
                uploaded_at = ?,
                last_checked = ?,
                mirror_720_missing = 1,
                mirror_480_missing = 1,
                mirror_updated_at = ?,
                pending_review_until = 0
            WHERE id = ?
        """, [pd_url, pd_id, pd_url, pd_id, size_mb, stored_source, is_multi_audio, audio_score,
              subs_found, audio_found, subs_found, audio_found, duration_found, duration_found, now_str, int(time.time()),
              int(time.time()), ep_id])

        # Store parsed erai_title
        parsed_erai = parse_erai_anime_title(v_name)
        if parsed_erai and not erai_title:
            await execute_sql("UPDATE anime SET erai_title = ? WHERE id = ?", [parsed_erai, anime_db_id])

        return f"✅ Episode {ep_num}: {v_name} ({size_mb} MB) → Pixeldrain [{pd_id}] | Subs=[{subs_found}] Audio=[{audio_found}]"

    except Exception as ex:
        await execute_sql("UPDATE episodes SET last_checked = ? WHERE id = ?", [int(time.time()), ep_id])
        return f"❌ Episode {ep_num}: {ex}"
    finally:
        if dl_dir:
            shutil.rmtree(dl_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════
#  Direct URL Mode — skip search, download specific torrent
# ═══════════════════════════════════════════════════════════════

async def process_direct_url(anime_info: dict, ep_num: int, anime_db_id: int, nyaa_url: str, force: bool = False) -> str:
    """Download a specific torrent from a Nyaa URL, skipping search/matching entirely."""
    romaji = anime_info["title"]["romaji"] or ""

    # Get erai_title if stored
    erai_rows = await execute_sql("SELECT erai_title FROM anime WHERE id = ?", [anime_db_id])
    erai_title = erai_rows[0].get("erai_title") if erai_rows else None

    # Get airing date for this episode (if available)
    aired_at = 0
    airing_nodes = anime_info.get("airingSchedule", {}).get("nodes", [])
    for node in airing_nodes:
        if node.get("episode") == ep_num:
            aired_at = node.get("airingAt", 0)
            break

    # Check if episode already exists
    existing = await execute_sql(
        "SELECT id, pixeldrain_id, pixeldrain_1080_id, backup_720_id, backup_480_id FROM episodes WHERE anime_id = ? AND episode_number = ?",
        [anime_db_id, ep_num]
    )

    if existing and not force:
        return f"⏭️ Episode {ep_num} already exists (use Force to re-download)"

    # If force, delete old Pixeldrain files first
    if existing and force:
        old = existing[0]
        for key in ["pixeldrain_id", "pixeldrain_1080_id", "backup_720_id", "backup_480_id"]:
            old_id = old.get(key)
            if old_id:
                log_message(f"🗑️ Deleting old Pixeldrain file: {old_id}")
                delete_from_pixeldrain(old_id)
        await execute_sql("DELETE FROM episodes WHERE id = ?", [old["id"]])
        log_message(f"🗑️ Deleted old episode {ep_num} from DB")

    # Insert episode as pending
    await execute_sql("""
        INSERT INTO episodes (anime_id, episode_number, status, aired_at)
        VALUES (?, ?, 'pending', ?)
        ON CONFLICT(anime_id, episode_number) DO NOTHING
    """, [anime_db_id, ep_num, aired_at or int(time.time())])

    # Get the ep_id
    ep_row = await execute_sql(
        "SELECT id FROM episodes WHERE anime_id = ? AND episode_number = ?",
        [anime_db_id, ep_num]
    )
    if not ep_row:
        return f"❌ Episode {ep_num}: Failed to create DB entry"
    ep_id = ep_row[0]["id"]

    # Convert Nyaa view URL to download URL if needed
    torrent_url = nyaa_url.strip()
    if "/view/" in torrent_url and "/download/" not in torrent_url:
        # https://nyaa.si/view/1234567 -> https://nyaa.si/download/1234567.torrent
        torrent_url = torrent_url.replace("/view/", "/download/") + ".torrent"

    log_message(f"🎯 Direct URL: {torrent_url}")

    # Download
    dl_dir = None
    try:
        dl_dir, v_path, v_name, v_size, info_hash = await asyncio.to_thread(download_torrent, torrent_url, f"Direct-Ep{ep_num}")
        size_mb = round(v_size / 1048576, 2)
        stored_source = (
            f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(v_name)}"
            if info_hash else torrent_url
        )

        subs_found, audio_found, duration_found = inspect_media_tracks(v_path)
        log_message(f"🎵 Tracks: Subs=[{subs_found}] Audio=[{audio_found}] Duration={duration_found}s")

        audio_score = get_audio_score(v_name)
        is_multi_audio = 1 if audio_score >= 3 else 0

        upload = await asyncio.to_thread(upload_pixeldrain, v_path, v_name)
        pd_id = upload["id"]
        pd_url = upload["url"]

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await execute_sql("""
            UPDATE episodes
            SET status = 'ready',
                stream_url = ?,
                pixeldrain_id = ?,
                pixeldrain_1080_url = ?,
                pixeldrain_1080_id = ?,
                file_size_mb = ?,
                magnet_link = ?,
                is_multi_audio = ?,
                audio_score = ?,
                subtitles = ?,
                audio_tracks = ?,
                subtitles_1080 = ?,
                audio_tracks_1080 = ?,
                duration = CASE WHEN ? > 0 THEN ? ELSE duration END,
                uploaded_at = ?,
                last_checked = ?,
                mirror_720_missing = 1,
                mirror_480_missing = 1,
                mirror_updated_at = ?,
                pending_review_until = 0
            WHERE id = ?
        """, [pd_url, pd_id, pd_url, pd_id, size_mb, stored_source, is_multi_audio, audio_score,
              subs_found, audio_found, subs_found, audio_found, duration_found, duration_found, now_str, int(time.time()),
              int(time.time()), ep_id])

        # Store parsed erai_title
        parsed_erai = parse_erai_anime_title(v_name)
        if parsed_erai and not erai_title:
            await execute_sql("UPDATE anime SET erai_title = ? WHERE id = ?", [parsed_erai, anime_db_id])

        return f"✅ Episode {ep_num}: {v_name} ({size_mb} MB) → Pixeldrain [{pd_id}] | Subs=[{subs_found}] Audio=[{audio_found}]"

    except Exception as ex:
        await execute_sql("UPDATE episodes SET last_checked = ? WHERE id = ?", [int(time.time()), ep_id])
        return f"❌ Episode {ep_num}: {ex}"
    finally:
        if dl_dir:
            shutil.rmtree(dl_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════
#  Batch Download Mode — one torrent, many episodes (chunked)
# ═══════════════════════════════════════════════════════════════

BATCH_CHUNK_SIZE = int(os.environ.get("BATCH_CHUNK_SIZE", "5"))

async def process_batch_download(anime_info: dict, episodes: list, anime_db_id: int, nyaa_url: str, force: bool = False) -> list:
    """Download a batch torrent in chunks — download 5 files, upload them, delete, repeat.
    This keeps disk usage under ~2 GB even for 30+ GB batch torrents."""
    results = []

    # Get erai_title if stored
    erai_rows = await execute_sql("SELECT erai_title FROM anime WHERE id = ?", [anime_db_id])
    erai_title = erai_rows[0].get("erai_title") if erai_rows else None

    # Convert Nyaa view URL to download URL if needed
    torrent_url = nyaa_url.strip()
    if "/view/" in torrent_url and "/download/" not in torrent_url:
        torrent_url = torrent_url.replace("/view/", "/download/") + ".torrent"

    log_message(f"📦 Batch Download Mode (chunk size: {BATCH_CHUNK_SIZE})")
    log_message(f"🔗 URL: {torrent_url}")
    log_message(f"📋 Episodes requested: {len(episodes)} episodes ({episodes[0]}-{episodes[-1]})")
    log_message("")

    # Step 1: Fetch .torrent metadata only (tiny file)
    dl_dir = None
    try:
        dl_dir, torrent_file_path, raw_payload = await asyncio.to_thread(fetch_torrent_file, torrent_url)
        info_hash = extract_info_hash(raw_payload) if raw_payload else None
        log_message(f"✅ Fetched .torrent metadata")
    except Exception as ex:
        return [f"❌ Failed to fetch .torrent file: {ex}"]

    try:
        # Step 2: List all files in the torrent
        torrent_files = await asyncio.to_thread(list_torrent_files, torrent_file_path)
        log_message(f"📁 Torrent contains {len(torrent_files)} files")

        # Step 3: Map torrent file indices to episode numbers
        ep_to_file_idx = {}  # ep_num -> {index, filename, size}
        for tf in torrent_files:
            fname = tf["filename"]
            if not fname.endswith((".mkv", ".mp4", ".avi", ".webm")):
                continue
            ep_num = parse_episode_from_filename(fname)
            if ep_num >= 0:
                ep_to_file_idx[ep_num] = tf

        log_message(f"🗺️ Mapped {len(ep_to_file_idx)} video files to episode numbers")
        if ep_to_file_idx:
            mapped_eps = sorted(ep_to_file_idx.keys())
            log_message(f"   Range: {mapped_eps[0]}-{mapped_eps[-1]}")
            # Show first 5 mappings for verification
            for ep in mapped_eps[:5]:
                tf = ep_to_file_idx[ep]
                log_message(f"   Ep {ep} → file index {tf['index']}: {tf['filename'][:60]}")
        log_message("")

        # Filter episodes to only those available
        available_eps = [ep for ep in episodes if ep in ep_to_file_idx]
        missing_eps = [ep for ep in episodes if ep not in ep_to_file_idx]
        for ep in missing_eps:
            msg = f"❌ Episode {ep}: No matching file found in batch torrent"
            results.append(msg)
            log_message(msg)

        if not available_eps:
            log_message("❌ No requested episodes found in torrent!")
            return results

        # Pre-filter: skip already existing episodes (unless force)
        eps_to_process = []
        for ep_num in available_eps:
            existing = await execute_sql(
                "SELECT id FROM episodes WHERE anime_id = ? AND episode_number = ?",
                [anime_db_id, ep_num]
            )
            if existing and not force:
                msg = f"⏭️ Episode {ep_num} already exists (use Force to re-download)"
                results.append(msg)
                log_message(msg)
            else:
                eps_to_process.append(ep_num)

        if not eps_to_process:
            log_message("All episodes already exist!")
            return results

        log_message(f"\n🚀 Processing {len(eps_to_process)} episodes in chunks of {BATCH_CHUNK_SIZE}")
        log_message("")

        # Step 4: Process in chunks
        for chunk_start in range(0, len(eps_to_process), BATCH_CHUNK_SIZE):
            chunk_eps = eps_to_process[chunk_start:chunk_start + BATCH_CHUNK_SIZE]
            chunk_num = (chunk_start // BATCH_CHUNK_SIZE) + 1
            total_chunks = (len(eps_to_process) + BATCH_CHUNK_SIZE - 1) // BATCH_CHUNK_SIZE

            log_message(f"{'═' * 50}")
            log_message(f"📦 Chunk {chunk_num}/{total_chunks}: Episodes {chunk_eps}")
            log_message(f"{'═' * 50}")

            # Get file indices for this chunk
            chunk_indices = [ep_to_file_idx[ep]["index"] for ep in chunk_eps]

            # Download only these files
            try:
                downloaded_files = await asyncio.to_thread(
                    download_selected_files, torrent_file_path, dl_dir, chunk_indices
                )
                log_message(f"✅ Downloaded {len(downloaded_files)} files for chunk {chunk_num}")
            except Exception as ex:
                for ep in chunk_eps:
                    msg = f"❌ Episode {ep}: Download failed in chunk {chunk_num}: {ex}"
                    results.append(msg)
                    log_message(msg)
                continue

            # Map downloaded files to episode numbers
            chunk_file_map = {}
            for fp, fname, fsize in downloaded_files:
                ep_from_file = parse_episode_from_filename(fname)
                if ep_from_file >= 0:
                    chunk_file_map[ep_from_file] = (fp, fname, fsize)

            # Process each episode in this chunk
            for ep_num in chunk_eps:
                log_message(f"{'─' * 40}")
                log_message(f"⏳ Episode {ep_num}...")

                if ep_num not in chunk_file_map:
                    msg = f"❌ Episode {ep_num}: File not found after download"
                    results.append(msg)
                    log_message(msg)
                    continue

                fp, fname, fsize = chunk_file_map[ep_num]

                # Handle force: delete old data
                existing = await execute_sql(
                    "SELECT id, pixeldrain_id, pixeldrain_1080_id, backup_720_id, backup_480_id FROM episodes WHERE anime_id = ? AND episode_number = ?",
                    [anime_db_id, ep_num]
                )
                if existing and force:
                    old = existing[0]
                    for key in ["pixeldrain_id", "pixeldrain_1080_id", "backup_720_id", "backup_480_id"]:
                        old_id = old.get(key)
                        if old_id:
                            delete_from_pixeldrain(old_id)
                    await execute_sql("DELETE FROM episodes WHERE id = ?", [old["id"]])

                # Get airing date
                aired_at = 0
                airing_nodes = anime_info.get("airingSchedule", {}).get("nodes", [])
                for node in airing_nodes:
                    if node.get("episode") == ep_num:
                        aired_at = node.get("airingAt", 0)
                        break

                # Insert episode
                await execute_sql("""
                    INSERT INTO episodes (anime_id, episode_number, status, aired_at)
                    VALUES (?, ?, 'pending', ?)
                    ON CONFLICT(anime_id, episode_number) DO NOTHING
                """, [anime_db_id, ep_num, aired_at or int(time.time())])

                ep_row = await execute_sql(
                    "SELECT id FROM episodes WHERE anime_id = ? AND episode_number = ?",
                    [anime_db_id, ep_num]
                )
                if not ep_row:
                    msg = f"❌ Episode {ep_num}: Failed to create DB entry"
                    results.append(msg)
                    log_message(msg)
                    continue
                ep_id = ep_row[0]["id"]

                try:
                    size_mb = round(fsize / 1048576, 2)
                    stored_source = (
                        f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(fname)}"
                        if info_hash else torrent_url
                    )

                    subs_found, audio_found, duration_found = inspect_media_tracks(fp)
                    log_message(f"🎵 Subs=[{subs_found}] Audio=[{audio_found}] Duration={duration_found}s")

                    audio_score = get_audio_score(fname)
                    is_multi_audio = 1 if audio_score >= 3 else 0

                    upload = await asyncio.to_thread(upload_pixeldrain, fp, fname)
                    pd_id = upload["id"]
                    pd_url = upload["url"]

                    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    await execute_sql("""
                        UPDATE episodes
                        SET status = 'ready',
                            stream_url = ?,
                            pixeldrain_id = ?,
                            pixeldrain_1080_url = ?,
                            pixeldrain_1080_id = ?,
                            file_size_mb = ?,
                            magnet_link = ?,
                            is_multi_audio = ?,
                            audio_score = ?,
                            subtitles = ?,
                            audio_tracks = ?,
                            subtitles_1080 = ?,
                            audio_tracks_1080 = ?,
                            duration = CASE WHEN ? > 0 THEN ? ELSE duration END,
                            uploaded_at = ?,
                            last_checked = ?,
                            mirror_720_missing = 1,
                            mirror_480_missing = 1,
                            mirror_updated_at = ?,
                            pending_review_until = 0
                        WHERE id = ?
                    """, [pd_url, pd_id, pd_url, pd_id, size_mb, stored_source, is_multi_audio, audio_score,
                          subs_found, audio_found, subs_found, audio_found, duration_found, duration_found, now_str, int(time.time()),
                          int(time.time()), ep_id])

                    # Store parsed erai_title
                    parsed_erai = parse_erai_anime_title(fname)
                    if parsed_erai and not erai_title:
                        await execute_sql("UPDATE anime SET erai_title = ? WHERE id = ?", [parsed_erai, anime_db_id])
                        erai_title = parsed_erai

                    msg = f"✅ Episode {ep_num}: {fname} ({size_mb} MB) → [{pd_id}]"
                    results.append(msg)
                    log_message(msg)

                except Exception as ex:
                    await execute_sql("UPDATE episodes SET last_checked = ? WHERE id = ?", [int(time.time()), ep_id])
                    msg = f"❌ Episode {ep_num}: {ex}"
                    results.append(msg)
                    log_message(msg)

            # Delete downloaded video files to free disk space for next chunk
            for fp, fname, fsize in downloaded_files:
                try:
                    os.remove(fp)
                except OSError:
                    pass
            log_message(f"🗑️ Cleaned chunk {chunk_num} files to free disk space")
            log_message("")

    finally:
        if dl_dir:
            shutil.rmtree(dl_dir, ignore_errors=True)

    return results

async def ensure_database_schema():
    try:
        existing_cols = await execute_sql("PRAGMA table_info(episodes)")
        existing = {row["name"] for row in existing_cols or []}
        columns = {
            "duration": "INTEGER",
            "subtitles": "TEXT",
            "audio_tracks": "TEXT",
            "subtitles_1080": "TEXT",
            "audio_tracks_1080": "TEXT",
            "pixeldrain_1080_url": "TEXT",
            "pixeldrain_1080_id": "TEXT",
            "mirror_720_missing": "INTEGER NOT NULL DEFAULT 0",
            "mirror_480_missing": "INTEGER NOT NULL DEFAULT 0",
            "mirror_updated_at": "INTEGER",
            "pending_review_until": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, col_type in columns.items():
            if name not in existing:
                await execute_sql(f"ALTER TABLE episodes ADD COLUMN {name} {col_type}")
                log_message(f"Added column '{name}' to episodes table.")
    except Exception as e:
        log_message(f"Schema maintenance notice: {e}")

# ═══════════════════════════════════════════════════════════════
#  Orchestration
# ═══════════════════════════════════════════════════════════════

async def run_pipeline(anilist_id_str: str, episodes_str: str, force: bool, nyaa_url: str = "") -> str:
    clear_log()
    log_message("=" * 60)
    log_message("🤖 AniRec Content Analyzer - Starting")
    log_message("=" * 60)
    await ensure_database_schema()

    # Validate inputs
    anilist_raw = anilist_id_str.strip()
    # Support full AniList URL or direct ID
    url_match = re.search(r'/anime/(\d+)', anilist_raw)
    if url_match:
        anilist_id = int(url_match.group(1))
    else:
        try:
            anilist_id = int(anilist_raw)
        except (ValueError, AttributeError):
            return "❌ Invalid Media ID. Enter a number (e.g. 21) or full AniList URL."

    episodes = parse_episodes_input(episodes_str)
    if not episodes:
        return "❌ Invalid content range. Use formats like: 1-12, 5,8,10, or 1-5,8,10-12"

    direct_mode = bool(nyaa_url and nyaa_url.strip())
    batch_mode = direct_mode and len(episodes) > 1

    log_message(f"🎯 Media ID: {anilist_id}")
    log_message(f"📋 Segments: {episodes}")
    log_message(f"🔄 Force reprocess: {'Yes' if force else 'No'}")
    if batch_mode:
        log_message(f"📦 Batch mode: {nyaa_url.strip()}")
    elif direct_mode:
        log_message(f"🔗 Direct URL mode: {nyaa_url.strip()}")
    log_message("")

    # Fetch anime info
    try:
        anime_info = await fetch_anime_by_id(anilist_id)
    except Exception as e:
        log_message(f"❌ Failed to fetch anime: {e}")
        return get_log()

    romaji = anime_info["title"]["romaji"] or ""
    english = anime_info["title"]["english"] or ""
    format_type = anime_info.get("format") or "TV"
    synonyms = anime_info.get("synonyms") or []
    cover_url = anime_info.get("coverImage", {}).get("large") or ""
    banner_url = anime_info.get("bannerImage") or ""
    synopsis = anime_info.get("description") or ""
    genres = json.dumps(anime_info.get("genres") or [])

    log_message(f"✅ Anime: {romaji}")
    if english:
        log_message(f"   English: {english}")
    log_message(f"   Format: {format_type} | Status: {anime_info.get('status')}")
    log_message("")

    # Ensure anime exists in DB
    await execute_sql("""
        INSERT INTO anime (anilist_id, title_romaji, title_english, title_native, synonyms,
                           cover_url, banner_url, synopsis, genres, format, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(anilist_id) DO UPDATE SET
            title_romaji = excluded.title_romaji,
            title_english = excluded.title_english,
            synonyms = excluded.synonyms,
            cover_url = CASE WHEN anime.cover_url IS NULL OR anime.cover_url = '' THEN excluded.cover_url ELSE anime.cover_url END,
            status = excluded.status
    """, [anilist_id, romaji, english, anime_info["title"].get("native") or "",
          json.dumps(synonyms), cover_url, banner_url, synopsis, genres, format_type,
          anime_info.get("status") or "RELEASING"])

    # Get anime DB ID
    db_row = await execute_sql("SELECT id FROM anime WHERE anilist_id = ?", [anilist_id])
    if not db_row:
        log_message("❌ Failed to get anime DB ID")
        return get_log()
    anime_db_id = db_row[0]["id"]

    # Process episodes
    results = []
    if batch_mode:
        # Batch: download once, process all episodes from the same torrent
        results = await process_batch_download(anime_info, episodes, anime_db_id, nyaa_url.strip(), force=force)
    else:
        for i, ep_num in enumerate(episodes):
            log_message(f"{'─' * 50}")
            log_message(f"⏳ Processing Episode {ep_num} ({i+1}/{len(episodes)})...")
            if direct_mode:
                result = await process_direct_url(anime_info, ep_num, anime_db_id, nyaa_url.strip(), force=force)
            else:
                result = await process_single_episode(anime_info, ep_num, anime_db_id, force=force)
            results.append(result)
            log_message(result)
            log_message("")

    # Summary
    log_message("=" * 60)
    log_message("📊 SUMMARY")
    log_message("=" * 60)
    success = sum(1 for r in results if r.startswith("✅"))
    skipped = sum(1 for r in results if r.startswith("⏭️"))
    failed = sum(1 for r in results if r.startswith("❌"))
    log_message(f"✅ Success: {success} | ⏭️ Skipped: {skipped} | ❌ Failed: {failed}")
    for r in results:
        log_message(f"  {r}")

    return get_log()

# ═══════════════════════════════════════════════════════════════
#  CLI Entry Point
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    anilist_id = os.environ.get("ANILIST_ID", "").strip()
    episodes = os.environ.get("EPISODES", "").strip()
    force = os.environ.get("FORCE_REDOWNLOAD", "false").lower() in ("true", "1", "yes")
    nyaa_url = os.environ.get("NYAA_URL", "").strip()

    if not anilist_id or not episodes:
        print("Usage: Set ANILIST_ID and EPISODES environment variables")
        print('  ANILIST_ID=21 EPISODES="1-12" python app.py')
        print('  ANILIST_ID=21 EPISODES="1" NYAA_URL="https://nyaa.si/view/..." python app.py')
        sys.exit(1)

    result = asyncio.run(run_pipeline(anilist_id, episodes, force, nyaa_url))
    print(result)

    # Exit with error code if any episodes failed
    if "❌" in result and "✅" not in result:
        sys.exit(1)


