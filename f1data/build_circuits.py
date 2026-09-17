import json, math, os
from shapely.geometry import LineString

BASE = os.path.dirname(__file__)

WIKI_COORD = {
    'monza':       (45.62055556, 9.28944444),
    'monaco':      (43.73472222, 7.42055556),
    'spa':         (50.43722222, 5.97138889),
    'silverstone': (52.071, -1.016),
    'suzuka':      (34.8417, 136.5389),
    'interlagos':  (-23.70111111, -46.69722222),
    'zandvoort':   (52.38888889, 4.54083333),
    'redbullring': (47.21972222, 14.76472222),
    'cota':        (30.13277778, -97.64111111),
    'marinabay':   (1.29153056, 103.86385),
    'bahrain':     (26.0325, 50.51055556),
    'hungaroring': (47.58222222, 19.25111111),
}

CW = {'monza','monaco','spa','silverstone','suzuka','zandvoort','redbullring','bahrain','hungaroring'}
CCW = {'interlagos','marinabay','cota'}

NAMES = {
    'monza': 'Monza', 'monaco': 'Monaco', 'spa': 'Spa-Francorchamps',
    'silverstone': 'Silverstone', 'suzuka': 'Suzuka', 'interlagos': 'Interlagos',
    'zandvoort': 'Zandvoort', 'redbullring': 'Red Bull Ring',
    'cota': 'Circuit of the Americas', 'marinabay': 'Marina Bay',
    'bahrain': 'Bahrain', 'hungaroring': 'Hungaroring',
}

def shoelace(pts):
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0

def nearest_index(coords, target_lat, target_lon):
    best_i, best_d = 0, None
    for i, (lon, lat) in enumerate(coords):
        d = (lat - target_lat) ** 2 + (lon - target_lon) ** 2
        if best_d is None or d < best_d:
            best_d, best_i = d, i
    return best_i

def build(key):
    with open(os.path.join(BASE, f'{key}.json'), encoding='utf-8') as f:
        gj = json.load(f)
    coords = gj['features'][0]['geometry']['coordinates']  # [lon, lat]
    # Drop duplicate closing vertex if the ring is explicitly closed.
    if coords[0] == coords[-1]:
        coords = coords[:-1]

    wlat, wlon = WIKI_COORD[key]
    idx = nearest_index(coords, wlat, wlon)
    rotated = coords[idx:] + coords[:idx]

    # Orient so traversal order matches the track's real racing direction.
    # shoelace() here is the plain math (x=lon, y=lat, y-up) convention:
    # positive = counter-clockwise on a north-up map, negative = clockwise.
    area = shoelace(rotated)
    want_cw = key in CW
    is_cw = area < 0
    if want_cw != is_cw:
        rotated = [rotated[0]] + list(reversed(rotated[1:]))

    # Simplify (Douglas-Peucker) in lon/lat space, tuned per track to land in
    # a reasonable point budget while keeping chicanes legible.
    line = LineString(rotated)
    target_lo, target_hi = 22, 45
    tol = 0.00005
    simplified = rotated
    for _ in range(40):
        simp_line = line.simplify(tol, preserve_topology=False)
        pts = list(simp_line.coords)
        if pts[0] != pts[-1]:
            pass
        n = len(pts)
        if target_lo <= n <= target_hi:
            simplified = pts
            break
        if n > target_hi:
            tol *= 1.35
        else:
            tol *= 0.7
        simplified = pts
    # simplify() may reorder/snap the start slightly; re-anchor exactly on
    # our chosen start vertex by re-inserting it if it got simplified away.
    start_pt = rotated[0]
    if simplified[0] != start_pt:
        simplified = [start_pt] + simplified
    if simplified[-1] == simplified[0]:
        simplified = simplified[:-1]

    lons = [p[0] for p in simplified]
    lats = [p[1] for p in simplified]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)

    margin = 12.0
    W, H = 200.0, 110.0
    span_lon = max(max_lon - min_lon, 1e-9)
    span_lat = max(max_lat - min_lat, 1e-9)
    scale = min((W - 2 * margin) / span_lon, (H - 2 * margin) / span_lat)
    off_x = margin + ((W - 2 * margin) - span_lon * scale) / 2
    off_y = margin + ((H - 2 * margin) - span_lat * scale) / 2

    def to_svg(lon, lat):
        x = off_x + (lon - min_lon) * scale
        # North-up: higher latitude -> smaller y (top of the image).
        y = off_y + (max_lat - lat) * scale
        return round(x, 1), round(y, 1)

    svg_pts = [to_svg(lon, lat) for lon, lat in simplified]
    d = 'M' + 'L'.join(f'{x} {y}' for x, y in svg_pts) + 'Z'
    return d, len(svg_pts)

if __name__ == '__main__':
    out = {}
    for key in WIKI_COORD:
        d, n = build(key)
        out[key] = d
        print(key, n, 'pts')
    with open(os.path.join(BASE, 'circuits_out.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=2)
