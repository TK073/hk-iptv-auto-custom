import requests
import re
import json
import base64
import time
import shutil
import socket
import subprocess
import threading
import datetime
from urllib.parse import urlparse, urlunparse, quote, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from functools import lru_cache
from opencc import OpenCC
import m3u8

cc = OpenCC('s2t')

IPTV_UA = 'okhttp/3.15.0 (Linux; Android 11; TVBox)'
HEADERS = {
    'User-Agent': IPTV_UA,
    'Accept': '*/*',
    'Connection': 'keep-alive'
}

# --- 1. 強化版香港頻道別名表 (含全套國際庫英文別名，避免漏抓) ---
CHANNEL_ALIASES = {
    "翡翠台": [
        "翡翠台", "tvb翡翠台", "翡翠", "jade", "tvb jade", "tvb-jade",
        "翡翠台 1080p", "翡翠台 4k", "tvb 翡翠台"
    ],
    "無綫新聞台": [
        "無綫新聞台", "無線新聞台", "無綫新聞", "無線新聞", "tvb新聞", "tvb無綫新聞",
        "tvb news", "tvb-news", "inews", "無綫新聞台 1080p", "tvb news channel"
    ],
    "明珠台": [
        "明珠台", "tvb明珠台", "明珠", "pearl", "tvb pearl", "tvb-pearl", "tvb 明珠台"
    ],
    "TVB Plus": [
        "tvb plus", "tvbplus", "j2", "j5", "tvb j2"
    ],
    "無綫財經體育資訊台": [
        "無綫財經體育資訊台", "無線財經體育資訊台", "無綫財經", "無線財經",
        "財經體育資訊台", "無綫財經台", "tvb finance"
    ],
    "ViuTV": [
        "viutv", "viu tv", "viutv 99", "viu99", "99台", "viu tv 99"
    ],
    "ViuTVsix": [
        "viutvsix", "viutv 6", "viutv6", "viu6", "96台", "viutv 96", "viu tv six"
    ],
    "HOY TV": [
        "hoy tv", "hoytv", "奇妙電視", "香港開電視", "77台", "hoy tv 77", "fantastic tv", "open tv"
    ],
    "HOY 資訊台": [
        "hoy 資訊台", "hoy 资讯台", "hoy資訊台", "hoy78", "78台", "hoy info", "hoy infotainment"
    ],
    "港台電視31": [
        "港台電視31", "港台电视31", "rthk 31", "rthk31", "港台31", "香港電台31",
        "rthk tv 31", "rthktv31", "rthk tv31"
    ],
    "港台電視32": [
        "港台電視32", "港台电视32", "rthk 32", "rthk32", "港台32", "香港電台32",
        "rthk tv 32", "rthktv32", "rthk tv32"
    ],
    "Now新聞台": [
        "now新聞台", "now新闻台", "now新聞", "now新闻", "now 332", "now tv 新聞",
        "now news", "nownews", "now tv news"
    ],
    "Now直播台": [
        "now直播台", "now直播", "now 331", "now tv 直播", "now live", "nowlive"
    ],
    "有線新聞台": [
        "有線新聞台", "有线新闻台", "有線新聞", "有线新闻", "香港有線新聞",
        "cable news", "cablenews", "i-cable news"
    ]
}

# 用字歸一：先用 OpenCC 把任意繁簡寫法統一成繁體，再用下表把「臺」併為「台」，
# 最後由 norm_name 去掉空白與標點。CHANNEL_ALIASES 因此不必窮舉簡繁兩套寫法
# （原實例只收了繁體「無線/無綫」寫法，簡體「无线新闻台」等 12+ 條上游條目全部漏配）。
_POST_FOLD = str.maketrans({'臺': '台'})
_NAME_DROP = frozenset(' \t-_·.()（）[]【】<>《》,，、:：|/"\\\'')

# 最終輸出的頻道順序 (按照香港收視習慣)
ORDER_KEYWORDS = [
    "翡翠台", "無綫新聞台", "明珠台", "TVB Plus", "無綫財經體育資訊台",
    "ViuTV", "ViuTVsix",
    "HOY TV", "HOY 資訊台",
    "港台電視31", "港台電視32",
    "Now新聞台", "Now直播台", "有線新聞台"
]

