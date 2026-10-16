#!/usr/bin/env node
// Confronta gli stessi tragitti calcolati con due versioni dei dati della mappa.
// Uso: node tools/compare_routes.js vecchi.json nuovi.json ["2026-10-20T08:30"]
// L'ora (facoltativa, predefinita martedi' 8:30) decide quali regole a orario valgono.
// Il motore viene letto direttamente da index.html, cosi' si prova sempre quello vero.
'use strict';
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const m = html.match(/<script>\s*(\/\/ Motore di calcolo percorsi[\s\S]*?)<\/script>/);
if (!m) { console.error('Motore non trovato in index.html'); process.exit(1); }
const mod = { exports: {} };
new Function('module', m[1])(mod);
const { Router } = mod.exports;

// partenza di prova (i tragitti personali non vanno nel progetto, che e' pubblico)
const FROM = (process.env.SCOOTLY_DA || 'Corso Magenta,50').split(',').map(x => x.trim());
const TRIPS = [
  ['Piazza del Duomo', '1'], ["Piazza Duca d'Aosta", '1'], ['Piazzale Loreto', '1'], ['Via Brera', '10'],
  ['Piazzale Lagosta', '1'], ['Corso di Porta Romana', '120'], ['Piazzale Lorenzo Lotto', '1'],
  ['Corso Buenos Aires', '40'], ['Corso Sempione', '30'], ['Via Paolo Sarpi', '20'], ['Corso Como', '10'],
  ['Piazza Cinque Giornate', '1'], ['Viale Bligny', '20'], ['Corso Magenta', '50'], ['Viale Monza', '10'],
  ['Piazzale Luigi Cadorna', '1'], ['Via Tortona', '27'], ['Corso Venezia', '16'], ['Viale Piave', '1'], ['Via Giorgio Washington', '10'],
];

function load(f) {
  const d = JSON.parse(fs.readFileSync(f, 'utf8'));
  const R = new Router(d);
  R.setTime(WHEN);
  return { d, R };
}
function addr(d, street, num) {
  const list = d.addr[street];
  if (!list || !list.length) return null;
  // se il civico non c'e', prende il numero piu' basso (uguale per tutte e due le versioni)
  const byNum = list.slice().sort((p, q) => (parseInt(p[0]) || 0) - (parseInt(q[0]) || 0) || (p[0] < q[0] ? -1 : 1));
  const a = list.find(x => x[0] === num) || byNum[0];
  return [a[1], a[2]];
}
const name = (d, s) => s.name;
function run(set, to) {
  const from = addr(set.d, ...FROM), dest = addr(set.d, ...to);
  if (!from || !dest) return { err: 'indirizzo non trovato' };
  const r = set.R.route(from, dest, 'moto');
  if (!r) return { err: 'nessun percorso' };
  return { min: r.time / 60, km: r.length / 1000, via: r.steps.map(s => s.name), motoOnly: r.steps.filter(s => s.motoOnly).map(s => s.name) };
}

const [, , fa, fb, when] = process.argv;
const WHEN = new Date(when || '2026-10-20T08:30');
const A = load(fa), B = load(fb);
let diff = 0;
for (const t of TRIPS) {
  const a = run(A, t), b = run(B, t);
  const label = `${FROM.join(' ')} → ${t.join(' ')}`;
  const fmt = r => r.err ? r.err : `${r.min.toFixed(1)} min, ${r.km.toFixed(2)} km`;
  const same = !a.err && !b.err && a.via.join('|') === b.via.join('|');
  if (!same) diff++;
  console.log(`\n${same ? '=' : '≠'} ${label}\n   prima: ${fmt(a)}\n   dopo:  ${fmt(b)}`);
  if (!same) {
    if (!a.err) console.log('   prima via: ' + a.via.join(' > '));
    if (!b.err) console.log('   dopo via:  ' + b.via.join(' > '));
  }
}
console.log(`\n${diff} tragitti su ${TRIPS.length} cambiano.`);
