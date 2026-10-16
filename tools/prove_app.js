#!/usr/bin/env node
// Prove automatiche dell'app vera, in un browser che fa finta di essere un iPhone (Playwright + Chromium).
// Controllano quello che vede e usa il proprietario: avvio, ricerca, preferiti, guida con la voce,
// tempi dei tragitti, tema scuro la sera. Su GitHub girano a ogni modifica (.github/workflows/prove.yml).
// Uso: python3 -m http.server 8765 (dalla cartella del progetto), poi: node tools/prove_app.js [indirizzo]
// Esce con errore (codice 1) se anche una sola prova fallisce.
'use strict';
const fs = require('fs');
const path = require('path');
let pw;
try { pw = require('playwright'); } catch (e) { pw = require(require('child_process').execSync('npm root -g').toString().trim() + '/playwright'); }
const { chromium, devices } = pw;

const ROOT = path.join(__dirname, '..');
const URL = process.argv[2] || 'http://127.0.0.1:8765/index.html';
const html = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
const mod = { exports: {} };
new Function('module', html.match(/<script>\s*(\/\/ Motore di calcolo percorsi[\s\S]*?)<\/script>/)[1])(mod);
const d = JSON.parse(fs.readFileSync(path.join(ROOT, 'data', 'strade.json'), 'utf8'));
const R = new mod.exports.Router(d); R.setTime(new Date());
const [LAT0, LON0, KX, KY] = d.origin;
const ll = p => ({ latitude: LAT0 + p[1] / KY, longitude: LON0 + p[0] / KX });

// tragitto di prova (indirizzi pubblici, non personali)
const A = d.addr['Corso Magenta'].find(x => x[0] === '50'), B = d.addr['Corso di Porta Romana'].find(x => x[0] === '6');
const DEST = 'corso di porta romana 6';
const route = R.route([A[1], A[2]], [B[1], B[2]], 'moto');
const L = route.line, cum = [0];
for (let i = 1; i < L.length; i++) cum.push(cum[i - 1] + Math.hypot(L[i][0] - L[i - 1][0], L[i][1] - L[i - 1][1]));
const at = s => { let i = 0; while (i < cum.length - 2 && cum[i + 1] < s) i++; const t = (s - cum[i]) / ((cum[i + 1] - cum[i]) || 1); return [L[i][0] + (L[i + 1][0] - L[i][0]) * t, L[i][1] + (L[i + 1][1] - L[i][1]) * t]; };

let failed = 0;
function check(ok, msg) { console.log((ok ? '  ok   ' : '  NO   ') + msg); if (!ok) failed++; }