OFFICIAL_CHANNELS = [
    {"name": "港台電視31", "url": "https://rthktv31-live.akamaized.net/hls/live/2036818/RTHKTV31/master.m3u8"},
    {"name": "港台電視32", "url": "https://rthktv32-live.akamaized.net/hls/live/2036819/RTHKTV32/master.m3u8"}
]

# 取代原先的「整域放行」：只有这两条精确 URL 享受宽松字节阈值。
# 原先 main.py 对任何含 akamaized.net 的 URL 直接判可播，等于跳过整条 CDN 域名的校验，
# 意大利 RAI News24、越南 Pearl FM 电台因此能顶着港台频道名混进输出。
LENIENT_STREAM_URLS = {off['url'] for off in OFFICIAL_CHANNELS}

# 已实测确认的污染源：上游清单把 URL 标成港臺频道名，实为外国电视台 / 纯电台流
BLOCK_URL_TOKENS = ('rainews', 'pearlfm')

# 官方源同轮重试次数。实测 RTHK32 在美区 runner 上两次被单次抽样判死（本机同一 URL 是
# 32 MB/s / 262ms），而官方台是整份清单的保底，一次网络抖动不该让台消失，所以同轮内重试。
# 这不是跨轮记忆——每轮仍从零重测，只是把"一次抽样"变成"最多三次抽样"。
OFFICIAL_ATTEMPTS = 3

# ffprobe 解碼級校驗：必須解析出真實視頻軌才算可播，擋掉純電臺流與空殼 m3u8。
# CI Runner 由 workflow 的 apt-get install ffmpeg 提供 ffprobe；本機若沒裝則自動降級為僅測速。
FFPROBE_BIN = shutil.which('ffprobe')
FFPROBE_TIMEOUT = 12
REQUIRE_VIDEO_TRACK = True

# 測速+解碼階段的總時間預算。候選數已從 181 漲到 240+，再疊加 ffprobe，
# 沒有預算會讓單次執行無上限拖長（本地實測拖到 9 分鐘以上仍未跑完）。
TEST_BUDGET_SECONDS = 600

# 可接受的最高首字节延迟。实测出现过「可播」但延遲 139139 ms 的源——
# requests 的 timeout 是单次 socket 操作超时，服务器一点点吐数据就能拖到几分钟，
# 对播放器来说这等於不可用，所以延迟超上限直接判死。
MAX_ACCEPTABLE_DELAY_MS = 5000

TARGET_README_URLS = [
    "https://raw.githubusercontent.com/youhunwl/TVAPP/main/README.md",
    "https://raw.githubusercontent.com/ngo5/IPTV/main/README.md",
    "https://raw.githubusercontent.com/laoma2053/awesome-zhuiju-free/main/README.md",
    "https://raw.githubusercontent.com/dongyubin/IPTV/main/README.md",
    "https://raw.githubusercontent.com/Zhou-Li-Bin/Tvbox-QingNing/main/README.md",
    "https://raw.githubusercontent.com/Newtxin/TVBoxSource/main/README.md"
]

# 你指定的所有高品質直連清單
SPECIFIC_HK_DIRECT_SOURCES = [
    "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/hk.m3u",
    "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_hong_kong.m3u8",
    "https://raw.githubusercontent.com/s14685/tv/main/iptvhk.txt",
    "https://raw.githubusercontent.com/hujingguang/ChinaIPTV/main/HongKong.m3u8",
    "https://epg.pw/test_channels_hong_kong.m3u",
    "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv6.m3u",
    "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.m3u",
    "https://raw.githubusercontent.com/suxuang/myIPTV/main/ipv4.m3u",
    "https://raw.githubusercontent.com/suxuang/myIPTV/main/ipv6.m3u",
    "https://raw.githubusercontent.com/Kimentanm/aptv/master/m3u/iptv.m3u",
    "https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.m3u",
    "https://raw.githubusercontent.com/YueChan/Live/main/IPTV.m3u",
    "https://raw.githubusercontent.com/kimwang1978/collect-tv-txt/main/merged_output.txt",
    "https://raw.githubusercontent.com/ssili126/tv/main/itvlist.txt",
    "https://raw.githubusercontent.com/Fairy8o/IPTV/main/PDX-V4.txt",
    "https://raw.githubusercontent.com/Fairy8o/IPTV/main/PDX-V6.txt",
    "https://raw.githubusercontent.com/Ftindy/IPTV-URL/main/IPV6.m3u",
    "https://raw.githubusercontent.com/qingwen07/awesome-iptv/main/tvbox_live_all.txt"
]

