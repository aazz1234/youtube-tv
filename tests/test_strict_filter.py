# -*- coding: utf-8 -*-
"""厳格フィルタ（長さ不明/10分以上/ライブ配信アーカイブは videos.json に入れない）のテスト。yt-dlp は呼ばない（出力をスタブ）。
実行: python -m unittest discover -s tests -v"""
import json, os, sys, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import ytv_common as C
import ytv_core as K

NOW = 1_800_000_000
CAT = C.CATS[0]


def vid(n):
    return ("vid%08d" % n)[:11]


def cfg_one(cat=CAT, n=20, **kw):
    ch = {"name": "ch", "id": "UC" + "a" * 22, "n": n, "min": 0, "max": 599}
    ch.update(kw)
    return {"channels": {c: ([ch] if c == cat else []) for c in C.CATS},
            "policy": {c: {"cap": 50} for c in C.CATS}, "pick": {}, "categories": [], "max_duration": 600}


def empty_static():
    return {c: [] for c in C.CATS}


def run(entries, durations=None, cache=None, cat=CAT, **kw):
    cfg = cfg_one(cat, **kw)
    ents = [dict({"p": NOW - 100 * i, "t": "title %d" % i, "d": 0}, **e) for i, e in enumerate(entries)]
    stub = durations or (lambda ids: {})
    out, stats = K.compose(cfg, empty_static(), cache, {cfg["channels"][cat][0]["id"]: ents}, NOW,
                           verify=lambda ids: {i: True for i in ids}, durations=stub)
    return out, stats


def ids_of(out, cat=CAT):
    return [x["id"] for x in out["videos"][cat]]


