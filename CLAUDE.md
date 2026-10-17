# Scootly

## Scopo

Web app personale per muoversi a Milano in **scooter 125cc (Euro 4/5)**.
Google Maps tratta lo scooter come un'auto e fa fare giri lunghi; questa app
calcola il percorso migliore per lo scooter conoscendo le regole di ogni strada
(sensi unici, strade aperte solo alle moto, corsie riservate aperte alle moto,
svolte vietate, zone pedonali).

- Zona coperta: **dentro la circonvallazione esterna** (anello della filovia 90/91).
- Il tragitto principale dell'utente è casa → ufficio, in centro. **Gli indirizzi personali non vanno mai
  scritti nei file né nei messaggi dei commit** (il repository è pubblico): per provarli usa
  `tools/tragitti_privati.json` (escluso da git, formato in `tools/README.md`); se manca, chiedili all'utente.
- L'utente è maggiorenne con patente A e B: le superstrade urbane (motorroad) sono permesse al 125.
- Usata su **iPhone (Safari / app sulla schermata Home)**, pubblicata su **GitHub Pages**.
- Il proprietario **non è uno sviluppatore**: spiegagli sempre tutto in modo semplice
  e **in italiano**, senza gergo tecnico (o spiegandolo).

## Come è costruita

Nessun framework, nessuna compilazione, nessuna dipendenza: GitHub Pages serve i file così come sono.

