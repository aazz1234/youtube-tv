#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""動画リスト(videos.json)を更新するスクリプト。HTTPサーバ不要で、GitHub Actions からも手元からも動きます。

  python refresh_site.py             # 更新して videos.json / state.json を書く
  python refresh_site.py --dry-run   # 結果を表示するだけ（何も書かない）

流れ:  channels.json の各チャンネル → RSS → (だめなら) yt-dlp → (どちらもだめなら) 前回の結果を保持
       長さは yt-dlp で確認（全ジャンル max_duration_sec=600秒【未満】）、埋め込み可否は oEmbed で確認。
安全装置: 1チャンネルも取得できなかった / 新しいリストが前回の50%未満 / エラー のときは何も書きません
          （終了コード: 0=更新した or 変更なし, 1=失敗して何も書かなかった）。
"""
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("TZ", "Asia/Tokyo")          # 表示用の時刻は日本時間
try:
    time.tzset()
except AttributeError:
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ytv_common as C
import ytv_core as K

K.RESOLVE_MAX = int(os.environ.get("RESOLVE_MAX", "120"))      # 1回の更新で yt-dlp で長さを調べる動画の上限（ローカル版は40）
MIN_RATIO = 0.5            # 新しいリストが前回の何割未満なら書かないか


def gha(kind, msg):
    """GitHub Actions のログ注釈（手元ではただの表示）"""
    print("::%s::%s" % (kind, msg), flush=True)


def count(videos):
    return sum(len(v) for v in videos.values())


def slim(videos):
    return {c: [{k: v[k] for k in ("id", "t", "c", "d") if k in v} for v in videos.get(c, [])] for c in C.CATS}


def write_json(path, obj, indent=None):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":") if indent is None else None, indent=indent)
        f.write("\n")
    os.replace(tmp, path)


def main(argv):
    dry = "--dry-run" in argv
    t0 = time.time()
    cfg = C.load_config()
    gen_static, static = K.load_static()
    cache = K.load_cache()
    prev_total = count(cache["videos"]) if cache else count(static)
    cids = sorted({ch["id"] for c in C.CATS for ch in cfg["channels"][c]})
    fetched, srcs = {}, {"rss": 0, "ytdlp": 0, "none": 0}

    def one(cid):
        return cid, K.fetch_channel(cid)

    with ThreadPoolExecutor(max_workers=K.FEED_WORKERS) as ex:
        for cid, (src, ents) in ex.map(one, cids):
            fetched[cid] = ents
            srcs[src or "none"] += 1
    K.log("取得: RSS %d / yt-dlp %d / 失敗 %d (全%dチャンネル)" % (srcs["rss"], srcs["ytdlp"], srcs["none"], len(cids)))
    if srcs["rss"] + srcs["ytdlp"] == 0:
        gha("warning", "全チャンネルの取得に失敗しました（RSS も yt-dlp もだめ）。videos.json は更新しません。")
        return 1
    now = int(time.time())
    new, stats = K.compose(cfg, static, cache, fetched, now, static_gen=gen_static)
    total = count(new["videos"])
    K.log("新着%d本 / 合計 %d本（前回 %d本） " % (stats["new"], total, prev_total) +
          " ".join("%s=%d" % (c, len(new["videos"][c])) for c in C.CATS))
    if prev_total and total < prev_total * MIN_RATIO:
        gha("error", "新しいリストが前回の%d%%未満（%d本 < %d本）のため、書き込みません。" % (MIN_RATIO * 100, total, prev_total))
        return 1
    if not total:
        gha("error", "リストが空のため書き込みません。")
        return 1
    out = {"generated": K.now_str(now), "last_success": now, "videos": new["videos"],
           "verified": new["verified"], "bad": new["bad"], "durs": new["durs"]}
    if dry:
        K.log("--dry-run: 書き込みません（%d秒）" % (time.time() - t0))
        return 0
    write_json(os.path.join(HERE, "state.json"), out)
    write_json(os.path.join(HERE, "videos.json"), {"generated": out["generated"], "videos": slim(out["videos"])}, indent=None)
    for f in (K.HTML_FILE, K.NATIVE_FILE):            # channels.json のジャンル定義が変わっていたらページにも反映
        try:
            if C.sync_page(f):
                K.log("ジャンル一覧を channels.json に合わせて更新:", os.path.basename(f))
        except Exception as e:
            K.log("ジャンル一覧の更新に失敗:", os.path.basename(f), e)
    K.log("videos.json を更新しました（%d秒）" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:
        gha("error", "更新中にエラー: %s: %s（何も書き込んでいません）" % (type(e).__name__, e))
        sys.exit(1)
