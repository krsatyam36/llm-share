#!/usr/bin/env python3
"""
screen-stream — Stream laptop + external monitor to a tablet browser over local WiFi.

Screens (from xrandr):
  laptop   → eDP   (AMD GPU)   1920x1080 @ +0,+1080
  external → HDMI  (RTX 3050)  1920x1080 @ +216,+0

Usage:  screen-share-tab   (or ./start.sh)
Tablet: http://screen-stream.local:8766  (or http://<ip>:8766)
"""

import asyncio
import atexit
import http.server
import json
import logging
import os
import socket
import socketserver
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ─── Dependency check ────────────────────────────────────────────────────────
try:
    import websockets
    import websockets.exceptions
except ImportError:
    print("ERROR: websockets not installed.\nRun: pip3 install websockets", file=sys.stderr)
    sys.exit(1)

# ─── Configuration ────────────────────────────────────────────────────────────
DISPLAY_ENV = os.environ.get('DISPLAY', ':0')
WS_PORT     = 8765
HTTP_PORT   = 8766
FPS         = 30
BITRATE     = '1500k'

SCREENS = {
    'laptop':   {'size': '1920x1080', 'offset': '0,1080'},
    'external': {'size': '1920x1080', 'offset': '0,0'},
}

# Global display bounds — used for cursor coordinate mapping
SCREEN_BOUNDS = {
    'laptop':   {'x': 0, 'y': 1080, 'w': 1920, 'h': 1080},
    'external': {'x': 0, 'y': 0,    'w': 1920, 'h': 1080},
}

SCRIPT_DIR = Path(__file__).parent

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def find_xauthority() -> str:
    uid = os.getuid()
    candidates = [
        os.environ.get('XAUTHORITY', ''),
        os.path.expanduser('~/.Xauthority'),
        f'/run/user/{uid}/gdm/Xauthority',
        f'/run/user/{uid}/.mutter-Xwaylandauth',
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return ''


def get_ws_path(websocket) -> str:
    try:
        return websocket.request.path
    except AttributeError:
        return getattr(websocket, 'path', '/')


def _ffmpeg_has_encoder(name: str) -> bool:
    r = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'],
                       capture_output=True, text=True)
    return f' {name} ' in r.stdout


def _detect_vaapi_device() -> str:
    import glob
    for node in sorted(glob.glob('/dev/dri/renderD*')):
        try:
            r = subprocess.run(
                ['vainfo', '--display', 'drm', '--device', node],
                capture_output=True, text=True, timeout=3,
            )
            if 'AMD' in r.stdout or 'radeon' in r.stdout.lower() or 'H.264' in r.stdout:
                return node
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    import glob as _g
    nodes = sorted(_g.glob('/dev/dri/renderD*'))
    return nodes[0] if nodes else ''


HW: dict = {}


def detect_hw_encoders() -> None:
    global HW
    has_nvenc = _ffmpeg_has_encoder('h264_nvenc')
    has_vaapi = _ffmpeg_has_encoder('h264_vaapi')
    vaapi_dev = _detect_vaapi_device() if has_vaapi else ''
    HW['nvenc']     = has_nvenc
    HW['vaapi']     = has_vaapi and bool(vaapi_dev)
    HW['vaapi_dev'] = vaapi_dev
    log.info(f"Encoders — NVENC: {'yes' if HW['nvenc'] else 'no'} | "
             f"VAAPI: {'yes (' + vaapi_dev + ')' if HW['vaapi'] else 'no'}")


