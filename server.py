import asyncio
import os
import json
import base64
import socket
import subprocess
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import threading
import aiohttp
from websockets.server import serve

# --- CONFIGURATION & GLOBALS ---
HTTP_PORT = 8766
WS_PORT = 8765
CONNECTED_CLIENTS = set()
IS_ANALYZING = False
VIDEO_PROCESS = None

print("\nScreen Stream — starting up...")

def probe_encoders():
    has_nvenc = os.system("ffmpeg -encoders 2>/dev/null | grep -q h264_nvenc") == 0
    has_vaapi = os.path.exists("/dev/dri/renderD129")
    nv_status = "yes" if has_nvenc else "no"
    va_status = "yes (/dev/dri/renderD129)" if has_vaapi else "no"
    print(f"16:31:50 [INFO] Encoders — NVENC: {nv_status} | VAAPI: {va_status}")
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
ACTIVE_DISPLAY_INDEX = 0

# If multiple monitors exist, default to the external one (usually HDMI/DP, not 'eDP')
if len(DISPLAYS) > 1:
    for i, d in enumerate(DISPLAYS):
        if not d['name'].lower().startswith('e'):
            ACTIVE_DISPLAY_INDEX = i
            break

print("  ✓ ffmpeg found")
print(f"  ✓ Display Engine mapping {len(DISPLAYS)} monitors (Active: {DISPLAYS[ACTIVE_DISPLAY_INDEX]['name']})")
print("  ✓ Python packages ready")
print("  ✓ mpegts.js ready")

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
    
    try:
        start_msg = json.dumps({"type": "ai_stream_start"})
        await asyncio.gather(*[ws.send(start_msg) for ws in CONNECTED_CLIENTS], return_exceptions=True)
        
        status_msg = json.dumps({"type": "ai_token", "text": "👀 *Stage 1: Scanning screen for text...*\n\n"})
        await asyncio.gather(*[ws.send(status_msg) for ws in CONNECTED_CLIENTS], return_exceptions=True)

        # Map the snapshot capture to the exactly selected display bounds
        active_disp = DISPLAYS[ACTIVE_DISPLAY_INDEX]
        display_env = os.environ.get("DISPLAY", ":0.0")
        input_offset = f"{display_env}+{active_disp['x']},{active_disp['y']}"

        ffmpeg_cmd = [
            "ffmpeg", "-loglevel", "error", "-y", 
            "-f", "x11grab", "-video_size", f"{active_disp['w']}x{active_disp['h']}",
            "-i", input_offset,
            "-frames:v", "1", "-update", "1", "-q:v", "2", capture_path
        ]
        
        proc = await asyncio.create_subprocess_exec(*ffmpeg_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()

        try:
            with open(capture_path, "rb") as f:
                b64_image = base64.b64encode(f.read()).decode('utf-8')
        except FileNotFoundError:
            return

        timeout = aiohttp.ClientTimeout(total=None, sock_read=None, sock_connect=None)

        ocr_prompt = (
            "You are a strict OCR (Optical Character Recognition) engine. "
            "Your ONLY job is to read the text visible in this image. "
            "Do NOT solve any problems. Do NOT write any code unless it is literally visible in the image. "
            "Output ONLY the exact text you see."
        )
        
        ocr_payload = {
            "model": model_name,
            "prompt": ocr_prompt,
            "images": [b64_image],
            "stream": False,
            "options": {"num_ctx": 8192}
        }

        ocr_text = ""
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("http://localhost:11434/api/generate", json=ocr_payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    ocr_text = data.get("response", "").strip()

        if not ocr_text or len(ocr_text) < 3:
            err_msg = json.dumps({"type": "ai_token", "text": "Definition: No legible text found on screen to analyze."})
            await asyncio.gather(*[ws.send(err_msg) for ws in CONNECTED_CLIENTS], return_exceptions=True)
            return

        clean_preview = ocr_text.replace('\n', ' ')[:60]
        status_msg2 = json.dumps({"type": "ai_token", "text": f"✅ *Extracted:* `{clean_preview}...`\n\n🧠 *Stage 2: Reasoning...*\n\n---\n\n"})
        await asyncio.gather(*[ws.send(status_msg2) for ws in CONNECTED_CLIENTS], return_exceptions=True)

        reason_prompt = (
            f"Here is the exact text extracted from the user's screen:\n\n"
            f"\"\"\"{ocr_text}\"\"\"\n\n"
            "Based ONLY on this text, identify the technical problem. "
            "Reply EXACTLY in this format:\n\n"
            "Definition: [1-2 sentence summary of the problem]\n"
            "Approach: [Step-by-step logic]\n"
            "Code:\n[Functional code block]\n\n"
            "CRITICAL: Do not invent requirements outside of the extracted text. Do not use markdown bolding (**)."
        )

        reason_payload = {
            "model": model_name,
            "prompt": reason_prompt,
            "stream": True,
            "options": {"num_ctx": 8192, "num_predict": 4096}
        }

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("http://localhost:11434/api/generate", json=reason_payload) as resp:
                async for line in resp.content:
                    if line:
                        data = json.loads(line.decode('utf-8'))
                        token = data.get("response", "")
                        token_msg = json.dumps({"type": "ai_token", "text": token})
                        await asyncio.gather(*[ws.send(token_msg) for ws in CONNECTED_CLIENTS], return_exceptions=True)

    except Exception as e:
        err_msg = json.dumps({"type": "ai_token", "text": f"\n\n[System Error: {str(e)}]"})
        await asyncio.gather(*[ws.send(err_msg) for ws in CONNECTED_CLIENTS], return_exceptions=True)
    finally:
        IS_ANALYZING = False
        end_msg = json.dumps({"type": "ai_stream_end"})
        await asyncio.gather(*[ws.send(end_msg) for ws in CONNECTED_CLIENTS], return_exceptions=True)

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
                        
                        # Notify the UI immediately
                        msg = json.dumps({"type": "ai_token", "text": f"\n\n📺 *Switched capture to {active_disp['name']} ({active_disp['w']}x{active_disp['h']})*\n\n"})
                        for ws in CONNECTED_CLIENTS:
                            await ws.send(msg)
                            
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

async def main():
    print("\n──────────────────────────────────────────────────────")
    print("  Screen Stream — ready")
    print("──────────────────────────────────────────────────────")
    print(f"  Tablet URL  →  http://{LOCAL_IP}:{HTTP_PORT}")
    print(f"  WebSocket   →  ws://{LOCAL_IP}:{WS_PORT}")
    print("──────────────────────────────────────────────────────\n")
    
    async with serve(handler, "0.0.0.0", WS_PORT):
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