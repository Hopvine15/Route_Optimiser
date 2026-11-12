"""Command-line interface for hexroute."""

import os
from pathlib import Path
from typing import Optional
import typer

from hexroute.config import load_config
from hexroute.coverage import compute_coverage, generate_coverage_map
from hexroute.export import export_route_gpx, generate_route_map
from hexroute.fetcher import sync_competition_targets
from hexroute.graph import get_walking_graph
from hexroute.grid import generate_grid_check_map, get_disk, latlng_to_cell
from hexroute.ingest import ingest_activities
from hexroute.optimise import plan_optimal_route
from hexroute.targets import (
    HexStatus,
    expand_unclaimed_targets,
    generate_targets_map,
    load_targets_csv,
    save_targets_csv,
    score_targets,
)

app = typer.Typer(
    name="hexroute",
    help="Hex Route Optimiser for Strava Territory Competitions.",
    no_args_is_help=True,
)

grid_app = typer.Typer(
    name="grid",
    help="Hex grid verification and inspection commands.",
    no_args_is_help=True,
)
app.add_typer(grid_app, name="grid")

targets_app = typer.Typer(
    name="targets",
    help="Target territory scoring and visualization commands.",
    no_args_is_help=True,
)
app.add_typer(targets_app, name="targets")


@grid_app.command(name="check")
def grid_check(
    lat: Optional[float] = typer.Option(
        None,
        "--lat",
        help="Latitude of check point (defaults to home_lat in config.toml)",
    ),
    lng: Optional[float] = typer.Option(
        None,
        "--lng",
        help="Longitude of check point (defaults to home_lng in config.toml)",
    ),
    res: Optional[int] = typer.Option(
        None,
        "--res",
        help="H3 resolution (defaults to h3_resolution in config.toml, usually 8)",
    ),
    ring: int = typer.Option(
        1,
        "--ring",
        help="Neighbor ring depth to render around center cell (default: 1)",
    ),
    out: Optional[str] = typer.Option(
        None,
        "--out",
        help="Output HTML map path (defaults to out/grid_check.html)",
    ),
    config: Optional[str] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to custom config.toml file",
    ),
) -> None:
    """
    Generate an HTML map of the H3 cell at (lat, lng) and its neighbors.
    Use this to visually verify that H3 resolution matches the competition site grid.
    """
    cfg = load_config(config)

    target_lat = lat if lat is not None else float(cfg["general"]["home_lat"])
    target_lng = lng if lng is not None else float(cfg["general"]["home_lng"])
    target_res = res if res is not None else int(cfg["general"]["h3_resolution"])

    out_dir = Path(cfg["paths"]["out_dir"])
    target_out = Path(out) if out is not None else out_dir / "grid_check.html"

    cell = latlng_to_cell(target_lat, target_lng, target_res)
    typer.echo(f"Checking H3 cell for lat={target_lat}, lng={target_lng} (res={target_res})...")
    typer.echo(f"Center H3 Index: {cell}")

    output_file = generate_grid_check_map(
        lat=target_lat,
        lng=target_lng,
        res=target_res,
        ring=ring,
        out_path=target_out,
    )

    typer.secho(f"✓ Grid check map generated at: {output_file}", fg=typer.colors.GREEN, bold=True)
    typer.echo("Open this file in your browser to verify cell boundaries against the competition site.")


@app.command(name="ingest")
def ingest(
    data_dir: Optional[str] = typer.Option(
        None,
        "--data-dir",
        "-d",
        help="Directory containing GPX/FIT activity files (defaults to config data_dir)",
    ),
    cache_dir: Optional[str] = typer.Option(
        None,
        "--cache-dir",
        help="Directory to store parsed activity cache (defaults to config cache_dir)",
    ),
    rebuild: bool = typer.Option(
        False,
        "--rebuild",
        "-r",
        help="Force re-parsing of all activity files instead of using cache",
    ),
    config: Optional[str] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to custom config.toml file",
    ),
) -> None:
    """
    Ingest all GPX and FIT files from the data directory into cached GPS point streams.
    """
    cfg = load_config(config)
    d_dir = data_dir or cfg["paths"]["data_dir"]
    c_dir = cache_dir or cfg["paths"]["cache_dir"]

    typer.echo(f"Scanning '{d_dir}' for GPX/FIT activity files...")
    result = ingest_activities(data_dir=d_dir, cache_dir=c_dir, force_rebuild=rebuild)

    typer.secho("=== Ingestion Summary ===", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"Total files found:      {result.total_files}")
    typer.echo(f"GPS activities parsed:  {result.parsed_activities}")
    if result.skipped_no_gps > 0:
        typer.secho(f"Indoor/No-GPS skipped:  {result.skipped_no_gps}", fg=typer.colors.YELLOW)
    typer.echo(f"Total GPS points:       {result.total_points:,}")

    if result.earliest_date and result.latest_date:
        typer.echo(f"Date range:             {result.earliest_date[:10]} to {result.latest_date[:10]}")
    else:
        typer.echo("Date range:             N/A (no timestamps found)")

    if result.total_files == 0:
        typer.secho(
            f"\nTip: Place your Strava bulk export (.gpx, .fit, .gpx.gz, .fit.gz) in '{d_dir}/'",
            fg=typer.colors.MAGENTA,
        )
    else:
        typer.secho(f"✓ Cached data ready in '{c_dir}/'", fg=typer.colors.GREEN, bold=True)


