# -*- coding: utf-8 -*-
"""動画リスト更新のロジック（標準ライブラリのみ。yt-dlp があれば長さの確認などに使います）。
refresh_site.py から使われます。ローカル版(serve.py)と同じロジックです。
  取得: RSS → yt-dlp → どちらもダメなら前回の結果を保持 / 長さ: yt-dlp / 埋め込み可否: oEmbed / 長さ上限: channels.json の max_duration_sec
"""
import importlib.util, json, os, re, shutil, subprocess, sys, threading, time
import xml.etree.ElementTree as ET
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ytv_common as C

VERIFY_TTL = 7 * 86400               # oEmbed で確認済みの動画を再確認しない期間
BAD_TTL = 30 * 86400                 # 埋め込み不可と判明した動画を記憶する期間
OEMBED_WORKERS = 6
FEED_WORKERS = 6
RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id="
OEMBED_URL = "https://www.youtube.com/oembed?format=json&url="
VIDEOS_FILE = os.path.join(HERE, "state.json")      # 内部状態(確認済み・長さ・不可の記憶)。公開用の videos.json は refresh_site.py が別に書く
HTML_FILE = os.path.join(HERE, "index.html")
NATIVE_FILE = os.path.join(HERE, "native.html")
DATA_RE = re.compile(r"(/\*VIDEOS_START\*/).*?(/\*VIDEOS_END\*/)", re.S)
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

try:                                   # Windows コンソール(cp932)でも文字化けで落ちないように
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


def log(*a):
    try:
        print("[%s]" % time.strftime("%H:%M:%S"), *a, flush=True)
    except Exception:
        pass


def gen_epoch(s):
    try:
        return int(time.mktime(time.strptime(s, "%Y-%m-%d %H:%M")))
    except Exception:
        return 0


def now_str(ts=None):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts or time.time()))


# ------------------------------------------------------------------ 標準リスト（HTML埋め込み）と保存済みキャッシュ
def load_static():
    """youtube-tv.html に埋め込まれた標準リスト (generated, {cat:[...]}) を返す。読めなければ空。"""
    try:
        with open(HTML_FILE, encoding="utf-8") as f:
            html = f.read()
        m = re.search(r"/\*VIDEOS_START\*/\s*const DATA=(.*?);\s*/\*VIDEOS_END\*/", html, re.S)
        d = json.loads(m.group(1))
        return d.get("generated", ""), {c: list(d["videos"].get(c, [])) for c in C.CATS}
    except Exception as e:
        log("埋め込みリストを読めませんでした:", e)
        return "", {c: [] for c in C.CATS}


def load_cache():
    try:
        with open(VIDEOS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        vids = d.get("videos") or {}
        return {"generated": d.get("generated", ""),
                "videos": {c: [v for v in vids.get(c, []) if isinstance(v, dict) and ID_RE.match(str(v.get("id", "")))] for c in C.CATS},
                "verified": d.get("verified") or {}, "bad": d.get("bad") or {}, "durs": d.get("durs") or {},
                "last_success": d.get("last_success", 0)}
    except Exception:
        return None


def save_cache(cache):
    tmp = VIDEOS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, VIDEOS_FILE)