BLOCK_KEYWORDS = [
    "FOX", "Pluto", "Local Now", "NBC", "CBS", "ABC", "AXS", "Snowy", 
    "Reuters", "Mirror", "ET Now", "The Now", "Right Now", "News Now",
    "Chopper", "Wow", "UHD", "8K", "Career", "Comics", "Movies", "tv360",
    "Anthony Bourdain", "HEi Now", "MS NOW", "Now 14", "NowMedia", "Castr",
    "虎牙", "斗鱼", "B站", "哔哩", "bilibili", "YY", "轮播", "电影", "电视剧",
    "浙江", "杭州", "西湖", "廣東", "珠江", "大灣區", "深圳", "福建",
    "澳門", "Macau", "澳視", "蓮花",
    "CCTV", "CGTN", "鳳凰", "凤凰", "華麗", "星河", "測試", "test", "iHOY"
]

# --- 2. 工具函數 ---

def clean_and_encode_url(url: str) -> str:
    url = url.strip().rstrip(')>],;\'"')
    if "github.com/" in url and "/blob/" in url:
        url = url.replace("github.com/", "raw.githubusercontent.com/").replace("/blob/", "/")
    try:
        parts = urlparse(url)
        netloc = parts.netloc.encode('idna').decode('ascii')
        path = quote(parts.path, safe='/:@%')
        query = quote(parts.query, safe='=&%:@')
        return urlunparse((parts.scheme, netloc, path, parts.params, query, parts.fragment))
    except Exception:
        return url

def stream_dedupe_key(url: str) -> str:
    """同一流的去重鍵：忽略 http/https 協定差異、尾斜杠，以及 .m3u / .m3u8 後綴差異。
    原本只比 URL 全等，導致同一條 RTHK32 流的 https 與 http 兩版同時進輸出。"""
    parts = urlparse(clean_and_encode_url(url).rstrip('/'))
    path = parts.path.lower()
    for ext in ('.m3u8', '.m3u'):
        if path.endswith(ext):
            path = path[:-len(ext)]
            break
    return f"{parts.netloc.lower()}|{path}|{parts.query}"

# IPv6 來源識別：清單檔名本身帶 ipv6/v6 標記，或流地址是 IPv6 字面量（[::1] 形式）。
# GitHub Runner 有 IPv6 連得通，但普通用戶的網路往往只有 IPv4，播不出來就被算成「可播」。
# 這類源不直接丟棄，而是分組標示，避免「CI 全綠、用戶黑屏」。
IPV6_SOURCE_RE = re.compile(r'ipv6|ipvv6|pdx-v6|/v6', re.I)


def looks_ipv6(stream_url: str, source_url: str = "") -> bool:
    host = urlparse(stream_url).hostname or ''
    if host.startswith('['):
        return True
    return bool(source_url and IPV6_SOURCE_RE.search(source_url))


@lru_cache(maxsize=512)
def host_has_ipv4(host: str) -> bool:
    try:
        return any(i[0] == socket.AF_INET for i in socket.getaddrinfo(host, None))
    except OSError:
        return False


def is_ipv6_only(stream_url: str) -> bool:
    host = urlparse(stream_url).hostname or ''
    if host.startswith('['):
        return True
    return bool(host) and not host_has_ipv4(host)

FETCH_STATS = {}      # url -> 抓取結果 ('OK' / 'HTTP 404' / '逾時' / '連不上' / '異常 Xxx')
PLAYLIST_STATS = {}   # 播放列表 url -> {'entries': 名條目數, 'hits': 命中頻道數, 'v6': 疑似純 IPv6}
_STATS_LOCK = threading.Lock()


def _record_fetch(url: str, result: str):
    with _STATS_LOCK:
        FETCH_STATS[url] = result


