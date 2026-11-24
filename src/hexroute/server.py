"""Interactive Web UI and local API server for Hexroute Optimiser."""

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
import urllib.parse
import webbrowser

from hexroute.config import load_config
from hexroute.coverage import compute_coverage
from hexroute.export import export_route_gpx, get_path_coordinates
from hexroute.fetcher import sync_competition_targets
from hexroute.graph import get_walking_graph
from hexroute.grid import cell_to_boundary, cell_to_latlng, get_disk, latlng_to_cell
from hexroute.ingest import ingest_activities
from hexroute.optimise import plan_optimal_route
from hexroute.targets import (
    HexStatus,
    expand_unclaimed_targets,
    load_targets_csv,
    save_targets_csv,
    score_targets,
)

logger = logging.getLogger("hexroute.server")

# In-memory cache for latest planned route GPX
_LATEST_GPX_CONTENT: str = ""
_LATEST_GPX_FILENAME: str = "route.gpx"


def get_ui_html() -> str:
    """Return the complete standalone HTML/CSS/JS single-page application."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Hexroute Optimiser - Team Territory Route Planner</title>
    
    <!-- Leaflet & FontAwesome -->
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

    <style>
        :root {
            --primary: #4361ee;
            --primary-hover: #3a56d4;
            --success: #2a9d8f;
            --accent: #e76f51;
            --team-blue: #4a6d8c;
            --bg-dark: #12161f;
            --panel-bg: rgba(255, 255, 255, 0.94);
            --panel-border: rgba(220, 226, 235, 0.9);
            --text-main: #1f2328;
            --text-muted: #57606a;
            --shadow: 0 8px 32px rgba(0, 0, 0, 0.15);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }

        body, html {
            width: 100%;
            height: 100%;
            overflow: hidden;
            color: var(--text-main);
        }

        #app-container {
            position: relative;
            width: 100%;
            height: 100%;
        }

        #map {
            width: 100%;
            height: 100%;
            z-index: 1;
        }

        /* Floating Sidebar Controls */
        #sidebar {
            position: absolute;
            top: 20px;
            left: 20px;
            width: 380px;
            max-height: calc(100vh - 40px);
            background: var(--panel-bg);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border-radius: 16px;
            border: 1px solid var(--panel-border);
            box-shadow: var(--shadow);
            z-index: 1000;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            transition: all 0.3s ease;
        }

        .sidebar-header {
            padding: 16px 20px;
            background: linear-gradient(135deg, #1e293b, #0f172a);
            color: white;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .sidebar-header h1 {
            font-size: 17px;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 8px;
            letter-spacing: -0.3px;
        }

        .team-badge {
            background: rgba(138, 164, 194, 0.25);
            color: #b8d4f0;
            font-size: 11px;
            padding: 3px 8px;
            border-radius: 12px;
            font-weight: 600;
            border: 1px solid rgba(138, 164, 194, 0.4);
        }

        .sidebar-body {
            padding: 18px 20px;
            overflow-y: auto;
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }

        .form-section {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .section-label {
            font-size: 12px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.6px;
            color: var(--text-muted);
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        /* Mode Selector (Walk vs Bike) */
        .segmented-control {
            display: grid;
            grid-template-columns: 1fr 1fr;
            background: #e2e8f0;
            border-radius: 10px;
            padding: 3px;
            gap: 4px;
        }

        .segmented-control input[type="radio"] {
            display: none;
        }

        .segmented-control label {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            padding: 8px 10px;
            font-size: 13px;
            font-weight: 600;
            color: var(--text-muted);
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s ease;
            user-select: none;
        }

        .segmented-control input[type="radio"]:checked + label {
            background: white;
            color: var(--primary);
            box-shadow: 0 2px 6px rgba(0, 0, 0, 0.1);
        }

        /* Coordinates inputs */
        .coord-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
        }

        .input-group {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }

        .input-group label {
            font-size: 11px;
            font-weight: 600;
            color: var(--text-muted);
        }

        .input-group input {
            width: 100%;
            padding: 8px 10px;
            font-size: 13px;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            background: white;
            color: var(--text-main);
            outline: none;
            transition: border-color 0.2s;
        }

        .input-group input:focus {
            border-color: var(--primary);
        }

        .hint-text {
            font-size: 11px;
            color: #64748b;
            display: flex;
            align-items: center;
            gap: 5px;
            background: #f1f5f9;
            padding: 6px 10px;
            border-radius: 6px;
        }

        .hint-btn {
            background: none;
            border: none;
            color: var(--primary);
            font-weight: 600;
            font-size: 11px;
            cursor: pointer;
            padding: 0;
            margin-left: auto;
            text-decoration: underline;
        }

        /* Distance Input & Slider */
        .slider-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .manual-budget-container {
            display: flex;
            align-items: center;
            gap: 4px;
            background: white;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            padding: 2px 8px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
            transition: border-color 0.2s, box-shadow 0.2s;
        }

        .manual-budget-container:focus-within {
            border-color: var(--primary);
            box-shadow: 0 0 0 2px rgba(67, 97, 238, 0.2);
        }

        .manual-budget-container input[type="number"] {
            width: 70px;
            border: none;
            outline: none;
            font-size: 14px;
            font-weight: 700;
            color: var(--primary);
            text-align: right;
            background: transparent;
        }

        .manual-budget-container .unit-label {
            font-size: 12px;
            font-weight: 700;
            color: var(--text-muted);
        }

        input[type="range"] {
            width: 100%;
            height: 6px;
            background: #cbd5e1;
            border-radius: 4px;
            outline: none;
            accent-color: var(--primary);
            cursor: pointer;
        }

        .preset-pills {
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
        }

        .pill-btn {
            background: #f1f5f9;
            border: 1px solid #e2e8f0;
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 600;
            color: #475569;
            cursor: pointer;
            transition: all 0.15s ease;
        }

        .pill-btn:hover {
            background: #e2e8f0;
            color: var(--text-main);
        }

        .pill-btn.active {
            background: var(--primary);
            color: white;
            border-color: var(--primary);
        }

        /* Route Option (Loop toggle) */
        .toggle-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-size: 13px;
            font-weight: 600;
            padding: 4px 0;
        }

        .switch {
            position: relative;
            display: inline-block;
            width: 42px;
            height: 22px;
        }

        .switch input {
            opacity: 0;
            width: 0;
            height: 0;
        }

        .slider-round {
            position: absolute;
            cursor: pointer;
            top: 0; left: 0; right: 0; bottom: 0;
            background-color: #cbd5e1;
            transition: .3s;
            border-radius: 22px;
        }

        .slider-round:before {
            position: absolute;
            content: "";
            height: 16px;
            width: 16px;
            left: 3px;
            bottom: 3px;
            background-color: white;
            transition: .3s;
            border-radius: 50%;
        }

        input:checked + .slider-round {
            background-color: var(--primary);
        }

        input:checked + .slider-round:before {
            transform: translateX(20px);
        }

        /* Buttons */
        .btn-primary {
            width: 100%;
            padding: 12px 16px;
            background: var(--primary);
            color: white;
            border: none;
            border-radius: 10px;
            font-size: 14px;
            font-weight: 700;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            box-shadow: 0 4px 12px rgba(67, 97, 238, 0.35);
            transition: all 0.2s ease;
        }

        .btn-primary:hover {
            background: var(--primary-hover);
            transform: translateY(-1px);
        }

        .btn-primary:active {
            transform: translateY(0);
        }

        .btn-primary:disabled {
            background: #94a3b8;
            box-shadow: none;
            cursor: not-allowed;
            transform: none;
        }

        .btn-secondary {
            width: 100%;
            padding: 9px 14px;
            background: #f8fafc;
            color: #334155;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            transition: background 0.2s;
        }

        .btn-secondary:hover {
            background: #f1f5f9;
        }

        /* Results Card */
        #results-panel {
            display: none;
            background: white;
            border-radius: 12px;
            border: 1px solid #e2e8f0;
            padding: 14px;
            flex-direction: column;
            gap: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.06);
            border-left: 4px solid var(--success);
        }

        .results-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 13px;
            font-weight: 700;
            color: #0f172a;
        }

        .stats-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
        }

        .stat-card {
            background: #f8fafc;
            padding: 8px 10px;
            border-radius: 8px;
            display: flex;
            flex-direction: column;
            gap: 2px;
        }

        .stat-card .label {
            font-size: 11px;
            color: #64748b;
            font-weight: 600;
        }

        .stat-card .value {
            font-size: 16px;
            font-weight: 700;
            color: var(--text-main);
        }

        .stat-card .value.highlight {
            color: var(--success);
        }

        .gpx-download-btn {
            background: var(--success);
            color: white;
            text-decoration: none;
            padding: 10px 14px;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 700;
            text-align: center;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            box-shadow: 0 2px 8px rgba(42, 157, 143, 0.3);
            transition: opacity 0.2s;
        }

        .gpx-download-btn:hover {
            opacity: 0.92;
        }

        /* Status Banner */
        #status-banner {
            display: none;
            padding: 10px 14px;
            border-radius: 8px;
            font-size: 12px;
            font-weight: 600;
            align-items: center;
            gap: 8px;
        }

        #status-banner.info {
            display: flex;
            background: #eff6ff;
            color: #1d4ed8;
            border: 1px solid #bfdbfe;
        }

        #status-banner.error {
            display: flex;
            background: #fef2f2;
            color: #b91c1c;
            border: 1px solid #fecaca;
        }

        .spinner {
            border: 2px solid rgba(0, 0, 0, 0.1);
            width: 14px;
            height: 14px;
            border-radius: 50%;
            border-left-color: currentColor;
            animation: spin 0.8s linear infinite;
        }

        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }

        /* Map Legend */
        .map-legend {
            position: absolute;
            bottom: 25px;
            right: 25px;
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(8px);
            padding: 12px 16px;
            border-radius: 12px;
            border: 1px solid var(--panel-border);
            box-shadow: var(--shadow);
            z-index: 1000;
            font-size: 12px;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .legend-title {
            font-weight: 700;
            font-size: 12px;
            color: var(--text-main);
            margin-bottom: 2px;
        }

        .legend-item {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .legend-color {
            width: 14px;
            height: 14px;
            border-radius: 4px;
            border: 1.5px solid rgba(0, 0, 0, 0.2);
        }

        @media (max-width: 600px) {
            #sidebar {
                top: auto;
                bottom: 0;
                left: 0;
                width: 100%;
                max-height: 70vh;
                border-radius: 20px 20px 0 0;
            }
            .map-legend {
                display: none;
            }
        }
    </style>
</head>
<body>

<div id="app-container">
    <!-- Map Canvas -->
    <div id="map"></div>

    <!-- Floating UI Controls Panel -->
    <div id="sidebar">
        <div class="sidebar-header">
            <h1><i class="fa-solid fa-hexagon-nodes"></i> Hexroute</h1>
            <span class="team-badge" id="team-badge-text">Agile & Fragile</span>
        </div>

        <div class="sidebar-body">
            <!-- Status Alert Banner -->
            <div id="status-banner"></div>

            <!-- Mode Selector -->
            <div class="form-section">
                <span class="section-label">Exercise Mode</span>
                <div class="segmented-control">
                    <input type="radio" id="mode-walk" name="exercise-mode" value="walk" checked>
                    <label for="mode-walk"><i class="fa-solid fa-person-running"></i> Run / Walk</label>

                    <input type="radio" id="mode-bike" name="exercise-mode" value="bike">
                    <label for="mode-bike"><i class="fa-solid fa-person-biking"></i> Cycling</label>
                </div>
            </div>

            <!-- Start Location -->
            <div class="form-section">
                <div class="section-label">
                    <span>Start Location</span>
                    <button class="hint-btn" onclick="useMyLocation()"><i class="fa-solid fa-location-crosshairs"></i> GPS</button>
                </div>
                <div class="coord-grid">
                    <div class="input-group">
                        <label>Latitude</label>
                        <input type="number" id="start-lat" step="0.0001" value="51.5074" placeholder="51.5074">
                    </div>
                    <div class="input-group">
                        <label>Longitude</label>
                        <input type="number" id="start-lng" step="0.0001" value="-0.1278" placeholder="-0.1278">
                    </div>
                </div>
                <div class="hint-text">
                    <i class="fa-solid fa-hand-pointer"></i> <span>Click anywhere on the map to set start pin</span>
                </div>
            </div>

            <!-- Distance Budget -->
            <div class="form-section">
                <div class="slider-header">
                    <span class="section-label">Distance Budget</span>
                    <div class="manual-budget-container">
                        <input type="number" id="budget-input" min="0.5" max="300" step="0.5" value="8.0" oninput="updateBudgetFromInput(this.value)">
                        <span class="unit-label">km</span>
                    </div>
                </div>
                <input type="range" id="budget-slider" min="1.0" max="60.0" step="0.5" value="8.0" oninput="updateBudgetFromSlider(this.value)">
                
                <div class="preset-pills">
                    <button class="pill-btn" onclick="setBudget(5.0)">5k</button>
                    <button class="pill-btn active" onclick="setBudget(8.0)">8k</button>
                    <button class="pill-btn" onclick="setBudget(10.0)">10k</button>
                    <button class="pill-btn" onclick="setBudget(20.0)">20k</button>
                    <button class="pill-btn" onclick="setBudget(50.0)">50k</button>
                    <button class="pill-btn" onclick="setBudget(100.0)">100k</button>
                </div>
            </div>

            <!-- Route Type (Loop vs One-Way) -->
            <div class="form-section">
                <div class="toggle-row">
                    <span><i class="fa-solid fa-arrows-spin"></i> Circular Loop Route</span>
                    <label class="switch">
                        <input type="checkbox" id="loop-toggle" checked>
                        <span class="slider-round"></span>
                    </label>
                </div>
            </div>

            <!-- Plan Button -->
            <button class="btn-primary" id="plan-btn" onclick="planRoute()">
                <i class="fa-solid fa-route"></i> Plan Optimal Route
            </button>

            <!-- Results Summary Panel -->
            <div id="results-panel">
                <div class="results-header">
                    <span>✨ Optimization Result</span>
                    <span id="route-type-badge" style="font-size: 11px; color: #64748b;">Loop</span>
                </div>
                <div class="stats-grid">
                    <div class="stat-card">
                        <span class="label">Total Distance</span>
                        <span class="value" id="res-distance">-- km</span>
                    </div>
                    <div class="stat-card">
                        <span class="label">Score Gained</span>
                        <span class="value highlight" id="res-score">+0 pts</span>
                    </div>
                    <div class="stat-card">
                        <span class="label">New Hexes Claimed</span>
                        <span class="value" id="res-new-hexes">0</span>
                    </div>
                    <div class="stat-card">
                        <span class="label">Team Territory Visited</span>
                        <span class="value" id="res-our-hexes">0</span>
                    </div>
                </div>

                <a id="gpx-download-link" href="#" class="gpx-download-btn">
                    <i class="fa-solid fa-file-arrow-down"></i> Download GPX for GPS/Watch
                </a>
            </div>

            <!-- Live Competition Sync -->
            <button class="btn-secondary" onclick="syncLiveStrods()">
                <i class="fa-solid fa-arrows-rotate"></i> Sync Live Strods Territory
            </button>

            <!-- Legal Disclaimer & User Responsibility -->
            <div style="margin-top: 6px; padding: 10px 12px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; font-size: 11px; color: #64748b; line-height: 1.4;">
                <div style="font-weight: 700; color: #334155; margin-bottom: 3px; display: flex; align-items: center; gap: 5px;">
                    <i class="fa-solid fa-triangle-exclamation" style="color: #f59e0b;"></i> Legal Disclaimer
                </div>
                Automated route suggestion. Do not follow routes blindly. You are solely responsible for verifying public rights-of-way, safety, private property, and local laws. User assumes all risk.
            </div>
        </div>
    </div>

    <!-- Map Legend -->
    <div class="map-legend">
        <div class="legend-title">Territory Legend</div>
        <div class="legend-item">
            <div class="legend-color" style="background: #2a9d8f; border-color: #1d7873;"></div>
            <span>New Unclaimed Capture (+2 pts)</span>
        </div>
        <div class="legend-item">
            <div class="legend-color" style="background: #e76f51; border-color: #d90429;"></div>
            <span>Enemy Territory Flip (+3 pts)</span>
        </div>
        <div class="legend-item">
            <div class="legend-color" style="background: #8aa4c2; border-color: #4a6d8c;"></div>
            <span>Our Held Territory (Agile & Fragile)</span>
        </div>
        <div class="legend-item">
            <div class="legend-color" style="background: #4361ee; height: 4px; border-radius: 2px;"></div>
            <span>Optimized Route Path</span>
        </div>
        <div class="legend-item">
            <div class="legend-color" style="background: #3b82f6; opacity: 0.4;"></div>
            <span>Visited (from ingested activities)</span>
        </div>
        <div class="legend-item">
            <div class="legend-color" style="background: #1d4ed8; height: 4px; border-radius: 2px;"></div>
            <span>GPS Track</span>
        </div>
    </div>
</div>

<script>
    let map;
    let startMarker;
    let finishMarker;
    let routePolyline;
    let hexLayersGroup;

    // Default configuration (neutral London center fallback)
    let config = {
        home_lat: 51.5074,
        home_lng: -0.1278,
        default_budget_km: 8.0,
        team_name: "Agile & Fragile",
        team_colour: "#8aa4c2"
    };

    // Initialize Map on Page Load
    document.addEventListener("DOMContentLoaded", async () => {
        await loadAppConfig();
        initMap();
        loadBackgroundTargets();
        loadCoverage();
    });

    async function loadAppConfig() {
        try {
            const res = await fetch("/api/config");
            if (res.ok) {
                config = await res.json();
                document.getElementById("start-lat").value = config.home_lat.toFixed(4);
                document.getElementById("start-lng").value = config.home_lng.toFixed(4);
                document.getElementById("team-badge-text").innerText = config.team_name || "Agile & Fragile";
                setBudget(config.default_budget_km || 8.0);
            }
        } catch (e) {
            console.warn("Could not load backend config, using defaults:", e);
        }
    }

    function initMap() {
        const startLat = parseFloat(document.getElementById("start-lat").value) || config.home_lat;
        const startLng = parseFloat(document.getElementById("start-lng").value) || config.home_lng;

        map = L.map("map", {
            center: [startLat, startLng],
            zoom: 14,
            zoomControl: false
        });

        L.control.zoom({ position: "topright" }).addTo(map);

        L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
            maxZoom: 19,
            attribution: '© <a href="https://openstreetmap.org">OpenStreetMap</a> contributors'
        }).addTo(map);

        hexLayersGroup = L.featureGroup().addTo(map);

        // Custom Start Pin Icon
        const startIcon = L.divIcon({
            html: '<div style="background: #2a9d8f; color: white; width: 32px; height: 32px; border-radius: 50% 50% 50% 0; transform: rotate(-45deg); display: flex; align-items: center; justify-content: center; box-shadow: 0 3px 8px rgba(0,0,0,0.35); border: 2px solid white;"><i class="fa-solid fa-play" style="transform: rotate(45deg); font-size: 13px; margin-left: 2px;"></i></div>',
            className: 'custom-pin',
            iconSize: [32, 32],
            iconAnchor: [16, 32]
        });

        startMarker = L.marker([startLat, startLng], {
            draggable: true,
            icon: startIcon
        }).addTo(map);

        startMarker.bindTooltip("<b>Start Point</b><br>Drag to reposition", { permanent: false, direction: "top" });

        startMarker.on("dragend", (e) => {
            const pos = e.target.getLatLng();
            updateStartCoords(pos.lat, pos.lng);
        });

        // Click on map to place start marker
        map.on("click", (e) => {
            updateStartCoords(e.latlng.lat, e.latlng.lng);
            startMarker.setLatLng(e.latlng);
        });
    }

    function updateStartCoords(lat, lng) {
        document.getElementById("start-lat").value = lat.toFixed(5);
        document.getElementById("start-lng").value = lng.toFixed(5);
    }

    function useMyLocation() {
        if (!navigator.geolocation) {
            showStatus("Geolocation is not supported by your browser.", "error");
            return;
        }
        showStatus("Locating your GPS position...", "info");
        navigator.geolocation.getCurrentPosition(
            (pos) => {
                const lat = pos.coords.latitude;
                const lng = pos.coords.longitude;
                updateStartCoords(lat, lng);
                startMarker.setLatLng([lat, lng]);
                map.setView([lat, lng], 15);
                hideStatus();
            },
            (err) => {
                showStatus("Could not get GPS location: " + err.message, "error");
            },
            { enableHighAccuracy: true, timeout: 8000 }
        );
    }

    function updateBudgetFromSlider(val) {
        const num = parseFloat(val);
        if (!isNaN(num)) {
            document.getElementById("budget-input").value = num.toFixed(1);
            highlightPresetPill(num);
        }
    }

    function updateBudgetFromInput(val) {
        const num = parseFloat(val);
        if (!isNaN(num) && num > 0) {
            const slider = document.getElementById("budget-slider");
            if (num > parseFloat(slider.max)) {
                slider.max = Math.ceil(num * 1.25);
            }
            slider.value = num;
            highlightPresetPill(num);
        }
    }

    function setBudget(val) {
        const num = parseFloat(val);
        const slider = document.getElementById("budget-slider");
        if (num > parseFloat(slider.max)) {
            slider.max = Math.ceil(num * 1.25);
        }
        slider.value = num;
        document.getElementById("budget-input").value = num.toFixed(1);
        highlightPresetPill(num);
    }

    function highlightPresetPill(val) {
        document.querySelectorAll(".pill-btn").forEach(btn => {
            const pVal = parseFloat(btn.innerText.replace('k', ''));
            btn.classList.toggle("active", pVal === val);
        });
    }

    function showStatus(msg, type = "info") {
        const banner = document.getElementById("status-banner");
        banner.className = type;
        banner.innerHTML = type === "info" 
            ? `<div class="spinner"></div> <span>${msg}</span>`
            : `<i class="fa-solid fa-triangle-exclamation"></i> <span>${msg}</span>`;
        banner.style.display = "flex";
    }

    function hideStatus() {
        document.getElementById("status-banner").style.display = "none";
    }

    async function loadBackgroundTargets() {
        try {
            const res = await fetch("/api/targets");
            if (res.ok) {
                const data = await res.json();
                renderTargetHexes(data.targets || []);
            }
        } catch (e) {
            console.log("No initial targets to display:", e);
        }
    }

    function renderTargetHexes(targets) {
        if (!targets || !targets.length) return;
        if (!hexLayersGroup) {
            hexLayersGroup = L.featureGroup().addTo(map);
        }
        targets.forEach(t => {
            const isOurs = t.status === "ours";
            const isEnemy = t.status === "enemy";
            const color = isOurs ? "#8aa4c2" : (isEnemy ? "#e76f51" : "#f4a261");
            const fillOpacity = isOurs ? 0.35 : 0.15;

            const poly = L.polygon(t.boundary, {
                color: isOurs ? "#4a6d8c" : color,
                weight: 1.5,
                fillColor: color,
                fillOpacity: fillOpacity
            });

            poly.bindTooltip(`<b>${t.id}</b><br>Status: ${t.status.toUpperCase()}<br>Score: ${t.score || 0} pts`);
            hexLayersGroup.addLayer(poly);
        });
    }

    let coverageLayerGroup;
    let trackLayerGroup;

    async function loadCoverage() {
        try {
            const res = await fetch("/api/coverage");
            if (!res.ok) return;
            const data = await res.json();
            renderCoverage(data);
        } catch (e) {
            console.log("No coverage data:", e);
        }
    }

    function renderCoverage(data) {
        if (!data) return;
        const coverage = data.coverage || [];
        const tracks = data.tracks || [];
        if (!coverage.length && !tracks.length) return;

        if (coverageLayerGroup) map.removeLayer(coverageLayerGroup);
        if (trackLayerGroup) map.removeLayer(trackLayerGroup);

        coverageLayerGroup = L.featureGroup();
        trackLayerGroup = L.featureGroup();

        // Render covered hexes with a semi-transparent blue fill
        coverage.forEach(c => {
            const visits = c.visit_count || 1;
            const opacity = Math.min(0.15 + visits * 0.08, 0.45);
            const poly = L.polygon(c.boundary, {
                color: "#3b82f6",
                weight: 1,
                fillColor: "#3b82f6",
                fillOpacity: opacity
            });
            poly.bindTooltip(`<b>Visited Hex</b><br>Visits: ${visits}`);
            coverageLayerGroup.addLayer(poly);
        });

        // Render raw GPS track polylines
        tracks.forEach(t => {
            if (t.coords && t.coords.length > 1) {
                L.polyline(t.coords, {
                    color: "#1d4ed8",
                    weight: 2.5,
                    opacity: 0.6
                }).addTo(trackLayerGroup);
            }
        });

        coverageLayerGroup.addTo(map);
        trackLayerGroup.addTo(map);
    }

    async function planRoute() {
        const planBtn = document.getElementById("plan-btn");
        const lat = parseFloat(document.getElementById("start-lat").value);
        const lng = parseFloat(document.getElementById("start-lng").value);
        const budgetInput = document.getElementById("budget-input");
        const budgetKm = parseFloat(budgetInput ? budgetInput.value : document.getElementById("budget-slider").value) || 8.0;
        const isLoop = document.getElementById("loop-toggle").checked;
        const exerciseMode = document.querySelector('input[name="exercise-mode"]:checked').value;

        if (isNaN(lat) || isNaN(lng)) {
            showStatus("Please enter valid starting coordinates.", "error");
            return;
        }

        planBtn.disabled = true;
        planBtn.innerHTML = '<div class="spinner" style="margin-right: 6px;"></div> Optimising Route...';

        // Larger budgets take longer (bigger graph download + more cells).
        // Scale timeout accordingly, with a 180 s cap.
        const timeoutSecs = Math.min(30 + budgetKm * 3, 180);
        showStatus(`Downloading ${exerciseMode} network & solving orienteering TSP... (timeout ${timeoutSecs}s)`, "info");

        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), timeoutSecs * 1000);

        try {
            const response = await fetch("/api/plan", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    start_lat: lat,
                    start_lng: lng,
                    budget_km: budgetKm,
                    loop: isLoop,
                    exercise_type: exerciseMode
                }),
                signal: controller.signal
            });

            clearTimeout(timeoutId);

            const result = await response.json();

            if (!response.ok || !result.success) {
                throw new Error(result.error || "Failed to calculate route");
            }

            hideStatus();
            displayRouteResult(result);
        } catch (err) {
            if (err.name === "AbortError") {
                showStatus(`Route planning timed out after ${timeoutSecs}s. Try a smaller distance or a start location closer to your targets.`, "error");
            } else {
                showStatus("Route Planning Error: " + err.message, "error");
            }
        } finally {
            planBtn.disabled = false;
            planBtn.innerHTML = '<i class="fa-solid fa-route"></i> Plan Optimal Route';
        }
    }

    function displayRouteResult(data) {
        // Clear previous route layers
        if (routePolyline) map.removeLayer(routePolyline);
        if (finishMarker) map.removeLayer(finishMarker);
        if (hexLayersGroup) {
            hexLayersGroup.clearLayers();
        } else {
            hexLayersGroup = L.featureGroup().addTo(map);
        }

        // 1. Render Hexagons
        // A. Existing Our Territory
        (data.existing_our_hexes || []).forEach(hex => {
            const poly = L.polygon(hex.boundary, {
                color: "#4a6d8c",
                weight: 2,
                fillColor: "#8aa4c2",
                fillOpacity: 0.40
            });
            poly.bindTooltip(`🔵 OUR TERRITORY: ${hex.id}`);
            hexLayersGroup.addLayer(poly);
        });

        // B. Other surrounding targets
        (data.other_targets || []).forEach(hex => {
            const isEnemy = hex.status === "enemy";
            const color = isEnemy ? "#e76f51" : "#f4a261";
            const poly = L.polygon(hex.boundary, {
                color: color,
                weight: 1,
                fillColor: color,
                fillOpacity: 0.12
            });
            poly.bindTooltip(`${hex.status.toUpperCase()}: ${hex.id} (${hex.score} pts)`);
            hexLayersGroup.addLayer(poly);
        });

        // C. Newly Claimed Hexes (Highlighted)
        (data.newly_claimed_hexes || []).forEach(hex => {
            const isEnemy = hex.status === "enemy";
            const fillCol = isEnemy ? "#e76f51" : "#2a9d8f";
            const strokeCol = isEnemy ? "#d90429" : "#1d7873";
            const poly = L.polygon(hex.boundary, {
                color: strokeCol,
                weight: 3.5,
                fillColor: fillCol,
                fillOpacity: 0.65
            });
            poly.bindPopup(`
                <div style="font-size: 13px;">
                    <span style="background: ${fillCol}; color: white; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 11px;">
                        ✨ ${isEnemy ? "ENEMY FLIP" : "NEW UNCLAIMED"}
                    </span><br><br>
                    <b>Hex ID:</b> <code>${hex.id}</code><br>
                    <b>Points:</b> <b style="color: ${fillCol}; font-size: 15px;">+${hex.score} pts</b>
                </div>
            `);
            poly.bindTooltip(`✨ NEW CLAIM: ${hex.id} (+${hex.score} pts)`);
            hexLayersGroup.addLayer(poly);
        });

        // 2. Render Path Polyline
        if (data.path_coords && data.path_coords.length >= 2) {
            routePolyline = L.polyline(data.path_coords, {
                color: "#4361ee",
                weight: 5.5,
                opacity: 0.92,
                lineJoin: "round"
            }).addTo(map);

            // Finish Marker
            const lastCoord = data.path_coords[data.path_coords.length - 1];
            const finishIcon = L.divIcon({
                html: '<div style="background: #e63946; color: white; width: 30px; height: 30px; border-radius: 50% 50% 50% 0; transform: rotate(-45deg); display: flex; align-items: center; justify-content: center; box-shadow: 0 3px 8px rgba(0,0,0,0.35); border: 2px solid white;"><i class="fa-solid fa-flag-checkered" style="transform: rotate(45deg); font-size: 12px; margin-left: 2px;"></i></div>',
                className: 'custom-pin-finish',
                iconSize: [30, 30],
                iconAnchor: [15, 30]
            });

            finishMarker = L.marker(lastCoord, { icon: finishIcon }).addTo(map);
            finishMarker.bindTooltip("<b>Finish Point</b>", { direction: "top" });

            map.fitBounds(routePolyline.getBounds(), { padding: [40, 40] });
        }

        // 3. Update Stats in Sidebar
        document.getElementById("res-distance").innerText = (data.total_distance_m / 1000.0).toFixed(2) + " km";
        document.getElementById("res-score").innerText = "+" + data.total_score.toFixed(1) + " pts";
        document.getElementById("res-new-hexes").innerText = (data.newly_claimed_hexes || []).length;
        document.getElementById("res-our-hexes").innerText = (data.existing_our_hexes || []).length;
        document.getElementById("route-type-badge").innerText = data.is_loop ? "Circular Loop" : "One-Way Route";

        const gpxBtn = document.getElementById("gpx-download-link");
        gpxBtn.href = "/api/download-gpx";
        gpxBtn.download = `hexroute_${(data.total_distance_m/1000).toFixed(1)}k_${data.total_score.toFixed(0)}pts.gpx`;

        document.getElementById("results-panel").style.display = "flex";
    }

    async function syncLiveStrods() {
        showStatus("Fetching live territory from Strods API...", "info");
        try {
            const res = await fetch("/api/fetch-live", { method: "POST" });
            const data = await res.json();
            if (res.ok && data.success) {
                showStatus(`Synced ${data.count} territories successfully!`, "info");
                setTimeout(hideStatus, 3000);
                loadBackgroundTargets();
            } else {
                showStatus("Live sync notice: " + (data.message || data.error || "Using cached data"), "info");
                setTimeout(hideStatus, 4000);
            }
        } catch (e) {
            showStatus("Strods API sync notice: " + e.message, "info");
            setTimeout(hideStatus, 4000);
        }
    }
</script>

</body>
</html>
"""


class HexrouteRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the Hexroute interactive web UI and API."""

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress standard access logs to avoid clutter, log errors only."""
        if args and str(args[1]) not in ("200", "304"):
            logger.info("%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), format % args))

    def _send_json(self, data: Any, status: int = 200) -> None:
        """Send a JSON HTTP response with security headers."""
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8080")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, html_str: str, status: int = 200) -> None:
        """Send an HTML HTTP response."""
        payload = html_str.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        """Handle GET requests."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._send_html(get_ui_html())
            return

        if path == "/api/config":
            try:
                cfg = load_config()
                res = {
                    "home_lat": float(cfg["general"]["home_lat"]),
                    "home_lng": float(cfg["general"]["home_lng"]),
                    "default_budget_km": float(cfg["general"]["default_budget_km"]),
                    "h3_resolution": int(cfg["general"]["h3_resolution"]),
                    "team_name": str(cfg.get("competition", {}).get("team_name", "Agile & Fragile")),
                    "team_colour": str(cfg.get("competition", {}).get("team_colour", "#8aa4c2")),
                }
                self._send_json(res)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if path == "/api/targets":
            try:
                cfg = load_config()
                res_level = int(cfg["general"]["h3_resolution"])
                targets = load_targets_csv("data/targets.csv")

                # Deduce our territory from ingested activities
                d_dir = cfg["paths"]["data_dir"]
                c_dir = cfg["paths"]["cache_dir"]
                ingest_result = ingest_activities(data_dir=d_dir, cache_dir=c_dir)
                coverage_data = compute_coverage(ingest_result.activities, res=res_level) if ingest_result.activities else {}
                our_territory = set(coverage_data.keys())

                scored = score_targets(targets, our_territory=our_territory, scoring_config=cfg.get("scoring"))

                target_list = []
                for cell, t in scored.items():
                    boundary = cell_to_boundary(cell)
                    target_list.append({
                        "id": cell,
                        "status": t.status,
                        "score": t.final_score,
                        "boundary": [[p[0], p[1]] for p in boundary],
                    })
                self._send_json({"targets": target_list})
            except Exception as e:
                self._send_json({"targets": [], "error": str(e)})
            return

        if path == "/api/coverage":
            try:
                cfg = load_config()
                res_level = int(cfg["general"]["h3_resolution"])
                d_dir = cfg["paths"]["data_dir"]
                c_dir = cfg["paths"]["cache_dir"]
                ingest_result = ingest_activities(data_dir=d_dir, cache_dir=c_dir)

                if not ingest_result.activities:
                    self._send_json({"coverage": [], "track_count": 0})
                    return

                coverage_data = compute_coverage(ingest_result.activities, res=res_level)
                coverage_list = []
                for cell, rec in coverage_data.items():
                    boundary = cell_to_boundary(cell)
                    coverage_list.append({
                        "id": cell,
                        "visit_count": rec.visit_count,
                        "boundary": [[p[0], p[1]] for p in boundary],
                    })

                # Also include raw track coordinates for display
                tracks = []
                for act_id, points in ingest_result.activities.items():
                    if len(points) < 2:
                        continue
                    tracks.append({
                        "id": act_id,
                        "coords": [[p.lat, p.lng] for p in points],
                        "point_count": len(points),
                    })

                self._send_json({"coverage": coverage_list, "tracks": tracks, "track_count": len(tracks)})
            except Exception as e:
                self._send_json({"coverage": [], "tracks": [], "track_count": 0, "error": str(e)})
            return

        if path == "/api/download-gpx":
            global _LATEST_GPX_CONTENT, _LATEST_GPX_FILENAME
            if not _LATEST_GPX_CONTENT:
                self.send_error(HTTPStatus.NOT_FOUND, "No planned GPX route available yet")
                return

            payload = _LATEST_GPX_CONTENT.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/gpx+xml; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{_LATEST_GPX_FILENAME}"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Page not found")

    def do_POST(self) -> None:
        """Handle POST requests."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/fetch-live":
            try:
                cfg = load_config()
                club_id = str(cfg.get("competition", {}).get("club_id", "2345"))
                my_team = str(cfg.get("competition", {}).get("team_name", "Agile & Fragile"))
                count = sync_competition_targets(club_id=club_id, my_team=my_team, out_csv="data/targets.csv")
                self._send_json({"success": True, "count": count, "message": f"Fetched {count} cells from Strods"})
            except Exception as e:
                self._send_json({"success": False, "error": str(e), "message": "Using cached targets"})
            return

        if path == "/api/plan":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)

            try:
                data = json.loads(body) if body else {}
                start_lat = float(data.get("start_lat", 51.5074))
                start_lng = float(data.get("start_lng", -0.1278))
                budget_km = float(data.get("budget_km", 8.0))
                is_loop = bool(data.get("loop", True))
                exercise_type = str(data.get("exercise_type", "walk")).lower()

                # Input bounds validation
                start_lat = max(min(start_lat, 90.0), -90.0)
                start_lng = max(min(start_lng, 180.0), -180.0)
                budget_km = max(min(budget_km, 300.0), 0.5)
                network_type = "bike" if exercise_type in ("bike", "cycling", "cycle") else "walk"

                cfg = load_config()
                res_level = int(cfg["general"]["h3_resolution"])
                cache_dir = Path(cfg["paths"]["cache_dir"])
                out_dir = Path(cfg["paths"]["out_dir"])
                out_dir.mkdir(parents=True, exist_ok=True)

                # Load & expand targets
                targets_path = Path("data/targets.csv")
                targets = load_targets_csv(targets_path)
                if not targets:
                    center_cell = latlng_to_cell(start_lat, start_lng, res_level)
                    cells = get_disk(center_cell, 2)
                    targets = {c: HexStatus.ENEMY if i % 2 == 0 else HexStatus.UNCLAIMED for i, c in enumerate(sorted(cells))}
                    targets[center_cell] = HexStatus.OURS
                    save_targets_csv(targets, targets_path)

                search_radius_km = max(budget_km / 2.0, 3.0)
                expanded = expand_unclaimed_targets(
                    targets,
                    center_lat=start_lat,
                    center_lng=start_lng,
                    radius_km=search_radius_km,
                    res=res_level,
                )
                scored = score_targets(expanded, scoring_config=cfg.get("scoring"))

                # Deduce our territory from ingested activities so the
                # optimiser knows which hexes we already own.
                d_dir = cfg["paths"]["data_dir"]
                c_dir = cfg["paths"]["cache_dir"]
                ingest_result = ingest_activities(data_dir=d_dir, cache_dir=c_dir)
                if ingest_result.activities:
                    coverage_data = compute_coverage(ingest_result.activities, res=res_level)
                    our_territory = set(coverage_data.keys())
                    scored = score_targets(expanded, our_territory=our_territory, scoring_config=cfg.get("scoring"))

                # Download/load OSM network
                # Cap graph radius at 10 km — the optimiser only considers
                # MAX_TARGET_CELLS (25) cells near the start, so a smaller
                # graph keeps download and Dijkstra times manageable while
                # still covering the local area.  Larger budgets benefit
                # from a wider search area but the graph size scales as
                # radius squared, making 25 km+ downloads take minutes.
                graph_radius_m = max(min(budget_km * 1000.0 / 2.0, 10000.0), 2500.0)
                graph = get_walking_graph(
                    lat=start_lat,
                    lng=start_lng,
                    dist_metres=graph_radius_m,
                    cache_dir=cache_dir,
                    network_type=network_type,
                )

                # Optimize route
                plan_result = plan_optimal_route(
                    graph=graph,
                    start_lat=start_lat,
                    start_lng=start_lng,
                    budget_km=budget_km,
                    scored_targets=scored,
                    is_loop=is_loop,
                    res=res_level,
                )

                # Extract path coordinates
                coords = get_path_coordinates(graph, plan_result.path_nodes)
                if not coords:
                    coords = [[start_lat, start_lng]]

                # Build polygon datasets for newly claimed, our existing, and other hexes
                newly_claimed = []
                for cell in sorted(plan_result.newly_claimed_hexes):
                    t = scored.get(cell)
                    boundary = cell_to_boundary(cell)
                    newly_claimed.append({
                        "id": cell,
                        "status": t.status if t else "unclaimed",
                        "score": t.final_score if t else 2.0,
                        "boundary": [[p[0], p[1]] for p in boundary],
                    })

                existing_ours = []
                for cell in sorted(plan_result.existing_our_hexes):
                    boundary = cell_to_boundary(cell)
                    existing_ours.append({
                        "id": cell,
                        "status": "ours",
                        "score": 0.0,
                        "boundary": [[p[0], p[1]] for p in boundary],
                    })

                other_targets = []
                for cell, t in scored.items():
                    if cell not in plan_result.newly_claimed_hexes and cell not in plan_result.existing_our_hexes:
                        boundary = cell_to_boundary(cell)
                        other_targets.append({
                            "id": cell,
                            "status": t.status,
                            "score": t.final_score,
                            "boundary": [[p[0], p[1]] for p in boundary],
                        })

                # Export GPX to in-memory cache and file
                gpx_file = out_dir / "route.gpx"
                export_route_gpx(graph, plan_result.path_nodes, out_path=gpx_file)

                global _LATEST_GPX_CONTENT, _LATEST_GPX_FILENAME
                _LATEST_GPX_CONTENT = gpx_file.read_text(encoding="utf-8")
                _LATEST_GPX_FILENAME = f"hexroute_{budget_km:.1f}k_{plan_result.total_score:.0f}pts.gpx"

                res_json = {
                    "success": True,
                    "total_distance_m": plan_result.total_distance_m,
                    "total_score": plan_result.total_score,
                    "budget_km": budget_km,
                    "is_loop": is_loop,
                    "exercise_type": exercise_type,
                    "path_coords": coords,
                    "newly_claimed_hexes": newly_claimed,
                    "existing_our_hexes": existing_ours,
                    "other_targets": other_targets,
                }
                self._send_json(res_json)
            except Exception as e:
                logger.exception("Error planning route via API")
                self._send_json({"success": False, "error": str(e)}, status=500)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "API endpoint not found")


def run_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    open_browser: bool = True,
) -> None:
    """Start the local Hexroute interactive web server."""
    # Ensure localhost binding to comply with security requirements
    if host not in ("127.0.0.1", "localhost"):
        host = "127.0.0.1"

    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, HexrouteRequestHandler)
    url = f"http://{host}:{port}"
    print(f"\n=======================================================")
    print(f"🚀 Hexroute Web UI is running at: {url}")
    print(f"👉 Click anywhere on the map to set starting location")
    print(f"👉 Select route distance and exercise type (walk/bike)")
    print(f"👉 Press Ctrl+C in terminal to stop server")
    print(f"=======================================================\n")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Hexroute Web UI...")
    finally:
        httpd.server_close()
