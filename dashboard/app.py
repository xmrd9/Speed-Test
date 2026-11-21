"""
netdash - real-time network speed dashboard.

Collects live throughput (if the OS allows it), latency, and periodic
speed tests, and streams them to the browser over Server-Sent Events.

Works on desktop (Windows/macOS/Linux) and on Android via Termux.
"""
import asyncio
import json
import os
import sqlite3
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

try:
    import psutil  # optional: only used for live interface counters
except ImportError:
    psutil = None

BASE = Path(__file__).parent
DB_PATH = BASE / "netdash.db"

# --- Configuration (override with environment variables) ---
IFACE = os.environ.get("NET_IFACE")                                    # e.g. "wlan0", "eth0", "Wi-Fi"
PING_HOST = os.environ.get("PING_HOST", "1.1.1.1")
SPEEDTEST_INTERVAL = int(os.environ.get("SPEEDTEST_INTERVAL", "20"))   # rest between tests (seconds)
TEST_SECONDS = int(os.environ.get("TEST_SECONDS", "5"))                # length of each download/upload phase
STATE = {"counters_ok": True}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36",
}
CF_HEADERS = {**HEADERS,
              "Referer": "https://speed.cloudflare.com/",
              "Origin": "https://speed.cloudflare.com"}

DOWN_URLS = [
    "https://speed.cloudflare.com/__down?bytes=10000000",
    "https://proof.ovh.net/files/100Mb.dat",
    "https://speedtest.tele2.net/10MB.zip",
]
UP_URLS = [
    "https://speed.cloudflare.com/__up",
    "https://speedtest.tele2.net/upload.php",
]

# --- In-memory state ---
subscribers: set[asyncio.Queue] = set()
live_history = deque(maxlen=300)   # last 5 minutes of 1 s samples
ping_history = deque(maxlen=120)   # last 10 minutes of 5 s samples


# --- Helpers ---
def hdrs(url):
    return CF_HEADERS if "cloudflare" in url else HEADERS


def db_run(sql, args=()):
    con = sqlite3.connect(DB_PATH)
    try:
        with con:
            return con.execute(sql, args).fetchall()
    finally:
        con.close()


def publish(event: str, data: dict):
    """Send one SSE message to every connected browser."""
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    for q in list(subscribers):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass  # slow client: drop the message rather than block


def read_counters():
    """Return total (bytes_received, bytes_sent) for the chosen interface(s)."""
    if psutil is None:
        raise RuntimeError("psutil is not installed")
    stats = psutil.net_io_counters(pernic=True)
    if IFACE:
        c = stats[IFACE]
        return c.bytes_recv, c.bytes_sent
    rx = tx = 0
    for name, c in stats.items():
        n = name.lower()
        if n in ("lo", "lo0") or n.startswith("loopback"):
            continue  # ignore local-only traffic
        rx += c.bytes_recv
        tx += c.bytes_sent
    return rx, tx


def emit_live(down, up):
    s = {"t": time.time(), "down": down, "up": up}
    live_history.append(s)
    publish("live", s)


# --- Collectors ---
async def live_loop():
    """Per-second throughput from interface counters. Stops quietly if the
    OS blocks them (e.g. Android), and the speed test reports live values."""
    try:
        prev_rx, prev_tx = read_counters()
    except Exception as e:
        print("Interface counters unavailable, using speed-test mode:", e)
        STATE["counters_ok"] = False
        return
    prev_t = time.monotonic()
    while True:
        await asyncio.sleep(1)
        try:
            rx, tx = read_counters()
        except Exception as e:
            print("Counter read failed, using speed-test mode:", e)
            STATE["counters_ok"] = False
            return
        now = time.monotonic()
        dt = now - prev_t
        emit_live(max(0, (rx - prev_rx) * 8 / dt / 1e6),
                  max(0, (tx - prev_tx) * 8 / dt / 1e6))
        prev_rx, prev_tx, prev_t = rx, tx, now