def looks_like_html_page(head_text: str) -> bool:
    """识别「返回的是网页而不是流/清单」——反代失效时常见这种落地页。
    只看 body 开头，不看 Content-Type：jdshipin 这类代理会把合法 m3u8 标成 text/html，
    用 Content-Type 判定会误杀一大批好源。"""
    body = (head_text or '').lstrip()[:600]
    if not body:
        return False
    lowered = body.lower()
    if '#EXTM3U' in body or '#EXTINF' in body:
        return False
    return (lowered.startswith('<!doctype html') or lowered.startswith('<html')
            or lowered.startswith('<head'))


def fetch_raw_content(url: str, timeout: int = 15) -> str:
    safe_url = clean_and_encode_url(url)
    try:
        r = requests.get(safe_url, headers=HEADERS, timeout=timeout)
        if r.status_code != 200:
            _record_fetch(safe_url, f"HTTP {r.status_code}")
            return ""
        r.encoding = 'utf-8'
        _record_fetch(safe_url, "OK")
        return r.text
    except requests.exceptions.Timeout:
        _record_fetch(safe_url, "逾時")
    except requests.exceptions.RequestException as exc:
        _record_fetch(safe_url, f"異常 {type(exc).__name__}")
    return ""

def parse_tvbox_payload(text: str) -> dict:
    text = text.strip()
    if not text:
        return {}
    try:
        if text.startswith('{') or text.startswith('['):
            return json.loads(text)
    except Exception:
        pass
    try:
        clean_b64 = re.sub(r'[^A-Za-z0-9+/=]', '', text)
        decoded = base64.b64decode(clean_b64).decode('utf-8', errors='ignore')
        if '{' in decoded:
            json_str = decoded[decoded.find('{'):decoded.rfind('}')+1]
            return json.loads(json_str)
    except Exception:
        pass
    return {}

def process_candidate_url(target_url: str, visited: set = None, depth: int = 0) -> list:
    if visited is None:
        visited = set()
    if depth > 3:
        return []

    safe_url = clean_and_encode_url(target_url)
    if safe_url in visited:
        return []
    visited.add(safe_url)

    lower_path = urlparse(safe_url).path.lower()
    if any(lower_path.endswith(ext) for ext in ['.m3u', '.m3u8', '.txt']) and 'dc.txt' not in lower_path:
        return [safe_url]

    text = fetch_raw_content(safe_url, timeout=8)
    if not text:
        return []

    first_few_lines = text.split('\n')[:15]
    if '#EXTM3U' in text or any('#genre#' in l for l in first_few_lines) or any(',' in l and 'http' in l for l in first_few_lines):
        return [safe_url]

    data = parse_tvbox_payload(text)
    if not isinstance(data, dict):
        return []

    extracted_lives = []
    if 'lives' in data and isinstance(data['lives'], list):
        for item in data['lives']:
            if isinstance(item, dict):
                l_url = item.get('url')
                if l_url and isinstance(l_url, str) and l_url.startswith('http'):
                    extracted_lives.append(clean_and_encode_url(l_url))
                elif 'channels' in item and isinstance(item['channels'], list):
                    for sub in item['channels']:
                        for u in sub.get('urls', []):
                            if isinstance(u, str) and u.startswith('http'):
                                extracted_lives.append(clean_and_encode_url(u))

    if 'urls' in data and isinstance(data['urls'], list):
        for sub_item in data['urls']:
            if isinstance(sub_item, dict) and 'url' in sub_item:
                sub_url = sub_item['url']
                if isinstance(sub_url, str) and sub_url.startswith('http'):
                    extracted_lives.extend(process_candidate_url(sub_url, visited, depth + 1))

    return list(set(extracted_lives))

def extract_all_sources() -> list:
    print("🌐 開始動態提取所有上游資源...", flush=True)
    direct_sources_set = set([clean_and_encode_url(u) for u in SPECIFIC_HK_DIRECT_SOURCES])
    all_extracted_playlists = set(direct_sources_set)
    candidate_urls = set()

    print(f"📌 [預載成功] 已加載 {len(direct_sources_set)} 個指定高品質直連源清單", flush=True)

    for readme_url in TARGET_README_URLS:
        content = fetch_raw_content(readme_url, timeout=12)
        if not content:
            continue
        raw_urls = re.findall(r'https?://[^\s#<>"\']+', content)
        for u in raw_urls:
            clean_u = u.strip().rstrip(')>],;\'"')
            if any(ext in clean_u.lower() for ext in ['.apk', '.exe', '.zip', 'shields.io', 'badge.svg', '.jpg', '.jpeg', '.gif']):
                continue
            candidate_urls.add(clean_u)

    print(f"🔍 全網導航庫共獲取到 {len(candidate_urls)} 個候選網址，開始深入解碼...", flush=True)
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(process_candidate_url, u) for u in candidate_urls]
        for f in as_completed(futures):
            try:
                res = f.result()
                all_extracted_playlists.update(res)
            except Exception:
                pass

    final_sources = list(all_extracted_playlists)
    print(f"✅ 全部分析完畢！共彙整出 {len(final_sources)} 個直播清單 (包含全部直連源與影視倉)。", flush=True)
    return final_sources

