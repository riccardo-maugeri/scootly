#!/usr/bin/env python3
"""
Costruisce i dati della mappa di Scootly.

Fonti:
  1. OpenStreetMap: estratto della Lombardia in formato .osm.pbf
     (https://download.openstreetmap.fr/extracts/europe/italy/lombardia-latest.osm.pbf)
  2. Comune di Milano: numeri civici ufficiali con coordinate (CSV del dataset ds634)

Uso:
  python3 tools/build_data.py --osm lombardia.osm.pbf --osm-state lombardia.state.txt \
      --civici civici.csv --out data/strade.json

Requisiti: pip install osmium shapely

Il risultato ha lo stesso formato dei dati che l'app usava dentro index.html
(vedi CLAUDE.md, "Formato dei dati"), con in piu':
  - meta: date delle fonti e regole usate
  - poi:  luoghi di interesse per la ricerca (Duomo, Esselunga, Apple, ...)
  - restr: un quinto valore = a chi vale il divieto di svolta (1 moto, 2 auto, 3 entrambi)

Le regole di accesso sono pensate per uno scooter 125cc (in OSM: "motorcycle").
Il tag "moped" (ciclomotori fino a 50cc) NON vale per un 125.
"""
import argparse
import csv
import datetime as dt
import json
import math
import os
import pickle
import re
import sys
import unicodedata
from collections import defaultdict

import osmium
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree

# Stessa origine e stesse costanti dei dati precedenti: Casa e destinazioni recenti
# sono salvate sul telefono in queste coordinate, quindi NON vanno cambiate.
ORIGIN = [45.4655, 9.1865, 78073.01438028117, 110540.0]
LAT0, LON0, KX, KY = ORIGIN

# Rettangolo largo attorno alla circonvallazione esterna (serve solo a leggere meno dati)
BBOX = (9.120, 45.430, 9.240, 45.512)  # lon min, lat min, lon max, lat max

# Distanze (metri) oltre la linea della 90/91:
# - ZONE_MARGIN: la "zona coperta" (entrambe le carreggiate e i controviali della circonvallazione)
# - ROAD_MARGIN: fin dove includiamo le strade, civici e luoghi. E' piu' largo della zona
#   cosi' i piazzali sull'anello (Loreto, Lotto, ...) restano interi e non diventano vicoli ciechi.
ZONE_MARGIN = 150
ROAD_MARGIN = 500

# highway -> classe usata dal motore (vedi SPEED in index.html)
CLASSES = {
    'trunk': 0, 'trunk_link': 0,
    'primary': 1, 'primary_link': 1,
    'secondary': 2, 'secondary_link': 2,
    'tertiary': 3, 'tertiary_link': 3,
    'unclassified': 4, 'residential': 4, 'road': 4,
    'living_street': 5,
    'service': 6, 'busway': 6,
    'pedestrian': 7,
    'motorway': 8, 'motorway_link': 8,
}
# vie di solito non percorribili: entrano solo se un tag apre esplicitamente alle moto
MINOR = {'footway', 'cycleway', 'path', 'track', 'bridleway', 'steps'}
# accesso predefinito (se nessun tag dice altro) per tipo di strada
DEFAULT_NO = {'pedestrian', 'busway'} | MINOR

YES = {'yes', 'permissive', 'designated', 'official', 'discouraged'}
DEST = {'destination', 'customers'}  # 'delivery' (solo carico/scarico) e 'permit' = vietato per noi

MOTO_KEYS = ['motorcycle', 'motor_vehicle', 'vehicle', 'access']
CAR_KEYS = ['motorcar', 'motor_vehicle', 'vehicle', 'access']
MOTO_ONEWAY = ['oneway:motorcycle', 'oneway:motor_vehicle', 'oneway:vehicle', 'oneway']
CAR_ONEWAY = ['oneway:motorcar', 'oneway:motor_vehicle', 'oneway:vehicle', 'oneway']

PAVE = {'sett', 'cobblestone', 'unhewn_cobblestone', 'cobblestone:flattened'}
DRIVEWAY = {'driveway', 'parking_aisle', 'drive-through'}
CONDITIONAL_KEYS = ['motorcycle:conditional', 'motor_vehicle:conditional', 'vehicle:conditional',
                    'access:conditional', 'oneway:conditional', 'oneway:motorcycle:conditional']


def proj(lat, lon):
    return ((lon - LON0) * KX, (lat - LAT0) * KY)


def haversine(lat1, lon1, lat2, lon2):
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def first_value(v):
    return v.split(';')[0].strip().lower() if v else v


def access_value(tags, keys, direction, highway):
    """0 vietato, 1 permesso, 2 solo destinatari. Il mezzo piu' specifico vince, poi il verso."""
    for k in keys:
        v = tags.get(f'{k}:{direction}') or tags.get(k)
        v = first_value(v)
        if v is None or v == 'unknown':
            continue
        if v in YES:
            return 1
        if v in DEST:
            return 2
        return 0  # no, private, psv, bus, agricultural, ...
    return 0 if highway in DEFAULT_NO else 1


