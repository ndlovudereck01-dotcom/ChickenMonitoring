"""Download openly licensed chicken/poultry photos and clips for training.

Uses the Wikimedia Commons API (no key needed) and only keeps media whose licence
allows reuse (CC0 / public domain / CC BY / CC BY-SA / GFDL). Every download is
recorded in ``sources.json`` and ``ATTRIBUTION.md`` next to the files so the
provenance travels with the dataset.

    python manage.py fetch_photos --limit 40
    python manage.py fetch_photos --query "poultry house" --limit 60 --videos 3
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = (
    "ChickenVisionTraining/1.0 (local model training; https://commons.wikimedia.org/)"
)

DEFAULT_QUERIES = (
    "broiler chicken",
    "poultry farm chickens",
    "chicken house hens",
    "hen with chicks",
    "chicken flock",
)

FREE_LICENCE_HINTS = ("cc0", "cc-zero", "public domain", "cc by", "cc-by", "pd-", "gfdl")
REJECT_LICENCE_HINTS = ("non-free", "fair use", "all rights reserved", "copyright")


def _safe_name(title: str) -> str:
    stem = title.removeprefix("File:")
    stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", stem)
    stem = re.sub(r"[^0-9A-Za-z]+", "_", stem).strip("_").lower()
    return (stem or "image")[:80]


def _licence(metadata: dict) -> tuple[str, str, str, str]:
    """Return (licence name, licence url, artist, credit) from Commons extmetadata."""
    def pick(key: str) -> str:
        value = metadata.get(key) or {}
        return re.sub(r"<[^>]+>", "", str(value.get("value", ""))).strip()

    name = pick("LicenseShortName") or pick("UsageTerms") or "unknown"
    url = pick("LicenseUrl")
    artist = pick("Artist") or pick("Credit") or "unknown"
    credit = pick("Credit")
    return name, url, artist, credit


def _is_free(name: str) -> bool:
    lowered = name.lower()
    if any(hint in lowered for hint in REJECT_LICENCE_HINTS):
        return False
    return any(hint in lowered for hint in FREE_LICENCE_HINTS)


class Command(BaseCommand):
    help = (
        "Download openly licensed poultry photos (and optionally clips) from Wikimedia "
        "Commons into a training folder, with attribution recorded alongside."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--query",
            action="append",
            metavar="TEXT",
            help="Search phrase; repeatable. Defaults to a set of poultry phrases.",
        )
        parser.add_argument(
            "--limit", type=int, default=40, help="Photos kept per query (default: %(default)s)."
        )
        parser.add_argument(
            "--videos",
            type=int,
            default=0,
            metavar="N",
            help="Keep up to N short clips per query (default: 0).",
        )
        parser.add_argument("--out", default="datasets/raw/web", help="Destination folder.")
        parser.add_argument("--width", type=int, default=1280, help="Downloaded photo width.")
        parser.add_argument("--max-video-mb", type=float, default=60.0)
        parser.add_argument("--min-width", type=int, default=480)
        parser.add_argument("--delay", type=float, default=0.4, help="Politeness delay (s).")

    # ----------------------------------------------------------------- internals
    def _api(self, params: dict, retries: int = 3) -> dict:
        query = urllib.parse.urlencode({"format": "json", **params})
        request = urllib.request.Request(f"{API}?{query}", headers={"User-Agent": USER_AGENT})
        last: Exception | None = None
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception as exc:  # network hiccup: back off and retry
                last = exc
                time.sleep(1.5 * (attempt + 1))
        raise CommandError(f"Wikimedia API request failed: {last}")

    def _download(self, url: str, destination: Path) -> bool:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"    download failed: {exc}"))
            return False
        if len(payload) < 4096:
            self.stdout.write(self.style.WARNING("    skipped tiny/invalid file"))
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return True

    def _search(self, query: str, kind: str, limit: int, offset: int = 0) -> list[dict]:
        params = {
            "action": "query",
            "generator": "search",
            "gsrsearch": f"{query} filetype:{kind}",
            "gsrnamespace": "6",
            "gsrlimit": str(min(50, max(1, limit))),
            "gsroffset": str(offset),
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|size|mime",
            "iiurlwidth": "1280",
        }
        data = self._api(params)
        pages = (data.get("query") or {}).get("pages") or {}
        return [page for page in pages.values() if page.get("imageinfo")]

    def _flush(self, known: dict[str, dict], out_root: Path) -> None:
        """Write manifest + attribution after every query so a killed run keeps them."""
        entries = sorted(known.values(), key=lambda item: item["file"])
        (out_root / "sources.json").write_text(
            json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        lines = [
            "# Training media attribution",
            "",
            "These files were downloaded from Wikimedia Commons with",
            "`python manage.py fetch_photos`. Each row lists the licence the author",
            "published under; keep this file with the dataset and credit the authors",
            "if you redistribute the trained model or the images.",
            "",
            "| File | Title | Licence | Author | Source |",
            "| --- | --- | --- | --- | --- |",
        ]
        for entry in entries:
            artist = (entry.get("artist") or "unknown").replace("|", "/")[:80]
            lines.append(
                f"| `{entry['file']}` | {entry.get('title', '')} | {entry.get('licence', '')} "
                f"| {artist} | {entry.get('page', '')} |"
            )
        (out_root / "ATTRIBUTION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # -------------------------------------------------------------------- handle
    def handle(self, *args, **options):
        out_root = Path(options["out"]).expanduser().resolve()
        out_root.mkdir(parents=True, exist_ok=True)
        queries = options["query"] or list(DEFAULT_QUERIES)
        max_video_bytes = int(options["max_video_mb"] * 1024 * 1024)

        sources_path = out_root / "sources.json"
        known: dict[str, dict] = {}
        if sources_path.exists():
            try:
                existing = json.loads(sources_path.read_text(encoding="utf-8"))
                known = {entry["file"]: entry for entry in existing if "file" in entry}
            except (json.JSONDecodeError, OSError, KeyError):
                known = {}

        added_photos = added_videos = 0
        for query in queries:
            self.stdout.write(self.style.MIGRATE_HEADING(f"Search: {query}"))
            photos = 0
            offset = 0
            while photos < options["limit"]:
                pages = self._search(query, "bitmap", min(50, options["limit"] - photos), offset)
                if not pages:
                    break
                for page in pages:
                    info = page["imageinfo"][0]
                    if photos >= options["limit"]:
                        break
                    title = page.get("title", "")
                    licence, licence_url, artist, credit = _licence(info.get("extmetadata") or {})
                    if not _is_free(licence):
                        continue
                    width = int(info.get("width") or 0)
                    if width and width < options["min_width"]:
                        continue
                    stem = _safe_name(title)
                    filename = f"{stem}.jpg"
                    if filename in known:
                        photos += 1
                        continue
                    url = info.get("thumburl") or info.get("url")
                    if not url:
                        continue
                    destination = out_root / filename
                    if destination.exists() and destination.stat().st_size > 4096:
                        # Already downloaded (possibly by an interrupted run): keep the
                        # file, record its provenance, skip the network copy.
                        known.setdefault(
                            filename,
                            {
                                "file": filename,
                                "kind": "photo",
                                "title": title,
                                "query": query,
                                "licence": licence,
                                "licence_url": licence_url,
                                "artist": artist,
                                "credit": credit,
                                "page": info.get("descriptionurl", ""),
                            },
                        )
                        photos += 1
                        continue
                    if self._download(url, destination):
                        known[filename] = {
                            "file": filename,
                            "kind": "photo",
                            "title": title,
                            "query": query,
                            "licence": licence,
                            "licence_url": licence_url,
                            "artist": artist,
                            "credit": credit,
                            "page": info.get("descriptionurl", ""),
                        }
                        photos += 1
                        added_photos += 1
                    time.sleep(options["delay"])
                offset += len(pages)
                if len(pages) < min(50, options["limit"]):
                    break
            self.stdout.write(f"  photos: {photos}")
            self._flush(known, out_root)

            if options["videos"]:
                videos = 0
                for page in self._search(query, "video", max(options["videos"], 5)):
                    if videos >= options["videos"]:
                        break
                    info = page["imageinfo"][0]
                    size = int(info.get("size") or 0)
                    if not 0 < size <= max_video_bytes:
                        continue
                    licence, licence_url, artist, credit = _licence(info.get("extmetadata") or {})
                    if not _is_free(licence):
                        continue
                    url = info.get("url")
                    if not url:
                        continue
                    extension = Path(urllib.parse.urlparse(url).path).suffix.lower() or ".webm"
                    if extension not in {".webm", ".ogv", ".mp4", ".ogg"}:
                        continue
                    filename = f"{_safe_name(page.get('title', ''))}{extension}"
                    if filename in known:
                        continue
                    destination = out_root / "videos" / filename
                    if destination.exists() and destination.stat().st_size > 4096:
                        known.setdefault(
                            filename,
                            {
                                "file": f"videos/{filename}",
                                "kind": "video",
                                "title": page.get("title", ""),
                                "query": query,
                                "licence": licence,
                                "licence_url": licence_url,
                                "artist": artist,
                                "credit": credit,
                                "page": info.get("descriptionurl", ""),
                            },
                        )
                        videos += 1
                        continue
                    if self._download(url, destination):
                        known[filename] = {
                            "file": f"videos/{filename}",
                            "kind": "video",
                            "title": page.get("title", ""),
                            "query": query,
                            "licence": licence,
                            "licence_url": licence_url,
                            "artist": artist,
                            "credit": credit,
                            "page": info.get("descriptionurl", ""),
                        }
                        videos += 1
                        added_videos += 1
                    time.sleep(options["delay"])
                self.stdout.write(f"  videos: {videos}")
            self._flush(known, out_root)

        entries = sorted(known.values(), key=lambda item: item["file"])
        self._flush(known, out_root)
        total = len(entries)
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"{total} file(s) in {out_root} "
                f"(+{added_photos} photos, +{added_videos} videos this run)."
            )
        )
        self.stdout.write(f"  attribution: {out_root / 'ATTRIBUTION.md'}")
        self.stdout.write("Next: auto-label and split them")
        self.stdout.write(
            f"  python manage.py build_dataset --source {options['out']} "
            "--interval 2 --review datasets/review"
        )
