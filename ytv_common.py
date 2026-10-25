# -*- coding: utf-8 -*-
"""refresh_site.py（GitHub Actions）が使う設定読み込み。チャンネル設定は channels.json だけを編集してください。"""
import json, os, re, time
import urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "channels.json")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept-Language": "ja,en;q=0.8"}
MAX_DURATION_DEFAULT = 600        # 全カテゴリ共通の長さ上限(秒)。この値「未満」の動画だけを使う（channels.json の max_duration_sec で変更）
DEFAULT_CATEGORIES = [
    {"id": "news", "label": "ニュース", "short": "ニュース", "color": "#1f6fd1", "weight": 2},
    {"id": "mv", "label": "ミュージックビデオ", "short": "MV", "color": "#d1438a", "weight": 3},
    {"id": "docs", "label": "ドキュメンタリー", "short": "ドキュメンタリー", "color": "#2a9d6a", "weight": 1},
]
CAT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


def _read_raw(path=None):
    with open(path or CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def parse_categories(raw):
    """channels.json の categories を検証して [{id,label,short,color,weight}] にする。無ければ既定の3つ。"""
    out, seen = [], set()
    for c in (raw.get("categories") or DEFAULT_CATEGORIES):
        if not isinstance(c, dict) or not CAT_ID_RE.match(str(c.get("id", ""))) or c["id"] in seen:
            continue
        seen.add(c["id"])
        try:
            w = float(c.get("weight", 1))
        except (TypeError, ValueError):
            w = 1.0
        out.append({"id": c["id"], "label": str(c.get("label") or c["id"]), "short": str(c.get("short") or c.get("label") or c["id"]),
                    "color": str(c.get("color") or "#888888"), "weight": w if w > 0 else 1.0})
    return out or [dict(c) for c in DEFAULT_CATEGORIES]


def max_duration(raw):
    try:
        v = int(raw.get("max_duration_sec", MAX_DURATION_DEFAULT))
        return v if v > 0 else MAX_DURATION_DEFAULT
    except (TypeError, ValueError):
        return MAX_DURATION_DEFAULT


try:
    CATS = tuple(c["id"] for c in parse_categories(_read_raw()))     # カテゴリ一覧（channels.json の categories が唯一の定義）
except Exception:
    CATS = tuple(c["id"] for c in DEFAULT_CATEGORIES)

DEFAULT_POLICY = {"news": {"cap": 60, "max_age_days": 3, "min_keep": 15}, "mv": {"cap": 100}}
GENERIC_POLICY = {"cap": 50}


def load_config(path=None):
    """channels.json を読み、"@名前" の参照(presets)を展開して返す。
    戻り値: dict(channels={cat:[ch,...]}, policy={...}, pick={...}, categories=[...], max_duration=秒)
    各チャンネルの max は max_duration-1 に揃えます（長さは必ず上限未満）。"""
    raw = _read_raw(path)
    cats = parse_categories(raw)
    cat_ids = [c["id"] for c in cats]
    md = max_duration(raw)
    presets = raw.get("presets", {})
    channels = {}
    for cat in cat_ids:
        out = []
        for ch in raw.get("channels", {}).get(cat, []):
            ch = dict(ch)
            for k in ("include", "exclude"):
                v = ch.get(k)
                if isinstance(v, str) and v.startswith("@"):
                    ch[k] = presets.get(v[1:], "")
            ch.setdefault("n", 3)
            ch.setdefault("min", 0)
            ch["max"] = min(int(ch.get("max", md - 1)), md - 1)
            out.append(ch)
        channels[cat] = out
    policy = {c: dict(DEFAULT_POLICY.get(c, GENERIC_POLICY)) for c in cat_ids}
    for c, p in (raw.get("policy") or {}).items():
        if c in policy and isinstance(p, dict):
            policy[c].update(p)
    return {"channels": channels, "policy": policy, "pick": raw.get("pick", {}), "categories": cats, "max_duration": md}


# ------------------------------------------------------------------ ページ側の一覧（channels.json から自動生成）
CFG_RE = re.compile(r"(/\*CATS_START\*/).*?(/\*CATS_END\*/)", re.S)


def page_cfg_js(path=None):
    """HTML に埋め込む  const CFG={...};  の中身（channels.json の categories と max_duration_sec）。"""
    raw = _read_raw(path)
    d = {"max_sec": max_duration(raw), "cats": parse_categories(raw)}
    js = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
    return js.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def sync_page(html_path, cfg_path=None):
    """HTML の /*CATS_START*/ ～ /*CATS_END*/ を channels.json の内容に揃える。変更したら True。"""
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    if not CFG_RE.search(html):
        return False
    new = CFG_RE.sub(lambda m: m.group(1) + "const CFG=" + page_cfg_js(cfg_path) + ";" + m.group(2), html, count=1)
    if new == html:
        return False
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(new)
    return True


def compile_or_none(pattern):
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error:
        return None


def http_get(url, timeout=20, retries=2, retry_codes=(429, 500, 502, 503, 404), sleep=1.5):
    """(status, body) を返す。通信失敗は (0, b"")。retry_codes のときだけ再試行。"""
    st, body = 0, b""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            st, body = e.code, b""
            if e.code not in retry_codes:
                return st, body
        except Exception:
            st, body = 0, b""
        if attempt < retries - 1:
            time.sleep(sleep * (attempt + 1))
    return st, body


# ------------------------------------------------------------------ 埋め込みフォールバックリスト（HTML 内の /*VIDEOS_START*/ ～ /*VIDEOS_END*/）
DATA_RE = re.compile(r"(/\*VIDEOS_START\*/).*?(/\*VIDEOS_END\*/)", re.S)


def sync_embedded(html_path, generated, videos):
    """HTML に埋め込んだ内蔵リスト（videos.json が取れないときのフォールバック）を、いまの（厳格フィルタ済みの）リストに揃える。変更したら True。"""
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    if not DATA_RE.search(html):
        return False
    js = json.dumps({"generated": generated, "videos": videos}, ensure_ascii=False, separators=(",", ":"))
    js = js.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    new = DATA_RE.sub(lambda m: m.group(1) + "const DATA=" + js + ";" + m.group(2), html, count=1)
    if new == html:
        return False
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(new)
    return True
