# Chicken Vision

A self-hosted chicken monitoring and management system. It watches live RTSP cameras or
uploaded footage, detects birds with **YOLOv11**, keeps one identity per bird with
**DeepSORT**, and turns that into a live headcount, directional movement counts, zone
occupancy, alerts and reports — all in a Django web app designed to run on a normal PC
(no GPU required).

```
camera / video ──► YOLOv11 detector ──► DeepSORT tracker ──► counting engine
                          │                     │                   │
                     annotated frames      bird identities     occupancy, unique
                          │                                        birds, IN/OUT,
                          ▼                                        zones, alerts
                 MJPEG live view + H.264 run video ──► Django UI ──► SQLite/PostgreSQL
```

## What you get

- **Live monitoring** — annotated MJPEG stream per camera, with current occupancy,
  unique birds, IN/OUT crossings and processing FPS, refreshed by polling without
  reloading the page.
- **Counting that survives occlusion** — a bird that disappears behind a feeder and
  comes back is re-associated with its previous identity (time + distance window), so
  the unique count does not inflate. Occupancy has a grace window so a one-frame
  dropout does not empty the house.
- **Directional movement** — draw a counting line on the live frame; crossings are
  counted with 3-frame hysteresis so a bird pecking across the line does not produce
  a hundred events.
- **Zones** — feeder, drinker, roost, door or custom polygons with peak/average
  occupancy per run.
- **Recorded runs** — upload MP4/MOV/AVI/MKV/WebM, get an annotated H.264 video,
  time series, per-bird track table and CSV exports.
- **Alerts** — overcrowding above a per-camera threshold, camera offline, net outflow,
  failed runs; acknowledge with an audit trail.
- **Roles** — admin / operator / viewer with least privilege by default.
- **Security** — Argon2 passwords, login throttling, CSRF on every mutation, a strict
  CSP with no inline code, private media served only through authenticated views,
  upload validation (extension + size + OpenCV probe + SHA-256) and an append-only
  audit log.

## Quick start

Requires Python 3.11+ (tested on 3.14) and ~3 GB of disk for PyTorch.

```bash
# 1. Install (CPU wheels first: they keep the download small)
python3 -m venv .venv
.venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -r requirements.txt

# 2. Configure
cp .env.example .env
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(50))"   # paste into DJANGO_SECRET_KEY
# set DJANGO_DEBUG=1 for local use

# 3. Database, demo accounts and cameras
.venv/bin/python manage.py migrate
.venv/bin/python manage.py bootstrap_demo      # prints one-time demo passwords
.venv/bin/python manage.py doctor              # environment pre-flight

# 4. Run it (two terminals)
.venv/bin/python manage.py run_worker          # detection worker
.venv/bin/python manage.py runserver 0.0.0.0:8000
```

Open <http://localhost:8000>, sign in as `demo_admin`, add a stream URL to a camera and
press **Start live**. `scripts/dev.sh` performs steps 1–3 in one go.

> The first detection run downloads `yolo11n.pt` (~5 MB) into `models/`. Until you train a
> chicken-specific model, the detector uses COCO class `bird`, which works but is less
> accurate on chicks and crowded floors.

## Using it

1. **Add a camera** — name, location and an RTSP/HTTP stream URL
   (`rtsp://user:pass@192.168.1.20:554/stream1`). Use a read-only camera account: the URL
   is stored in the database.
2. **Draw geometry** — on the camera page choose *Area* (floor the birds use) or
   *Counting line* (two points), click the live frame, then **Save geometry**. Coordinates
   are normalised, so they survive resolution changes.
3. **Start the session** — the worker connects, publishes annotated frames and writes
   samples. Metrics update in place; stopping the session finalises the run.
4. **Review** — *Runs* has the annotated video, population chart, per-bird tracks and CSV
   export. *Reports* aggregates by day and camera. *Alerts* is the operational queue.

### Roles

| Role | Read | Start/stop cameras, upload, acknowledge | Delete runs, audit log, users |
|---|---|---|---|
| viewer | ✅ | — | — |
| operator | ✅ | ✅ | — |
| admin | ✅ | ✅ | ✅ |

New accounts default to **viewer**; promote them in Django admin (`/admin/`).

## Configuration

Everything comes from `.env` (see `.env.example` for the full list).

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | required | Signing key; app refuses to start without it when `DEBUG=0` |
| `DJANGO_DEBUG` | `0` | `1` for local development only |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Hostnames users visit |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | — | `https://farm.example.com` when behind a proxy |
| `DJANGO_BEHIND_TLS_PROXY` | `0` | Trust `X-Forwarded-*` and enable secure cookies/HSTS |
| `DB_ENGINE` / `DB_*` | SQLite | Set `DB_ENGINE=postgres` for PostgreSQL |
| `CACHE_URL` | LocMem | `redis://…` shares rate limits across web workers |
| `CHICKEN_MODEL_PATH` | `models/chicken_yolo11.pt` | Custom weights; falls back to `yolo11n.pt` |
| `CHICKEN_CLASS_IDS` | auto | Force class ids, e.g. `0` for a single-class chicken model |
| `CHICKEN_DEVICE` | `cpu` | `cpu`, `0`, `0,1` |
| `CHICKEN_CONFIDENCE` / `CHICKEN_IOU` / `CHICKEN_IMGSZ` | `0.35` / `0.5` / `640` | Detector defaults (per camera overrides win) |
| `CHICKEN_FRAME_SKIP` | per camera (`2`) | Detect every Nth frame |
| `CHICKEN_MIN_TRACK_HITS` | `5` | Detections before a track is counted |
| `CHICKEN_DEDUPE_WINDOW_S` | `6` | Re-identification window for returning birds |
| `CHICKEN_WORKER_CONCURRENCY` | `2` | Parallel runs per worker process |
| `CHICKEN_MAX_UPLOAD_MB` | `512` | Upload limit |
| `CHICKEN_MAX_VIDEO_SECONDS` | `14400` | Reject longer videos |
| `CHICKEN_LIVE_FPS` | `10` | Annotated frames published per second |

