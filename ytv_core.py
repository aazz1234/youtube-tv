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


def fetch_ytdlp(cid):
    """yt-dlp で最新動画一覧 → [{id,t,p,d}]。使えない/失敗は None。連続失敗が続いたら以後あきらめる(無駄な待ちを避ける)。"""
    cmd = ytdlp_cmd()
    if not cmd:
        return None
    with _yt_lock:
        if _yt_state["ok"] == 0 and _yt_state["fail"] >= 3:
            return None
    try:
        r = subprocess.run(cmd + ["-q", "--no-warnings", "--flat-playlist", "--playlist-end", "15", "--dump-single-json",
                                  "--extractor-args", "youtube:lang=ja", "https://www.youtube.com/channel/%s/videos" % cid],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        d = json.loads(r.stdout, strict=False) if r.stdout.strip() else None
    except Exception:
        d = None
    out = []
    if d and d.get("channel_id") == cid:
        for e in d.get("entries") or []:
            if not e or not ID_RE.match(str(e.get("id", ""))) or e.get("live_status") in ("is_live", "is_upcoming", "post_live"):
                continue
            p = e.get("timestamp") or 0
            if not p and e.get("upload_date"):
                p = parse_iso("%s-%s-%sT00:00:00+09:00" % (e["upload_date"][:4], e["upload_date"][4:6], e["upload_date"][6:8]))
            out.append({"id": e["id"], "t": e.get("title") or e["id"], "p": int(p or 0), "d": int(e.get("duration") or 0)})
    with _yt_lock:
        _yt_state["ok" if out else "fail"] += 1
    return out or None


def fetch_channel(cid):
    """(source, entries)。source: 'rss' | 'ytdlp' | None"""
    try:
        r = fetch_rss(cid)
        if r:
            return "rss", r
    except Exception as e:
        log("RSS取得エラー", cid, e)
    try:
        r = fetch_ytdlp(cid)
        if r:
            return "ytdlp", r
    except Exception as e:
        log("yt-dlp取得エラー", cid, e)
    return None, None


def select_for_channel(entries, ch, cat, pol, now):
    """チャンネル設定(include/exclude/長さ)とカテゴリ方針で絞り、新しい順に最大 n 本。"""
    inc, exc = C.compile_or_none(ch.get("include")), C.compile_or_none(ch.get("exclude"))
    max_age = pol.get("max_age_days")
    res = []
    for e in entries:
        t = e["t"]
        if inc and not inc.search(t):
            continue
        if exc and exc.search(t):
            continue
        d = e.get("d") or 0
        if d and not (ch["min"] <= d <= ch["max"]):
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


# ------------------------------------------------------------------ 長さの確認 (RSSには長さが無い)
RESOLVE_MAX = 40            # 1回の更新で長さを調べる動画の上限（YouTube に優しく）
RESOLVE_WORKERS = 2


def probe_duration(vid):
    """yt-dlp で (長さ秒 or None, ライブ/配信予定か)。yt-dlp が無い/失敗は (None, False)。"""
    cmd = ytdlp_cmd()
    if not cmd:
        return None, False
    try:
        r = subprocess.run(cmd + ["-q", "--no-warnings", "--skip-download", "--no-playlist", "--print", "%(duration)s|%(live_status)s",
                                  "https://www.youtube.com/watch?v=" + vid],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40)
        line = (r.stdout.strip().splitlines() or [""])[-1]
        dur, _, live = line.partition("|")
        return (int(float(dur)) if dur not in ("", "NA", "None") else None), live in ("is_live", "is_upcoming", "post_live")
    except Exception:
        return None, False
    finally:
        time.sleep(0.4)


def resolve_durations(ids):
    """{id: (長さ秒 or None, ライブか)}。yt-dlp があるときだけ、最大 RESOLVE_MAX 本を調べる。"""
    ids = list(ids)[:RESOLVE_MAX]
    if not ids or not ytdlp_cmd():
        return {}
    with ThreadPoolExecutor(max_workers=RESOLVE_WORKERS) as ex:
        return dict(zip(ids, ex.map(probe_duration, ids)))


# ------------------------------------------------------------------ リスト合成
def ts_of(v):
    return v.get("p") or v.get("a") or 0


def compose(cfg, static, cache, fetched, now, verify=verify_ids, static_gen="", durations=resolve_durations):
    """新しいキャッシュ内容を作る。fetched: {cid: entries or None}。失敗しても既存リストは消さない。
    長さが max_duration 以上と分かっている動画は、標準リスト・前回の結果・新着のどれからも除く。"""
    pol = cfg["policy"]
    cap = cfg.get("max_duration") or C.MAX_DURATION_DEFAULT
    durs = {k: int(v) for k, v in ((cache or {}).get("durs") or {}).items() if isinstance(v, (int, float))}
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
            for x in select_for_channel(ents, ch, c, pol[c], now):
                if not x["d"] and durs.get(x["id"]):
                    x["d"] = durs[x["id"]]
                if x["id"] in bad or owner.get(x["id"], c) != c or (x["d"] and x["d"] >= cap):
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

    # カテゴリごとの方針で並べて上限で切る
    pool = {}
    for c in C.CATS:
        p = pol[c]
        items = list(merged[c].values())
        items = [x for x in items if x["id"] not in bad and not (x.get("d") and x["d"] >= cap)]
        if c == "news":
            max_age = (p.get("max_age_days") or 3) * 86400
            freshn = sorted([x for x in items if ts_of(x) and now - ts_of(x) <= max_age], key=lambda x: -ts_of(x))
            fid = {x["id"] for x in freshn}
            rest = sorted([x for x in items if x["id"] not in fid], key=lambda x: -ts_of(x))
            lst = freshn
            if len(lst) < p.get("min_keep", 15):        # 新しいものが少なければ、古いものでも最低本数は確保
                lst = lst + rest[: p.get("min_keep", 15) - len(lst)]
        elif c != "mv":          # docs ほか: 新着 → 標準リスト → その他（新しい順）
            newf = sorted([x for x in items if x["id"] in fresh[c]], key=lambda x: -ts_of(x))
            st = [x for x in items if x.get("s") and x["id"] not in fresh[c]]
            oth = sorted([x for x in items if not x.get("s") and x["id"] not in fresh[c]], key=lambda x: -ts_of(x))
            lst = newf + st + oth
        else:   # mv: 厳選(標準)は全部残し、新着+既存の新しい順で上限まで
            st = [x for x in items if x.get("s")]
            oth = sorted([x for x in items if not x.get("s")], key=lambda x: -ts_of(x))
            lst = st + oth
        pool[c] = lst[: p.get("cap", 60) + 20]

    # 長さが分からない動画（RSS由来）は yt-dlp があれば調べ、上限以上・ライブなら外す（結果は durs に記憶）
    unk_items = {x["id"]: ts_of(x) for c in C.CATS for x in pool[c] if not x.get("d") and x["id"] not in durs}
    unk = sorted(unk_items, key=lambda i: -unk_items[i])              # 新しいものから調べる
    probed = durations(unk) if unk else {}
    for i, (dur, live) in probed.items():
        if live:
            bad[i] = now
        elif dur:
            durs[i] = dur
    for c in C.CATS:
        for x in pool[c]:
            if not x.get("d") and durs.get(x["id"]):
                x["d"] = durs[x["id"]]
        pool[c] = [x for x in pool[c] if x["id"] not in bad and not (x.get("d") and x["d"] >= cap)]

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
        if not lst:                                        # 万一空なら標準リストを最低限として使う
            lst = [dict(x, p=0, a=0, s=1) for x in static[c] if not (x.get("d") and x["d"] >= cap)]
        out[c] = lst
    alive = {x["id"] for c in C.CATS for x in out[c]}
    verified = {k: v for k, v in verified.items() if k in alive and now - v < 30 * 86400}
    durs = dict(list(durs.items())[-3000:])                           # 長さの記憶（上限以上だったものも覚えておき、再確認しない）
    old_ids = {x["id"] for c in C.CATS for x in old[c].values()}
    added = len({i for c in C.CATS for i in fresh[c]} - old_ids)
    return {"videos": out, "verified": verified, "bad": bad, "durs": durs}, {"channels_ok": n_ok, "channels_failed": n_fail,
                                                              "new": added, "dropped": len(dropped)}