def oneway_value(tags, keys, highway):
    """1 solo nel verso del disegno, -1 solo al contrario, 0 doppio senso, None se chiusa (reversible)."""
    for k in keys:
        v = first_value(tags.get(k))
        if v is None:
            continue
        if v in ('yes', 'true', '1'):
            return 1
        if v in ('-1', 'reverse'):
            return -1
        if v in ('no', 'false', '0'):
            return 0
        if v in ('reversible', 'alternating'):
            return None
    if tags.get('junction') in ('roundabout', 'circular') or highway in ('motorway', 'motorway_link'):
        return 1
    return 0


def permissions(tags, highway, keys, oneway_keys):
    ow = oneway_value(tags, oneway_keys, highway)
    fw = access_value(tags, keys, 'forward', highway)
    bw = access_value(tags, keys, 'backward', highway)
    if ow is None:
        return 0, 0
    if ow == 1:
        bw = 0
    elif ow == -1:
        fw = 0
    return fw, bw


# ---------- regole a orario (tag ":conditional" di OSM) ----------
# Formato nei dati, campo 13 della via (solo se ci sono regole):
#   [[finestre, motoAvanti, motoIndietro, autoAvanti, autoIndietro], ...]
#   finestra = [mesi (bit 0 = gennaio), giorni (bit 0 = lunedi'), inizio, fine] in minuti dalla mezzanotte;
#   se fine <= inizio la finestra passa la mezzanotte (es. 22:00-06:00).
# Quando una finestra e' attiva, i permessi della regola sostituiscono quelli normali.
COND_BASE = set(MOTO_KEYS + CAR_KEYS + MOTO_ONEWAY + CAR_ONEWAY)
MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
DAYS = ['mo', 'tu', 'we', 'th', 'fr', 'sa', 'su']
ALL_MONTHS, ALL_DAYS = (1 << 12) - 1, (1 << 7) - 1


def split_top(s, sep=';'):
    out, depth, cur = [], 0, ''
    for ch in s:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == sep and depth == 0:
            out.append(cur)
            cur = ''
        else:
            cur += ch
    out.append(cur)
    return [x.strip() for x in out if x.strip()]


def range_mask(tok, names):
    mask = 0
    for part in tok.split(','):
        ab = part.split('-')
        if not all(x in names for x in ab):
            return None
        i, j = names.index(ab[0]), names.index(ab[-1])
        k = i
        while True:
            mask |= 1 << k
            if k == j:
                break
            k = (k + 1) % len(names)
    return mask


def hm(t):
    h, _, m = t.partition(':')
    return (int(h) * 60 + int(m or 0)) % 1440


def parse_hours(cond):
    """'Mo-Fr 07:00-10:00; Sa 08:00-12:00' -> lista di finestre, None se non e' un orario."""
    # "... EXCEPT permit": vale per chi non ha permessi, quindi anche per noi
    cond = re.sub(r'\)\s*except\b.*$', ')', cond.strip(), flags=re.I)
    if cond.startswith('(') and cond.endswith(')'):
        cond = cond[1:-1]
    wins = []
    for rule in re.split(r';', cond):
        rule = rule.strip().lower()
        if not rule or re.fullmatch(r'ph( off)?', rule):
            continue
        rule = re.sub(r'\bph off\b|,?\s*ph\b', '', rule).strip()
        months, days, times = ALL_MONTHS, ALL_DAYS, []
        for tok in re.split(r'\s+(?![^\s]*-\s)', re.sub(r'\s*-\s*', '-', re.sub(r'\s*,\s*', ',', rule))):
            if not tok:
                continue
            m = range_mask(tok, MONTHS)
            if m is not None:
                months = m
                continue
            dmask = range_mask(tok, DAYS)
            if dmask is not None:
                days = dmask
                continue
            if re.fullmatch(r'\d{1,2}(:\d\d)?-\d{1,2}(:\d\d)?(,\d{1,2}(:\d\d)?-\d{1,2}(:\d\d)?)*', tok):
                for span in tok.split(','):
                    a, b = span.split('-')
                    times.append((hm(a), hm(b)))
                continue
            return None  # non e' un orario (es. "permit_holder", date precise): regola ignorata
        if not times:
            times = [(0, 0)]  # tutto il giorno
        for a, b in times:
            wins.append([months, days, a, b])
    return wins or None


def conditional_rules(tags, hw):
    rules, skipped = [], []
    for k, v in tags.items():
        if not k.endswith(':conditional'):
            continue
        base = k[:-len(':conditional')]
        root = re.sub(r':(forward|backward)$', '', base)
        if base not in COND_BASE and root not in COND_BASE:
            continue
        for clause in split_top(v):
            val, _, cond = clause.partition('@')
            wins = parse_hours(cond) if cond else None
            if not wins:
                skipped.append(clause)
                continue
            t2 = dict(tags)
            t2[base] = val.strip()
            mf, mb = permissions(t2, hw, MOTO_KEYS, MOTO_ONEWAY)
            cf, cb = permissions(t2, hw, CAR_KEYS, CAR_ONEWAY)
            rules.append([wins, mf, mb, cf, cb])
    return rules, skipped