@app.command(name="coverage")
def coverage(
    data_dir: Optional[str] = typer.Option(
        None,
        "--data-dir",
        "-d",
        help="Directory containing GPX/FIT activity files",
    ),
    cache_dir: Optional[str] = typer.Option(
        None,
        "--cache-dir",
        help="Directory to store/load parsed activity cache",
    ),
    res: Optional[int] = typer.Option(
        None,
        "--res",
        help="H3 resolution (defaults to h3_resolution in config.toml)",
    ),
    out: Optional[str] = typer.Option(
        None,
        "--out",
        "-o",
        help="Output HTML coverage map path (defaults to out/coverage.html)",
    ),
    include_tracks: bool = typer.Option(
        True,
        "--tracks/--no-tracks",
        help="Include toggleable raw GPS tracks layer on the map",
    ),
    config: Optional[str] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to custom config.toml file",
    ),
) -> None:
    """
    Calculate and render H3 hexagon territory coverage map from ingested activities.
    """
    cfg = load_config(config)
    d_dir = data_dir or cfg["paths"]["data_dir"]
    c_dir = cache_dir or cfg["paths"]["cache_dir"]
    target_res = res if res is not None else int(cfg["general"]["h3_resolution"])

    out_dir = Path(cfg["paths"]["out_dir"])
    target_out = Path(out) if out is not None else out_dir / "coverage.html"

    home_lat = float(cfg["general"]["home_lat"]) if "home_lat" in cfg["general"] else None
    home_lng = float(cfg["general"]["home_lng"]) if "home_lng" in cfg["general"] else None

    typer.echo("Loading activity streams...")
    ingest_result = ingest_activities(data_dir=d_dir, cache_dir=c_dir)

    if not ingest_result.activities:
        typer.secho("No GPS activities found. Please add GPX/FIT files to data/ first.", fg=typer.colors.YELLOW)
        return

    typer.echo(f"Computing H3 cell coverage at resolution {target_res}...")
    coverage_data = compute_coverage(ingest_result.activities, res=target_res)

    typer.secho(f"✓ Total unique hexes covered: {len(coverage_data):,}", fg=typer.colors.CYAN, bold=True)

    tracks = ingest_result.activities if include_tracks else None
    output_path = generate_coverage_map(
        coverage=coverage_data,
        activities=tracks,
        out_path=target_out,
        home_lat=home_lat,
        home_lng=home_lng,
    )

    typer.secho(f"✓ Coverage map generated at: {output_path}", fg=typer.colors.GREEN, bold=True)
    typer.echo("Open this file in your browser to inspect visited territory.")


