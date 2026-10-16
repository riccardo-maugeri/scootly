#!/usr/bin/env bash
# Scarica le fonti della mappa nella cartella indicata (predefinita: fonti/).
#   - estratto OpenStreetMap della Lombardia (~350 MB) e la sua data
#   - numeri civici ufficiali del Comune di Milano (CSV)
#   - aree pedonali e ZTL del Comune (GeoJSON, per il controllo)
# I link dei dataset del Comune cambiano a ogni aggiornamento: si leggono dal catalogo.
set -euo pipefail
DIR="${1:-fonti}"
mkdir -p "$DIR"
OSM=https://download.openstreetmap.fr/extracts/europe/italy
CKAN=https://dati.comune.milano.it/api/3/action/package_show

curl -fsSL --retry 4 -o "$DIR/lombardia.state.txt" "$OSM/lombardia.state.txt"
curl -fsSL --retry 4 -o "$DIR/lombardia.osm.pbf" "$OSM/lombardia-latest.osm.pbf"

risorsa() {  # $1 = dataset, $2 = formato -> link del file
  curl -fsSL --retry 4 "$CKAN?id=$1" | python3 -c "
import json, sys
fmt = sys.argv[1].lower()
for r in json.load(sys.stdin)['result']['resources']:
    if r.get('format', '').lower() == fmt:
        print(r['url']); break
else:
    sys.exit('formato ' + fmt + ' non trovato')" "$2"
}

curl -fsSL --retry 4 -o "$DIR/civici.zip" "$(risorsa ds634-numeri-civici-coordinate CSV)"
python3 -c "
import zipfile, sys
z = zipfile.ZipFile(sys.argv[1])
name = next(n for n in z.namelist() if n.lower().endswith('.csv'))
open(sys.argv[2], 'wb').write(z.read(name))" "$DIR/civici.zip" "$DIR/civici.csv"
curl -fsSL --retry 4 -o "$DIR/aree.geojson" "$(risorsa ds51_trafficotrasporti_aree_pedonali_ztl GeoJSON)"

ls -la "$DIR"