| File | Cosa fa |
|---|---|
| `index.html` | **L'app** (~95 KB): stile, motore percorsi, interfaccia, navigazione. |
| `data/strade.json` | **La mappa** (~3 MB): strade, regole, civici, luoghi. Generata da `tools/build_data.py`, non si modifica a mano. |
| `data/sfondo.json` | **Lo sfondo** (~1,5 MB, ~550 KB compresso): parchi, acqua, Navigli, ferrovie, edifici. Generato da `build_data.py --sfondo`; caricato dopo le strade (l'app è usabile subito). Ogni forma è `[x0, y0, dx1, dy1, ...]` (differenze in metri). |
| `data/rapporto.md` | Rapporto in italiano sull'ultima mappa generata (`tools/check_data.py`). |
| `sw.js` | Service worker: copia app e mappa sul telefono per usarle anche senza rete. Pagine: prima la rete; mappa: subito la copia salvata, la nuova si scarica in sottofondo. |
| `tools/` | Strumenti per costruire, controllare e collaudare i dati (vedi `tools/README.md`). |
| `.github/workflows/prove.yml` | A ogni richiesta di pubblicazione (e su `main`): `collaudo.js` e `tools/prove_app.js`, l'app vera in Chromium come un iPhone (avvio, dati vecchi, ricerca, preferiti, guida con voce e tempi, tema scuro, iPhone SE). |
| `.github/workflows/aggiorna-mappa.yml` | Ogni lunedì ricostruisce la mappa e la pubblica solo se passano i controlli (regole del Comune tutte applicate, nessun disaccordo nuovo con il Comune, nessun cambiamento anomalo, collaudo). Dettagli in `tools/README.md`. |
| `manifest.webmanifest`, `icon-*.png`, `apple-touch-icon.png` | Installazione come app sulla Home. |

Dentro `index.html`, in ordine:

1. **CSS** con tema chiaro/scuro: scuro se lo è l'iPhone, e comunque da 20 min dopo il tramonto a 10 min
   prima dell'alba (`nightTheme()`, alba e tramonto calcolati per Milano).
2. **Motore percorsi** (`ScooterRouter`, il blocco che inizia con `// Motore di calcolo percorsi`, riga ~233):
   funziona sia nel browser sia in Node (`module.exports`); gli strumenti in `tools/` lo leggono da qui.
3. **Interfaccia** (`(async function () {`, riga ~675–1556): carica `data/strade.json`, ricerca indirizzi,
   disegno della mappa su `<canvas>`, scheda risultati, modo correzione, copia di sicurezza, navigazione GPS con voce.

### Formato dei dati (`data/strade.json`)

Si costruiscono con `tools/build_data.py` (OpenStreetMap + civici ufficiali del Comune + `tools/regole_milano.json`),
si controllano con `tools/check_data.py` e si collaudano con `tools/collaudo.js`: vedi `tools/README.md`.
(Fino all'ottobre 2026 erano incorporati in `index.html`, con una versione vecchia di origine sconosciuta.) Contiene
`meta` (date delle fonti), `poi` (luoghi di interesse), `area` (zona coperta), un quinto valore
in `restr` (a chi vale il divieto: 1 moto, 2 auto, 3 entrambi) e, solo per le vie che le hanno,
un campo 13 con le regole a orario: `[[finestre, motoAvanti, motoIndietro, autoAvanti, autoIndietro], ...]`,
finestra = `[mesi (bit 0 = gennaio), giorni (bit 0 = lunedì), inizioMin, fineMin]` (fine ≤ inizio = passa la mezzanotte).
Il motore le attiva con `setTime(data)` (l'app usa l'ora attuale a ogni calcolo).

- `origin`: `[lat0, lon0, KX, KY]`. Coordinate in metri: `x = (lon-lon0)*KX`, `y = (lat-lat0)*KY` (y verso nord).
  Il GPS usa la stessa formula (verificato: i nodi coincidono con OSM proiettato così).
- `names`: elenco dei nomi delle vie.
- `ways[i]`: `[osmId, nomeIdx, classe, motoAvanti, motoIndietro, autoAvanti, autoIndietro, soloDestinatari, pave, limite, passoCarrabile, pedonale, rotonda]`
  - classe: `0` trunk, `1` primary, `2` secondary, `3` tertiary, `4` residential, `5` living_street, `6` service, `7` pedestrian, `8` motorway
  - permessi per verso: `0` vietato, `1` permesso, `2` solo destinatari (vedi `destZones()`)
  - "avanti" = verso in cui la via è disegnata in OSM (frecce bianche nel modo correzione)
  - `limite` = maxspeed in km/h (`0` = non indicato); `passoCarrabile` = service driveway/parcheggio
- `nodes[i]`: `[x, y]` in metri (interi).
- `edges[i]`: `[nodoA, nodoB, wayIdx, lunghezzaM, semafori, [x,y,... punti intermedi], tram]`
- `restr[i]`: `[wayIdxDa, nodoVia, wayIdxA, soloQuesta, mezzi]` (`1` = svolta obbligatoria, `0` = svolta vietata; mezzi: 1 moto, 2 auto, 3 entrambi). Indici di `ways`, non osmId.
- `restrv[i]`: `[wayIdxDa, [wayIdxIntermedie], wayIdxA, mezzi]`: divieti attraverso una o più strade
  (es. inversione a U nel varco dello spartitraffico). Nel motore lo stato di Dijkstra è
  `verso*32 + gruppo`: chi entra nelle vie intermedie dalla via di partenza è "marcato" e non può andare
  sulle vie vietate; gli stati marcati (pochi) stanno in una Map.
- `ctx`: linee delle zone pedonali (solo disegno).
- `addr`: `{ "Nome via": [[civico, x, y], ...] }`.
- `poi[i]`: `[nome, altriNomi (marca, nome breve...), categoria in italiano, x, y, importanza 0-2]`.

### Motore percorsi (in breve)

- Grafo a "archi orientati" (`2*edge + verso`), Dijkstra con costo in secondi.
- Velocità per classe (`SPEED`), limite × 0,85, pavé più lento, semafori, svolte a sinistra/destra,
  inversioni a U vietate agli incroci, svolte vietate/obbligatorie da OSM (anche solo per un mezzo).
- Strade "solo destinatari" (permesso 2): raggruppate in zone (`destZones()`); in una zona si entra
  solo se c'è l'arrivo e se ne esce solo se c'era la partenza. Niente più penalità fissa.
- `route()` restituisce anche `edges` (versi usati), `d0s`/`t0s` (metri e secondi dalla partenza per tratto).
- Arrivo (`snapTargets()`): oltre alla strada percorribile più vicina considera le altre entro
  max(30, min(250, 1,5 × distanza)) m, con una penalità di `WALK_S` = 1,5 s per ogni metro in più a piedi
  (es. Duomo: arrivo in Piazza Diaz, 1,7 km invece di 3,0). Il pezzo a piedi è disegnato a puntini.
- `snap()` aggancia un punto alla strada percorribile più vicina (griglia da 150 m) **che sia nella
  rete principale** (`mainNet()`: versi da cui si raggiunge tutto e si torna), così partenza e arrivo
  non finiscono in vicoli isolati. La rete si ricalcola quando cambiano regole a orario o correzioni.
- `snap(x, y, mode, heading)` / `route(from, to, mode, { heading })`: con la direzione di marcia la partenza
  va sulla strada e nel verso coerenti (usato nei ricalcoli in navigazione).
- Modalità pioggia (`setRain`): pavé ×2,5 e binari del tram ×3 nel costo. Resta nel motore ma l'app non la usa più
  (tolta su richiesta del proprietario; la vecchia chiave `scooter-pioggia` si cancella all'avvio).
- Correzioni dell'utente per osmId: `vietata`, `aperta`, `senso`, `senso_inv`, `evita` (×3), `preferita` (×0,7),
  `chiusa` (per lavori, con `fino`: data ISO; scaduta = ignorata e tolta da `purgeExpired()`). In guida il
  pulsante "Strada chiusa" propone la via attuale e le prossime entro 400 m e le chiude per 3 giorni
  (solo i tratti del percorso, non tutta la via); dalla schermata correzioni: oggi, 1 settimana, 1 mese.
- Il percorso "auto" è calcolato solo per confronto.

### Apertura e introduzione

Apertura (`#splash`, a ogni avvio, fa da schermata di caricamento): icona che entra con lo scooter, alone morbido (una volta sola, niente animazioni ripetute),
nome lettera per lettera (nessuna barra di caricamento: mentre si prepara la mappa non si muove niente, cosi' non scatta). Sparisce quando la mappa è pronta (evento `scootly-ready`,
mandato anche se la mappa non si carica), dopo almeno 2,6 s alla prima apertura e 0,9 s (versione veloce)
le volte dopo; al massimo 8 s; a mappa pronta un tocco la salta.
Per non far scattare le animazioni, il lavoro pesante non si sovrappone: `data/strade.json` si scarica subito ma
si legge solo quando il logo è entrato (`window.__entered`: 1,7 s la prima volta, 0,55 s dopo); lo sfondo
(`loadBg()`) si carica 0,4 s dopo che l'apertura è sparita (la prima volta, dopo l'introduzione). Solo animazioni
di `transform`/`opacity` (niente `filter: blur` animati, niente `left`/`width`): su iPhone le altre scattano. Poi (evento `scootly-splash`) parte l'introduzione.


Introduzione: overlay `#intro` (HTML, stile e script propri, prima dello script dell'interfaccia: si vede subito, mentre la mappa si
carica). Due schermate su sfondo "aurora": benvenuto con icona e tre funzioni, poi "Prima di partire" (posizione,
schermo acceso, cartelli, "Aggiungi alla Home" solo se aperta da Safari). Si vede finché manca `scooter-intro`;
finché è aperta (`window.__intro`) l'app non chiede la posizione: la chiede "Consenti posizione e inizia"
(evento `scootly-intro`). Si rivede da Correggi → Altro → "Rivedi l'introduzione" (`window.showIntro()`).

### Disegno della mappa

`draw()` calcola una volta per immagine il rettangolo visibile (`VB`), poi: sfondo (`drawBg()`, con una
trasformazione metri→pixel e un indice a celle da 250 m; edifici solo da `view.s ≥ 0.45`), confine della
zona (`drawArea()`), strade, percorsi, etichette (vie, fermate metro "M", luoghi famosi con importanza 2).
In guida ~16 ms per immagine nel browser di prova.

### GPS (in `index.html`)

- `inBounds()` = c'è mappa (strade fino a 500 m oltre l'anello): il GPS è accettato; `inArea()` = dentro
  la zona coperta (anello + 150 m): fuori c'è solo un avviso.

- Partenza: `locatePrecise()` ascolta il GPS fino a ~25 s, scarta letture vecchie, media le buone
  (scooter fermo), aggiorna la partenza a ogni miglioramento; avvisa se l'iPhone dà solo la posizione
  approssimativa ("Posizione esatta" spenta).
- Navigazione: `project()` cerca la posizione solo poco indietro/avanti sul percorso e considera la
  direzione; fuori percorso se oltre max(30, precisione+15) m per 2 letture e 3 s (o verso opposto per
  3 letture); `animFrame()` fa scorrere l'icona lungo la strada a ~30 fps stimando con la velocità.
  Prova di guida simulata nel browser: rumore ±10 m e una lettura sbagliata di 70 m → 0 ricalcoli.
- Schermata di guida come Google Maps (`renderNav()`): riquadro verde in alto con freccia, distanza e via
  (e "Poi" con la manovra dopo, se entro 250 m), barra in basso con minuti, km e ora di arrivo, X per uscire,
  limite di velocità della via (campo `limite`), "Ricentra" se sposti la mappa. Zoom automatico (`navZoom()`):
  più vicino da fermo e prima delle svolte, più lontano quando vai veloce.
- Voce (come Google Maps, `voiceTick()`): per ogni manovra al massimo tre frasi, con tempi che dipendono
  dalla velocità media `nav.vs` (almeno 25 km/h): appena fatta la manovra precedente "Continua su Via X per
  800 metri" (se la prossima è lontana), preavviso ~22 s prima (≥ 160 m) "Tra 200 metri, svolta a destra in
  Via X", all'incrocio ~7 s prima (35–130 m) "Svolta a destra in Via X" (+ ", poi ..." se la seguente è entro
  max(60 m, 6 s)). Partenza: "Procedi in direzione nord su ...". Arrivo: "la destinazione si trova sulla
  destra/sinistra" (o quanti metri a piedi). Ricalcolo in silenzio, poi la nuova indicazione. Usa la migliore
  voce italiana del telefono (preferisce le voci "Avanzata"/"Premium").