async def latency_loop():
    while True:
        ms = None
        try:
            start = time.perf_counter()
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(PING_HOST, 443), timeout=2
            )
            ms = (time.perf_counter() - start) * 1000  # TCP handshake ~ RTT
            writer.close()
        except Exception:
            pass  # leave ms as None -> shown as a gap
        sample = {"t": time.time(), "ms": ms}
        ping_history.append(sample)
        publish("ping", sample)
        await asyncio.sleep(5)


async def measure_down(client, url, live):
    total = last_total = 0
    start = last_t = time.perf_counter()
    while time.perf_counter() - start < TEST_SECONDS:
        async with client.stream("GET", url, headers=hdrs(url)) as r:
            r.raise_for_status()
            async for chunk in r.aiter_bytes():
                total += len(chunk)
                now = time.perf_counter()
                if now - last_t >= 1:
                    if live:
                        emit_live((total - last_total) * 8 / (now - last_t) / 1e6, 0)
                    last_t, last_total = now, total
                if now - start >= TEST_SECONDS:
                    break
    if total == 0:
        raise RuntimeError("no data received")
    return total * 8 / (time.perf_counter() - start) / 1e6


async def measure_up(client, url, live):
    payload = b"0" * 1_000_000
    sent = 0
    start = time.perf_counter()
    while time.perf_counter() - start < TEST_SECONDS:
        t0 = time.perf_counter()
        r = await client.post(url, content=payload, headers=hdrs(url))
        r.raise_for_status()
        sent += len(payload)
        if live:
            emit_live(0, len(payload) * 8 / (time.perf_counter() - t0) / 1e6)
    return sent * 8 / (time.perf_counter() - start) / 1e6


async def first_working(client, urls, fn, live, label):
    last_err = None
    for url in urls:
        try:
            return await fn(client, url, live)
        except Exception as e:
            print(f"{label} via {url} failed: {e}")
            last_err = e
    raise last_err


async def run_speedtest():
    live = not STATE["counters_ok"]   # report per-second speeds ourselves
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        down = await first_working(client, DOWN_URLS, measure_down, live, "download")
        try:
            up = await first_working(client, UP_URLS, measure_up, live, "upload")
        except Exception:
            up = 0.0  # keep the download result even if every upload server refuses
    return {"t": time.time(), "down": down, "up": up}


async def speedtest_loop():
    await asyncio.sleep(5)  # let the server finish starting
    while True:
        try:
            result = await run_speedtest()
            db_run("INSERT INTO speedtests VALUES (?, ?, ?)",
                   (result["t"], result["down"], result["up"]))
            publish("speedtest", result)
        except Exception as e:
            print("speed test failed:", e)
        await asyncio.sleep(SPEEDTEST_INTERVAL)


# --- App ---
@asynccontextmanager
async def lifespan(app):
    db_run("CREATE TABLE IF NOT EXISTS speedtests "
           "(t REAL PRIMARY KEY, down REAL, up REAL)")
    tasks = [asyncio.create_task(f()) for f in (live_loop, latency_loop, speedtest_loop)]
    yield
    for t in tasks:
        t.cancel()


async def index(request):
    return FileResponse(BASE / "static" / "index.html")


async def history(request):
    rows = db_run("SELECT t, down, up FROM speedtests ORDER BY t DESC LIMIT 100")
    return JSONResponse({
        "live": list(live_history),
        "ping": list(ping_history),
        "speedtests": [{"t": t, "down": d, "up": u} for t, d, u in reversed(rows)],
    })


async def stream(request):
    async def events():
        q = asyncio.Queue(maxsize=100)
        subscribers.add(q)
        try:
            yield "retry: 3000\n\n"  # browser reconnect delay
            while True:
                try:
                    yield await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # stops proxies closing idle connections
        finally:
            subscribers.discard(q)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/history", history),
        Route("/stream", stream),
    ],
    lifespan=lifespan,
)
