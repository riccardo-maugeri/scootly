# Strumenti per i dati della mappa

Questi programmi **non fanno parte dell'app**: servono a preparare e controllare i dati.
Girano su un computer (o su GitHub), non sul telefono.

| Programma | Cosa fa |
|---|---|
| `scarica_fonti.sh` | Scarica le tre fonti (OpenStreetMap, civici e aree del Comune). I link del Comune si leggono dal catalogo, perché cambiano a ogni aggiornamento. |
| `build_data.py` | Prende la mappa di OpenStreetMap e i numeri civici del Comune e produce i dati dell'app (strade, chi può passare, svolte vietate, civici, luoghi di interesse). |
| `check_data.py` | Controlla i dati e scrive un rapporto in italiano: differenze con ZTL e aree pedonali del Comune, strade "da verificare", regole a orario, pezzi di rete isolati, differenze con la versione precedente. |
| `regole_milano.json` | Le regole ufficiali del Comune che correggono OpenStreetMap (ZTL, aree pedonali, divieti di transito), ognuna con motivo e ordinanza. Le applica `build_data.py`. |
| `collaudo.js` | Prove automatiche da superare prima di pubblicare: dati completi e recenti, tragitti di riferimento a vari orari, regole note (Garibaldi, Monte Ceneri, Duomo), 300 tragitti a caso senza strade vietate né zone "solo destinatari" attraversate. Se una prova fallisce esce con errore. |
| `eccezioni_comune.json` | Disaccordi con il Comune già esaminati e lasciati come in OpenStreetMap (casi dubbi). Ogni disaccordo **nuovo** blocca l'aggiornamento automatico. |
| `prove_app.js` | Prove dell'app vera in un browser che fa finta di essere un iPhone: avvio (anche con dati vecchi salvati), ricerca, preferiti, guida con voce, tempi dei tragitti, tema scuro, schermo piccolo. Girano su GitHub a ogni richiesta di pubblicazione (`.github/workflows/prove.yml`). |
| `compare_routes.js` | Calcola gli stessi tragitti con due versioni dei dati e mostra cosa cambia. |

## Fonti

- OpenStreetMap, estratto Lombardia (aggiornato ogni giorno):
  `https://download.openstreetmap.fr/extracts/europe/italy/lombardia-latest.osm.pbf`
  e `.../lombardia.state.txt` (data dell'estratto)
- Comune di Milano, numeri civici con coordinate:
  `https://dati.comune.milano.it/dataset/ds634-numeri-civici-coordinate` (CSV)
- Comune di Milano, aree pedonali e ZTL (per il controllo):
  `https://dati.comune.milano.it/dataset/ds51_trafficotrasporti_aree_pedonali_ztl` (GeoJSON)

## Aggiornamento automatico

`.github/workflows/aggiorna-mappa.yml` gira **ogni lunedì mattina** (e a mano da GitHub → Actions →
"Aggiorna la mappa"): scarica le fonti, ricostruisce la mappa, scrive il rapporto, confronta i tragitti
con la versione precedente, fa il collaudo e **pubblica solo se tutto passa**. Si ferma (GitHub manda una
mail, l'app resta con la mappa precedente, il rapporto si scarica dalla pagina dell'esecuzione) se:

- una regola del Comune cita una via che in OSM non c'è più (`build_data.py --rigoroso`);
- compare un disaccordo nuovo con le aree pedonali/ZTL del Comune (`check_data.py --blocca-nuovi`):
  va aggiunta una regola in `regole_milano.json` o, se dubbio, un'eccezione in `eccezioni_comune.json`;
- la mappa è cambiata in modo anomalo rispetto alla settimana prima: oltre l'1% di vie con permesso
  scooter diverso, più di 5 strade principali diventate vietate, o meno strade/civici/luoghi
  (`collaudo.js --precedente`);
- una qualsiasi prova del collaudo fallisce.

## Come si usano

```bash
pip install osmium shapely
bash tools/scarica_fonti.sh fonti      # oppure scarica a mano le fonti qui sotto
python3 tools/build_data.py --osm lombardia.osm.pbf --osm-state lombardia.state.txt \
    --civici civici.csv --out strade.json
python3 tools/check_data.py --data strade.json --aree disciplina_aree.geojson \
    --old strade-precedenti.json --out rapporto.md
node tools/compare_routes.js strade-precedenti.json strade.json
node tools/collaudo.js strade.json
```

## Tragitti personali (privati)

Il repository è pubblico: i tragitti personali (es. casa ↔ ufficio) **non** si scrivono nei file del
progetto. `collaudo.js` li legge da `tools/tragitti_privati.json`, che è escluso da git:

```json
{ "tragitti": [ { "nome": "casa-ufficio", "da": ["Nome via", "civico"], "a": ["Nome via", "civico"], "max_m": 3200 } ] }
```

Su GitHub si possono passare con un segreto del repository chiamato `SCOOTLY_TRAGITTI` (stesso contenuto):
il workflow lo usa se c'è. Senza, il collaudo usa un tragitto di prova.

## Regole usate per lo scooter (125cc)

- **Quando Comune e OpenStreetMap non sono d'accordo vince il Comune** (`regole_milano.json`).
  Il rapporto di `check_data.py` (con `--proposte`) elenca i nuovi disaccordi da rivedere: i casi
  dubbi (via solo in parte dentro un'area, regole valide in un solo verso, zone fuori dall'anello)
  non vanno applicati senza verifica.

- In OpenStreetMap lo scooter 125 è un `motorcycle`. Il tag `moped` (ciclomotori fino a 50cc) **non** vale.
- Vince il mezzo più specifico: `motorcycle` → `motor_vehicle` → `vehicle` → `access`.
- Sensi unici: `oneway:motorcycle` vince su `oneway`; le rotonde sono sempre a senso unico.
- `destination`, `customers` = solo destinatari (si può arrivare, non attraversare).
- `delivery` (solo carico e scarico), `permit` (solo con permesso), `private` = vietato.
- Le strade a scorrimento veloce (`motorroad`) sono aperte: dal 14/12/2024 i 125 (almeno 120 cc,
  conducente maggiorenne) possono circolare anche su autostrade e tangenziali.
- Regole a orario (`:conditional`, es. `no @ (22:00-06:00)`): vengono tradotte in fasce orarie
  (mesi, giorni, ore) e il motore usa quelle valide al momento della partenza. Le condizioni che non
  sono orari (es. "solo residenti") vengono ignorate e il programma le elenca.
- Confine: la linea della filovia 90/91 presa da OpenStreetMap. La "zona coperta" arriva a 150 m
  oltre la linea; strade, civici e luoghi arrivano fino a 500 m, così i piazzali sull'anello restano interi.