# --- 3. Guovin 測速與分片驗證引擎 ---

def probe_video_track(url: str, timeout: int = FFPROBE_TIMEOUT) -> tuple:
    """用 ffprobe 實解封包，必須出現 video 軌才算真視頻流。
    回傳 (是否通過, 描述)；未通過時描述即原因，方便定位電臺流 / 空殼清單 / 逾時。"""
    safe_url = clean_and_encode_url(url)
    try:
        proc = subprocess.run(
            [FFPROBE_BIN, '-hide_banner', '-v', 'error', '-print_format', 'json',
             '-show_entries', 'stream=codec_type,codec_name,width,height',
             '-show_entries', 'format=format_name,bit_rate',
             '-analyzeduration', '3000000', '-probesize', '1000000',
             '-user_agent', IPTV_UA, safe_url],
            capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return False, "ffprobe 逾時"
    except Exception as exc:
        return False, f"ffprobe 異常 {type(exc).__name__}"

    try:
        info = json.loads(proc.stdout or '{}')
    except Exception:
        return False, (proc.stderr.strip()[:70] or 'ffprobe 輸出非 JSON')

    streams = info.get('streams', [])
    videos = [s for s in streams if s.get('codec_type') == 'video' and (s.get('width') or 0) > 0]
    if not videos:
        kinds = ','.join(sorted({s.get('codec_type', '?') for s in streams})) or '無任何流'
        return False, f"無視頻軌({kinds})"
    v = videos[0]
    fmt = info.get('format', {}).get('format_name', '?')
    return True, f"{v.get('codec_name')} {v.get('width')}x{v.get('height')} [{fmt}]"


def test_stream_speed(url: str, timeout: int = 5) -> tuple:
    safe_url = clean_and_encode_url(url)
    
    if any(tok in safe_url.lower() for tok in BLOCK_URL_TOKENS):
        return False, 0, float('inf')

    t_start = time.time()
    try:
        r = requests.get(safe_url, headers=HEADERS, timeout=timeout)
        if r.status_code != 200:
            return False, 0, float('inf')
        
        text = r.text
        if '#EXTM3U' not in text:
            if looks_like_html_page(text[:600]):
                return False, 0, float('inf')
            delay = (time.time() - t_start) * 1000
            speed = len(r.content) / (1024 * 1024) / max((time.time() - t_start), 0.001)
            return len(r.content) > 1024, speed, delay

        parsed = m3u8.loads(text)
        target_seg_url = None

        if parsed.is_variant and parsed.playlists:
            sub_url = urljoin(safe_url, parsed.playlists[0].uri)
            sub_r = requests.get(sub_url, headers=HEADERS, timeout=timeout)
            if sub_r.status_code == 200:
                sub_parsed = m3u8.loads(sub_r.text)
                if sub_parsed.segments:
                    target_seg_url = urljoin(sub_url, sub_parsed.segments[0].uri)
        elif parsed.segments:
            target_seg_url = urljoin(safe_url, parsed.segments[0].uri)
        else:
            lines = [l.strip() for l in text.split('\n') if l.strip() and not l.startswith('#')]
            if lines:
                target_seg_url = urljoin(safe_url, lines[0])

        if not target_seg_url:
            return False, 0, float('inf')

        t_seg_start = time.time()
        seg_res = requests.get(target_seg_url, headers=HEADERS, timeout=timeout, stream=True)
        if seg_res.status_code != 200:
            return False, 0, float('inf')

        delay = (time.time() - t_seg_start) * 1000
        bytes_read = 0
        head_bytes = b''
        t_download_start = time.time()

        for chunk in seg_res.iter_content(chunk_size=32768):
            if not head_bytes:
                head_bytes = chunk[:600]
                if looks_like_html_page(head_bytes.decode('utf-8', 'ignore')):
                    # 清单指向 YouTube 网页 / 落地页时，光看字节数会把它当成「能出画」
                    seg_res.close()
                    return False, 0, float('inf')
            bytes_read += len(chunk)
            if bytes_read >= 256 * 1024 or (time.time() - t_download_start) >= 2.5:
                break
        seg_res.close()

        elapsed = time.time() - t_download_start
        speed = (bytes_read / (1024 * 1024)) / max(elapsed, 0.001)

        min_bytes = 4 * 1024 if safe_url in LENIENT_STREAM_URLS else 40 * 1024
        is_alive = bytes_read >= min_bytes
        return is_alive, speed, delay

    except Exception:
        return False, 0, float('inf')

def evaluate_stream(url: str) -> tuple:
    """判活的唯一入口：测速 → 延迟上限 → ffprobe 视频轨，三道门按成本从低到高排列。
    返回 (是否可用, 速率, 延迟, 失败类别, 说明)；类别供上层区分统计。"""
    is_alive, speed, delay = test_stream_speed(url)
    if not is_alive:
        return False, speed, delay, "speed", "測速未通過"
    if delay > MAX_ACCEPTABLE_DELAY_MS:
        return False, speed, delay, "delay", f"延遲 {delay:.0f} ms > 上限 {MAX_ACCEPTABLE_DELAY_MS} ms"
    if FFPROBE_BIN and REQUIRE_VIDEO_TRACK:
        dec_ok, dec_detail = probe_video_track(url)
        if not dec_ok:
            return False, speed, delay, "decode", dec_detail
    return True, speed, delay, "", ""


@lru_cache(maxsize=8192)
def norm_name(text: str) -> str:
    compacted = ''.join(c for c in text.lower() if c not in _NAME_DROP)
    return cc.convert(compacted).translate(_POST_FOLD)

# 最長別名優先，避免短別名 ('viutv') 搶走長別名 ('viutvsix') 的歸屬
_ALIAS_INDEX = sorted(
    ((norm_name(a), std) for std, aliases in CHANNEL_ALIASES.items() for a in aliases),
    key=lambda pair: (-len(pair[0]), pair[0])
)

def match_standard_channel_name(raw_name: str) -> str:
    clean_n = norm_name(raw_name)
    if not clean_n:
        return ""
    for alias, std_name in _ALIAS_INDEX:
        if alias in clean_n:
            return std_name
    return ""

def parse_single_playlist(source_url: str) -> list:
    channels = []
    # 對巨型檔案給予 20 秒充足下載時間
    content = fetch_raw_content(source_url, timeout=12)
    v6_hint = looks_ipv6("", source_url)
    entries = hits = 0
    if not content:
        with _STATS_LOCK:
            PLAYLIST_STATS[source_url] = {"entries": 0, "hits": 0, "v6": v6_hint}
        return channels

    lines = [l.strip() for l in content.split('\n') if l.strip()]
    current_raw_name = ""
    is_m3u = any(line.startswith('#EXTM3U') or line.startswith('#EXTINF') for line in lines[:10])

    for line in lines:
        if is_m3u:
            if line.startswith("#EXTINF"):
                match = re.search(r',(.+)$', line)
                if match:
                    current_raw_name = match.group(1).strip()
            elif line.startswith("http"):
                stream_url = line.split('$')[0].strip()
                entries += 1
                if current_raw_name:
                    if not any(b.lower() in current_raw_name.lower() for b in BLOCK_KEYWORDS):
                        std_name = match_standard_channel_name(current_raw_name)
                        if std_name:
                            hits += 1
                            channels.append({"name": std_name, "raw_name": current_raw_name,
                                             "url": stream_url, "v6": v6_hint or looks_ipv6(stream_url)})
                current_raw_name = ""
        else:
            if ',' in line and not line.startswith('http'):
                parts = line.split(',', 1)
                if len(parts) == 2:
                    raw_n = parts[0].strip()
                    url_p = parts[1].split('$')[0].strip()
                    if url_p.startswith('http'):
                        entries += 1
                        if not any(b.lower() in raw_n.lower() for b in BLOCK_KEYWORDS):
                            std_name = match_standard_channel_name(raw_n)
                            if std_name:
                                hits += 1
                                channels.append({"name": std_name, "raw_name": raw_n,
                                                 "url": url_p, "v6": v6_hint or looks_ipv6(url_p)})

    with _STATS_LOCK:
        PLAYLIST_STATS[source_url] = {"entries": entries, "hits": hits, "v6": v6_hint}
    return channels

# --- 4. 主執行流程 ---

def print_source_health_report(total_sources: int):
    """摊开上游健康度：谁挂了、谁零贡献、谁的命中率极低——原先这些全被静默吞掉。"""
    dead = {u: r for u, r in FETCH_STATS.items() if r != "OK"}
    reasons = Counter(r.split()[0] for r in dead.values())
    hit_pairs = sorted(PLAYLIST_STATS.items(), key=lambda kv: -kv[1]["hits"])
    contributing = [kv for kv in hit_pairs if kv[1]["hits"] > 0]

    print("\n📋 上游健康度报告", flush=True)
    print(f"  清單來源 {total_sources}｜成功解析 {len(PLAYLIST_STATS)}｜有貢獻 {len(contributing)}｜零貢獻 {len(PLAYLIST_STATS) - len(contributing)}", flush=True)
    print(f"  抓取失敗 {len(dead)} 筆，原因分佈: {dict(reasons) if reasons else '無'}", flush=True)
    print("  產出最高的來源（命中 / 名條目數，比値越低說明清單越杂）:", flush=True)
    for url, st in contributing[:10]:
        rate = st["hits"] / st["entries"] * 100 if st["entries"] else 0
        print(f"    {st['hits']:>4} / {st['entries']:>6} ({rate:4.1f}%)  {url[:86]}", flush=True)
    print("  失敗且屬於本次來源清單的上游:", flush=True)
    for url, reason in sorted((u, r) for u, r in dead.items() if u in PLAYLIST_STATS)[:12]:
        print(f"    [{reason}] {url[:86]}", flush=True)


def fetch_and_parse() -> list:
    found_channels = []
    seen_keys = set()

    playlist_sources = extract_all_sources()
    print(f"\n🚀 開始並行解析 {len(playlist_sources)} 個清單中的香港電視頻道...", flush=True)

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(parse_single_playlist, s): s for s in playlist_sources}
        for f in as_completed(futures):
            source_url = futures[f]
            try:
                ch_list = f.result()
                added = 0
                for ch in ch_list:
                    key = stream_dedupe_key(ch['url'])
                    if key not in seen_keys:
                        seen_keys.add(key)
                        found_channels.append(ch)
                        added += 1
                if added > 0:
                    tag = "【直連清單】" if any(d in source_url for d in SPECIFIC_HK_DIRECT_SOURCES) else "【影視倉源】"
                    print(f"  ⭐ {tag} 貢獻 {added} 個有效候選香港台: {source_url}", flush=True)
            except Exception as exc:
                with _STATS_LOCK:
                    FETCH_STATS[source_url] = f"解析異常 {type(exc).__name__}"

    print(f"\n📊 全部解析完畢，共提取到 {len(found_channels)} 個香港電視候選串流。", flush=True)
    print_source_health_report(len(playlist_sources))
    return found_channels