def build_ffmpeg_cmd(screen_id: str, bitrate: str, fps: int,
                     crop: dict | None = None) -> list[str]:
    s        = SCREENS[screen_id]
    keyframe = fps  # 1 keyframe/sec → fast reconnect sync
    src_w, src_h = map(int, s['size'].split('x'))

    input_args = [
        'ffmpeg', '-loglevel', 'warning',
        '-fflags', 'nobuffer',
        '-thread_queue_size', '512',
        '-f', 'x11grab',
        '-r', str(fps),
        '-video_size', s['size'],
        '-i', f"{DISPLAY_ENV}+{s['offset']}",
    ]

    # Build crop+scale filter chain when focus mode is active
    crop_vf: list[str] = []
    if crop:
        crop_vf = [
            f"crop={crop['w']}:{crop['h']}:{crop['x']}:{crop['y']}",
            f"scale={src_w}:{src_h}",
        ]

    if screen_id == 'external' and HW.get('nvenc'):
        encode_args = [
            '-c:v', 'h264_nvenc', '-preset', 'p1', '-tune', 'll',
            '-zerolatency', '1', '-rc', 'cbr',
            '-b:v', bitrate, '-maxrate', bitrate, '-bufsize', '375k',
            '-g', str(keyframe),
        ]
        if crop_vf:
            encode_args = ['-vf', ','.join(crop_vf)] + encode_args
    elif screen_id == 'laptop' and HW.get('vaapi'):
        input_args = [
            'ffmpeg', '-loglevel', 'warning',
            '-fflags', 'nobuffer',
            '-vaapi_device', HW['vaapi_dev'],
            '-thread_queue_size', '512',
            '-f', 'x11grab', '-r', str(fps),
            '-video_size', s['size'],
            '-i', f"{DISPLAY_ENV}+{s['offset']}",
        ]
        vaapi_vf = crop_vf + ['format=nv12', 'hwupload']
        encode_args = [
            '-vf', ','.join(vaapi_vf),
            '-c:v', 'h264_vaapi',
            '-b:v', bitrate, '-g', str(keyframe),
        ]
    else:
        encode_args = [
            '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
            '-b:v', bitrate, '-maxrate', bitrate, '-bufsize', '375k',
            '-pix_fmt', 'yuv420p', '-g', str(keyframe),
        ]
        if crop_vf:
            encode_args = ['-vf', ','.join(crop_vf)] + encode_args

    return input_args + encode_args + ['-f', 'mpegts', 'pipe:1']


# ─── mDNS (screen-stream.local) ──────────────────────────────────────────────

def start_mdns(ip: str, port: int) -> None:
    try:
        from zeroconf import ServiceInfo, Zeroconf
        info = ServiceInfo(
            "_http._tcp.local.",
            "screen-stream._http._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=port,
            properties={},
            server="screen-stream.local.",
        )
        zc = Zeroconf()
        zc.register_service(info)
        atexit.register(lambda: (zc.unregister_service(info), zc.close()))
        log.info("mDNS: screen-stream.local registered")
    except ImportError:
        log.warning("mDNS: zeroconf not installed — install with: pip install zeroconf")
    except Exception as exc:
        log.warning(f"mDNS: {exc}")


# ─── WebSocket stream handler ─────────────────────────────────────────────────

async def stream_screen(websocket, screen_id: str, bitrate: str, fps: int,
                        crop: dict | None = None) -> None:
    env = os.environ.copy()
    env['DISPLAY'] = DISPLAY_ENV
    xa = find_xauthority()
    if xa:
        env['XAUTHORITY'] = xa

    cmd = build_ffmpeg_cmd(screen_id, bitrate, fps, crop)
    focus_info = f" crop={crop['x']},{crop['y']},{crop['w']}x{crop['h']}" if crop else ""
    log.info(f"[{screen_id}] {websocket.remote_address} — {bitrate} {fps}fps{focus_info}")

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                log.warning(f"[{screen_id}] ffmpeg exited — client will auto-reconnect")
                break
            await websocket.send(chunk)
    except (websockets.exceptions.ConnectionClosed,
            websockets.exceptions.ConnectionClosedOK,
            websockets.exceptions.ConnectionClosedError):
        log.info(f"[{screen_id}] client disconnected")
    except Exception as exc:
        log.error(f"[{screen_id}] error: {exc}")
    finally:
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        log.info(f"[{screen_id}] stream closed")


