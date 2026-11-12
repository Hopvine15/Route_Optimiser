"""Activity ingestion for GPX and FIT files with local disk caching."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import gzip
from io import BytesIO
import json
import logging
from pathlib import Path
from typing import Any
import fitparse
import gpxpy

logger = logging.getLogger("hexroute.ingest")

SEMICIRCLE_TO_DEG = 180.0 / (2**31)


@dataclass(slots=True)
class GPSPoint:
    activity_id: str
    timestamp: str | None  # ISO 8601 string
    lat: float
    lng: float
    elevation: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "activity_id": self.activity_id,
            "timestamp": self.timestamp,
            "lat": self.lat,
            "lng": self.lng,
            "elevation": self.elevation,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GPSPoint":
        return cls(
            activity_id=data["activity_id"],
            timestamp=data.get("timestamp"),
            lat=float(data["lat"]),
            lng=float(data["lng"]),
            elevation=float(data["elevation"]) if data.get("elevation") is not None else None,
        )


@dataclass
class ActivitySummary:
    activity_id: str
    filename: str
    file_type: str
    point_count: int
    has_gps: bool
    start_time: str | None = None
    end_time: str | None = None


@dataclass
class IngestResult:
    total_files: int
    parsed_activities: int
    skipped_no_gps: int
    total_points: int
    earliest_date: str | None
    latest_date: str | None
    activities: dict[str, list[GPSPoint]]
    summaries: list[ActivitySummary]


def _open_file_content(path: Path) -> bytes:
    """Read raw bytes from either plain or gzipped file."""
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as f:
            return f.read()
    return path.read_bytes()


def parse_gpx_content(content: bytes | str, activity_id: str) -> list[GPSPoint]:
    """Parse GPX content into a list of GPSPoints."""
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")

    gpx = gpxpy.parse(content)
    points: list[GPSPoint] = []

    for track in gpx.tracks:
        for segment in track.segments:
            for pt in segment.points:
                if pt.latitude is not None and pt.longitude is not None:
                    ts_str = pt.time.isoformat() if pt.time else None
                    points.append(
                        GPSPoint(
                            activity_id=activity_id,
                            timestamp=ts_str,
                            lat=pt.latitude,
                            lng=pt.longitude,
                            elevation=pt.elevation,
                        )
                    )

    # Check waypoints or routes if tracks were empty
    if not points:
        for route in gpx.routes:
            for pt in route.points:
                if pt.latitude is not None and pt.longitude is not None:
                    ts_str = pt.time.isoformat() if pt.time else None
                    points.append(
                        GPSPoint(
                            activity_id=activity_id,
                            timestamp=ts_str,
                            lat=pt.latitude,
                            lng=pt.longitude,
                            elevation=pt.elevation,
                        )
                    )

    return points


def parse_fit_content(content: bytes, activity_id: str) -> list[GPSPoint]:
    """Parse FIT file content into a list of GPSPoints."""
    fitfile = fitparse.FitFile(BytesIO(content))
    points: list[GPSPoint] = []

    for record in fitfile.get_messages("record"):
        values = record.get_values()
        lat_raw = values.get("position_lat")
        lng_raw = values.get("position_long")

        if lat_raw is not None and lng_raw is not None:
            # FIT position coordinates are stored in semicircles
            # If values are large (> 180), convert from semicircles to degrees
            lat = lat_raw * SEMICIRCLE_TO_DEG if abs(lat_raw) > 180 else float(lat_raw)
            lng = lng_raw * SEMICIRCLE_TO_DEG if abs(lng_raw) > 180 else float(lng_raw)

            # Validate range
            if -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0:
                ts = values.get("timestamp")
                ts_str = ts.isoformat() if isinstance(ts, datetime) else None
                altitude = values.get("altitude") or values.get("enhanced_altitude")
                elevation = float(altitude) if altitude is not None else None

                points.append(
                    GPSPoint(
                        activity_id=activity_id,
                        timestamp=ts_str,
                        lat=lat,
                        lng=lng,
                        elevation=elevation,
                    )
                )

    return points


def parse_activity_file(path: Path) -> tuple[str, list[GPSPoint], ActivitySummary]:
    """Parse a single GPX or FIT file."""
    # Deduce base activity ID and type
    name = path.name
    is_gz = name.endswith(".gz")
    base_name = name[:-3] if is_gz else name
    ext = Path(base_name).suffix.lower()
    activity_id = Path(base_name).stem

    raw_bytes = _open_file_content(path)

    if ext == ".gpx":
        points = parse_gpx_content(raw_bytes, activity_id)
        file_type = "gpx"
    elif ext == ".fit":
        points = parse_fit_content(raw_bytes, activity_id)
        file_type = "fit"
    else:
        raise ValueError(f"Unsupported file format: {path}")

    has_gps = len(points) > 0
    start_time = points[0].timestamp if has_gps else None
    end_time = points[-1].timestamp if has_gps else None

    summary = ActivitySummary(
        activity_id=activity_id,
        filename=name,
        file_type=file_type,
        point_count=len(points),
        has_gps=has_gps,
        start_time=start_time,
        end_time=end_time,
    )

    return activity_id, points, summary


def get_cache_paths(cache_dir: str | Path) -> tuple[Path, Path]:
    """Get metadata and points cache paths."""
    c_dir = Path(cache_dir)
    c_dir.mkdir(parents=True, exist_ok=True)
    return c_dir / "activities_meta.json", c_dir / "activities_points.json.gz"


def load_cached_ingest(cache_dir: str | Path) -> IngestResult | None:
    """Load previously cached ingest data if available."""
    meta_path, points_path = get_cache_paths(cache_dir)
    if not (meta_path.exists() and points_path.exists()):
        return None

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        with gzip.open(points_path, "rt", encoding="utf-8") as f:
            raw_activities = json.load(f)

        activities: dict[str, list[GPSPoint]] = {
            act_id: [GPSPoint.from_dict(p) for p in pts]
            for act_id, pts in raw_activities.items()
        }

        summaries = [
            ActivitySummary(**s) for s in meta.get("summaries", [])
        ]

        return IngestResult(
            total_files=meta["total_files"],
            parsed_activities=meta["parsed_activities"],
            skipped_no_gps=meta["skipped_no_gps"],
            total_points=meta["total_points"],
            earliest_date=meta.get("earliest_date"),
            latest_date=meta.get("latest_date"),
            activities=activities,
            summaries=summaries,
        )
    except Exception as e:
        logger.warning(f"Failed to read activity cache: {e}. A fresh ingest will run.")
        return None


def save_cached_ingest(cache_dir: str | Path, result: IngestResult) -> None:
    """Save ingest results to cache directory."""
    meta_path, points_path = get_cache_paths(cache_dir)

    meta = {
        "total_files": result.total_files,
        "parsed_activities": result.parsed_activities,
        "skipped_no_gps": result.skipped_no_gps,
        "total_points": result.total_points,
        "earliest_date": result.earliest_date,
        "latest_date": result.latest_date,
        "summaries": [asdict(s) for s in result.summaries],
    }

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    raw_points = {
        act_id: [p.to_dict() for p in pts]
        for act_id, pts in result.activities.items()
    }

    with gzip.open(points_path, "wt", encoding="utf-8") as f:
        json.dump(raw_points, f)


def ingest_activities(
    data_dir: str | Path = "data",
    cache_dir: str | Path = "cache",
    force_rebuild: bool = False,
) -> IngestResult:
    """
    Discover and ingest all GPX and FIT files from data_dir.
    Caches parsed results into cache_dir for instant subsequent runs.
    """
    d_dir = Path(data_dir)
    c_dir = Path(cache_dir)

    if not force_rebuild:
        cached = load_cached_ingest(c_dir)
        # Only use cache if it actually found files; a cache from an
        # empty-data-dir run would have total_files=0 and hide new files.
        if cached is not None and cached.total_files > 0:
            return cached

    d_dir.mkdir(parents=True, exist_ok=True)
    c_dir.mkdir(parents=True, exist_ok=True)

    # Match files: .gpx, .fit, .gpx.gz, .fit.gz
    patterns = ["*.gpx", "*.fit", "*.gpx.gz", "*.fit.gz"]
    files: list[Path] = []
    for pat in patterns:
        files.extend(d_dir.glob(pat))
        files.extend(d_dir.glob(pat.upper()))

    files = sorted(set(files))

    activities: dict[str, list[GPSPoint]] = {}
    summaries: list[ActivitySummary] = []
    skipped_no_gps = 0
    all_timestamps: list[str] = []

    for file_path in files:
        try:
            act_id, points, summary = parse_activity_file(file_path)
            summaries.append(summary)

            if not summary.has_gps:
                skipped_no_gps += 1
                logger.info(f"Skipping indoor/no-GPS activity: {file_path.name}")
                continue

            activities[act_id] = points
            for pt in points:
                if pt.timestamp:
                    all_timestamps.append(pt.timestamp)

        except Exception as e:
            logger.warning(f"Error parsing {file_path.name}: {e}")

    total_points = sum(len(pts) for pts in activities.values())
    all_timestamps.sort()

    earliest_date = all_timestamps[0] if all_timestamps else None
    latest_date = all_timestamps[-1] if all_timestamps else None

    result = IngestResult(
        total_files=len(files),
        parsed_activities=len(activities),
        skipped_no_gps=skipped_no_gps,
        total_points=total_points,
        earliest_date=earliest_date,
        latest_date=latest_date,
        activities=activities,
        summaries=summaries,
    )

    save_cached_ingest(c_dir, result)
    return result