def generate_m3u(channels: list):
    if not FFPROBE_BIN:
        print("\n⚠️ 本机未检测到 ffprobe，跳过解码级校验（GitHub Runner 已安装 ffmpeg，会执行校验）", flush=True)
    print(f"\n⚡ 正在啟動【Guovin 測速引擎】：實測下載速率 (Speed) 與 延遲 (Delay)...", flush=True)
    
    channel_test_results = {}
    
    decode_rejects = []

    budget_start = time.monotonic()

    def test_worker(ch):
        if time.monotonic() - budget_start > TEST_BUDGET_SECONDS:
            return ch, False, 0, float('inf'), "未測（超出總時間預算）"
        ok, speed, delay, kind, reason = evaluate_stream(ch['url'])
        if kind == "decode":
            decode_rejects.append((ch['name'], ch['url'], reason))
        return ch, ok, speed, delay, reason

    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = [executor.submit(test_worker, ch) for ch in channels]
        for f in as_completed(futures):
            ch, is_alive, speed, delay, reason = f.result()
            c_name = ch['name']
            if c_name not in channel_test_results:
                channel_test_results[c_name] = []

            if is_alive:
                channel_test_results[c_name].append({
                    "name": c_name,
                    "url": ch['url'],
                    "speed": speed,
                    "delay": delay,
                    "v6": ch.get('v6', False)
                })
                print(f"  🟢 [可播] {c_name} | 速率: {speed:.2f} MB/s | 延遲: {delay:.0f} ms", flush=True)
            else:
                print(f"  🔴 [不可播] {c_name} | {reason}", flush=True)

    if time.monotonic() - budget_start > TEST_BUDGET_SECONDS:
        print(f"\n⏱️ 已用滿 {TEST_BUDGET_SECONDS}s 時間預算，剩餘候選未測速", flush=True)

    if decode_rejects:
        print(f"\n🔬 ffprobe 解码校验剔除 {len(decode_rejects)} 条「能下载但无视频轨」的源：", flush=True)
        for c_name, c_url, reason in decode_rejects:
            print(f"  ⛔ {c_name} | {reason} | {c_url[:80]}", flush=True)

    final_list = []
    
    for off in OFFICIAL_CHANNELS:
        ok, speed, delay, reason = False, 0, float('inf'), ""
        attempt = 1
        for attempt in range(1, OFFICIAL_ATTEMPTS + 1):
            ok, speed, delay, kind, reason = evaluate_stream(off['url'])
            if ok:
                break
            if attempt < OFFICIAL_ATTEMPTS:
                print(f"  ⏳ [官方源第 {attempt} 次未过] {off['name']} | {reason} | 重试", flush=True)
                time.sleep(2)
        if ok:
            final_list.append({**off, "speed": speed, "delay": delay})
            note = f"（第 {attempt} 次通过）" if attempt > 1 else ""
            print(f"  🟢 [官方源已验证] {off['name']} | 速率: {speed:.2f} MB/s | 延遲: {delay:.0f} ms {note}", flush=True)
        else:
            print(f"  ⚠️ [官方源 {OFFICIAL_ATTEMPTS} 次均未通过，已剔除] {off['name']} | {reason} | {off['url']}", flush=True)

    used_keys = {stream_dedupe_key(item['url']) for item in final_list}

    for c_name in ORDER_KEYWORDS:
        candidates = channel_test_results.get(c_name, [])
        if not candidates:
            continue
        
        candidates.sort(key=lambda x: (-x['speed'], x['delay']))
        for item in candidates:
            key = stream_dedupe_key(item['url'])
            if key in used_keys:
                continue
            used_keys.add(key)
            final_list.append(item)

    # 輸出 MoonTV / TiviMate 標準兩行格式
    lines = ['#EXTM3U x-tvg-url="https://epg.112114.xyz/pp.xml" url-tvg="https://epg.112114.xyz/pp.xml"']
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_hkt = now_utc.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
    lines.append(f'# Updated: {now_hkt.strftime("%Y-%m-%d %H:%M:%S")} HKT')

    for item in final_list:
        name = item["name"]
        # logo 路徑含中文，必須 percent-encode，否則严格的 HTTP 客户端直接报 unicode 错误
        logo_url = "https://epg.112114.xyz/logo/" + quote(f"{name}.png", safe='')
        group = "Hong Kong [IPv6]" if item.get("v6") and is_ipv6_only(item["url"]) else "Hong Kong"
        lines.append(f'#EXTINF:-1 tvg-name="{name}" tvg-logo="{logo_url}" group-title="{group}",{name}')
        # 把取流用的 UA 一起写进订阅表，播放器才能复现脚本端的可播结果
        lines.append(f'#EXTVLCOPT:http-user-agent={IPTV_UA}')
        lines.append(f'{item["url"]}')

    content = "\n".join(lines) + "\n"

    with open("hk_live.m3u", "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\n✅ 匯出完成：{len(final_list)} 條已驗證可播、且經同流去重的香港電視頻道。", flush=True)

if __name__ == "__main__":
    candidates = fetch_and_parse()
    generate_m3u(candidates)