def parse_speed(v):
    if not v:
        return 0
    v = v.strip().lower()
    if v in ('it:urban',):
        return 50
    m = re.match(r'^(\d+)', v)
    return int(m.group(1)) if m else 0


# ---------- luoghi di interesse ----------
POI_KEEP_AMENITY = {
    'restaurant', 'cafe', 'bar', 'pub', 'fast_food', 'ice_cream', 'pharmacy', 'hospital', 'clinic',
    'doctors', 'dentist', 'university', 'college', 'school', 'kindergarten', 'library', 'theatre',
    'cinema', 'arts_centre', 'place_of_worship', 'townhall', 'police', 'post_office', 'bank',
    'fuel', 'motorcycle_parking', 'parking', 'marketplace', 'community_centre', 'courthouse',
    'nightclub', 'veterinary', 'coworking_space', 'events_venue', 'exhibition_centre', 'conference_centre',
}
POI_KEEP_TOURISM = {'attraction', 'museum', 'gallery', 'hotel', 'hostel', 'viewpoint', 'artwork'}
POI_KEEP_LEISURE = {'park', 'garden', 'sports_centre', 'stadium', 'fitness_centre', 'swimming_pool', 'playground'}
POI_LABEL = {
    'restaurant': 'ristorante', 'cafe': 'bar', 'bar': 'bar', 'pub': 'pub', 'fast_food': 'fast food',
    'ice_cream': 'gelateria', 'pharmacy': 'farmacia', 'hospital': 'ospedale', 'clinic': 'clinica',
    'doctors': 'medico', 'dentist': 'dentista', 'university': 'università', 'college': 'scuola',
    'school': 'scuola', 'kindergarten': 'asilo', 'library': 'biblioteca', 'theatre': 'teatro',
    'cinema': 'cinema', 'arts_centre': 'centro culturale', 'place_of_worship': 'chiesa',
    'townhall': 'municipio', 'police': 'polizia', 'post_office': 'posta', 'bank': 'banca',
    'fuel': 'benzinaio', 'motorcycle_parking': 'parcheggio moto', 'parking': 'parcheggio',
    'marketplace': 'mercato', 'community_centre': 'centro civico', 'courthouse': 'tribunale',
    'nightclub': 'discoteca', 'veterinary': 'veterinario', 'coworking_space': 'coworking',
    'events_venue': 'eventi', 'exhibition_centre': 'fiera', 'conference_centre': 'congressi',
    'attraction': 'attrazione', 'museum': 'museo', 'gallery': 'galleria', 'hotel': 'hotel',
    'hostel': 'ostello', 'viewpoint': 'panorama', 'artwork': 'opera d\'arte',
    'park': 'parco', 'garden': 'giardino', 'sports_centre': 'centro sportivo', 'stadium': 'stadio',
    'fitness_centre': 'palestra', 'swimming_pool': 'piscina', 'playground': 'parco giochi',
    'supermarket': 'supermercato', 'convenience': 'alimentari', 'electronics': 'elettronica',
    'mall': 'centro commerciale', 'department_store': 'grande magazzino', 'clothes': 'abbigliamento',
    'bakery': 'panetteria', 'books': 'libreria', 'hairdresser': 'parrucchiere', 'motorcycle': 'moto',
    'mobile_phone': 'telefonia', 'shoes': 'scarpe', 'optician': 'ottico', 'florist': 'fiorista',
    'butcher': 'macelleria', 'hardware': 'ferramenta', 'doityourself': 'fai da te', 'furniture': 'arredamento',
    'sports': 'sport', 'chemist': 'drogheria', 'cosmetics': 'cosmetici', 'jewelry': 'gioielleria',
    'station': 'stazione', 'subway': 'metro',
}


# luoghi utili anche senza nome (per uno scooter): il nome diventa la categoria o la marca
NAMELESS_OK = {'motorcycle_parking': 'Parcheggio moto', 'fuel': 'Benzinaio'}
# categorie dei luoghi "famosi", che nella ricerca vengono prima
LANDMARK = {'attrazione', 'monumento', 'museo', 'chiesa', 'stazione', 'metro', 'università', 'ospedale', 'parco',
            'teatro', 'stadio', 'galleria', 'fiera', 'tribunale', 'municipio', 'centro commerciale', 'biblioteca'}


# ---------- sfondo della mappa (solo disegno) ----------
PARK_LEISURE = {'park', 'garden', 'playground', 'pitch', 'dog_park', 'common', 'nature_reserve'}
PARK_LANDUSE = {'grass', 'recreation_ground', 'cemetery', 'forest', 'meadow', 'village_green', 'flowerbed'}
PARK_NATURAL = {'wood', 'scrub', 'grassland'}


def bg_kind(tags):
    """tipo di elemento di sfondo: parchi, acqua, canali, ferrovie, edifici (o None)"""
    if tags.get('leisure') in PARK_LEISURE or tags.get('landuse') in PARK_LANDUSE or tags.get('natural') in PARK_NATURAL:
        return 'parchi'
    if tags.get('natural') == 'water' or tags.get('landuse') in ('reservoir', 'basin') or tags.get('waterway') == 'riverbank':
        return 'acqua'
    if tags.get('waterway') in ('canal', 'river') and tags.get('tunnel') not in ('yes', 'culvert'):
        return 'canali'
    if tags.get('railway') == 'rail' and tags.get('tunnel') != 'yes' and tags.get('service') not in ('siding', 'yard', 'spur'):
        return 'ferrovie'
    b = tags.get('building')
    if b and b != 'no' and tags.get('location') != 'underground' and tags.get('layer', '0')[:1] != '-':
        return 'edifici'
    return None