@targets_app.command(name="show")
def targets_show(
    file: Optional[str] = typer.Option(
        "data/targets.csv",
        "--file",
        "-f",
        help="Path to targets CSV file (h3_index,status)",
    ),
    out: Optional[str] = typer.Option(
        None,
        "--out",
        "-o",
        help="Output HTML targets map path (defaults to out/targets.html)",
    ),
    data_dir: Optional[str] = typer.Option(
        None,
        "--data-dir",
        "-d",
        help="Directory containing past activities to deduce our territory",
    ),
    cache_dir: Optional[str] = typer.Option(
        None,
        "--cache-dir",
        help="Cache directory for activities",
    ),
    config: Optional[str] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to custom config.toml file",
    ),
) -> None:
    """
    Score target hexes and render an interactive targets map.
    """
    cfg = load_config(config)
    csv_path = Path(file or "data/targets.csv")
    out_dir = Path(cfg["paths"]["out_dir"])
    target_out = Path(out) if out is not None else out_dir / "targets.html"

    home_lat = float(cfg["general"]["home_lat"]) if "home_lat" in cfg["general"] else None
    home_lng = float(cfg["general"]["home_lng"]) if "home_lng" in cfg["general"] else None

    # Load targets
    targets = load_targets_csv(csv_path)
    if not targets:
        typer.secho(f"Warning: No targets found in '{csv_path}'.", fg=typer.colors.YELLOW)
        typer.echo("Use 'hexroute targets init' to generate a starter targets CSV.")

    # Deduce our territory from past activities if available
    d_dir = data_dir or cfg["paths"]["data_dir"]
    c_dir = cache_dir or cfg["paths"]["cache_dir"]
    ingest_result = ingest_activities(data_dir=d_dir, cache_dir=c_dir)
    res = int(cfg["general"]["h3_resolution"])
    coverage_data = compute_coverage(ingest_result.activities, res=res) if ingest_result.activities else {}
    our_territory = set(coverage_data.keys())

    # Score targets
    scoring_cfg = cfg.get("scoring", {})
    scored_hexes = score_targets(targets, our_territory=our_territory, scoring_config=scoring_cfg)

    typer.echo(f"Scored {len(scored_hexes)} target hexes.")
    enemy_count = sum(1 for s in scored_hexes.values() if s.status == HexStatus.ENEMY)
    unclaimed_count = sum(1 for s in scored_hexes.values() if s.status == HexStatus.UNCLAIMED)
    ours_count = sum(1 for s in scored_hexes.values() if s.status == HexStatus.OURS)
    typer.echo(f"  - Enemy hexes:     {enemy_count}")
    typer.echo(f"  - Unclaimed hexes: {unclaimed_count}")
    typer.echo(f"  - Our territory:   {ours_count if ours_count else len(our_territory)}")

    out_map = generate_targets_map(
        scored_hexes=scored_hexes,
        our_territory=our_territory,
        out_path=target_out,
        home_lat=home_lat,
        home_lng=home_lng,
    )
    typer.secho(f"✓ Targets map generated at: {out_map}", fg=typer.colors.GREEN, bold=True)


@targets_app.command(name="init")
def targets_init(
    file: Optional[str] = typer.Option(
        "data/targets.csv",
        "--file",
        "-f",
        help="Target CSV file to create",
    ),
    lat: Optional[float] = typer.Option(
        None,
        "--lat",
        help="Center latitude (defaults to home_lat in config)",
    ),
    lng: Optional[float] = typer.Option(
        None,
        "--lng",
        help="Center longitude (defaults to home_lng in config)",
    ),
    ring: int = typer.Option(
        2,
        "--ring",
        "-r",
        help="Neighbor ring radius (default: 2 rings = 19 hexes)",
    ),
    res: Optional[int] = typer.Option(
        None,
        "--res",
        help="H3 resolution (default from config)",
    ),
    config: Optional[str] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to custom config.toml file",
    ),
) -> None:
    """
    Initialize a starter targets CSV file around a location.
    """
    cfg = load_config(config)
    target_lat = lat if lat is not None else float(cfg["general"]["home_lat"])
    target_lng = lng if lng is not None else float(cfg["general"]["home_lng"])
    target_res = res if res is not None else int(cfg["general"]["h3_resolution"])
    csv_path = Path(file or "data/targets.csv")

    center_cell = latlng_to_cell(target_lat, target_lng, target_res)
    cells = get_disk(center_cell, ring)

    # Initialize center as ours, outer ring as enemy/unclaimed
    targets: dict[str, str] = {}
    for i, c in enumerate(sorted(cells)):
        if c == center_cell:
            targets[c] = HexStatus.OURS
        elif i % 2 == 0:
            targets[c] = HexStatus.ENEMY
        else:
            targets[c] = HexStatus.UNCLAIMED

    save_targets_csv(targets, csv_path)
    typer.secho(f"✓ Initialized {len(targets)} targets in '{csv_path}'", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"Center cell: {center_cell}")