### Dati salvati sul telefono (`localStorage`)

`scooter-corr` (correzioni), `scooter-casa`, `scooter-preferiti` (altri posti con un nome, es. "Lavoro":
`[{nome, via, p: [x, y]}]`, salvati con la stella ☆ accanto a "Vai"; chiamarne uno "Casa" aggiorna `scooter-casa`), `scooter-recenti`, `scooter-viaggi` (vecchio confronto
stimato/reale, usato solo finché non ci sono tempi imparati), `scooter-tempi` (tempi imparati: per via OSM
e verso `[secondi, metri, osservazioni]` e per classe `[secondiVeri, secondiPrevisti]`, con i viaggi recenti
che pesano di più), `scooter-voce`, `scooter-intro` (introduzione già vista), `scooter-tragitti` (tempi veri porta a porta: `[{a, b, s, d, h}]`, partenza
e arrivo in metri, secondi; per lo stesso tragitto, partenza e arrivo entro 300 m, la scheda mostra la media, o la
mediana da 3 viaggi, e l'ora di arrivo in guida segue quei tempi), `scooter-in-corso` (viaggio in guida, salvato ogni
8 s: se l'app viene chiusa entro 400 m dall'arrivo, alla riapertura il viaggio conta con l'ora dell'ultima posizione).