(async () => {
  const browser = await chromium.launch();
  // nuovo "telefono": iPhone 13 a Milano, GPS sulla partenza, voce finta che scrive le frasi
  async function phone(opts = {}) {
    const ctx = await browser.newContext({ ...devices[opts.device || 'iPhone 13'], geolocation: { ...ll([A[1], A[2]]), accuracy: 8 }, permissions: ['geolocation'], locale: 'it-IT', timezoneId: 'Europe/Rome' });
    await ctx.addInitScript(() => {
      window.__said = [];
      Object.defineProperty(window, 'speechSynthesis', { value: { speak: u => window.__said.push(u.text), cancel() {}, getVoices: () => [], addEventListener() {} } });
      window.SpeechSynthesisUtterance = function (t) { this.text = t; };
    });
    if (opts.hour) await ctx.addInitScript(h => { const D = Date; const off = new D(new D().toISOString().slice(0, 10) + 'T' + h) - new D(); window.Date = class extends D { constructor(...a) { super(...(a.length ? a : [D.now() + off])); } static now() { return D.now() + off; } }; }, opts.hour);
    if (opts.storage) await ctx.addInitScript(st => { if (!sessionStorage.getItem('__init')) { for (const k in st) localStorage.setItem(k, st[k]); sessionStorage.setItem('__init', '1'); } }, opts.storage);
    const page = await ctx.newPage(); page.errors = [];
    page.on('pageerror', e => page.errors.push(e.message));
    page.on('dialog', dg => dg.accept(page.answer || undefined));
    page.ready = async () => { await page.waitForFunction(() => !document.getElementById('card').textContent.includes('Carico la mappa'), null, { timeout: 30000 }); await page.waitForTimeout(1500); };
    page.dest = async q => { await page.fill('#q-to', q); await page.waitForTimeout(400); await page.keyboard.press('Enter'); await page.waitForTimeout(1500); };
    await page.goto(URL); await page.ready();
    return { ctx, page };
  }

  console.log('1. Avvio');
  {
    const { ctx, page } = await phone();
    check(await page.$eval('#card', c => c.hidden), 'all\'avvio si vede solo la mappa (nessun riquadro in basso)');
    check(!(await page.$('#rainmode')) && !(await page.$('#simgo')), 'niente pulsanti pioggia e prova simulata');
    check(page.errors.length === 0, `nessun errore (${page.errors.join(' | ')})`);
    await ctx.close();
  }

  console.log('2. Avvio con dati vecchi salvati sul telefono');
  {
    const { ctx, page } = await phone({ storage: {
      'scooter-casa': JSON.stringify([A[1], A[2]]), 'scooter-pioggia': 'true',
      'scooter-corr': JSON.stringify({ 1: { tipo: 'chiusa', fino: '2020-01-01T00:00:00Z', osm: 1, nome: 'prova' } }),
    } });
    check(page.errors.length === 0, `nessun errore (${page.errors.join(' | ')})`);
    check(await page.evaluate(() => localStorage.getItem('scooter-pioggia')) === null, 'la vecchia impostazione pioggia viene tolta');
    check(await page.evaluate(() => Object.keys(JSON.parse(localStorage.getItem('scooter-corr') || '{}')).length) === 0, 'la chiusura per lavori scaduta viene tolta');
    check((await page.$$eval('.chip', a => a.map(x => x.textContent))).includes('Casa'), 'Casa resta tra i preferiti');
    await ctx.close();
  }

  console.log('3. Ricerca e percorso');
  {
    const { ctx, page } = await phone();
    await page.fill('#q-to', 'duomo'); await page.waitForTimeout(400);
    check((await page.$$eval('#suggest button', a => a.map(x => x.textContent))).some(t => /duomo/i.test(t)), 'cercando "duomo" compare il Duomo');
    await page.keyboard.press('Escape');
    await page.dest(DEST);
    const txt = (await page.textContent('#card')).replace(/\s+/g, ' ');
    check(/\d+\s*min/.test(txt) && /Vai/.test(txt), `percorso calcolato: "${txt.trim().slice(0, 60)}"`);
    check(!/risparmi/i.test(txt), 'niente riga "In scooter risparmi..."');
    await page.mouse.click(200, 420); await page.waitForTimeout(300);
    check(!(await page.$('#setto')), 'toccando la mappa non si sceglie la destinazione');
    await ctx.close();
  }

  console.log('4. Preferiti con la stella');
  {
    const { ctx, page } = await phone();
    await page.dest(DEST);
    page.answer = 'Lavoro'; await page.click('#favbtn'); await page.waitForTimeout(400);
    check((await page.$$eval('.chip', a => a.map(x => x.textContent))).includes('Lavoro'), 'salvato "Lavoro": compare il pulsante in alto');
    await page.reload(); await page.ready();
    await page.click('.chip:has-text("Lavoro")'); await page.waitForTimeout(1500);
    check(/porta romana/i.test(await page.inputValue('#q-to')), 'dopo la riapertura il pulsante Lavoro porta all\'arrivo salvato');
    page.answer = undefined; await page.click('#favbtn'); await page.waitForTimeout(400);
    check(!(await page.$$eval('.chip', a => a.map(x => x.textContent))).includes('Lavoro'), 'la stella piena toglie il preferito');
    check(page.errors.length === 0, `nessun errore (${page.errors.join(' | ')})`);
    await ctx.close();
  }

  console.log('5. Guida: schermata, voce, tempi (l\'app si chiude a 150 m dall\'arrivo)');
  {
    const { ctx, page } = await phone();
    await page.dest(DEST);
    await page.click('#go'); await page.waitForTimeout(500);
    check(await page.$eval('#navtop', e => !e.hidden) && await page.$eval('#navbot', e => !e.hidden), 'con "Vai" si entra in modalità guida (riquadro in alto e barra in basso)');
    const end = route.length - 150;
    for (let s = 0; s <= end; s += 4) { await ctx.setGeolocation({ ...ll(at(s)), accuracy: 6 }); await page.waitForTimeout(260); }
    await page.waitForTimeout(800);
    const said = await page.evaluate(() => window.__said);
    check(/^Procedi in direzione (nord|sud|est|ovest|nord-est|nord-ovest|sud-est|sud-ovest)/.test(said[0] || ''), `partenza: "${said[0]}"`);
    check(said.some(t => /^Tra \d+ (metri|chilometri?),/.test(t)), 'preavvisi "Tra ... metri, ..."');
    check(said.filter(t => /^Svolta /.test(t)).length >= 5, `indicazioni all'incrocio "Svolta ..." (${said.filter(t => /^Svolta /.test(t)).length})`);
    check(said.filter(t => /^Procedi/.test(t)).length === 1, 'nessun ricalcolo inutile (la partenza si dice una volta sola)');
    const dist = await page.textContent('#nt-dist');
    check(/^\d+(,\d)? (m|km)$/.test(dist), `distanza nel riquadro: "${dist}"`);
    check(/\d+ min/.test(await page.textContent('#nb-min')) && /\d\d:\d\d/.test(await page.textContent('#nb-rest')), 'barra in basso con minuti e ora di arrivo');
    const saved = await page.evaluate(() => JSON.parse(localStorage.getItem('scooter-in-corso') || 'null'));
    check(!!(saved && saved.lastP), 'il viaggio in corso è salvato sul telefono');
    check(page.errors.length === 0, `nessun errore (${page.errors.join(' | ')})`);
    // l'utente trova posto e chiude l'app: alla riapertura il viaggio conta
    await page.reload(); await page.ready();
    const h = await page.evaluate(() => JSON.parse(localStorage.getItem('scooter-tragitti') || '[]'));
    check(h.length === 1 && h[0].s >= 60, `app chiusa vicino all'arrivo: il tragitto conta lo stesso (${h.length ? h[0].s + ' s' : 'nessuno'})`);
    await ctx.setGeolocation({ ...ll([A[1], A[2]]), accuracy: 6 }); await page.reload(); await page.ready(); // di nuovo alla partenza
    await page.dest(DEST);
    check(/come l'ultima volta/.test(await page.textContent('#card')), 'rifacendo lo stesso tragitto, il tempo è quello vero ("come l\'ultima volta")');
    await ctx.close();
  }

  console.log('6. Tema scuro la sera');
  for (const [h, want] of [['12:00:00', false], ['23:30:00', true]]) {
    const { ctx, page } = await phone({ hour: h });
    check((await page.evaluate(() => document.documentElement.dataset.theme)) === (want ? 'dark' : undefined), `alle ${h.slice(0, 5)}: tema ${want ? 'scuro' : 'normale'}`);
    await ctx.close();
  }

  console.log('7. Schermo piccolo (iPhone SE)');
  {
    const { ctx, page } = await phone({ device: 'iPhone SE' });
    await page.dest(DEST);
    const box = await page.$eval('#go', b => { const r = b.getBoundingClientRect(); return [r.top, r.bottom, innerHeight]; });
    check(box[1] <= box[2] && box[0] > 0, '"Vai" si vede tutto');
    await ctx.close();
  }

  await browser.close();
  console.log(failed ? `\nPROVE FALLITE: ${failed}.` : '\nTutte le prove dell\'app superate.');
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