@targets_app.command(name="fetch")
@app.command(name="fetch")
def targets_fetch(
    url: Optional[str] = typer.Option(
        "https://api.strods.uk/clubs/2345/land-grab",
        "--url",
        "-u",
        help="Competition territory API endpoint URL",
    ),
    file: Optional[str] = typer.Option(
        None,
        "--file",
        "-f",
        help="Path to local competition JSON file instead of live network request",
    ),
    team: str = typer.Option(
        "Agile & Fragile",
        "--team",
        "-t",
        help="Your team name (to identify 'ours' vs 'enemy')",
    ),
    cookie: Optional[str] = typer.Option(
        None,
        "--cookie",
        help="Optional session cookie string or token (defaults to COMPETITION_SESSION_COOKIE in .env)",
    ),
    out_file: str = typer.Option(
        "data/targets.csv",
        "--out",
        "-o",
        help="Path to output targets CSV",
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        "-r",
        help="Force fresh fetch bypassing 5-minute cache",
    ),
    config: Optional[str] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to custom config.toml file",
    ),
) -> None:
    """
    Fetch live hex territory ownership from the competition endpoint (or local JSON) and update targets.csv.
    """
    cfg = load_config(config)
    res = int(cfg["general"]["h3_resolution"])
    session_cookie = cookie or os.environ.get("COMPETITION_SESSION_COOKIE")

    source_desc = f"file '{file}'" if file else f"endpoint '{url}'"
    typer.echo(f"Syncing live territory data from {source_desc} for team '{team}'...")
    try:
        ownership, saved_csv = sync_competition_targets(
            endpoint_url=url if not file else None,
            file_path=file,
            our_team_name=team,
            session_cookie=session_cookie,
            out_csv_path=out_file,
            res=res,
            force_refresh=refresh,
        )

        ours = sum(1 for o in ownership.values() if o.status == HexStatus.OURS)
        enemy = sum(1 for o in ownership.values() if o.status == HexStatus.ENEMY)
        unclaimed = sum(1 for o in ownership.values() if o.status == HexStatus.UNCLAIMED)

        typer.secho(f"✓ Synced {len(ownership)} hexes to '{saved_csv}'", fg=typer.colors.GREEN, bold=True)
        typer.echo(f"  - Our Hexes:   {ours}")
        typer.echo(f"  - Enemy Hexes: {enemy}")
        typer.echo(f"  - Unclaimed:   {unclaimed}")
        typer.echo("Run 'hexroute targets show' or 'hexroute plan' to see the updated scores.")
    except Exception as e:
        typer.secho(f"Sync Error: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command(name="plan")
def plan(
    km: Optional[float] = typer.Option(
        None,
        "--km",
        "-k",
        help="Distance budget in kilometres (defaults to default_budget_km in config.toml)",
    ),
    lat: Optional[float] = typer.Option(
        None,
        "--lat",
        help="Start latitude (defaults to home_lat in config.toml)",
    ),
    lng: Optional[float] = typer.Option(
        None,
        "--lng",
        help="Start longitude (defaults to home_lng in config.toml)",
    ),
    loop: bool = typer.Option(
        True,
        "--loop/--one-way",
        help="Whether route is a loop starting & ending at start location",
    ),
    mode: str = typer.Option(
        "walk",
        "--mode",
        "-m",
        help="Exercise mode: 'walk' (run/walk) or 'bike' (cycling)",
    ),
    targets_file: Optional[str] = typer.Option(
        "data/targets.csv",
        "--targets",
        "-t",
        help="Path to targets CSV file",
    ),
    out_gpx: Optional[str] = typer.Option(
        None,
        "--out-gpx",
        help="Path for exported GPX file (defaults to out/route.gpx)",
    ),
    out_map: Optional[str] = typer.Option(
        None,
        "--out-map",
        help="Path for route HTML map (defaults to out/route.html)",
    ),
    config: Optional[str] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to custom config.toml file",
    ),
) -> None:
    """
    Plan an optimal running route on real OpenStreetMap paths within a distance budget.
    """
    cfg = load_config(config)

    budget_km = km if km is not None else float(cfg["general"]["default_budget_km"])
    start_lat = lat if lat is not None else float(cfg["general"]["home_lat"])
    start_lng = lng if lng is not None else float(cfg["general"]["home_lng"])
    res = int(cfg["general"]["h3_resolution"])

    out_dir = Path(cfg["paths"]["out_dir"])
    cache_dir = Path(cfg["paths"]["cache_dir"])
    gpx_path = Path(out_gpx) if out_gpx else out_dir / "route.gpx"
    map_path = Path(out_map) if out_map else out_dir / "route.html"

    typer.secho("=== Hexroute Orienteering Plan ===", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"Start Location:  {start_lat:.4f}, {start_lng:.4f}")
    typer.echo(f"Distance Budget: {budget_km:.1f} km ({'Loop' if loop else 'One-way'})")

    # Load targets
    targets_path = Path(targets_file or "data/targets.csv")
    targets = load_targets_csv(targets_path)
    if not targets:
        typer.secho(f"No targets found in '{targets_path}'. Auto-initializing local target ring...", fg=typer.colors.YELLOW)
        center_cell = latlng_to_cell(start_lat, start_lng, res)
        cells = get_disk(center_cell, 2)
        targets = {c: HexStatus.ENEMY if i % 2 == 0 else HexStatus.UNCLAIMED for i, c in enumerate(sorted(cells))}
        targets[center_cell] = HexStatus.OURS
        save_targets_csv(targets, targets_path)

    # Auto-expand reachable territory with unclaimed cells around start location
    search_radius_km = max(budget_km / 2.0, 3.0)
    expanded_targets = expand_unclaimed_targets(
        targets,
        center_lat=start_lat,
        center_lng=start_lng,
        radius_km=search_radius_km,
        res=res,
    )

    scored_targets = score_targets(expanded_targets, scoring_config=cfg.get("scoring"))

    # Download or load OSM network
    # Cap graph radius at 10 km — the optimiser only considers the top
    # MAX_TARGET_CELLS nearest cells, so a smaller graph keeps download
    # and Dijkstra times manageable while still covering the local area.
    graph_radius_m = max(min(budget_km * 1000.0 / 2.0, 10000.0), 2500.0)
    network_type = "bike" if mode.lower() in ("bike", "cycling", "cycle") else "walk"
    typer.echo(f"Loading OpenStreetMap {network_type} network...")
    graph = get_walking_graph(
        lat=start_lat,
        lng=start_lng,
        dist_metres=graph_radius_m,
        cache_dir=cache_dir,
        network_type=network_type,
    )

    typer.echo("Searching optimal route...")
    plan_result = plan_optimal_route(
        graph=graph,
        start_lat=start_lat,
        start_lng=start_lng,
        budget_km=budget_km,
        scored_targets=scored_targets,
        is_loop=loop,
        res=res,
    )

    typer.secho("\n=== Route Summary ===", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"Total Distance:       {plan_result.total_distance_m / 1000.0:.2f} km (Budget: {budget_km:.1f} km)")
    typer.echo(f"New Hexes Claimed:    {len(plan_result.newly_claimed_hexes)} (+{plan_result.total_score:.1f} pts)")
    typer.echo(f"Our Territory Passed: {len(plan_result.existing_our_hexes)} (0.0 pts)")
    typer.echo(f"Total Score Gained:   {plan_result.total_score:.1f} pts")

    # Export GPX & Map
    saved_gpx = export_route_gpx(graph, plan_result.path_nodes, out_path=gpx_path)
    saved_map = generate_route_map(
        graph=graph,
        path_nodes=plan_result.path_nodes,
        claimed_hexes=plan_result.claimed_hexes,
        scored_targets=scored_targets,
        total_distance_m=plan_result.total_distance_m,
        total_score=plan_result.total_score,
        out_path=map_path,
        start_lat=start_lat,
        start_lng=start_lng,
        newly_claimed_hexes=plan_result.newly_claimed_hexes,
        existing_our_hexes=plan_result.existing_our_hexes,
    )

    typer.secho(f"\n✓ GPX route exported to: {saved_gpx}", fg=typer.colors.GREEN, bold=True)
    typer.secho(f"✓ Interactive map saved: {saved_map}", fg=typer.colors.GREEN, bold=True)


