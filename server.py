import asyncio
import os
import json
import base64
import socket
import subprocess
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import threading
import aiohttp
import websockets
from datetime import datetime

# --- CONFIGURATION & GLOBALS ---
HTTP_PORT = 8766
WS_PORT = 8765
CONNECTED_CLIENTS = set()
IS_ANALYZING = False
VIDEO_PROCESS = None
OLLAMA_HOST = "http://localhost:11434"

print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Screen Stream — starting up...")

def probe_encoders():
    has_nvenc = os.system("ffmpeg -encoders 2>/dev/null | grep -q h264_nvenc") == 0
    has_vaapi = os.path.exists("/dev/dri/renderD129")
    nv_status = "yes" if has_nvenc else "no"
    va_status = "yes (/dev/dri/renderD129)" if has_vaapi else "no"
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"{ts} [INFO] Encoders — NVENC: {nv_status} | VAAPI: {va_status}")
    return has_nvenc, has_vaapi

HAS_NVENC, HAS_VAAPI = probe_encoders()

# --- XRANDR DYNAMIC RESOLUTION PARSER ---
def detect_displays():
    displays = []
    try:
        output = subprocess.check_output("xrandr", shell=True, text=True)
        for line in output.split('\n'):
            if " connected" in line:
                parts = line.split()
                name = parts[0]
                for p in parts[1:]:
                    if 'x' in p and '+' in p: # Matches formats like 1920x1080+1920+0
                        res, offsets = p.split('+', 1)
                        w, h = res.split('x')
                        ox, oy = offsets.split('+')
                        displays.append({
                            "name": name,
                            "w": w, "h": h,
                            "x": ox, "y": oy
                        })
                        break
    except Exception:
        pass
    # Fallback if xrandr fails
    if not displays:
        displays.append({"name": "Default", "w": "1920", "h": "1080", "x": "0", "y": "0"})
    return displays

DISPLAYS = detect_displays()
ACTIVE_DISPLAY_INDEX = int(os.environ.get("MONITOR", "0"))

ts = datetime.now().strftime('%H:%M:%S')
print(f"{ts}  ✓ ffmpeg found")
print(f"{ts}  ✓ Display Engine mapping {len(DISPLAYS)} monitors (Active: {DISPLAYS[ACTIVE_DISPLAY_INDEX]['name']})")
print(f"{ts}  ✓ Python packages ready")
print(f"{ts}  ✓ mpegts.js ready")

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

LOCAL_IP = get_local_ip()

def run_http_server():
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass 
    server = ThreadingHTTPServer(('0.0.0.0', HTTP_PORT), QuietHandler)
    server.serve_forever()

threading.Thread(target=run_http_server, daemon=True).start()

async def video_capture_job():
    global VIDEO_PROCESS
    if HAS_NVENC:
        encoder = ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull"]
    elif HAS_VAAPI:
        encoder = ["-vaapi_device", "/dev/dri/renderD129", "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi"]
    else:
        encoder = ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"]

    while True:
        if CONNECTED_CLIENTS:
            # Dynamically fetch the current active monitor's bounds
            active_disp = DISPLAYS[ACTIVE_DISPLAY_INDEX]
            display_env = os.environ.get("DISPLAY", ":0.0")
            input_offset = f"{display_env}+{active_disp['x']},{active_disp['y']}"
            
            ffmpeg_cmd = [
                "ffmpeg", "-loglevel", "quiet", "-y",
                "-f", "x11grab", "-video_size", f"{active_disp['w']}x{active_disp['h']}", "-framerate", "30",
                "-i", input_offset,
                *encoder,
                "-b:v", "3000k", "-g", "30", "-f", "mpegts", "-"
            ]

            VIDEO_PROCESS = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            try:
                while CONNECTED_CLIENTS:
                    chunk = await VIDEO_PROCESS.stdout.read(4096)
                    if not chunk:
                        break
                    await asyncio.gather(*[ws.send(chunk) for ws in CONNECTED_CLIENTS], return_exceptions=True)
            except Exception:
                pass
            finally:
                if VIDEO_PROCESS:
                    try:
                        VIDEO_PROCESS.kill()
                        await VIDEO_PROCESS.wait()
                    except Exception:
                        pass
                    VIDEO_PROCESS = None
        await asyncio.sleep(0.5)