async def ws_handler(websocket) -> None:
    raw_path = get_ws_path(websocket)
    parsed   = urlparse(raw_path)
    params   = parse_qs(parsed.query)

    screen_id = parsed.path.strip('/').removeprefix('screen/').split('?')[0]
    bitrate   = params.get('bitrate', [BITRATE])[0]
    fps       = max(1, min(60, int(params.get('fps', [str(FPS)])[0])))

    crop = None
    if 'crop' in params:
        try:
            cx, cy, cw, ch = map(int, params['crop'][0].split(','))
            src_w, src_h   = map(int, SCREENS[screen_id]['size'].split('x'))
            cw = min(cw, src_w - max(0, cx))
            ch = min(ch, src_h - max(0, cy))
            cx, cy = max(0, cx), max(0, cy)
            if cw > 0 and ch > 0:
                crop = {'x': cx, 'y': cy, 'w': cw, 'h': ch}
        except (ValueError, KeyError):
            pass

    if screen_id not in SCREENS:
        log.warning(f"Unknown path: {raw_path!r}")
        await websocket.close(1008, f"Unknown screen '{screen_id}'")
        return

    await stream_screen(websocket, screen_id, bitrate, fps, crop)


# ─── HTTP server ──────────────────────────────────────────────────────────────

# Detect xdotool / xclip once
_XDOTOOL = bool(subprocess.run(['which', 'xdotool'],
                                capture_output=True).returncode == 0)
_XCLIP   = bool(subprocess.run(['which', 'xclip'],
                                capture_output=True).returncode == 0)


def _x_env() -> dict:
    env = os.environ.copy()
    env['DISPLAY'] = DISPLAY_ENV
    xa = find_xauthority()
    if xa:
        env['XAUTHORITY'] = xa
    return env


def _xdotool(*args: str, _input: str | None = None) -> None:
    if not _XDOTOOL:
        return
    try:
        subprocess.run(['xdotool', *args], env=_x_env(),
                       timeout=0.5, capture_output=True,
                       input=(_input.encode() if _input else None))
    except Exception as exc:
        log.warning(f"xdotool: {exc}")


# Whitelist for xdotool key/keychord — prevents shell injection
import re as _re
_KEY_RE = _re.compile(r'^[A-Za-z0-9_+\- ]{1,64}$')


def handle_input(cmd: dict) -> None:
    """Dispatch a remote-input JSON command to xdotool."""
    t      = cmd.get('type')
    screen = cmd.get('screen', 'laptop')
    b      = SCREEN_BOUNDS.get(screen)

    def _abs() -> tuple[str, str]:
        x = int(cmd.get('x', 0))
        y = int(cmd.get('y', 0))
        if b:
            x, y = b['x'] + x, b['y'] + y
        return str(x), str(y)

    try:
        if t == 'move':
            ax, ay = _abs()
            _xdotool('mousemove', ax, ay)
        elif t == 'click':
            ax, ay = _abs()
            btn = str(int(cmd.get('button', 1)))
            _xdotool('mousemove', ax, ay, 'click', btn)
        elif t == 'down':
            ax, ay = _abs()
            btn = str(int(cmd.get('button', 1)))
            _xdotool('mousemove', ax, ay, 'mousedown', btn)
        elif t == 'up':
            ax, ay = _abs()
            btn = str(int(cmd.get('button', 1)))
            _xdotool('mousemove', ax, ay, 'mouseup', btn)
        elif t == 'scroll':
            ax, ay = _abs()
            dy = int(cmd.get('dy', 0))
            if dy == 0:
                return
            btn = '5' if dy > 0 else '4'   # X11: 4=up, 5=down
            n   = max(1, min(20, abs(dy)))
            _xdotool('mousemove', ax, ay,
                     'click', '--repeat', str(n), '--delay', '8', btn)
        elif t == 'key':
            keys = str(cmd.get('keys', ''))
            if _KEY_RE.match(keys):
                _xdotool('key', '--clearmodifiers', keys)
        elif t == 'type':
            text = str(cmd.get('text', ''))
            if text:
                # --delay 5ms gives reliable input without flooding
                _xdotool('type', '--delay', '5', '--', text)
    except Exception as exc:
        log.warning(f"input: {exc}")


def clipboard_get() -> str:
    if not _XCLIP:
        return ''
    try:
        r = subprocess.run(['xclip', '-selection', 'clipboard', '-o'],
                           env=_x_env(), capture_output=True,
                           text=True, timeout=1)
        return r.stdout
    except Exception:
        return ''


def clipboard_set(text: str) -> None:
    if not _XCLIP:
        return
    try:
        subprocess.run(['xclip', '-selection', 'clipboard', '-i'],
                       env=_x_env(), input=text.encode(), timeout=1)
    except Exception as exc:
        log.warning(f"xclip: {exc}")


