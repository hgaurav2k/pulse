#!/usr/bin/env python3
"""Generate a 512x512 PNG logo for Claude Pulse using only stdlib."""
import struct, zlib, math, os

W = H = 512
BG = (26, 22, 20)
ACCENT = (217, 119, 87)
GREEN = (22, 163, 74)
WHITE = (250, 249, 246)

pixels = [BG] * (W * H)

def blend(bg, fg, a):
    return tuple(int(bg[i]*(1-a) + fg[i]*a) for i in range(3))

def set_px(x, y, color, alpha=1.0):
    if 0 <= x < W and 0 <= y < H:
        if alpha < 1.0:
            pixels[y*W+x] = blend(pixels[y*W+x], color, alpha)
        else:
            pixels[y*W+x] = color

def fill_circle(cx, cy, r, color):
    for dy in range(-r-1, r+2):
        for dx in range(-r-1, r+2):
            d = math.sqrt(dx*dx+dy*dy)
            if d <= r+0.5:
                alpha = min(1.0, max(0, r+0.5-d))
                set_px(cx+dx, cy+dy, color, alpha)

def draw_rounded_rect(x0, y0, x1, y1, r, color):
    for y in range(y0, y1+1):
        for x in range(x0, x1+1):
            inside = False
            if x0+r <= x <= x1-r or y0+r <= y <= y1-r:
                inside = True
            else:
                for ccx, ccy in [(x0+r,y0+r),(x1-r,y0+r),(x0+r,y1-r),(x1-r,y1-r)]:
                    if math.sqrt((x-ccx)**2+(y-ccy)**2) <= r+0.5:
                        inside = True
                        break
            if inside:
                edge_dist = min(x-x0, x1-x, y-y0, y1-y)
                alpha = min(1.0, edge_dist+0.5) if edge_dist < 1 else 1.0
                set_px(x, y, color, alpha)

# Rounded square background
draw_rounded_rect(40, 40, W-41, H-41, 80, (38, 32, 28))

# Pulse waveform (ECG-like)
cx, cy = W//2, H//2
points = []
num_pts = 400
for i in range(num_pts):
    t = (i/(num_pts-1))*2-1
    x = int(cx + t*170)
    y_val = 0
    at = abs(t)
    if at < 0.06:
        y_val = -math.cos(t/0.06*math.pi)*130
    elif 0.06 <= at < 0.12:
        y_val = math.sin((at-0.06)/0.06*math.pi)*25
    elif 0.25 < at < 0.38:
        y_val = -math.sin((at-0.25)/0.13*math.pi)*28
    elif 0.55 < at < 0.68:
        y_val = -math.sin((at-0.55)/0.13*math.pi)*15
    points.append((x, int(cy+y_val)))

# Draw thick waveform with anti-aliasing
for i in range(len(points)-1):
    x0, y0 = points[i]
    x1, y1 = points[i+1]
    steps = max(abs(x1-x0), abs(y1-y0), 1)*2
    for s in range(steps+1):
        t = s/steps
        px = x0+(x1-x0)*t
        py = y0+(y1-y0)*t
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                d = math.sqrt(dx*dx+dy*dy)
                if d <= 3.5:
                    alpha = min(1.0, max(0, 3.8-d))
                    set_px(int(px+dx), int(py+dy), ACCENT, alpha)

# Green dot at peak
peak_x, peak_y = points[num_pts//2]
fill_circle(peak_x, peak_y-2, 10, GREEN)

# Outer circle ring
for angle_deg in range(3600):
    a = math.radians(angle_deg/10)
    for t_off in range(-3, 4):
        rx = int(cx + (210+t_off*0.5)*math.cos(a))
        ry = int(cy + (210+t_off*0.5)*math.sin(a))
        dist = abs(math.sqrt((rx-cx)**2+(ry-cy)**2)-210)
        alpha = max(0, 1-dist/1.8)
        set_px(rx, ry, ACCENT, alpha)

# Corner accent dots
for ddx, ddy in [(-1,-1),(1,-1),(-1,1),(1,1)]:
    fill_circle(int(cx+ddx*185), int(cy+ddy*185), 4, WHITE)

# Encode as PNG
def chunk(ctype, data):
    c = ctype+data
    return struct.pack('>I', len(data))+c+struct.pack('>I', zlib.crc32(c) & 0xffffffff)

raw = b''
for y in range(H):
    raw += b'\x00'
    for x in range(W):
        r, g, b = pixels[y*W+x]
        raw += struct.pack('BBB', min(255,max(0,r)), min(255,max(0,g)), min(255,max(0,b)))

png = b'\x89PNG\r\n\x1a\n'
png += chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 2, 0, 0, 0))
png += chunk(b'IDAT', zlib.compress(raw, 9))
png += chunk(b'IEND', b'')

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'logo.png')
with open(out, 'wb') as f:
    f.write(png)
print(f'Generated {out} ({len(png)} bytes)')
