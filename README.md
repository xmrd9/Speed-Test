# netdash

A small real-time network speed dashboard. A Python server measures your connection and streams the results to a web page that draws live charts. It runs on desktop (Windows, macOS, Linux) and on Android through Termux.

## What it shows

- **Download and upload speed** (Mbps), updated every second
- **Latency** (ms), measured every 5 seconds with a TCP connect to `1.1.1.1:443`
- **Speed test history**, with results stored in a local SQLite database

## How it works

```
 collectors (asyncio)  ──►  Server-Sent Events  ──►  browser (Chart.js)
 - interface counters         /stream                 live charts
 - latency probe              /api/history            backfill on page load
 - timed download/upload
```

- **Backend:** [Starlette](https://www.starlette.io/) + uvicorn, with `httpx` for the speed tests.
- **Frontend:** a single static HTML page using Chart.js. There is no build step.
- **Live throughput:** if the OS allows reading interface counters (`psutil`), speeds come from those. On Android that file is blocked, so the app automatically switches to *speed-test mode*: it runs short download/upload tests in a loop and reports a reading each second while they run.

## Project structure

```
netdash/
├── app.py            # server and collectors
├── static/
│   └── index.html    # dashboard page
├── requirements.txt
├── .gitignore
└── README.md
```

## Install on Android (Termux)

1. **Install Termux** from [F-Droid](https://f-droid.org/packages/com.termux/) or the [GitHub releases page](https://github.com/termux/termux-app/releases). The Play Store version is outdated and gives errors.

2. **Install Python and Git:**

   ```bash
   pkg update && pkg upgrade
   pkg install python git
   ```

3. **Get the project** (replace the URL with your repository):

   ```bash
   git clone https://github.com/<your-username>/netdash.git
   cd netdash
   ```

4. **Install the dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

   These are all pure Python, so nothing needs to be compiled. Do **not** install `fastapi`, because it pulls in `pydantic-core`, which needs a Rust build and fails on Android.

5. **Keep Termux alive in the background:**

   ```bash
   termux-wake-lock
   ```

   Also turn off battery optimization for Termux in Android Settings, or the server may stop when the screen turns off.

6. **Run it:**

   ```bash
   uvicorn app:app --host 127.0.0.1 --port 8000
   ```

7. **Open Chrome** on the phone and go to **http://localhost:8000**. The first speed test starts after about 5 seconds.

On startup you will see `Interface counters unavailable, using speed-test mode`. That is expected on Android.

To stop the server, press **Ctrl+C** in Termux.

### Viewing it from another device

Start the server with `--host 0.0.0.0`, then open `http://<phone-ip>:8000` from a device on the same Wi-Fi. There is no authentication, so only do this on a network you trust.

## Install on desktop (Windows, macOS, Linux)

```bash
git clone https://github.com/<your-username>/netdash.git
cd netdash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install psutil                 # optional: enables per-second counters
uvicorn app:app --host 127.0.0.1 --port 8000
```

Open http://localhost:8000. Run the command from inside the `netdash` folder, since `app:app` means the `app` object in `app.py`.

## Configuration

Set these as environment variables before starting:

| Variable | Default | Meaning |
|---|---|---|
| `NET_IFACE` | all non-loopback | Interface to read counters from (e.g. `wlan0`, `eth0`, `Wi-Fi`). Only used when `psutil` works. |
| `PING_HOST` | `1.1.1.1` | Host for the latency probe (port 443). |
| `SPEEDTEST_INTERVAL` | `20` | Seconds to rest between speed tests. |
| `TEST_SECONDS` | `5` | Length of each download and upload phase. |

Example:

```bash
SPEEDTEST_INTERVAL=300 TEST_SECONDS=3 uvicorn app:app --host 127.0.0.1 --port 8000
```

## Data usage

Continuous testing uses a lot of data. A 5-second download on a 100 Mbps link is about 60 MB, so the default settings can use several GB per hour. On mobile data, raise `SPEEDTEST_INTERVAL` (for example `300`) or lower `TEST_SECONDS`.

## Troubleshooting

- **Download/upload never change:** check the terminal for `download via <url> failed: ...` lines. The app tries Cloudflare, OVH, and Tele2 in turn. If all fail, your network or VPN may be blocking them. Try another network and turn off VPN or private DNS filtering.
- **`Error loading ASGI app. Could not import module "app"`:** you are in the wrong folder. `cd` into the folder that contains `app.py`.
- **`uvicorn: command not found`:** activate your virtual environment, or run `python -m uvicorn app:app --port 8000`.
- **Charts are blank:** the page loads Chart.js from cdnjs, so the browser needs internet access.
- **Results look low on fast connections:** short single-connection tests can under-report. Raise `TEST_SECONDS`.

## Notes

- Speed tests use public endpoints (Cloudflare, OVH, Tele2). Their availability can change, so edit `DOWN_URLS` and `UP_URLS` in `app.py` if one stops working.
- Results are measured from the device running the server, not from other devices on the network.
