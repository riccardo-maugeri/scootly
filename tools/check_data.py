#!/usr/bin/env python3
"""
Controlla i dati della mappa e scrive un rapporto in italiano (Markdown).

Controlli:
  1. confronto con le aree pedonali / ZTL ufficiali del Comune di Milano (chi puo' entrare)
  2. strade con nota "da verificare" (fixme) su OpenStreetMap
  3. strade con regole a orario (non ancora gestite dall'app)
  4. pezzi di rete isolati (da cui non si esce o in cui non si entra)
  5. (facoltativo) differenze con i dati precedenti

Uso:
  python3 tools/check_data.py --data data/strade.json --aree disciplina_aree.geojson \
      [--old vecchi.json] --out rapporto.md

Le aree del Comune: https://dati.comune.milano.it/dataset/ds51_trafficotrasporti_aree_pedonali_ztl
"""
import argparse
import json
import re
import sys
from collections import defaultdict

from shapely.geometry import LineString, shape
from shapely import transform
from shapely.strtree import STRtree

OSM = 'https://www.openstreetmap.org/way/'
TIPI = {'AP': 'area pedonale', 'ZTL': 'ZTL', 'DT': 'divieto di transito', 'DA': 'area a disciplina speciale'}
HOURS = re.compile(r'\d{1,2}[:.]?\d{0,2}\s*(alle|-|/)\s*(ore\s*)?\d{1,2}|notturn|attiv[ao] ', re.I)
MOTO_WORDS = re.compile(r'\bmoto|motocicl|motoveicol|cicli[- ]moto|cicli e moto|due ruote', re.I)


def geom_of(d, e):
    E = d['edges'][e]
    N = d['nodes']
    f = E[5]
    return [N[E[0]]] + [f[i:i + 2] for i in range(0, len(f), 2)] + [N[E[1]]]


def way_lines(d):
    """osmId -> (indice via, lista di LineString, lunghezza)"""
    out = {}
    for e, E in enumerate(d['edges']):
        w = d['ways'][E[2]]
        o = out.setdefault(w[0], [E[2], [], 0.0])
        o[1].append(LineString(geom_of(d, e)))
        o[2] += E[3]
    return out


def name_of(d, wi):
    i = d['ways'][wi][1]
    return (d['names'][i] if i >= 0 else '') or 'strada senza nome'