def run_http_server() -> None:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(SCRIPT_DIR), **kwargs)

        def do_GET(self):
            # ── /ping — RTT probe ────────────────────────────────────────
            if self.path == '/ping':
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                return

            # ── /cursor — live cursor position for highlight overlay ─────
            if self.path == '/cursor' and _XDOTOOL:
                try:
                    r = subprocess.run(
                        ['xdotool', 'getmouselocation'],
                        capture_output=True, text=True, timeout=0.05,
                        env={**os.environ, 'DISPLAY': DISPLAY_ENV},
                    )
                    # Output: "x:1234 y:567 screen:0 window:..."
                    parts = dict(p.split(':') for p in r.stdout.strip().split()
                                 if ':' in p)
                    cx = int(parts.get('x', -1))
                    cy = int(parts.get('y', -1))

                    # Map global coords to per-screen coords + which screen
                    result = {'x': -1, 'y': -1, 'screen': None}
                    for sid, b in SCREEN_BOUNDS.items():
                        if b['x'] <= cx < b['x'] + b['w'] and \
                           b['y'] <= cy < b['y'] + b['h']:
                            result = {
                                'x': cx - b['x'],
                                'y': cy - b['y'],
                                'screen': sid,
                                'sw': b['w'],
                                'sh': b['h'],
                            }
                            break

                    data = json.dumps(result).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:
                    self.send_response(503)
                    self.end_headers()
                return

            # ── /capabilities — tell client what server supports ─────────
            if self.path == '/capabilities':
                data = json.dumps({
                    'cursor':    _XDOTOOL,
                    'control':   _XDOTOOL,
                    'clipboard': _XCLIP,
                }).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
                return

            # ── /clipboard GET — read laptop clipboard ───────────────────
            if self.path == '/clipboard':
                text = clipboard_get()
                data = json.dumps({'text': text}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)
                return

            super().do_GET()

        def do_POST(self):
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length) if length else b''

            # ── /input — remote control commands ─────────────────────────
            if self.path == '/input':
                try:
                    cmd = json.loads(body or b'{}')
                    handle_input(cmd)
                    self.send_response(204)
                except Exception:
                    self.send_response(400)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                return

            # ── /clipboard POST — set laptop clipboard ──────────────────
            if self.path == '/clipboard':
                try:
                    data = json.loads(body or b'{}')
                    clipboard_set(str(data.get('text', '')))
                    self.send_response(204)
                except Exception:
                    self.send_response(400)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                return

            self.send_response(404)
            self.end_headers()

        def log_message(self, *_):
            pass

    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    with ReusableTCPServer(('', HTTP_PORT), Handler) as httpd:
        httpd.serve_forever()


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    ip = get_local_ip()
    detect_hw_encoders()
    start_mdns(ip, HTTP_PORT)

    enc_laptop   = 'h264_vaapi' if HW.get('vaapi') else 'libx264'
    enc_external = 'h264_nvenc' if HW.get('nvenc') else 'libx264'

    threading.Thread(target=run_http_server, daemon=True).start()
    log.info(f"HTTP → port {HTTP_PORT}")

    url = f"http://{ip}:{HTTP_PORT}"
    bar = '─' * 54
    print(f"\n{bar}")
    print(f"  Screen Stream — ready")
    print(f"{bar}")
    print(f"  Tablet URL  →  {url}")
    print(f"  mDNS URL    →  http://screen-stream.local:{HTTP_PORT}")
    print(f"  WebSocket   →  ws://{ip}:{WS_PORT}")
    print(f"  Laptop      →  eDP   1920x1080  [{enc_laptop}]")
    print(f"  External    →  HDMI  1920x1080  [{enc_external}]")
    print(f"  xdotool     →  {'available (cursor highlight on)' if _XDOTOOL else 'not found (cursor highlight off)'}")
    print(f"{bar}\n")

    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        print(f"\n  ^ Scan to open on tablet\n")
    except ImportError:
        pass

    print("  Ctrl+C to stop\n")

    async with websockets.serve(
        ws_handler,
        host='0.0.0.0',
        port=WS_PORT,
        ping_interval=15,
        ping_timeout=10,
        max_size=None,
    ):
        await asyncio.Future()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\nShutting down. Bye!')
