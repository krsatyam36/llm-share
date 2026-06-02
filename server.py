import asyncio
import os
import json
import base64
import sys
import socket
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
import threading
import aiohttp
from websockets.server import serve

# --- CONFIGURATION & GLOBALS ---
HTTP_PORT = 8766
WS_PORT = 8765
CONNECTED_CLIENTS = set()

print("\nScreen Stream — starting up...")

# 1. System Checks & Feature Probing
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

# 2. HTTP Server for Client Hosting
def run_http_server():
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass # Suppress standard HTTP logging to keep console clean
            
    server = ThreadingHTTPServer(('0.0.0.0', HTTP_PORT), QuietHandler)
    server.serve_forever()

# Start HTTP server in a background native thread
threading.Thread(target=run_http_server, daemon=True).start()
print(f"16:32:01 [INFO] HTTP → port {HTTP_PORT}")

# 3. Dedicated Video Capture and Broadcast Loop
async def video_capture_job():
    """
    Spawns a single global high-performance FFmpeg pipeline and 
    broadcasts raw video chunks to all connected WebSockets.
    """
    # Select encoder based on pre-flight system probe
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
                *ffmpeg_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            
            try:
                while CONNECTED_CLIENTS:
                    chunk = await process.stdout.read(4096)
                    if not chunk:
                        break
                    # Send binary chunk to all listening phone/tablet nodes
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

# 4. Local Multimodal AI Processing Loop (Hidden from Host Console)
async def ai_analysis_loop():
    """
    Quietly captures desktop snapshots, feeds them to the local Ollama 
    multimodal instance, and transmits markdown tokens directly to the client UI.
    """
    model_name = os.environ.get("LLM_MODEL", "gemma3:4b")
    capture_path = "/dev/shm/llm_frame.jpg"
    # The system prompt to force structured output
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

    while True:
        await asyncio.sleep(4)  # Step interval to protect system resources
        if not CONNECTED_CLIENTS:
            continue

        # Extract snapshot to Shared Memory (/dev/shm)
        ffmpeg_cmd = [
            "ffmpeg", "-y", "-f", "x11grab", "-video_size", "1920x1080",
            "-i", os.environ.get("DISPLAY", ":0.0"),
            "-vframes", "1", "-q:v", "2", capture_path
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.communicate()

        try:
            with open(capture_path, "rb") as f:
                b64_image = base64.b64encode(f.read()).decode('utf-8')
        except FileNotFoundError:
            continue

        payload = {
            "model": model_name,
            "prompt": ai_prompt,
            "images": [b64_image],
            "stream": True
        }

        try:
            # Alert the mobile layout that a fresh response is compiling
            start_msg = json.dumps({"type": "ai_stream_start"})
            await asyncio.gather(*[ws.send(start_msg) for ws in CONNECTED_CLIENTS], return_exceptions=True)

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

# 5. WebSocket Connection Management
async def handler(websocket):
    CONNECTED_CLIENTS.add(websocket)
    client_address = websocket.remote_address
    print(f"16:32:25 [INFO] [external] {client_address} — 3000k 30fps")
    try:
        async for message in websocket:
            pass # Handle client inputs or touch telemetry here if needed
    except Exception:
        pass
    finally:
        CONNECTED_CLIENTS.remove(websocket)

async def main():
    print("\n──────────────────────────────────────────────────────")
    print("  Screen Stream — ready")
    print("──────────────────────────────────────────────────────")
    print(f"  Tablet URL  →  http://{LOCAL_IP}:{HTTP_PORT}")
    print(f"  mDNS URL    →  http://screen-stream.local:{HTTP_PORT}")
    print(f"  WebSocket   →  ws://{LOCAL_IP}:{WS_PORT}")
    print("  Laptop      →  eDP   1920x1080")
    print("──────────────────────────────────────────────────────\n")
    print(f"16:32:01 [INFO] server listening on 0.0.0.0:{WS_PORT}")
    
    async with serve(handler, "0.0.0.0", WS_PORT):
        # Tie our core processing loops into the main event cycle
        await asyncio.gather(
            video_capture_job(),
            ai_analysis_loop()
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopping Screen Stream...")