def perm_txt(f, b):
    if f and b:
        return 'doppio senso' + (' (solo destinatari)' if 2 in (f, b) else '')
    if f or b:
        return 'senso unico' + (' (solo destinatari)' if 2 in (f, b) else '')
    return 'vietata'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', required=True)
    ap.add_argument('--aree', required=True)
    ap.add_argument('--old')
    ap.add_argument('--out', required=True)
    ap.add_argument('--proposte', help='scrive qui le correzioni proposte (da rivedere a mano)')
    ap.add_argument('--eccezioni', help='disaccordi con il Comune gia\' esaminati (tools/eccezioni_comune.json)')
    ap.add_argument('--blocca-nuovi', action='store_true',
                    help='esce con errore se ci sono disaccordi con il Comune nuovi (non nelle eccezioni)')
    a = ap.parse_args()

    d = json.load(open(a.data))
    note_path = a.data.rsplit('.', 1)[0] + '.note.json'
    try:
        note = json.load(open(note_path))
    except OSError:
        note = {'conditional': [], 'fixme': []}
    lat0, lon0, KX, KY = d['origin']
    W = d['ways']
    wl = way_lines(d)
    L = []
    m = d['meta']
    L.append('# Rapporto sui dati della mappa\n')
    L.append(f"- Mappa OpenStreetMap del: **{m.get('osm', '?')}**")
    L.append(f"- Numeri civici del Comune aggiornati al: **{m.get('civici', '?')}**")
    L.append(f"- Zona: {m.get('confine')}; regole: {m.get('regole')}")
    L.append(f"- {len(W)} vie, {len(d['edges'])} tratti, {len(d['restr'])} divieti di svolta, "
             f"{sum(len(v) for v in d['addr'].values())} civici, {len(d.get('poi', []))} luoghi di interesse\n")

    # ---------- 1. confronto con il Comune ----------
    g = json.load(open(a.aree))
    proj = lambda c: (c - [lon0, lat0]) * [KX, KY]
    areas = []
    for f in g['features']:
        p = f['properties']
        if p['tipo'] not in TIPI or not f['geometry']:
            continue
        areas.append((transform(shape(f['geometry']), proj).buffer(0), p))
    tree = STRtree([x[0] for x in areas])
    problems = defaultdict(list)
    proposals = []
    for osm, (wi, lines, length) in wl.items():
        w = W[wi]
        mf, mb = w[3], w[4]
        for ln in lines:
            for i in tree.query(ln):
                poly, p = areas[i]
                inter = poly.intersection(ln).length
                if inter < max(15, 0.6 * ln.length):
                    continue
                der = str(p.get('deroghe') or '')
                moto_ok = bool(MOTO_WORDS.search(der))
                tipo = p['tipo']
                msg = None
                if (mf or mb) and not moto_ok:
                    if tipo == 'AP' and 1 in (mf, mb):
                        msg = 'OSM: moto ammesse a tutti — Comune: area pedonale senza deroga per le moto'
                    elif tipo == 'AP':
                        msg = 'OSM: solo destinatari — Comune: area pedonale senza deroga per le moto (probabilmente giusto)'
                    elif tipo == 'ZTL' and 1 in (mf, mb):
                        msg = 'OSM: moto ammesse a tutti — Comune: ZTL senza deroga per le moto'
                    elif tipo == 'DT' and 1 in (mf, mb):
                        msg = 'OSM: transito libero — Comune: divieto di transito (solo destinatari)'
                elif not (mf or mb) and moto_ok and tipo in ('ZTL', 'AP'):
                    msg = 'OSM: moto vietate — Comune: moto ammesse'
                if msg:
                    nota = str(p.get('note') or '')
                    tag = {'motorcycle': 'yes'} if msg.startswith('OSM: moto vietate') else \
                        {'motorcycle': 'destination'} if tipo == 'DT' else {'motorcycle': 'no'}
                    if not msg.startswith('OSM: moto vietate') and (HOURS.search(nota) or HOURS.search(p['nome'])):
                        if len(w) > 13:
                            break  # OSM ha gia' le sue fasce orarie: le usa il motore
                        msg = 'Regola a orario del Comune che OSM non ha'
                        der = f'{nota} — ammessi: {der}'
                        tag = {'motorcycle:conditional': '?? da scrivere: ' + nota}
                    problems[msg].append((osm, wi, round(inter), p['nome'], der[:200]))
                    proposals.append({'osm': osm, 'via': name_of(d, wi), 'area': p['nome'], 'tipo': tipo,
                                      'dentro': round(inter / max(ln.length, 1), 2), 'metri': round(ln.length),
                                      'classe': w[2], 'tag': tag, 'deroghe': der[:200], 'ordinanza': p.get('ordinanza')})
                break
    L.append('## 1. Differenze con le aree pedonali e ZTL del Comune\n')
    L.append('Dove OpenStreetMap e il Comune non sono d\'accordo su chi può passare. '
             'Le linee a confine di un\'area possono essere falsi allarmi.\n')
    if not problems:
        L.append('Nessuna differenza trovata.\n')
    for msg, items in sorted(problems.items(), key=lambda kv: -len(kv[1])):
        L.append(f'### {msg} ({len(items)})\n')
        L.append('| Via | Metri | Area del Comune | Chi può entrare (Comune) |')
        L.append('|---|---|---|---|')
        merged = {}
        for osm, wi, mt, area, der in items:
            k = (name_of(d, wi), area)
            if k in merged:
                merged[k][1] += mt
            else:
                merged[k] = [osm, mt, der]
        for (nm, area), (osm, mt, der) in sorted(merged.items(), key=lambda kv: -kv[1][1]):
            L.append(f'| [{nm}]({OSM}{osm}) | {mt} | {area} | {der or "—"} |')
        L.append('')

    # ---------- regole del Comune che non trovano piu' la loro via ----------
    if note.get('regole_mancanti'):
        L.append('## ⚠️ Regole del Comune da ricontrollare\n')
        L.append('Queste vie citate in `tools/regole_milano.json` non ci sono più in OpenStreetMap (ridisegnate o '
                 'divise): la regola non viene applicata finché non si aggiorna il numero della via.\n')
        for osm, n in note['regole_mancanti']:
            L.append(f'- regola {n}: via [{osm}]({OSM}{osm})')
        L.append('')

    # ---------- 2. note "da verificare" ----------
    L.append('## 2. Strade aperte alle moto con nota "da verificare" su OpenStreetMap\n')
    rows = []
    for osm in note.get('fixme', []):
        if osm in wl:
            wi, _, length = wl[osm]
            rows.append((length, osm, wi))
    rows.sort(reverse=True)
    L.append(f'{len(rows)} strade. Le più lunghe:\n')
    L.append('| Via | Metri | Moto | Auto |')
    L.append('|---|---|---|---|')
    for length, osm, wi in rows[:40]:
        w = W[wi]
        L.append(f'| [{name_of(d, wi)}]({OSM}{osm}) | {round(length)} | {perm_txt(w[3], w[4])} | {perm_txt(w[5], w[6])} |')
    L.append('')

    # ---------- 3. regole a orario ----------
    rows = sorted(((wl[o][2], o, wl[o][0]) for o in note.get('conditional', []) if o in wl), reverse=True)
    L.append('## 3. Strade con regole che cambiano con l\'orario\n')
    L.append(f'{len(rows)} strade. L\'app applica queste regole in base all\'ora di partenza '
             '(le condizioni che non sono orari, es. "solo residenti", vengono ignorate).\n')
    for length, osm, wi in rows[:30]:
        L.append(f'- [{name_of(d, wi)}]({OSM}{osm}) ({round(length)} m)')
    L.append('')

    # ---------- 4. pezzi isolati ----------
    nD = len(d['edges']) * 2
    out_of = defaultdict(list)
    for e, E in enumerate(d['edges']):
        out_of[E[0]].append(2 * e)
        out_of[E[1]].append(2 * e + 1)
    def allowed(dd):
        w = W[d['edges'][dd >> 1][2]]
        return (w[3] if dd & 1 == 0 else w[4]) > 0

    def head(dd):
        E = d['edges'][dd >> 1]
        return E[1] if dd & 1 == 0 else E[0]

    adj = [[] for _ in range(nD)]
    for dd in range(nD):
        if not allowed(dd):
            continue
        for nx in out_of[head(dd)]:
            if (nx >> 1) == (dd >> 1) and len(out_of[head(dd)]) > 1:
                continue
            if allowed(nx):
                adj[dd].append(nx)
    # Tarjan iterativo
    index = [-1] * nD
    low = [0] * nD
    on = [False] * nD
    comp = [-1] * nD
    st, idx, nc = [], 0, 0
    for v0 in range(nD):
        if index[v0] >= 0 or not allowed(v0):
            continue
        stack = [(v0, 0)]
        index[v0] = low[v0] = idx
        idx += 1
        st.append(v0)
        on[v0] = True
        while stack:
            v, i = stack[-1]
            if i < len(adj[v]):
                stack[-1] = (v, i + 1)
                w = adj[v][i]
                if index[w] < 0:
                    index[w] = low[w] = idx
                    idx += 1
                    st.append(w)
                    on[w] = True
                    stack.append((w, 0))
                elif on[w]:
                    low[v] = min(low[v], index[w])
            else:
                stack.pop()
                if stack:
                    u = stack[-1][0]
                    low[u] = min(low[u], low[v])
                if low[v] == index[v]:
                    while True:
                        w = st.pop()
                        on[w] = False
                        comp[w] = nc
                        if w == v:
                            break
                    nc += 1
    size = defaultdict(float)
    for dd in range(nD):
        if comp[dd] >= 0:
            size[comp[dd]] += d['edges'][dd >> 1][3]
    main_c = max(size, key=size.get)
    tot = sum(size.values())
    iso = defaultdict(float)
    for dd in range(nD):
        if comp[dd] >= 0 and comp[dd] != main_c:
            w = W[d['edges'][dd >> 1][2]]
            if w[2] <= 5:
                iso[(name_of(d, d['edges'][dd >> 1][2]), w[0])] += d['edges'][dd >> 1][3]
    L.append('## 4. Pezzi di rete isolati\n')
    L.append(f'La rete principale percorribile in scooter copre il **{100 * size[main_c] / tot:.1f}%** dei tratti. '
             'Il resto sono vicoli, passi carrabili o strade tagliate dal confine. Strade vere fuori dalla rete principale:\n')
    for (nm, osm), length in sorted(iso.items(), key=lambda kv: -kv[1])[:25]:
        L.append(f'- [{nm}]({OSM}{osm}) ({round(length)} m)')
    L.append('')

    # ---------- 5. confronto con i dati vecchi ----------
    if a.old:
        o = json.load(open(a.old))
        old = {w[0]: w for w in o['ways']}
        new = {w[0]: w for w in W}
        new_idx = {w[0]: i for i, w in enumerate(W)}
        changed = []
        for osm, w in new.items():
            ow = old.get(osm)
            if ow and (ow[3], ow[4]) != (w[3], w[4]):
                changed.append((wl[osm][2] if osm in wl else 0, osm, ow, w))
        changed.sort(key=lambda r: -r[0])
        only_old = [k for k in old if k not in new]
        only_new = [k for k in new if k not in old]
        L.append('## 5. Differenze con la mappa usata finora\n')
        L.append(f'- Vie nuove (non c\'erano): {len(only_new)}; vie sparite: {len(only_old)}')
        L.append(f'- Vie in cui cambia il permesso per lo scooter: **{len(changed)}**\n')
        L.append('| Via | Metri | Prima | Adesso |')
        L.append('|---|---|---|---|')
        for length, osm, ow, w in changed[:60]:
            L.append(f'| [{name_of(d, new_idx[osm])}]({OSM}{osm}) | {round(length)} | {perm_txt(ow[3], ow[4])} | {perm_txt(w[3], w[4])} |')
        L.append('')

    # disaccordi nuovi (non ancora esaminati): in cima al rapporto
    known = set()
    if a.eccezioni:
        known = {e['osm'] for e in json.load(open(a.eccezioni))['eccezioni']}
    nuovi = {}
    for p in proposals:
        if p['osm'] not in known:
            nuovi.setdefault(p['osm'], p)
    if a.eccezioni:
        H = ['## ⚠️ Disaccordi nuovi con il Comune\n']
        if nuovi:
            H.append('Da esaminare: aggiungere una regola in `tools/regole_milano.json` oppure, se dubbi, '
                     'in `tools/eccezioni_comune.json`.\n')
            H += [f"- [{p['via']}]({OSM}{osm}) — {p['area']} ({p['tipo']}, {round(p['dentro'] * 100)}% dentro)"
                  for osm, p in nuovi.items()]
        else:
            H.append('Nessuno.')
        H.append('')
        L[5:5] = H  # subito dopo le righe iniziali (date e numeri)
    open(a.out, 'w').write('\n'.join(L) + '\n')
    if a.proposte:
        json.dump(proposals, open(a.proposte, 'w'), ensure_ascii=False, indent=1)
    if a.blocca_nuovi and nuovi:
        print(f'BLOCCATO: {len(nuovi)} disaccordi nuovi con il Comune (vedi il rapporto)', file=sys.stderr)
        for osm, p in nuovi.items():
            print(f"  {p['via']} ({osm}) — {p['area']}", file=sys.stderr)
        sys.exit(1)
    print('Rapporto scritto in', a.out, file=sys.stderr)


if __name__ == '__main__':
    main()
