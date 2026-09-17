import json, re, os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

BASE = os.path.dirname(__file__)
with open(os.path.join(BASE, 'circuits_out.json')) as f:
    circuits = json.load(f)

def parse_d(d):
    d = d.rstrip('Z')
    nums = re.findall(r'-?\d+\.?\d*', d.replace('M', '').replace('L', ' '))
    vals = list(map(float, nums))
    return list(zip(vals[0::2], vals[1::2]))

fig, axes = plt.subplots(3, 4, figsize=(16, 12))
for ax, (key, d) in zip(axes.flat, circuits.items()):
    pts = parse_d(d)
    xs = [p[0] for p in pts] + [pts[0][0]]
    ys = [p[1] for p in pts] + [pts[0][1]]
    ax.plot(xs, ys, '-o', markersize=2, color='#2f7f9e')
    ax.plot(pts[0][0], pts[0][1], 'o', color='red', markersize=8)
    ax.annotate('', xy=pts[2], xytext=pts[0],
                arrowprops=dict(arrowstyle='->', color='green', lw=2))
    ax.set_title(key)
    ax.invert_yaxis()
    ax.set_aspect('equal')
    ax.set_xticks([]); ax.set_yticks([])
plt.tight_layout()
plt.savefig(os.path.join(BASE, 'preview.png'), dpi=110)
print('saved')