async def run_ai_analysis():
    global IS_ANALYZING
    if IS_ANALYZING:
        return
    
    IS_ANALYZING = True
    model_name = os.environ.get("LLM_MODEL", "gemma3:4b")
    capture_path = "/dev/shm/llm_frame.jpg"
    
    async def broadcast(msg):
        await asyncio.gather(*[ws.send(msg) for ws in CONNECTED_CLIENTS], return_exceptions=True)

    try:
        await broadcast(json.dumps({"type": "ai_stream_start"}))
        await broadcast(json.dumps({"type": "ai_token", "text": "Stage 1: Capturing screen...\n"}))

        active_disp = DISPLAYS[ACTIVE_DISPLAY_INDEX]
        display_env = os.environ.get("DISPLAY", ":0.0")
        input_offset = f"{display_env}+{active_disp['x']},{active_disp['y']}"

        ffmpeg_cmd = [
            "ffmpeg", "-loglevel", "error", "-y",
            "-f", "x11grab", "-video_size", f"{active_disp['w']}x{active_disp['h']}",
            "-i", input_offset,
            "-frames:v", "1", "-q:v", "5", capture_path
        ]

        proc = await asyncio.create_subprocess_exec(*ffmpeg_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()

        try:
            with open(capture_path, "rb") as f:
                b64_image = base64.b64encode(f.read()).decode('utf-8')
        except FileNotFoundError:
            return

        await broadcast(json.dumps({"type": "ai_token", "text": "Stage 2: Scanning for code (vision OCR)...\n"}))

        ocr_prompt = (
            "Read ALL text visible in this screenshot exactly as written. "
            "Preserve code formatting, indentation, and line breaks. "
            "Output ONLY the raw text - no explanations."
        )

        timeout = aiohttp.ClientTimeout(total=300, sock_read=120)
        ocr_text = ""
        async with aiohttp.ClientSession(timeout=timeout) as session:
            ocr_payload = {
                "model": model_name,
                "prompt": ocr_prompt,
                "images": [b64_image],
                "stream": False
            }
            async with session.post(f"{OLLAMA_HOST}/api/generate", json=ocr_payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    ocr_text = data.get("response", "").strip()
                else:
                    text = await resp.text()
                    raise Exception(f"Ollama OCR error ({resp.status}): {text[:200]}")

        if not ocr_text or len(ocr_text) < 5:
            await broadcast(json.dumps({"type": "ai_token", "text": "[No code or text detected on screen. Make sure code is visible on the selected monitor.]"}))
            return

        clean_preview = ocr_text.replace('\n', ' ')[:80]
        await broadcast(json.dumps({"type": "ai_token", "text": f"Extracted: \"{clean_preview}...\"\n\nStage 3: Analyzing for bugs...\n\n---\n\n"}))

        reason_prompt = (
            f"Code from screen:\n\n"
            f"\"\"\"{ocr_text}\"\"\"\n\n"
            "Identify bugs in this code. Format:\n"
            "BUG: what is wrong\n"
            "WHY: root cause\n"
            "FIX: corrected code\n\n"
            "If no bugs: Looks correct."
        )

        async with aiohttp.ClientSession(timeout=timeout) as session:
            reason_payload = {
                "model": model_name,
                "prompt": reason_prompt,
                "stream": True
            }
            async with session.post(f"{OLLAMA_HOST}/api/generate", json=reason_payload) as resp:
                decoder = json.JSONDecoder()
                buf = b""
                done = False
                while True:
                    chunk = await resp.content.readany()
                    if chunk:
                        buf += chunk
                    pos = 0
                    while pos < len(buf) and not done:
                        try:
                            obj, end = decoder.raw_decode(buf[pos:].decode('utf-8'))
                            end_pos = pos + end
                            token = obj.get("response", "")
                            if token:
                                await broadcast(json.dumps({"type": "ai_token", "text": token}))
                            if obj.get("done", False):
                                done = True
                                break
                            pos = end_pos
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            break
                    buf = buf[pos:] if pos < len(buf) else b""
                    if done or not chunk:
                        break

    except Exception as e:
        await broadcast(json.dumps({"type": "ai_token", "text": f"\n\n[Error: {str(e)}]"}))
    finally:
        IS_ANALYZING = False
        await broadcast(json.dumps({"type": "ai_stream_end"}))

async def handler(websocket):
    CONNECTED_CLIENTS.add(websocket)
    try:
        async for message in websocket:
            if isinstance(message, str):
                try:
                    data = json.loads(message)
                    if data.get("type") == "trigger_eval":
                        asyncio.create_task(run_ai_analysis())
                    
                    # --- NEW SWITCH SCREEN LOGIC ---
                    elif data.get("type") == "switch_screen":
                        global ACTIVE_DISPLAY_INDEX, VIDEO_PROCESS
                        ACTIVE_DISPLAY_INDEX = (ACTIVE_DISPLAY_INDEX + 1) % len(DISPLAYS)
                        active_disp = DISPLAYS[ACTIVE_DISPLAY_INDEX]
                        
                        # Tell the UI to update monitor name and reload video
                        msg1 = json.dumps({"type": "screen_switched", "monitor": active_disp['name']})
                        for ws in CONNECTED_CLIENTS:
                            await ws.send(msg1)
                        msg2 = json.dumps({"type": "ai_token", "text": f"\nSwitched capture to {active_disp['name']}\n"})
                        for ws in CONNECTED_CLIENTS:
                            await ws.send(msg2)
                            
                        # Kill current ffmpeg; the video loop will instantly restart it with the new screen parameters
                        if VIDEO_PROCESS:
                            try:
                                VIDEO_PROCESS.kill()
                            except:
                                pass
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    finally:
        CONNECTED_CLIENTS.remove(websocket)

async def check_ollama():
    try:
        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{OLLAMA_HOST}/api/tags") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    models = [m["name"] for m in data.get("models", [])]
                    return models
    except Exception:
        return None

async def main():
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"\n{ts} ──────────────────────────────────────────────────────")
    print(f"{ts}   Screen Stream — ready")
    print(f"{ts} ──────────────────────────────────────────────────────")
    print(f"{ts}   Tablet URL  →  http://{LOCAL_IP}:{HTTP_PORT}")
    print(f"{ts}   WebSocket   →  ws://{LOCAL_IP}:{WS_PORT}")
    
    models = await check_ollama()
    if models:
        model_name = os.environ.get("LLM_MODEL", "gemma3:4b")
        if model_name in models:
            print(f"{ts}   Model       →  {model_name} (available)")
        else:
            print(f"{ts}   Model       →  {model_name} (WARNING: not in Ollama)")
            print(f"{ts}   Available   →  {', '.join(models[:5])}...")
    else:
        print(f"{ts}   Ollama      →  NOT REACHABLE (is ollama running?)")
    
    print(f"{ts} ──────────────────────────────────────────────────────\n")
    
    async with websockets.serve(handler, "0.0.0.0", WS_PORT):
        await asyncio.gather(video_capture_job())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Shutting Down] Tearing down listeners and clearing sockets...")
    except OSError as e:
        if e.errno == 98:
            print("\n[Port Conflict] Ports are busy. Cleaning sockets automatically...")
            os.system("fuser -k 8765/tcp 8766/tcp 2>/dev/null")
            print("[Resolved] Try executing ./share-llm again.")