Tempi imparati: in navigazione vera `recPoint()` registra (ora, metri fatti) quando sei sul percorso con
precisione ≤ 30 m; `recFlush()` (a fine viaggio e prima di ogni ricalcolo) calcola il tempo di ogni tratto,
tolte le soste oltre 90 s, e aggiorna `scooter-tempi`; `applyLearned()` li passa al motore (`setLearned`).
Le correzioni "da evitare"/"preferita" cambiano solo la scelta della strada, non i minuti. "Casa" **non** va mai scritta nel codice:
il sito è pubblico.

## Regole del progetto

1. **Spiega in italiano semplice** cosa cambi e perché, prima e dopo ogni modifica.
2. **Un problema alla volta**: una correzione = un commit chiaro. Niente "già che c'ero".
3. **Non rompere quello che funziona**: dopo ogni modifica al motore, prova percorsi reali in Node
   (es. da un indirizzo di prova verso Duomo, Centrale, Loreto, Brera, Porta Romana, Isola, Piazzale Lotto)
   e confronta prima/dopo.
4. **Tutto resta in un'app statica**: niente build, niente librerie esterne, niente server.
   Deve funzionare su iPhone Safari e offline.
5. **Quando cambi `index.html`, aumenta la versione della cache in `sw.js`** (`scooter-vN`),
   così il telefono prende la versione nuova.
6. **Non cambiare il formato dei dati** o le chiavi di `localStorage` senza gestire i dati vecchi
   (le correzioni e la Casa dell'utente non devono andare perse).
7. **Privacy**: nessun indirizzo personale nel codice, nei dati, nella documentazione o nei messaggi dei
   commit; nessun servizio esterno di tracciamento.
8. Quando Comune di Milano e OSM non sono d'accordo **vince il Comune** (decisione del proprietario):
   le correzioni stanno in `tools/regole_milano.json`, ognuna con motivo e ordinanza.
9. Le regole delle strade vengono da OSM, che a volte è sbagliato o vecchio: se l'utente segnala
   una strada sbagliata, verifica su OSM (`https://api.openstreetmap.org/api/0.6/way/<osmId>.json`)
   e spiega se l'errore è nei dati o nel motore.
10. Commenti nel codice in italiano, come quelli esistenti.

## Come provare

```bash
node tools/collaudo.js                      # prove automatiche sui dati in data/strade.json (devono passare tutte)
node tools/prove_app.js                     # prove dell'app nel browser (serve il server qui sotto e Playwright)
node tools/compare_routes.js vecchi.json data/strade.json   # cosa cambia nei tragitti tra due versioni
python3 -m http.server 8765                 # poi apri http://127.0.0.1:8765 (Playwright/Chromium per provarla come un iPhone)
```

Per usare il motore in uno script Node, leggilo da `index.html`:

```js
const html = require('fs').readFileSync('index.html', 'utf8');
const code = html.match(/<script>\s*(\/\/ Motore di calcolo percorsi[\s\S]*?)<\/script>/)[1];
const mod = { exports: {} }; new Function('module', code)(mod);
const R = new mod.exports.Router(require('./data/strade.json'));
R.setTime(new Date('2026-10-20T08:30'));   // le regole a orario dipendono dall'ora
```
