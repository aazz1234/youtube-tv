// native.html: 長さ不明(d無し/0)・600秒以上の動画をパートに入れないことのテスト（jsdom）。 実行: npm test
const fs = require('fs'), path = require('path'), assert = require('assert');
const { JSDOM } = require('jsdom');
const HTML = fs.readFileSync(path.join(__dirname, '../../native.html'), 'utf8');
const URL_ = 'https://aazz1234.github.io/youtube-tv/native.html';
const mk = n => ('t' + String(n).padStart(10, '0'));      // 11文字のID

function open_(fetchImpl) {
  return new JSDOM(HTML, { url: URL_, runScripts: 'dangerously', pretendToBeVisual: true,
    beforeParse(w) { w.fetch = fetchImpl; w.open = () => null; } });
}
function idsOf(dom) {
  const ids = [];
  dom.window.document.querySelectorAll('#parts a').forEach(a => { ids.push(...a.href.split('video_ids=')[1].split(',')); });
  return ids;
}
const wait = ms => new Promise(r => setTimeout(r, ms));
const ok = j => () => Promise.resolve({ ok: true, json: () => Promise.resolve(j) });

(async () => {
  // 1) 内蔵（フォールバック）リストだけのとき（videos.json が取れない）: 全部 d が分かっていて 600 未満
  {
    const m = HTML.match(/\/\*VIDEOS_START\*\/\s*const DATA=(.*?);\s*\/\*VIDEOS_END\*\//s);
    const DATA = JSON.parse(m[1]);
    const dmap = {}; let nAll = 0, nBad = 0;
    for (const c in DATA.videos) for (const v of DATA.videos[c]) { dmap[v.id] = Number(v.d) || 0; nAll++; if (!(v.d > 0 && v.d < 600)) nBad++; }
    assert.strictEqual(nBad, 0, 'native.html の内蔵リストに長さ不明/長すぎる動画: ' + nBad);
    const dom = open_(() => Promise.reject(new Error('offline')));
    await wait(300);
    const ids = idsOf(dom);
    assert.ok(ids.length > 0 && ids.length <= nAll);
    for (const id of ids) assert.ok(dmap[id] > 0 && dmap[id] < 600, 'bad id in fallback parts ' + id);
    console.log('ok fallback: ', ids.length, '本 / 内蔵', nAll, '本（すべて 0<d<600）');
    dom.window.close();
  }
  // 2) サーバのリスト: d無し・0・文字列・36000・600・599 が混ざる
  {
    const cat = JSON.parse(HTML.match(/const CFG=(\{.*?\});\/\*CATS_END/s)[1]).cats[0].id;
    const list = [
      { id: mk(1), t: 'ok-300', c: 'x', d: 300 }, { id: mk(2), t: 'no-d', c: 'x' }, { id: mk(3), t: 'zero', c: 'x', d: 0 },
      { id: mk(4), t: 'long', c: 'x', d: 36000 }, { id: mk(5), t: 'edge600', c: 'x', d: 600 }, { id: mk(6), t: 'edge599', c: 'x', d: 599 },
      { id: mk(7), t: 'nan', c: 'x', d: 'abc' }, { id: mk(8), t: 'null', c: 'x', d: null }, { id: mk(9), t: 'neg', c: 'x', d: -5 },
    ];
    const dom = open_(ok({ generated: 't', videos: { [cat]: list } }));
    await wait(400);
    const ids = idsOf(dom).sort();
    assert.deepStrictEqual(ids, [mk(1), mk(6)].sort(), '残るのは d が分かっていて 600 未満のものだけ: ' + ids);
    console.log('ok server list filtering:', ids.join(','));
    dom.window.close();
  }
  // 3) サーバのリストが全部 長さ不明 → 空扱いで拒否され、内蔵リスト(厳格)のまま
  {
    const cat = JSON.parse(HTML.match(/const CFG=(\{.*?\});\/\*CATS_END/s)[1]).cats[0].id;
    const dom = open_(ok({ generated: 't', videos: { [cat]: [{ id: mk(1), t: 'a', c: 'x' }, { id: mk(2), t: 'b', c: 'x', d: 36000 }] } }));
    await wait(400);
    const ids = idsOf(dom);
    assert.ok(!ids.includes(mk(1)) && !ids.includes(mk(2)));
    assert.ok(ids.length > 0, '内蔵リストが使われる');
    console.log('ok all-unknown server list rejected, fallback used:', ids.length);
    dom.window.close();
  }
  console.log('native tests passed');
})().catch(e => { console.error(e); process.exit(1); });