class StrictFilter(unittest.TestCase):
    def test_unknown_duration_dropped(self):
        out, _ = run([{"id": vid(1), "d": 0}])           # RSS 由来: 長さ不明 → 個別確認も失敗(スタブは何も返さない)
        self.assertEqual(ids_of(out), [])
        # 失敗は「不明」として記憶され、次回再挑戦される（出力はされない）
        self.assertEqual(out["durs"][vid(1)][0], 0) if vid(1) in out["durs"] else None

    def test_36000s_dropped(self):
        out, _ = run([{"id": vid(2), "d": 36000}, {"id": vid(3), "d": 300}])
        self.assertEqual(ids_of(out), [vid(3)])

    def test_300s_kept_and_boundaries(self):
        out, _ = run([{"id": vid(4), "d": 300}, {"id": vid(5), "d": 599}, {"id": vid(6), "d": 600}, {"id": vid(7), "d": 19}, {"id": vid(8), "d": 20}])
        self.assertEqual(sorted(ids_of(out)), sorted([vid(4), vid(5), vid(8)]))

    def test_probe_resolves_unknown(self):
        calls = []

        def stub(ids):
            calls.extend(ids)
            return {vid(10): (240, False), vid(11): (36000, False), vid(12): (None, True), vid(13): (None, False)}
        out, st = run([{"id": vid(10)}, {"id": vid(11)}, {"id": vid(12)}, {"id": vid(13)}], durations=stub)
        self.assertEqual(ids_of(out), [vid(10)])
        self.assertEqual(out["durs"][vid(10)][0], 240)
        self.assertEqual(out["durs"][vid(11)][0], 36000)
        self.assertEqual(out["durs"][vid(12)][1], 1)         # ライブ → 恒久的に除外
        self.assertEqual(out["durs"][vid(13)][3], 1)         # 失敗 → 1回失敗として記憶

    def test_live_status_post_live_dropped_in_flat(self):
        d = {"channel_id": "UC" + "a" * 22, "entries": [
            {"id": vid(20), "title": "a", "duration": 300, "live_status": "post_live"},
            {"id": vid(21), "title": "b", "duration": 300, "live_status": "is_live"},
            {"id": vid(22), "title": "c", "duration": 300, "live_status": "is_upcoming"},
            {"id": vid(23), "title": "d", "duration": 300, "was_live": True},
            {"id": vid(24), "title": "e", "duration": 300, "live_status": None},
            {"id": vid(25), "title": "f"}]}
        ents = K._flat_entries(d, "UC" + "a" * 22)
        self.assertEqual([e["id"] for e in ents], [vid(24), vid(25)])
        self.assertEqual(ents[1]["d"], 0)

    def test_probe_post_live_dropped(self):
        # 個別確認で live_status=post_live / was_live=true と分かったものは、短くても除外
        out, _ = run([{"id": vid(30)}], durations=lambda ids: {vid(30): (None, True)})
        self.assertEqual(ids_of(out), [])

    def test_probe_parses_ytdlp_json(self):
        class R:
            def __init__(self, o): self.stdout, self.stderr, self.returncode = o, "", 0
        outs = {"a": '{"duration": 300, "live_status": "not_live", "was_live": false}',
                "b": '{"live_status": "is_live", "was_live": false}',
                "c": '{"duration": 36000, "live_status": "was_live", "was_live": true}',
                "d": '{"duration": 36000, "live_status": "not_live", "was_live": false}'}
        orig_run, orig_cmd = K.subprocess.run, K.ytdlp_cmd
        K.ytdlp_cmd = lambda: ["yt-dlp"]
        try:
            for k, exp in (("a", (300, False)), ("b", (None, True)), ("c", (None, True)), ("d", (36000, False))):
                K.subprocess.run = lambda cmd, **kw: R(outs[k])
                self.assertEqual(K.probe_duration("x"), exp)
        finally:
            K.subprocess.run, K.ytdlp_cmd = orig_run, orig_cmd

    def test_old_entries_without_d_purged(self):
        cache = {"generated": "2026-01-01 00:00", "videos": {c: [] for c in C.CATS}, "verified": {}, "bad": {}, "durs": {}}
        cache["videos"][CAT] = [{"id": vid(40), "t": "x", "c": "ch", "d": 0}, {"id": vid(41), "t": "y", "c": "ch", "d": 36000},
                                {"id": vid(42), "t": "z", "c": "ch"}, {"id": vid(43), "t": "ok", "c": "ch", "d": 200}]
        out, _ = run([], cache=cache)
        self.assertEqual(ids_of(out), [vid(43)])

    def test_static_unknown_purged_and_never_fallback(self):
        cfg = cfg_one()
        static = empty_static()
        static[CAT] = [{"id": vid(50), "t": "s", "c": "ch", "d": 0}, {"id": vid(51), "t": "s", "c": "ch", "d": 99999}]
        out, _ = K.compose(cfg, static, None, {cfg["channels"][CAT][0]["id"]: []}, NOW, verify=lambda ids: {i: True for i in ids},
                           durations=lambda ids: {})
        self.assertEqual(ids_of(out), [])

    def test_title_regex(self):
        for t in ["【ライブ配信】作業用BGM", "【24時間】ライブ", "睡眠用BGM 8時間", "生配信アーカイブ", "【3時間耐久】", "ニュースライブ 夜", "【LIVE】速報"]:
            self.assertTrue(K.title_is_longform(t), t)
        for t in ["米津玄師 - Lemon MV", "新作映画 予告編", "24時間前の出来事", "普通のニュース 5月1日", "東京の街歩き"]:
            self.assertFalse(K.title_is_longform(t), t)
        out, _ = run([{"id": vid(60), "d": 300, "t": "【ライブ配信】アーカイブ"}, {"id": vid(61), "d": 300, "t": "ふつうの動画"}])
        self.assertEqual(ids_of(out), [vid(61)])

    def test_is_ok(self):
        self.assertFalse(K.is_ok({"id": "x", "d": 0, "t": "a"}))
        self.assertFalse(K.is_ok({"id": "x", "t": "a"}))
        self.assertFalse(K.is_ok({"id": "x", "d": 600, "t": "a"}))
        self.assertTrue(K.is_ok({"id": "x", "d": 599, "t": "a"}))
        self.assertTrue(K.is_ok({"id": "x", "d": 300, "t": "a"}))

    def test_committed_videos_json_is_strict(self):
        p = os.path.join(HERE, "videos.json")
        data = json.load(open(p, encoding="utf-8"))["videos"]
        for c, lst in data.items():
            for x in lst:
                self.assertTrue(20 <= (x.get("d") or 0) < 600, (c, x))


if __name__ == "__main__":
    unittest.main()