def poi_ok(tags):
    return ('name' in tags or 'brand' in tags or tags.get('amenity') in NAMELESS_OK) and poi_category(tags)


def poi_category(tags):
    a, s, t, l = tags.get('amenity'), tags.get('shop'), tags.get('tourism'), tags.get('leisure')
    if tags.get('railway') == 'station' or tags.get('public_transport') == 'station':
        return 'metro' if tags.get('station') == 'subway' or tags.get('subway') == 'yes' else 'stazione'
    if a in POI_KEEP_AMENITY:
        return POI_LABEL.get(a, a)
    if s:
        return POI_LABEL.get(s, 'negozio')
    if t in POI_KEEP_TOURISM:
        return POI_LABEL.get(t, t)
    if l in POI_KEEP_LEISURE:
        return POI_LABEL.get(l, l)
    if tags.get('historic') in ('monument', 'castle', 'church', 'building', 'city_gate', 'memorial', 'ruins', 'archaeological_site'):
        return 'monumento'
    if tags.get('building') in ('cathedral', 'church', 'basilica'):
        return 'chiesa'
    if tags.get('office'):
        return 'ufficio'
    return None


class Reader(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.loc = {}            # nodo osm -> (lat, lon), solo nel rettangolo
        self.signals = set()
        self.ways = {}           # strade: id -> (tags, refs)
        self.tram = []           # liste di riferimenti dei binari del tram
        self.squares = []        # aree pedonali (solo disegno)
        self.poi_nodes = []
        self.poi_ways = []
        self.ring_ways = set()   # vie della filovia 90/91
        self.restr = []
        self.poi_rels = []
        self.bg = []       # sfondo: (tipo, riferimenti) e (tipo, [vie esterne]) per i multipoligoni
        self.bg_rels = []
        self.way_refs_any = {}   # riferimenti di vie usate da relazioni (anello)

    def node(self, n):
        if not n.location.valid():
            return
        lon, lat = n.location.lon, n.location.lat
        if not (BBOX[0] <= lon <= BBOX[2] and BBOX[1] <= lat <= BBOX[3]):
            return
        self.loc[n.id] = (lat, lon)
        tags = n.tags
        if tags.get('highway') == 'traffic_signals':
            self.signals.add(n.id)
        if poi_ok(tags):
            self.poi_nodes.append((dict(tags), lat, lon))

    def way(self, w):
        refs = [r.ref for r in w.nodes]
        if not any(r in self.loc for r in refs):
            return  # fuori dal rettangolo di Milano
        tags = w.tags
        hw = tags.get('highway')
        self.way_refs_any[w.id] = refs
        if hw in CLASSES or hw in MINOR:
            self.ways[w.id] = (dict(tags), refs)
            if hw == 'pedestrian' and tags.get('area') == 'yes':
                self.squares.append(refs)
        elif tags.get('area:highway') == 'pedestrian':
            self.squares.append(refs)
        if tags.get('railway') == 'tram':
            self.tram.append(refs)
        if poi_ok(tags):
            self.poi_ways.append((dict(tags), refs))
        k = bg_kind(tags)
        if k:
            self.bg.append((k, refs))

    def relation(self, r):
        tags = r.tags
        t = tags.get('type')
        # luoghi disegnati come gruppi di forme (es. Duomo, Castello Sforzesco, Politecnico)
        if t == 'multipolygon' and bg_kind(tags) in ('parchi', 'acqua', 'edifici'):
            self.bg_rels.append((bg_kind(tags), [m.ref for m in r.members if m.type == 'w' and m.role in ('outer', '')]))
        if t in ('multipolygon', 'building', 'site') and poi_ok(tags):
            ways = [m.ref for m in r.members if m.type == 'w' and m.role in ('outer', '', 'outline')]
            if ways and any(w in self.way_refs_any for w in ways):
                self.poi_rels.append((dict(tags), ways))
        if t == 'route' and tags.get('route') == 'trolleybus' and tags.get('ref') in ('90', '91'):
            for m in r.members:
                if m.type == 'w':
                    self.ring_ways.add(m.ref)
        elif t == 'restriction':
            kind, modes = None, 3
            if 'restriction' in tags:
                kind = tags['restriction']
            elif 'restriction:motorcycle' in tags:
                kind, modes = tags['restriction:motorcycle'], 1
            elif 'restriction:motorcar' in tags:
                kind, modes = tags['restriction:motorcar'], 2
            elif 'restriction:motor_vehicle' in tags:
                kind = tags['restriction:motor_vehicle']
            if not kind:
                return
            exc = set(x.strip() for x in (tags.get('except') or '').split(';'))
            if 'motorcycle' in exc or 'motor_vehicle' in exc:
                modes &= ~1
            if 'motorcar' in exc or 'motor_vehicle' in exc:
                modes &= ~2
            fr, via, to, via_way, via_ways = [], [], [], False, []
            for m in r.members:
                if m.role == 'from' and m.type == 'w':
                    fr.append(m.ref)
                elif m.role == 'to' and m.type == 'w':
                    to.append(m.ref)
                elif m.role == 'via':
                    if m.type == 'n':
                        via.append(m.ref)
                    else:
                        via_way = True
                        via_ways.append(m.ref)
            self.restr.append({'id': r.id, 'kind': kind, 'modes': modes, 'from': fr, 'via': via, 'to': to,
                               'via_way': via_way, 'via_ways': via_ways, 'conditional': 'restriction:conditional' in tags})


def title_it(s):
    """'DEL DUOMO' -> 'del Duomo' con le particelle in minuscolo."""
    small = {'di', 'del', 'della', 'dei', 'degli', 'delle', 'dello', 'de', 'da', 'dal', 'dalla', 'e',
             'al', 'alla', 'ai', 'agli', 'alle', 'allo', 'in', 'a', 'sul', 'sulla', 'van', 'von', 'la', 'le', 'lo'}
    out = []
    for i, w in enumerate(s.lower().split()):
        if i > 0 and w in small:
            out.append(w)
        else:
            out.append("'".join(p[:1].upper() + p[1:] for p in w.split("'")))
    return ' '.join(out)


def norm(s):
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^a-z0-9]+', ' ', s.lower()).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--osm', required=True)
    ap.add_argument('--osm-state', help='file .state.txt con la data dell\'estratto')
    ap.add_argument('--civici', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--regole', default=os.path.join(os.path.dirname(__file__), 'regole_milano.json'),
                    help='regole ufficiali del Comune che correggono OSM')
    ap.add_argument('--sfondo', help='file dove scrivere lo sfondo della mappa (parchi, acqua, ferrovie, edifici)')
    ap.add_argument('--cache', help='file dove salvare la lettura di OSM per rifare i dati piu\' in fretta')
    a = ap.parse_args()

    if a.cache and os.path.exists(a.cache) and os.path.getmtime(a.cache) > os.path.getmtime(a.osm):
        print('Uso la lettura di OpenStreetMap gia\' salvata', file=sys.stderr)
        with open(a.cache, 'rb') as f:
            rd = pickle.load(f)
    else:
        print('Leggo OpenStreetMap...', file=sys.stderr)
        rd = Reader()
        rd.apply_file(a.osm, locations=False)
        if a.cache:
            with open(a.cache, 'wb') as f:
                pickle.dump(dict(rd.__dict__), f)
    if isinstance(rd, dict):
        state = rd
        rd = Reader.__new__(Reader)
        rd.__dict__.update(state)
    print(f'  nodi nel rettangolo {len(rd.loc)}, strade {len(rd.ways)}, vie 90/91 {len(rd.ring_ways)}', file=sys.stderr)

    loc = rd.loc

    def xy(nid):
        lat, lon = loc[nid]
        return proj(lat, lon)

    # ---------- confine: anello della 90/91 + margine ----------
    lines = []
    for wid in rd.ring_ways:
        refs = rd.way_refs_any.get(wid)
        if not refs:
            continue
        pts = [xy(r) for r in refs if r in loc]
        if len(pts) >= 2:
            lines.append(LineString(pts))
    if not lines:
        sys.exit('Non trovo il percorso della filovia 90/91: impossibile disegnare il confine')
    ring = unary_union(lines)

    def ring_area(margin):
        band = ring.buffer(margin, join_style='round')
        polys = list(band.geoms) if band.geom_type == 'MultiPolygon' else [band]
        outer = max(polys, key=lambda p: p.area)
        if not outer.interiors:
            sys.exit('La linea della 90/91 non forma un anello chiuso: controlla i dati')
        return Polygon(outer.exterior)

    zone = ring_area(ZONE_MARGIN)
    area = ring_area(ROAD_MARGIN)
    area_p = prep(area)
    inside = {}

    def is_in(nid):
        v = inside.get(nid)
        if v is None:
            v = nid in loc and area_p.contains(Point(xy(nid)))
            inside[nid] = v
        return v

    # ---------- strade ----------
    names, name_idx = [], {}

    def nidx(n):
        if not n:
            return -1
        if n not in name_idx:
            name_idx[n] = len(names)
            names.append(n)
        return name_idx[n]

    # regole del Comune: tag che sostituiscono quelli di OSM su vie precise
    override, rule_of = {}, {}
    if a.regole and os.path.exists(a.regole):
        for i, r in enumerate(json.load(open(a.regole))['regole']):
            for osm in r['osm']:
                override.setdefault(osm, {}).update(r['tag'])
                rule_of[osm] = i
    applied = set()

    ways, way_idx, runs = [], {}, []
    conditional, fixme = [], []
    cond_used, cond_skipped = 0, []
    for wid, (tags, refs) in rd.ways.items():
        if wid in override:
            tags = dict(tags)
            tags.update(override[wid])
            applied.add(wid)
        hw = tags['highway']
        if tags.get('area') == 'yes':
            continue
        if hw in ('construction', 'proposed') or tags.get('construction'):
            continue
        mf, mb = permissions(tags, hw, MOTO_KEYS, MOTO_ONEWAY)
        cf, cb = permissions(tags, hw, CAR_KEYS, CAR_ONEWAY)
        if hw in MINOR and not (mf or mb):
            continue
        if not (mf or mb or cf or cb) and hw not in ('pedestrian',):
            # vietata a tutti: la teniamo solo se pedonale (si disegna e si puo' correggere)
            if hw not in CLASSES:
                continue
        # tratti consecutivi dentro l'area
        cur, parts = [], []
        for r in refs:
            if is_in(r):
                cur.append(r)
            else:
                if len(cur) >= 2:
                    parts.append(cur)
                cur = []
        if len(cur) >= 2:
            parts.append(cur)
        if not parts:
            continue
        cls = CLASSES.get(hw, 7)
        w = [wid, nidx(tags.get('name') or ''), cls, mf, mb, cf, cb,
             1 if 2 in (mf, mb) else 0,
             1 if first_value(tags.get('surface')) in PAVE else 0,
             parse_speed(tags.get('maxspeed')),
             1 if tags.get('service') in DRIVEWAY else 0,
             1 if hw == 'pedestrian' else 0,
             1 if tags.get('junction') in ('roundabout', 'circular') else 0]
        rules, skipped = conditional_rules(tags, hw)
        rules = [r for r in rules if r[1:] != [mf, mb, cf, cb]]  # regole che non cambiano nulla
        if rules:
            w.append(rules)
            cond_used += 1
        if skipped:
            cond_skipped.append((wid, skipped))
        way_idx[wid] = len(ways)
        ways.append(w)
        for p in parts:
            runs.append((len(ways) - 1, p))
        if any(k in tags for k in CONDITIONAL_KEYS):
            conditional.append(wid)
        if (mf or mb) and ('fixme' in tags or 'FIXME' in tags):
            fixme.append(wid)

    # Nodi OSM diversi nello stesso punto (errore di disegno frequente): si uniscono, altrimenti la strada
    # resterebbe "spezzata" in quel punto. Si tengono i nodi consecutivi a meno di 5 cm.
    alias = {}

    def root(n):
        while n in alias:
            n = alias[n]
        return n
    for _, p in runs:
        for k in range(len(p) - 1):
            a_, b_ = root(p[k]), root(p[k + 1])
            if a_ != b_ and haversine(*loc[a_], *loc[b_]) < 0.05:
                alias[b_] = a_
    merged = 0
    if alias:
        new_runs = []
        for wi, p in runs:
            q = []
            for r in p:
                r = root(r)
                if not q or q[-1] != r:
                    q.append(r)
            if len(q) >= 2:
                new_runs.append((wi, q))
        merged = len(alias)
        runs = new_runs
    print(f'  nodi doppi uniti: {merged}', file=sys.stderr)

    # nodi del grafo: estremi dei tratti e incroci (nodi condivisi)
    use = defaultdict(int)
    for _, p in runs:
        for r in p:
            use[r] += 1
        use[p[0]] += 1
        use[p[-1]] += 1
    gnode, nodes = {}, []

    def gidx(r):
        if r not in gnode:
            gnode[r] = len(nodes)
            x, y = xy(r)
            nodes.append([round(x), round(y)])
        return gnode[r]

    edges = []
    for wi, p in runs:
        start = 0
        for i in range(1, len(p)):
            if use[p[i]] >= 2 or i == len(p) - 1:
                seg = p[start:i + 1]
                length = sum(haversine(*loc[seg[k]], *loc[seg[k + 1]]) for k in range(len(seg) - 1))
                if length < 0.05:  # non succede piu' (nodi doppi uniti sopra): per sicurezza
                    start = i
                    continue
                sig = sum(1 for r in seg[1:-1] if r in rd.signals) + 0.5 * ((seg[0] in rd.signals) + (seg[-1] in rd.signals))
                flat = []
                for r in seg[1:-1]:
                    x, y = xy(r)
                    flat += [round(x), round(y)]
                edges.append([gidx(seg[0]), gidx(seg[-1]), wi, round(length, 1), sig, flat, 0])
                start = i

    # ---------- binari del tram (per la modalita' pioggia) ----------
    tram_lines = []
    for refs in rd.tram:
        pts = [xy(r) for r in refs if r in loc]
        if len(pts) >= 2:
            tram_lines.append(LineString(pts).buffer(5))
    if tram_lines:
        tree = STRtree(tram_lines)
        for e in edges:
            g = [nodes[e[0]]] + [e[5][i:i + 2] for i in range(0, len(e[5]), 2)] + [nodes[e[1]]]
            ls = LineString(g)
            if ls.length <= 0:
                continue
            cand = tree.query(ls)
            if len(cand):
                cov = unary_union([tram_lines[i] for i in cand]).intersection(ls).length
                if cov / ls.length >= 0.5:
                    e[6] = 1

    # ---------- divieti di svolta ----------
    restr, restr_skipped = [], defaultdict(int)
    restr_via = []
    for r in rd.restr:
        if not (set(r['from']) & way_idx.keys() and set(r['to']) & way_idx.keys()):
            continue  # fuori da Milano
        if r['via_way']:
            # divieto attraverso una o piu' strade (es. inversione a U nel varco dello spartitraffico):
            # [daVia, [vieIntermedie], aVia, mezzi]. Solo i divieti ("no_"); gli obblighi "only_" via strada sono rarissimi.
            vw = r.get('via_ways', [])
            if (r['kind'].startswith('no_') and len(r['from']) == 1 and len(r['to']) == 1 and vw
                    and all(w in way_idx for w in vw + r['from'] + r['to']) and r['modes']):
                restr_via.append([way_idx[r['from'][0]], [way_idx[w] for w in vw], way_idx[r['to'][0]], r['modes']])
            else:
                restr_skipped['via una strada (non gestito)'] += 1
            continue
        if r['conditional']:
            restr_skipped['solo in certi orari'] += 1
        if len(r['from']) != 1 or len(r['to']) != 1 or len(r['via']) != 1:
            restr_skipped['incompleto'] += 1
            continue
        fw, tw, via = r['from'][0], r['to'][0], r['via'][0]
        if fw not in way_idx or tw not in way_idx or via not in gnode or r['modes'] == 0:
            continue
        only = 1 if r['kind'].startswith('only_') else 0
        if not only and not r['kind'].startswith('no_'):
            restr_skipped['tipo sconosciuto'] += 1
            continue
        restr.append([way_idx[fw], gnode[via], way_idx[tw], only, r['modes']])

    # ---------- aree pedonali (disegno) ----------
    ctx = []
    for refs in rd.squares:
        pts = [xy(r) for r in refs if r in loc]
        if len(pts) >= 3 and area_p.intersects(LineString(pts)):
            flat = []
            for x, y in pts:
                flat += [round(x), round(y)]
            ctx.append(flat)

    # ---------- numeri civici del Comune ----------
    osm_names = {norm(n): n for n in names if n}
    addr = defaultdict(list)
    civ_date = ''
    seen = set()
    with open(a.civici, encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f, delimiter=';'))
    base_numbers = {(r['CODICE_VIA'], r['NUMERO'] + r['LETTERA'].strip() + ('/' + r['BARRA'].strip() if r['BARRA'].strip() else ''))
                    for r in rows if not r['BARRA2'].strip()}
    for r in rows:
        if r['STATOCIVICO'] == 'In attesa Fine Lavori':
            continue
        num = r['NUMERO'] + r['LETTERA'].strip().upper() + ('/' + r['BARRA'].strip() if r['BARRA'].strip() else '')
        if r['BARRA2'].strip() and (r['CODICE_VIA'], num) in base_numbers:
            continue  # accesso secondario: c'e' gia' il civico principale
        try:
            lat, lon = float(r['LAT_WGS84']), float(r['LONG_WGS84'])
        except ValueError:
            continue
        x, y = proj(lat, lon)
        if not area_p.contains(Point(x, y)):
            continue
        full = title_it(r['TIPO'] + ' ' + r['DESCRITTIVO'])
        name = osm_names.get(norm(full), full)
        key = (name, num)
        if key in seen:
            continue
        seen.add(key)
        addr[name].append([num, round(x), round(y)])
        civ_date = max(civ_date, r.get('ULTIMA_MODIFICA') or '')
    for v in addr.values():
        v.sort(key=lambda a: (int(re.match(r'\d+', a[0]).group()) if re.match(r'\d+', a[0]) else 0, a[0]))

    # ---------- luoghi di interesse ----------
    poi, seen_poi = [], set()

    def add_poi(tags, lat, lon):
        x, y = proj(lat, lon)
        if not area_p.contains(Point(x, y)):
            return
        cat = poi_category(tags)
        name = tags.get('name') or tags.get('brand') or NAMELESS_OK.get(tags.get('amenity'))
        extra = ' '.join(sorted({tags[k] for k in ('brand', 'alt_name', 'short_name', 'official_name', 'name:en', 'old_name')
                                 if tags.get(k) and tags[k] != name}))
        key = (name, cat, round(x / 30), round(y / 30))
        if key in seen_poi:
            return
        seen_poi.add(key)
        # importanza: 2 = luogo famoso (es. Duomo), 1 = noto (ha una voce Wikipedia/Wikidata), 0 = altro
        imp = (1 if ('wikidata' in tags or 'wikipedia' in tags) else 0) + (1 if cat in LANDMARK else 0)
        poi.append([name, extra, cat, round(x), round(y), imp])

    for tags, lat, lon in rd.poi_nodes:
        add_poi(tags, lat, lon)
    for tags, refs in rd.poi_ways:
        pts = [loc[r] for r in refs if r in loc]
        if pts:
            add_poi(tags, sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    for tags, ways_ in getattr(rd, 'poi_rels', []):
        pts = [loc[r] for w in ways_ for r in rd.way_refs_any.get(w, []) if r in loc]
        if pts:
            add_poi(tags, sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    osm_date = ''
    if a.osm_state:
        for line in open(a.osm_state):
            if line.startswith('timestamp='):
                osm_date = line.split('=', 1)[1].strip().replace('\\:', ':')
    data = {
        'meta': {
            'osm': osm_date,
            'civici': civ_date,
            'costruito': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%MZ'),
            'confine': 'zona: filovia 90/91 + %d m; strade fino a %d m' % (ZONE_MARGIN, ROAD_MARGIN),
            'regole': 'scooter 125cc = motorcycle (moped ignorato); %d vie corrette con le regole del Comune' % len(applied),
        },
        'origin': ORIGIN, 'names': names, 'ways': ways, 'nodes': nodes, 'edges': edges,
        'restr': restr, 'restrv': restr_via, 'ctx': ctx, 'addr': dict(addr), 'poi': poi,
        'area': [[round(x), round(y)] for x, y in zone.exterior.simplify(5).coords],
    }
    if a.sfondo:
        write_background(rd, loc, xy, area, area_p, a.sfondo, osm_date)
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    print(f'Fatto: {len(ways)} vie, {len(edges)} tratti, {len(nodes)} incroci, {len(restr)} divieti di svolta, '
          f'{sum(len(v) for v in addr.values())} civici, {len(poi)} luoghi', file=sys.stderr)
    print(f'  divieti di svolta attraverso una strada: {len(restr_via)}; saltati: {dict(restr_skipped)}', file=sys.stderr)
    print(f'  vie con regole a orario: {cond_used} applicate; vie con nota "da verificare": {len(fixme)}', file=sys.stderr)
    print(f'  regole del Comune applicate a {len(applied)} vie su {len(override)}', file=sys.stderr)
    for osm in sorted(set(override) - applied):
        print(f'  ATTENZIONE: la regola {rule_of[osm] + 1} cita la via {osm} che non c\'e\' piu\' in OSM (o e\' fuori zona)', file=sys.stderr)
    for wid, sk in cond_skipped:
        print(f'  regola a orario non letta (way {wid}): {sk}', file=sys.stderr)
    with open(a.out.rsplit('.', 1)[0] + '.note.json', 'w') as f:
        json.dump({'conditional': conditional, 'fixme': fixme,
                   'regole_mancanti': [[osm, rule_of[osm] + 1] for osm in sorted(set(override) - applied)],
                   'orari_non_letti': [[wid, sk] for wid, sk in cond_skipped]}, f, ensure_ascii=False)


def write_background(rd, loc, xy, area, area_p, path, osm_date):
    """Sfondo: ogni forma e' [x0, y0, dx1, dy1, ...] in metri interi (differenze dal punto prima: file piu' piccolo)."""
    from shapely.geometry import LinearRing
    out = {'parchi': [], 'acqua': [], 'canali': [], 'ferrovie': [], 'edifici': []}
    SIMPL = {'parchi': 3, 'acqua': 2, 'canali': 3, 'ferrovie': 4, 'edifici': 1.2}
    MIN_AREA = {'parchi': 150, 'acqua': 50, 'edifici': 25}

    def enc(coords):
        flat, px, py = [], 0, 0
        for i, (x, y) in enumerate(coords):
            x, y = round(x), round(y)
            if i and x == px and y == py:
                continue
            flat += [x, y] if i == 0 else [x - px, y - py]
            px, py = x, y
        return flat

    def add(kind, refs):
        pts = [xy(r) for r in refs if r in loc]
        if len(pts) < 2:
            return
        line = kind in ('canali', 'ferrovie')
        if line:
            g = LineString(pts)
        else:
            if len(pts) < 4:
                return
            g = Polygon(pts)
            if not g.is_valid:
                g = g.buffer(0)
            if g.is_empty or g.area < MIN_AREA[kind]:
                return
        if not area_p.intersects(g):
            return
        g = g.simplify(SIMPL[kind], preserve_topology=True)
        if g.is_empty:
            return
        geoms = list(g.geoms) if hasattr(g, 'geoms') else [g]
        for gg in geoms:
            coords = list(gg.coords) if line else list(gg.exterior.coords)
            f = enc(coords)
            if len(f) >= 4:
                out[kind].append(f)

    for kind, refs in rd.bg:
        add(kind, refs)
    for kind, ways in rd.bg_rels:
        # multipoligono: si uniscono le vie esterne in anelli chiusi
        from shapely.ops import linemerge, polygonize
        coords = [[xy(r) for r in rd.way_refs_any.get(w, []) if r in loc] for w in ways]
        lines = [LineString(c) for c in coords if len(c) >= 2]
        if not lines:
            continue
        for poly in polygonize(linemerge(lines)):
            if poly.area >= MIN_AREA[kind] and area_p.intersects(poly):
                f = enc(list(poly.simplify(SIMPL[kind], preserve_topology=True).exterior.coords))
                if len(f) >= 4:
                    out[kind].append(f)
    out['meta'] = {'osm': osm_date}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, separators=(',', ':'))
    print('  sfondo: ' + ', '.join(f'{k} {len(v)}' for k, v in out.items() if k != 'meta'), file=sys.stderr)


if __name__ == '__main__':
    main()