@app.command(name="info")
def info(
    config: Optional[str] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to custom config.toml file",
    ),
) -> None:
    """Display current configuration and workspace status."""
    cfg = load_config(config)
    typer.echo("=== Hexroute Configuration ===")
    typer.echo(f"H3 Resolution:     {cfg['general']['h3_resolution']}")
    typer.echo(f"Home Coordinates:  {cfg['general']['home_lat']}, {cfg['general']['home_lng']}")
    typer.echo(f"Default Budget:    {cfg['general']['default_budget_km']} km")
    typer.echo(f"Data Directory:    {cfg['paths']['data_dir']}")
    typer.echo(f"Output Directory:  {cfg['paths']['out_dir']}")
    typer.echo(f"Cache Directory:   {cfg['paths']['cache_dir']}")


@app.command(name="serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host interface to bind (defaults to 127.0.0.1)"),
    port: int = typer.Option(8080, "--port", "-p", help="Port to listen on (default: 8080)"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not automatically open browser"),
) -> None:
    """
    Launch the interactive Hexroute Web UI in your browser for point-and-click route planning.
    """
    from hexroute.server import run_server
    run_server(host=host, port=port, open_browser=not no_browser)


@app.command(name="ui")
def ui(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host interface to bind (defaults to 127.0.0.1)"),
    port: int = typer.Option(8080, "--port", "-p", help="Port to listen on (default: 8080)"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not automatically open browser"),
) -> None:
    """
    Alias for 'hexroute serve' - Launch the interactive Web UI.
    """
    from hexroute.server import run_server
    run_server(host=host, port=port, open_browser=not no_browser)


if __name__ == "__main__":
    app()
