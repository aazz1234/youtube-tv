#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""動画リスト(videos.json)を更新するスクリプト。HTTPサーバ不要で、GitHub Actions からも手元からも動きます。

  python refresh_site.py             # 更新して videos.json / state.json を書く
  python refresh_site.py --dry-run   # 結果を表示するだけ（何も書かない）

流れ:  channels.json の各チャンネル → RSS → (だめなら) yt-dlp → (どちらもだめなら) 前回の結果を保持
       【厳格】長さが「分かっている」 20秒以上 max_duration_sec(600秒)【未満】で、ライブ/配信アーカイブでない動画だけを videos.json に残す。
       長さは yt-dlp の /videos タブ一覧(duration)か、個別確認(1回の更新で最大 RESOLVE_MAX=150本、結果は state.json の durs に記憶)で得る。
       分からない動画は出力しない（次回の更新で再挑戦）。埋め込み可否は oEmbed で確認。
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

K.RESOLVE_MAX = int(os.environ.get("RESOLVE_MAX", "150"))      # 1回の更新で yt-dlp で個別に長さを調べる動画の上限（残りは次回。45分のタイムアウト内）
MIN_RATIO = 0.5            # 新しいリストが前回の何割未満なら書かないか


def gha(kind, msg):
    """GitHub Actions のログ注釈（手元ではただの表示）"""
    print("::%s::%s" % (kind, msg), flush=True)


def count(videos):
    return sum(len(v) for v in videos.values())


def count_valid(videos, cap):
    """長さが分かっていて上限未満の動画の本数（前回比の安全装置に使う。長さ不明の古い動画は数えない）"""
    return sum(1 for v in videos.values() for x in v if K.is_ok(x, cap))


def assert_strict(videos, cap):
    bad = [(c, x.get("id")) for c, v in videos.items() for x in v if not K.is_ok(x, cap)]
    if bad:
        raise RuntimeError("厳格フィルタに通らない動画が残っています: %s" % bad[:5])


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
    cap = cfg.get("max_duration") or C.MAX_DURATION_DEFAULT
    prev_total = count_valid(cache["videos"], cap) if cache else count_valid(static, cap)   # 長さ不明の分は数えない（初回の一掃で減っても止まらないように）
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
    K.log("新着%d本 / 合計 %d本（前回の有効分 %d本） " % (stats["new"], total, prev_total) +
          " ".join("%s=%d" % (c, len(new["videos"][c])) for c in C.CATS))
    K.log("長さの個別確認: %d本 (成功%d / 失敗%d) 未確認のまま残り %d本 %s" % (stats["probed"], stats["probe_ok"], stats["probe_fail"],
          stats["unknown_left"], ("最後のエラー: " + stats["probe_err"]) if stats["probe_err"] else ""))
    assert_strict(new["videos"], cap)
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
    pub = slim(out["videos"])
    write_json(os.path.join(HERE, "videos.json"), {"generated": out["generated"], "videos": pub}, indent=None)
    for f in (K.HTML_FILE, K.NATIVE_FILE):            # 内蔵フォールバックリストも厳格フィルタ済みの内容に揃える
        try:
            if C.sync_embedded(f, out["generated"], pub):
                K.log("内蔵リストを更新:", os.path.basename(f))
        except Exception as e:
            K.log("内蔵リストの更新に失敗:", os.path.basename(f), e)
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
