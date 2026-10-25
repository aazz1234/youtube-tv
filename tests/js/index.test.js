// index.html: 長すぎる/ライブの動画のスキップ（再生状態に関係なく getDuration()>=上限 でスキップ）と、長さが分かる動画の優先のテスト（jsdom + 偽 YT プレイヤー）。
const fs = require('fs'), path = require('path'), assert = require('assert');
const { JSDOM } = require('jsdom');
const HTML_FULL = fs.readFileSync(process.env.INDEX_HTML || path.join(__dirname, '../../index.html'), 'utf8');
const HTML = HTML_FULL.replace(/(\/\*VIDEOS_START\*\/)\s*const DATA=.*?;\s*(\/\*VIDEOS_END\*\/)/s, '$1const DATA={"generated":"","videos":{}};$2');   // 内蔵リストは空にして、テスト用のリストだけで動かす
const CFG = JSON.parse(HTML_FULL.match(/const CFG=(\{.*?\});\/\*CATS_END/s)[1]);
const CAT = CFG.cats[0].id;
const mk = n => ('t' + String(n).padStart(10, '0'));
const wait = ms => new Promise(r => setTimeout(r, ms));

/* info(id) -> {state, dur, live} を返す関数を渡す。loadVideoById の履歴を返す */
function boot(list, info) {
  const loads = [];
  const dom = new JSDOM(HTML, { url: 'https://aazz1234.github.io/youtube-tv/', runScripts: 'dangerously', pretendToBeVisual: true,
    beforeParse(w) {
      w.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({ generated: 't', videos: { [CAT]: list } }) });
      w.HTMLElement.prototype.scrollIntoView = () => {};
      class Player {
        constructor(el, o) { this.o = o.events; this.id = null; const m = /\/embed\/([\w-]+)/.exec(el.src || ''); setTimeout(() => { if (m) this.loadVideoById(m[1]); this.o.onReady({}); }, 5); }
        loadVideoById(id) { this.id = id; loads.push(id); const s = info(id).state; setTimeout(() => this.o.onStateChange({ data: s }), 20); }
        getDuration() { return this.id ? info(this.id).dur : 0; }
        getVideoData() { return { video_id: this.id, isLive: !!(this.id && info(this.id).live) }; }
        getPlayerState() { return this.id ? info(this.id).state : -1; }
        getCurrentTime() { return 0; } isMuted() { return true; } mute() {} unMute() {} setVolume() {} playVideo() {} pauseVideo() {} seekTo() {}
      }
      w.YT = { Player };
    } });
  // iframe_api は読み込まれないので、準備完了を手動で通知
  return { dom, loads };
}
async function start(b) {
  const w = b.dom.window;
  await wait(200);                                  // videos.json 取り込み
  w.onYouTubeIframeAPIReady && w.onYouTubeIframeAPIReady();
  w.document.getElementById('start').click();
}

(async () => {
  // 1) 長さ36000秒の動画は、CUED(5) / UNSTARTED(-1) / BUFFERING(3) / PLAYING(1) のどの状態でも、約2〜3秒でスキップされる
  // LONG が確実に最初に選ばれるよう、そのリストだけにして検証
  for (const stuck of [5, -1, 3, 1]) {
    const LONG = mk(1), OK = mk(2);
    let seq = 0;
    const b = boot([{ id: LONG, t: 'long', c: 'x' }], id => ({ state: stuck, dur: 36000 }));
    await start(b); await wait(3200);
    assert.strictEqual(b.loads[0], LONG);
    const msg = b.dom.window.document.getElementById('msgbox').textContent;   // 他に動画が無い → スキップされて「再生できる動画がありません」
    assert.ok(/再生できる動画がありません/.test(msg), 'state ' + stuck + ': 長い動画がスキップされていない ' + b.loads);
    console.log('ok long video skipped in state', stuck, 'loads=', b.loads.length);
    b.dom.window.close();
  }
  // 2) ライブ(isLive)は cued のままでもスキップ（そのリストだけ → メッセージ表示、他に短い動画があればそちらへ）
  {
    const LIVE = mk(1);
    const b = boot([{ id: LIVE, t: 'live', c: 'x', d: 120 }], () => ({ state: 5, dur: 0, live: true }));
    await start(b); await wait(3500);
    assert.strictEqual(b.loads[0], LIVE);
    assert.ok(/再生できる動画がありません/.test(b.dom.window.document.getElementById('msgbox').textContent), 'live skipped (cued) ' + b.loads);
    console.log('ok live skipped while cued');
    b.dom.window.close();
  }
  // 3) 通常の短い動画はスキップされない（2.5秒経っても同じ動画のまま）
  {
    const b = boot([{ id: mk(1), t: 'a', c: 'x', d: 200 }], () => ({ state: 1, dur: 200 }));
    await start(b); await wait(3000);
    assert.strictEqual(b.loads.length, 1, 'short video must not be skipped: ' + b.loads);
    console.log('ok short video kept');
    b.dom.window.close();
  }
  // 4) 長さ不明の動画より、長さが分かる動画が優先される（known が1本でもあれば unknown は選ばれない）
  {
    const list = [{ id: mk(1), t: 'unk', c: 'x' }, { id: mk(2), t: 'unk', c: 'x' }, { id: mk(3), t: 'known', c: 'x', d: 100 }];
    let bad = 0;
    for (let k = 0; k < 6; k++) {
      const b = boot(list, () => ({ state: 1, dur: 100 }));
      await start(b); await wait(60);
      if (b.loads[0] !== mk(3)) bad++;
      b.dom.window.close();
    }
    assert.strictEqual(bad, 0, '長さが分かる動画を優先');
    console.log('ok prefer known duration');
  }
  // 5) 600秒以上と分かっているエントリは cleanList で除外（d>=600）
  {
    const b = boot([{ id: mk(1), t: 'long', c: 'x', d: 700 }, { id: mk(2), t: 'ok', c: 'x', d: 100 }], () => ({ state: 1, dur: 100 }));
    await start(b); await wait(100);
    assert.strictEqual(b.loads[0], mk(2));
    b.dom.window.close();
    console.log('ok d>=600 filtered');
  }
  console.log('index tests passed');
  process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