# ------------------------------------------------------------------ 取得: RSS → yt-dlp
def parse_iso(s):
    try:
        return int(datetime.fromisoformat(s.strip().replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())
    except Exception:
        return 0


def parse_rss(body):
    """RSS(Atom) → [{id,t,p}] 新しい順。"""
    root = ET.fromstring(body)
    out = []
    for e in root.findall("a:entry", NS):
        vid = (e.findtext("yt:videoId", namespaces=NS) or "").strip()
        if not ID_RE.match(vid):
            continue
        out.append({"id": vid, "t": (e.findtext("a:title", namespaces=NS) or vid).strip(),
                    "p": parse_iso(e.findtext("a:published", namespaces=NS) or "")})
    out.sort(key=lambda x: -x["p"])
    return out


def fetch_rss(cid):
    st, body = C.http_get(RSS_URL + cid, timeout=15, retries=3)
    if st != 200:
        return None
    try:
        r = parse_rss(body)
        return r if r else None
    except ET.ParseError:
        return None


_ytdlp_cmd = [None, False]


def ytdlp_cmd():
    """yt-dlp の起動コマンド(無ければ None)。"""
    if not _ytdlp_cmd[1]:
        cmd = None
        exe = shutil.which("yt-dlp")
        if exe:
            cmd = [exe]
        elif importlib.util.find_spec("yt_dlp"):
            cmd = [sys.executable, "-m", "yt_dlp"]
        _ytdlp_cmd[0], _ytdlp_cmd[1] = cmd, True
    return _ytdlp_cmd[0]


_yt_lock = threading.Lock()
_yt_state = {"ok": 0, "fail": 0}


FLAT_END = 30                     # チャンネルの /videos タブから取る本数（flat-playlist。長さ duration つき）
LIVE_STATUSES = ("is_live", "is_upcoming", "post_live", "was_live")


def _flat_entries(d, cid):
    """yt-dlp --flat-playlist の結果 → [{id,t,p,d,ls}]。ライブ/配信予定/配信終了(アーカイブ)は除く。長さ不明は d=0。"""
    out = []
    if not d or (d.get("channel_id") != cid and d.get("playlist_channel_id") != cid):
        return out
    for e in d.get("entries") or []:
        if not e or not ID_RE.match(str(e.get("id", ""))):
            continue
        if e.get("live_status") in LIVE_STATUSES or e.get("was_live"):
            continue
        p = e.get("timestamp") or 0
        if not p and e.get("upload_date"):
            p = parse_iso("%s-%s-%sT00:00:00+09:00" % (e["upload_date"][:4], e["upload_date"][4:6], e["upload_date"][6:8]))
        try:
            dur = int(float(e.get("duration") or 0))
        except (TypeError, ValueError):
            dur = 0
        out.append({"id": e["id"], "t": e.get("title") or e["id"], "p": int(p or 0), "d": dur})
    return out


def fetch_ytdlp(cid):
    """yt-dlp で /videos タブ(ショート除く)の最新動画一覧 → [{id,t,p,d}]（d=長さ秒。タブの一覧に含まれる）。使えない/失敗は None。
    連続失敗が続いたら以後あきらめる(無駄な待ちを避ける)。"""
    cmd = ytdlp_cmd()
    if not cmd:
        return None
    with _yt_lock:
        if _yt_state["ok"] == 0 and _yt_state["fail"] >= 3:
            return None
    d = None
    for attempt in range(2):
        try:
            r = subprocess.run(cmd + ["-q", "--no-warnings", "--flat-playlist", "--playlist-end", str(FLAT_END), "--dump-single-json",
                                      "--extractor-args", "youtube:lang=ja", "https://www.youtube.com/channel/%s/videos" % cid],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
            d = json.loads(r.stdout, strict=False) if r.stdout.strip() else None
        except Exception:
            d = None
        if d:
            break
        time.sleep(2)
    out = _flat_entries(d, cid)
    with _yt_lock:
        _yt_state["ok" if out else "fail"] += 1
    return out or None


def fetch_channel(cid):
    """(source, entries)。source: 'ytdlp'(長さつき。RSSの公開日時を足す) | 'rss'(長さなし→あとで個別に調べる) | None"""
    rss = None
    try:
        rss = fetch_rss(cid)
    except Exception as e:
        log("RSS取得エラー", cid, e)
    try:
        yt = fetch_ytdlp(cid)
    except Exception as e:
        log("yt-dlp取得エラー", cid, e)
        yt = None
    if yt:
        pub = {x["id"]: x["p"] for x in (rss or [])}
        for x in yt:
            x["p"] = x["p"] or pub.get(x["id"], 0)
        return "ytdlp", yt
    if rss:
        return "rss", rss
    return None, None


# ------------------------------------------------------------------ 厳格フィルタ（長さが「分かっていて」上限未満のものだけ使う）
MIN_SEC = 20                       # これ未満(秒)は使わない
HARD_TITLE_RE = re.compile(r"(?i)ライブ配信|生配信|生放送|アーカイブ|作業用\s*BGM|睡眠用|【\s*\d+\s*時間|\d+\s*時間(?!前|以内|後|目|ぶり|半|経過|の間)|\d+\s*hours?\b(?!\s*ago)|24\s*/\s*7|live\s*stream|livestream|live\s*archive")
SOFT_TITLE_RE = re.compile(r"(?i)耐久|【\s*live\s*】|\[\s*live\s*\]|ライブ中継|ニュースライブ|ライブ\s*】")
MV_TITLE_RE = re.compile(r"(?i)\bM/?V\b|music\s*video|official\s*video|trailer|予告|\bPV\b|teaser|予告編")


def title_is_longform(t):
    """タイトルから「ライブ配信のアーカイブ・長時間まとめ」と分かるもの。MV/予告編と分かるタイトルは SOFT のみ免除（HARD は常に除外）。"""
    t = t or ""
    if HARD_TITLE_RE.search(t):
        return True
    return bool(SOFT_TITLE_RE.search(t)) and not MV_TITLE_RE.search(t)


def is_ok(x, cap=None, d=None):
    """この動画を使ってよいか: 長さが分かっている / MIN_SEC 以上 cap 未満 / ライブ・アーカイブ風のタイトルでない。"""
    cap = cap or C.MAX_DURATION_DEFAULT
    dd = int(d if d is not None else (x.get("d") or 0))
    return MIN_SEC <= dd < cap and not title_is_longform(x.get("t"))


def select_for_channel(entries, ch, cat, pol, now, dmap=None):
    """チャンネル設定(include/exclude/長さ)とカテゴリ方針で絞り、新しい順に最大 n 本。
    長さ不明のものは候補として残す（あとで個別に調べ、分からなければ最終的に落とす）。dmap: {id: 長さ} 既知の長さ。"""
    inc, exc = C.compile_or_none(ch.get("include")), C.compile_or_none(ch.get("exclude"))
    max_age = pol.get("max_age_days")
    res = []
    for e in entries:
        t = e["t"]
        if inc and not inc.search(t):
            continue
        if exc and exc.search(t):
            continue
        if title_is_longform(t):
            continue
        d = e.get("d") or (dmap or {}).get(e["id"]) or 0
        if d and not (max(ch["min"], MIN_SEC) <= d <= ch["max"]):
            continue
        if max_age and e["p"] and now - e["p"] > max_age * 86400:
            continue
        res.append({"id": e["id"], "t": t, "c": ch["name"], "d": d, "p": e["p"]})
        if len(res) >= ch["n"]:
            break
    return res


# ------------------------------------------------------------------ oEmbed 確認
def oembed_status(vid):
    url = OEMBED_URL + urllib.parse.quote("https://www.youtube.com/watch?v=" + vid, safe="")
    return C.http_get(url, timeout=12, retries=2, retry_codes=(429, 500, 502, 503), sleep=1)[0]


def verify_ids(ids):
    """{id: True(OK)/False(401,404=不可)/None(不明)}。小さなスレッドプールで確認。"""
    ids = list(ids)
    if not ids:
        return {}
    with ThreadPoolExecutor(max_workers=OEMBED_WORKERS) as ex:
        sts = list(ex.map(oembed_status, ids))
    out = {}
    for i, st in zip(ids, sts):
        out[i] = True if st == 200 else (False if st in (401, 404) else None)
    return out


# ------------------------------------------------------------------ 長さの確認 (RSS由来・前回リスト由来で長さが無いもの。yt-dlp で1本ずつ)
RESOLVE_MAX = 150           # 1回の更新で個別に長さを調べる動画の上限（残りは次回。45分のタイムアウト内に収まる量）
RESOLVE_WORKERS = 4
PROBE_CLIENTS = "web_safari,mweb"      # bot判定(Sign in to confirm)を避けやすいクライアント。--ignore-no-formats-error で形式なしでも情報だけ取る
FAIL_RETRY_AFTER = 3 * 86400           # 5回失敗した動画は、この期間は再確認しない
_probe_stats = {"ok": 0, "fail": 0, "err": ""}


def probe_duration(vid):
    """yt-dlp で (長さ秒 or None, ライブ/配信予定/配信アーカイブか)。長さが取れなかったら (None, False)。"""
    cmd = ytdlp_cmd()
    if not cmd:
        return None, False
    try:
        for attempt in range(2):
            r = subprocess.run(cmd + ["-q", "--no-warnings", "--skip-download", "--no-playlist", "--ignore-no-formats-error",
                                      "--extractor-args", "youtube:player_client=" + PROBE_CLIENTS,
                                      "--print", "%(.{duration,live_status,was_live})j", "https://www.youtube.com/watch?v=" + vid],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            line = (r.stdout.strip().splitlines() or [""])[-1]
            try:
                j = json.loads(line)
            except Exception:
                j = None
            if j is not None:
                live = j.get("live_status") in LIVE_STATUSES or bool(j.get("was_live"))
                dur = j.get("duration")
                _probe_stats["ok"] += 1
                if live:
                    return None, True
                return (int(float(dur)) if dur else None), False
            _probe_stats["err"] = (r.stderr.strip().splitlines() or ["?"])[-1][:200]
            if re.search(r"unavailable|private|removed|terminated|not available|copyright", r.stderr, re.I):
                break                         # 削除済みなどは再試行しない
            time.sleep(1.5)
        _probe_stats["fail"] += 1
        return None, False
    except Exception as e:
        _probe_stats["fail"] += 1
        _probe_stats["err"] = "%s: %s" % (type(e).__name__, e)
        return None, False
    finally:
        time.sleep(0.3)


def resolve_durations(ids):
    """{id: (長さ秒 or None, ライブか)}。yt-dlp があるときだけ、最大 RESOLVE_MAX 本を RESOLVE_WORKERS 並列で調べる。"""
    ids = list(ids)[:RESOLVE_MAX]
    if not ids or not ytdlp_cmd():
        return {}
    with ThreadPoolExecutor(max_workers=RESOLVE_WORKERS) as ex:
        return dict(zip(ids, ex.map(probe_duration, ids)))


def norm_durs(raw):
    """state.json の durs → {id: [長さ(0=不明), ライブ(0/1), 確認した時刻, 失敗回数]}（旧形式の整数も受け付ける）"""
    out = {}
    for k, v in (raw or {}).items():
        if isinstance(v, (int, float)) and v > 0:
            out[k] = [int(v), 0, 0, 0]
        elif isinstance(v, (list, tuple)) and len(v) >= 4:
            try:
                out[k] = [int(v[0] or 0), 1 if v[1] else 0, int(v[2] or 0), int(v[3] or 0)]
            except (TypeError, ValueError):
                pass
    return out


# ------------------------------------------------------------------ リスト合成
def ts_of(v):
    return v.get("p") or v.get("a") or 0


def order_cat(c, items, fresh_ids, p, now):
    """カテゴリごとの方針で並べる（上限で切る前の並び）。"""
    if c == "news":
        max_age = (p.get("max_age_days") or 3) * 86400
        freshn = sorted([x for x in items if ts_of(x) and now - ts_of(x) <= max_age], key=lambda x: -ts_of(x))
        fid = {x["id"] for x in freshn}
        rest = sorted([x for x in items if x["id"] not in fid], key=lambda x: -ts_of(x))
        lst = freshn
        if len(lst) < p.get("min_keep", 15):        # 新しいものが少なければ、古いものでも最低本数は確保
            lst = lst + rest[: p.get("min_keep", 15) - len(lst)]
        return lst
    if c != "mv":          # docs ほか: 新着 → 標準リスト → その他（新しい順）
        newf = sorted([x for x in items if x["id"] in fresh_ids], key=lambda x: -ts_of(x))
        st = [x for x in items if x.get("s") and x["id"] not in fresh_ids]
        oth = sorted([x for x in items if not x.get("s") and x["id"] not in fresh_ids], key=lambda x: -ts_of(x))
        return newf + st + oth
    st = [x for x in items if x.get("s")]            # mv: 厳選(標準)は全部残し、新着+既存の新しい順で上限まで
    oth = sorted([x for x in items if not x.get("s")], key=lambda x: -ts_of(x))
    return st + oth


def compose(cfg, static, cache, fetched, now, verify=verify_ids, static_gen="", durations=resolve_durations):
    """新しいキャッシュ内容を作る。fetched: {cid: entries or None}。取得に失敗しても、長さが確認済みの既存リストは消さない。
    【厳格】長さが「分かっていて」 MIN_SEC 以上 cap(=max_duration_sec)未満で、ライブ/配信アーカイブでない動画だけを残す。
    長さ不明の動画は yt-dlp で個別に調べ（durs に記憶。失敗も記憶して次回再挑戦）、分からないうちは絶対に出力しない。"""
    pol = cfg["policy"]
    cap = cfg.get("max_duration") or C.MAX_DURATION_DEFAULT
    durs = norm_durs((cache or {}).get("durs"))
    dmap = {}                                           # 今回の取得で分かった長さ(タブ一覧の duration)
    for ents in fetched.values():
        for e in ents or []:
            if e.get("d"):
                dmap[e["id"]] = int(e["d"])
    for i, rec in durs.items():                         # 個別確認で分かっていた長さ
        if rec[0] and i not in dmap:
            dmap[i] = rec[0]
    live_ids = {i for i, rec in durs.items() if rec[1]}
    static_ids = {v["id"]: c for c in C.CATS for v in static[c]}
    prev = {c: [] for c in C.CATS}
    if cache:
        for c in C.CATS:
            prev[c] = cache["videos"].get(c, [])
    verified = dict((cache or {}).get("verified") or {})
    bad = {k: v for k, v in ((cache or {}).get("bad") or {}).items() if now - v < BAD_TTL}

    # カテゴリごとの既存アイテム(標準リスト + 前回キャッシュ)。公開日が不明なものは「リスト生成時刻」を新しさの目安にする
    sg = gen_epoch(static_gen) or now
    cg = gen_epoch((cache or {}).get("generated", "")) or sg
    old = {c: {} for c in C.CATS}
    for c in C.CATS:
        for v in static[c]:
            x = {"id": v["id"], "t": v.get("t") or v["id"], "c": v.get("c", ""), "d": int(v.get("d") or 0), "p": 0, "a": sg, "s": 1}
            old[c][x["id"]] = x
        names = {ch["name"] for ch in cfg["channels"][c]}
        for v in prev[c]:
            x = {"id": v["id"], "t": v.get("t") or v["id"], "c": v.get("c", ""), "d": int(v.get("d") or 0),
                 "p": int(v.get("p") or 0), "a": int(v.get("a") or cg)}
            if v["id"] in static_ids:
                x["s"] = 1
            elif x["c"] not in names:       # channels.json から外したチャンネルの古い分は整理
                continue
            if v["id"] in old[c]:
                old[c][v["id"]].update({k: x[k] for k in ("p", "a") if x[k]})   # (a は前回の初出時刻を優先)
                if x["d"] and not old[c][v["id"]]["d"]:
                    old[c][v["id"]]["d"] = x["d"]
            else:
                old[c][v["id"]] = x

    # 新着候補（docs → mv → news の順に確保。同じ動画が複数カテゴリに入らないようにする）
    owner = {i: c for c in C.CATS for i in old[c]}      # 既存のものは元のカテゴリを優先
    fresh = {c: {} for c in C.CATS}
    for c in [x for x in C.CATS if x != "news"] + ["news"]:         # news 以外が先に取り、ニュースは最後（特集などはそちらのジャンルへ）
        for ch in cfg["channels"][c]:
            ents = fetched.get(ch["id"])
            if not ents:
                continue
            for x in select_for_channel(ents, ch, c, pol[c], now, dmap):
                if x["id"] in bad or x["id"] in live_ids or owner.get(x["id"], c) != c or (x["d"] and x["d"] >= cap):
                    continue
                owner[x["id"]] = c
                fresh[c][x["id"]] = x
    n_ok = sum(1 for v in fetched.values() if v)
    n_fail = len(fetched) - n_ok

    merged = {}
    for c in C.CATS:
        m = {}
        for i, x in old[c].items():
            m[i] = dict(x)
        for i, x in fresh[c].items():
            y = m.get(i, {"a": now, "s": 1 if i in static_ids else 0})
            y.update({"id": i, "t": x["t"], "c": x["c"], "p": x["p"] or y.get("p", 0)})
            if x["d"]:
                y["d"] = x["d"]
            y.setdefault("d", 0)
            y.setdefault("a", now)
            m[i] = y
        merged[c] = m
    for c in C.CATS:                                   # 長さを補う（タブ一覧/個別確認で分かっているもの）
        for i, y in merged[c].items():
            if i in dmap:
                y["d"] = dmap[i]

    def usable(x):                                     # 長さ不明でも「まだ調べられる」ものは候補に残す（最終出力では必ず除く）
        i = x["id"]
        if i in bad or i in live_ids or title_is_longform(x.get("t")):
            return False
        d = x.get("d") or 0
        return (MIN_SEC <= d < cap) if d else True

    # 長さ不明の動画を yt-dlp で個別に調べる。並びの上位（cap+20）に入る不明なものを、予算(RESOLVE_MAX)の範囲で何度か繰り返す。
    tried, budget, probed_n = set(), RESOLVE_MAX, 0
    for _round in range(8):
        unk = {}
        for c in C.CATS:
            items = [x for x in merged[c].values() if usable(x)]
            lst = order_cat(c, items, set(fresh[c]), pol[c], now)[: pol[c].get("cap", 60) + 20]
            for x in lst:
                if not x.get("d") and x["id"] not in tried:
                    rec = durs.get(x["id"])
                    if rec and rec[3] >= 5 and now - rec[2] < FAIL_RETRY_AFTER:
                        tried.add(x["id"])          # 何度も失敗した動画はしばらく休ませる（出力はされない）
                        continue
                    unk[x["id"]] = (rec[3] if rec else 0, -ts_of(x))
        ids = sorted(unk, key=lambda i: unk[i])[:budget]        # 失敗の少ないもの → 新しいものから
        if not ids:
            break
        res = durations(ids)
        budget -= len(ids)
        probed_n += len(ids)
        for i in ids:
            tried.add(i)
            dur, live = res.get(i, (None, False))
            rec = durs.get(i) or [0, 0, 0, 0]
            if live:
                durs[i] = [0, 1, now, 0]
                live_ids.add(i)
            elif dur:
                durs[i] = [int(dur), 0, now, 0]
                dmap[i] = int(dur)
            elif i in res:
                durs[i] = [0, 0, now, rec[3] + 1]       # 不明のまま記憶 → 次回また調べる（出力はしない）
        for c in C.CATS:
            for i, y in merged[c].items():
                if i in dmap:
                    y["d"] = dmap[i]
        if budget <= 0:
            break

    pool = {}
    for c in C.CATS:
        items = [x for x in merged[c].values() if x["id"] not in bad and x["id"] not in live_ids and is_ok(x, cap)]   # ★長さ不明は落とす
        pool[c] = order_cat(c, items, set(fresh[c]), pol[c], now)[: pol[c].get("cap", 60) + 20]

    # 埋め込み可否 (oEmbed): 最近確認済みの動画は飛ばす
    need = sorted({x["id"] for c in C.CATS for x in pool[c] if now - verified.get(x["id"], 0) > VERIFY_TTL})
    res = verify(need)
    checked = [r for r in res.values() if r is not None]
    fails = [i for i, r in res.items() if r is False]
    systemic = len(checked) >= 10 and len(fails) > len(checked) * 0.5     # 大半が不可 = 通信/ブロックの異常とみなし無視
    if systemic:
        log("oEmbed の結果が不自然（%d/%d件が不可）のため、除外を行いません" % (len(fails), len(checked)))
        fails = []
    for i, r in res.items():
        if r is True:
            verified[i] = now
    for i in fails:
        bad[i] = now
    dropped = set(fails)

    out = {}
    for c in C.CATS:
        lst = [x for x in pool[c] if x["id"] not in dropped][: cfg["policy"][c].get("cap", 60)]
        if not lst:                                        # 万一空なら標準リスト（これも厳格フィルタ済みのもの）を最低限として使う
            lst = [dict(x, p=0, a=0, s=1) for x in static[c] if is_ok(x, cap) and x["id"] not in bad and x["id"] not in live_ids]
        out[c] = lst
    alive = {x["id"] for c in C.CATS for x in out[c]}
    verified = {k: v for k, v in verified.items() if k in alive and now - v < 30 * 86400}
    # 長さの記憶: 出力中の動画 + ライブ判定 + 失敗中(再挑戦用)を優先して残す。上限 4000
    keep = sorted(durs.items(), key=lambda kv: (kv[0] in alive or kv[1][1] == 1, kv[1][2]))[-4000:]
    durs = dict(keep)
    old_ids = {x["id"] for c in C.CATS for x in old[c].values()}
    added = len({i for c in C.CATS for i in fresh[c]} - old_ids)
    n_unknown = sum(1 for c in C.CATS for x in merged[c].values() if not x.get("d") and usable(x))
    return {"videos": out, "verified": verified, "bad": bad, "durs": durs}, {"channels_ok": n_ok, "channels_failed": n_fail,
                                                              "new": added, "dropped": len(dropped), "probed": probed_n,
                                                              "probe_ok": _probe_stats["ok"], "probe_fail": _probe_stats["fail"],
                                                              "probe_err": _probe_stats["err"], "unknown_left": n_unknown}
