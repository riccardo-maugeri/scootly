#!/usr/bin/env node
// Collaudo: prove automatiche da passare prima di pubblicare dati o motore nuovi.
// Uso: node tools/collaudo.js [dati.json]   (predefinito: data/strade.json)
// Esce con errore (codice 1) se anche una sola prova fallisce.
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
const m = html.match(/<script>\s*(\/\/ Motore di calcolo percorsi[\s\S]*?)<\/script>/);
const mod = { exports: {} };
new Function('module', m[1])(mod);
const { Router } = mod.exports;

const file = process.argv[2] || path.join(ROOT, 'data', 'strade.json');
const d = JSON.parse(fs.readFileSync(file, 'utf8'));
const R = new Router(d);

let failed = 0;
function check(ok, msg) {
  console.log((ok ? '  ok   ' : '  NO   ') + msg);
  if (!ok) failed++;
}
const addr = (street, num) => {
  const a = (d.addr[street] || []).find(x => x[0] === num);
  if (!a) throw new Error(`indirizzo mancante nei dati: ${street} ${num}`);
  return [a[1], a[2]];
};
const wayEdges = osm => { const wi = d.ways.findIndex(w => w[0] === osm); return d.edges.map((e, i) => e[2] === wi ? i : -1).filter(i => i >= 0); };
const at = t => R.setTime(new Date(t));
const km = r => (r.length / 1000).toFixed(2) + ' km';
const legal = r => r.edges.every(de => R.allowed(de >> 1, de & 1, 'moto'));

// ---------- 1. dati completi ----------
console.log('1. Dati');
check(d.ways.length > 14000, `vie: ${d.ways.length} (almeno 14000)`);
check(d.edges.length > 16000, `tratti: ${d.edges.length} (almeno 16000)`);
check(Object.values(d.addr).flat().length > 20000, 'civici: almeno 20000');
check((d.poi || []).length > 5000, `luoghi di interesse: ${(d.poi || []).length} (almeno 5000)`);
if (d.meta && d.meta.osm) {
  const days = (Date.now() - new Date(d.meta.osm)) / 864e5;
  check(days < 30, `mappa OSM del ${d.meta.osm.slice(0, 10)} (meno di 30 giorni fa)`);
}

// ---------- 2. tragitti di riferimento ----------
// I tragitti personali (es. casa <-> ufficio) NON stanno nel progetto, che e' pubblico: si leggono da
// tools/tragitti_privati.json (escluso da git) o dalla variabile SCOOTLY_TRAGITTI. Senza, si usa un tragitto di prova.
const TRAGITTI = (() => {
  try {
    if (process.env.SCOOTLY_TRAGITTI) return JSON.parse(process.env.SCOOTLY_TRAGITTI).tragitti;
    const f = path.join(__dirname, 'tragitti_privati.json');
    if (fs.existsSync(f)) return JSON.parse(fs.readFileSync(f, 'utf8')).tragitti;
  } catch (e) { console.log('  (tragitti privati non leggibili: uso quelli di prova)'); }
  return [{ nome: 'prova', da: ['Corso Magenta', '50'], a: ['Viale Piave', '1'], max_m: 4000 }];
})();
console.log(`2. Tragitti di riferimento (${TRAGITTI.length}), andata e ritorno a quattro orari`);
for (const tr of TRAGITTI) {
  const A1 = addr(...tr.da), B1 = addr(...tr.a);
  for (const t of ['2026-10-20T08:15', '2026-10-20T18:30', '2026-10-24T11:00', '2026-10-20T23:30']) {
    at(t);
    const a = R.route(A1, B1, 'moto'), b = R.route(B1, A1, 'moto');
    check(a && a.length < tr.max_m && legal(a), `${tr.nome} ${t} andata: ${a ? km(a) : 'nessun percorso'} (massimo ${tr.max_m} m, solo strade permesse)`);
    check(b && b.length < tr.max_m && legal(b), `${tr.nome} ${t} ritorno: ${b ? km(b) : 'nessun percorso'} (massimo ${tr.max_m} m, solo strade permesse)`);
  }
}
const CASA = addr(...TRAGITTI[0].da);