## Training a better model

The generic COCO `bird` class is a baseline. For production accuracy, fine-tune YOLOv11
on frames from your own houses:

```bash
# 1. Put labelled images in datasets/chickens/{images,labels}/{train,val}
#    (see datasets/chickens.yaml for the expected layout, 1 class: chicken)
# 2. Train (CPU works for small datasets, a GPU is much faster)
.venv/bin/python manage.py train_model --data datasets/chickens.yaml --epochs 100 --imgsz 640 --device cpu
# 3. The best checkpoint is installed as models/chicken_yolo11.pt
.venv/bin/python manage.py run_worker    # restart the worker to load it
```

`train_model` copies `best.pt` to `models/chicken_yolo11.pt`; class names are read from the
checkpoint, so a poultry-named class is detected automatically.

## Architecture

| Path | Responsibility |
|---|---|
| `monitoring/models.py` | Camera, Zone, VideoAsset, DetectionRun, BirdTrack, CountSample, ZoneStatistic, Alert, AuditLog |
| `monitoring/ml/detector.py` | YOLOv11 wrapper, class auto-resolution (poultry names → COCO `bird` → all) |
| `monitoring/ml/tracker.py` | DeepSORT adapter, IoU fallback tracker, hybrid that degrades mid-run instead of failing |
| `monitoring/ml/counting.py` | Occupancy, unique roots + re-ID, line crossing, zones |
| `monitoring/ml/pipeline.py` | Frame loop, sampling, annotation, persistence, cancellation |
| `monitoring/ml/worker.py` | DB-backed queue with atomic claims, thread pool, crash reconciliation |
| `monitoring/views.py` | Pages, live MJPEG, polling APIs, CSV export, Range-enabled video |
| `monitoring/security.py` | Role decorators, throttling, client IP |
| `monitoring/middleware.py` | CSP, Permissions-Policy and related headers |

There is no Celery/Redis requirement: jobs are rows in the database, claimed with a
conditional `UPDATE`, and the worker keeps its own thread pool. The web tier reads live
frames from `media/private/live/<camera>.jpg`, written atomically by the worker, so the
two processes never share memory. For multiple web processes on different machines, use
PostgreSQL and a shared `CACHE_URL`.

## Production deployment

```bash
# .env: DJANGO_DEBUG=0, DJANGO_SECRET_KEY=…, DJANGO_ALLOWED_HOSTS=farm.example.com
#       DJANGO_BEHIND_TLS_PROXY=1, DJANGO_CSRF_TRUSTED_ORIGINS=https://farm.example.com
scripts/serve.sh            # migrate + collectstatic + gunicorn on :8000
```

Put nginx or Caddy in front for TLS. Two details matter for live video:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 3600s;      # MJPEG streams stay open
    proxy_buffering off;           # do not buffer the stream
}
```

Run the worker as its own service so a deploy never interrupts detection:

```ini
# /etc/systemd/system/chickenvision-worker.service
[Unit]
Description=Chicken Vision detection worker
After=network.target

[Service]
WorkingDirectory=/srv/chickenvision
ExecStart=/srv/chickenvision/.venv/bin/python manage.py run_worker --concurrency 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

After an unclean shutdown, `manage.py run_worker --reconcile` marks half-finished runs as
failed so they can be retried; `run_worker` does this automatically at startup.

## Operations

```bash
.venv/bin/python manage.py doctor                    # configuration, packages, storage, queue
.venv/bin/python manage.py prune_data --dry-run      # what old samples/alerts would be deleted
.venv/bin/python manage.py prune_data --samples-days 30 --runs-days 180
.venv/bin/python manage.py run_worker --once         # process one queued run
.venv/bin/python manage.py test monitoring.tests      # 58 tests, ~1 min on CPU
```

Health check: `GET /healthz` returns `200 {"status":"ok","database":true}` without
authentication and without leaking details — wire it to your uptime monitor.

## Performance notes

- Detection is the bottleneck. On a 4-core CPU, `yolo11n` at 640 px with
  `frame_skip=2` processes roughly 2–6 frames per second per camera. Lower `imgsz` to 480
  or raise `frame_skip` to 3–4 for more cameras.
- The IoU fallback tracker is ~10× cheaper than DeepSORT; keep DeepSORT for crowded
  houses where appearance matters, and the fallback for sparse ones.
- Live frames are capped at `CHICKEN_LIVE_FPS` and JPEG quality 72 to keep the MJPEG
  stream light. The run video is transcoded to H.264 with the bundled ffmpeg binary
  (no system ffmpeg needed).

## Known limitations

- Unique counts are per run; summing runs in *Reports* over-counts a bird seen twice.
  Treat reports as activity, not a census.
- Re-identification is geometric/time based (plus DeepSORT appearance). Birds that leave
  and re-enter far away after the dedupe window are counted again.
- A camera session is infinite by design: a dropped RTSP stream is retried with backoff
  until you stop the session or the worker restarts.
- SQLite is fine for one worker + a few web processes; use PostgreSQL beyond that.
- The UI is English-only and uses system fonts.

## License and data

The application code is yours to run and modify. Model weights (`yolo11n.pt`) and any
dataset you train on keep their own licenses — check them before commercial use, and keep
farm footage private: uploads and annotated videos live under `media/private/` and are only
served through authenticated, Range-aware views.
