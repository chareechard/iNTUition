import json, math, os
BASE = os.path.dirname(__file__)
from build_circuits import WIKI_COORD

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlmb/2)**2
    return 2*R*math.asin(math.sqrt(a))

for key, (wlat, wlon) in WIKI_COORD.items():
    with open(os.path.join(BASE, f'{key}.json'), encoding='utf-8') as f:
        gj = json.load(f)
    coords = gj['features'][0]['geometry']['coordinates']
    if coords[0] == coords[-1]:
        coords = coords[:-1]
    best_d, best_i = None, 0
    for i, (lon, lat) in enumerate(coords):
        d = haversine(wlat, wlon, lat, lon)
        if best_d is None or d < best_d:
            best_d, best_i = d, i
    print(f'{key:14s} wiki=({wlat:.5f},{wlon:.5f})  nearest_idx={best_i:4d}/{len(coords)}  dist={best_d:6.1f} m')