// ---------- 3. regole note ----------
console.log('3. Regole note');
const garibaldi = wayEdges(4877686);
if (garibaldi.length) check(garibaldi.some(e => R.allowed(e, 0, 'moto') || R.allowed(e, 1, 'moto')), 'Corso Garibaldi aperto alle moto (ZTL con deroga, regola del Comune)');
const ceneri = wayEdges(53568989);
if (ceneri.length) {
  at('2026-10-20T12:00'); check(R.allowed(ceneri[0], 0, 'moto') > 0, 'Sopraelevata Monte Ceneri aperta alle 12');
  at('2026-10-20T23:00'); check(R.allowed(ceneri[0], 0, 'moto') === 0, 'Sopraelevata Monte Ceneri chiusa alle 23');
}
at('2026-10-20T12:00');
const pd = (d.addr['Piazza del Duomo'] || [])[0];
const duomo = pd && R.route(CASA, [pd[1], pd[2]], 'moto');
check(!!duomo, `${TRAGITTI[0].nome}: partenza -> Piazza del Duomo ${pd ? pd[0] : '?'}: percorso trovato (la piazza e' pedonale)`);

// ---------- 3b. divieti attraverso una strada (inversioni nei varchi dello spartitraffico) ----------
console.log('3b. Divieti di svolta attraverso una strada');
{
  at('2026-10-20T10:00');
  let viol = 0, tried = 0;
  for (const [fw, via, tw] of d.restrv || []) {
    const ef = d.edges.findIndex(e => e[2] === fw), et = d.edges.findIndex(e => e[2] === tw);
    if (ef < 0 || et < 0) continue;
    const gf = R.geom[ef], gt = R.geom[et], vs = new Set(via);
    for (const [fa, fb] of [[0.25, 0.25], [0.5, 0.75], [0.75, 0.5]]) {
      const pa = Math.floor(gf.length / 2 * fa) * 2, pb = Math.floor(gt.length / 2 * fb) * 2;
      const r = R.route([gf[pa], gf[pa + 1]], [gt[pb], gt[pb + 1]], 'moto'); tried++;
      if (!r) continue;
      const ws = r.edges.map(x => d.edges[x >> 1][2]);
      for (let i = 0; i < ws.length; i++) {
        if (ws[i] !== fw) continue;
        let j = i + 1; while (j < ws.length && ws[j] === fw) j++;
        let k = j; while (k < ws.length && vs.has(ws[k])) k++;
        if (k > j && k < ws.length && ws[k] === tw) { viol++; break; }
      }
    }
  }
  check(viol === 0, `${(d.restrv || []).length} divieti, ${tried} tragitti di prova: nessuna inversione vietata (${viol})`);
}

// ---------- 4. tragitti a caso ----------
console.log('4. 300 tragitti a caso tra civici, a tre orari');
const A = Object.values(d.addr).flat();
let seed = 7;
const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
for (const t of ['2026-10-20T08:30', '2026-10-20T23:30', '2026-10-25T15:00']) {
  at(t);
  const z = R.destZones('moto');
  let none = 0, bad = 0, through = 0;
  for (let i = 0; i < 100; i++) {
    const a = A[Math.floor(rnd() * A.length)], b = A[Math.floor(rnd() * A.length)];
    const r = R.route([a[1], a[2]], [b[1], b[2]], 'moto');
    if (!r) { none++; continue; }
    if (!legal(r)) bad++;
    const zs = z[r.edges[0] >> 1], zt = z[r.edges[r.edges.length - 1] >> 1];
    if (r.edges.some(de => z[de >> 1] >= 0 && z[de >> 1] !== zs && z[de >> 1] !== zt)) through++;
  }
  check(none === 0, `${t}: nessun "nessun percorso" (${none})`);
  check(bad === 0, `${t}: nessuna strada vietata usata (${bad})`);
  check(through === 0, `${t}: nessuna zona "solo destinatari" attraversata (${through})`);
}

console.log(failed ? `\nCOLLAUDO FALLITO: ${failed} prove non superate.` : '\nCollaudo superato.');
process.exit(failed ? 1 : 0);
