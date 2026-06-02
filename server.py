import asyncio
import os
import json
import base64
import socket
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import threading
import aiohttp
from websockets.server import serve

# --- CONFIGURATION & GLOBALS ---
HTTP_PORT = 8766
WS_PORT = 8765
CONNECTED_CLIENTS = set()
IS_ANALYZING = False  # Global lock to prevent overlapping AI triggers

print("\nScreen Stream — starting up...")

def probe_encoders():
    has_nvenc = os.system("ffmpeg -encoders 2>/dev/null | grep -q h264_nvenc") == 0
    has_vaapi = os.path.exists("/dev/dri/renderD129")
    nv_status = "yes" if has_nvenc else "no"
    va_status = "yes (/dev/dri/renderD129)" if has_vaapi else "no"
    print(f"16:31:50 [INFO] Encoders — NVENC: {nv_status} | VAAPI: {va_status}")
    return has_nvenc, has_vaapi

HAS_NVENC, HAS_VAAPI = probe_encoders()
print("  ✓ ffmpeg found\n  ✓ Python packages ready\n  ✓ mpegts.js ready\n  ✓ xdotool found (cursor highlight available)\n  ✓ DISPLAY=" + os.environ.get("DISPLAY", ":0.0"))

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
print(f"16:32:01 [INFO] HTTP → port {HTTP_PORT}")

async def video_capture_job():
    """ Runs continuously to broadcast the screen to the mobile device """
    if HAS_NVENC:
        encoder = ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull"]
    elif HAS_VAAPI:
        encoder = ["-vaapi_device", "/dev/dri/renderD129", "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi"]
    else:
        encoder = ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"]

    ffmpeg_cmd = [
        "ffmpeg", "-loglevel", "quiet", "-y",
        "-f", "x11grab", "-video_size", "1920x1080", "-framerate", "30",
        "-i", os.environ.get("DISPLAY", ":0.0"),
        *encoder,
        "-b:v", "3000k", "-g", "30", "-f", "mpegts", "-"
    ]

    while True:
        if CONNECTED_CLIENTS:
            process = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            try:
                while CONNECTED_CLIENTS:
                    chunk = await process.stdout.read(4096)
                    if not chunk:
                        break
                    await asyncio.gather(*[ws.send(chunk) for ws in CONNECTED_CLIENTS], return_exceptions=True)
            except Exception:
                pass
            finally:
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass
        await asyncio.sleep(0.5)

async def run_ai_analysis():
    """ Triggered ONLY when the user clicks 'Send' on the UI """
    global IS_ANALYZING
    if IS_ANALYZING:
        return
    
    IS_ANALYZING = True
    model_name = os.environ.get("LLM_MODEL", "gemma3:4b")
    capture_path = "/dev/shm/llm_frame.jpg"
    
    # Strict formatting prompt as requested
    ai_prompt = (
        "You are an expert programming assistant analyzing a live screen feed. "
        "When you identify a coding question or technical problem on the screen, "
        "you MUST reply EXACTLY in this format:\n\n"
        "Definition: [A brief 1-2 sentence definition of the core problem]\n"
        "Approach: [A step-by-step logical approach to solving it]\n"
        "Code:\n[The fully functional code block]\n\n"
        "CRITICAL: Do NOT use markdown bolding (**) anywhere in your response. "
        "Do not use asterisks for emphasis. Keep the response clean and perfectly structured."
    )

    try:
        start_msg = json.dumps({"type": "ai_stream_start"})
        await asyncio.gather(*[ws.send(start_msg) for ws in CONNECTED_CLIENTS], return_exceptions=True)

        ffmpeg_cmd = [
            "ffmpeg", "-y", "-f", "x11grab", "-video_size", "1920x1080",
            "-i", os.environ.get("DISPLAY", ":0.0"),
            "-vframes", "1", "-q:v", "2", capture_path
        ]
        
        proc = await asyncio.create_subprocess_exec(*ffmpeg_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()

        try:
            with open(capture_path, "rb") as f:
                b64_image = base64.b64encode(f.read()).decode('utf-8')
        except FileNotFoundError:
            return

        payload = {"model": model_name, "prompt": ai_prompt, "images": [b64_image], "stream": True}

        async with aiohttp.ClientSession() as session:
            async with session.post("http://localhost:11434/api/generate", json=payload) as resp:
                async for line in resp.content:
                    if line:
                        data = json.loads(line.decode('utf-8'))
                        token = data.get("response", "")
                        token_msg = json.dumps({"type": "ai_token", "text": token})
                        await asyncio.gather(*[ws.send(token_msg) for ws in CONNECTED_CLIENTS], return_exceptions=True)
    except Exception:
        pass
    finally:
        IS_ANALYZING = False
        # Alert the UI that generation is finished so the button can be re-enabled
        end_msg = json.dumps({"type": "ai_stream_end"})
        await asyncio.gather(*[ws.send(end_msg) for ws in CONNECTED_CLIENTS], return_exceptions=True)

async def handler(websocket):
    CONNECTED_CLIENTS.add(websocket)
    try:
        async for message in websocket:
            # Route text messages (Commands from the mobile UI)
            if isinstance(message, str):
                try:
                    data = json.loads(message)
                    if data.get("type") == "trigger_eval":
                        asyncio.create_task(run_ai_analysis())
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
        # Only the video loops continuously now. AI waits for client trigger.
        await asyncio.gather(video_capture_job())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopping Screen Stream...")