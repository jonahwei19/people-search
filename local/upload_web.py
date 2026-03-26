#!/usr/bin/env python3
"""People Search — Data Upload & Management.

Upload CSV/JSON files of people, review schema mappings, enrich via LinkedIn,
and manage datasets for search.

Storage: JSON files in datasets/ directory, embeddings as .npz.
Sufficient for local use with hundreds to low thousands of profiles.
Switch to SQLite when: multi-user, >10K profiles, or need concurrent writes.

Usage:
    python3 upload_web.py              # http://localhost:5556
    python3 upload_web.py --port 5557
"""

import argparse
import json
import os
import sys
import threading
import uuid
from pathlib import Path

# Add parent directory to path so we can import enrichment/ and search/
sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, request, jsonify, render_template_string

from enrichment import EnrichmentPipeline, FieldType
from enrichment.schema import FieldMapping
from enrichment.enrichers import is_valid_linkedin_url
from local.search_blueprint import search_bp, init_search, SEARCH_NAV, SEARCH_PAGE, SEARCH_JS

app = Flask(__name__)
app.register_blueprint(search_bp)

UPLOAD_DIR = Path(__file__).parent.parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

PIPELINE: EnrichmentPipeline = None
JOBS: dict[str, dict] = {}

# ── HTML ─────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%232563eb' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'><circle cx='11' cy='11' r='8'/><line x1='21' y1='21' x2='16.65' y2='16.65'/></svg>">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>People Search</title>
<style>
:root {
  --bg: #fafafa;
  --card: #fff;
  --border: #e8e8e8;
  --text: #1a1a1a;
  --text2: #666;
  --text3: #999;
  --accent: #2563eb;
  --accent-light: #eff6ff;
  --green: #16a34a;
  --green-light: #f0fdf4;
  --amber: #d97706;
  --amber-light: #fffbeb;
  --red: #dc2626;
  --red-light: #fef2f2;
  --radius: 10px;
  --shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
}
[data-theme="dark"] {
  --bg: #0f1117;
  --card: #1a1d27;
  --border: #2a2d3a;
  --text: #e4e4e7;
  --text2: #a1a1aa;
  --text3: #71717a;
  --accent: #3b82f6;
  --accent-light: #1e293b;
  --green: #22c55e;
  --green-light: #14271f;
  --amber: #f59e0b;
  --amber-light: #27200f;
  --red: #ef4444;
  --red-light: #2a1215;
  --shadow: 0 1px 3px rgba(0,0,0,0.3), 0 1px 2px rgba(0,0,0,0.2);
}
[data-theme="dark"] .sidebar { background: #0a0c12; }
[data-theme="dark"] .sidebar-nav a:hover,
[data-theme="dark"] .sidebar-nav a.active { background: #151820; }
[data-theme="dark"] .dropzone:hover,
[data-theme="dark"] .dropzone.over { background: var(--accent-light); }
[data-theme="dark"] .modal { background: var(--card); }
[data-theme="dark"] .schema-row:hover,
[data-theme="dark"] .profile-row:hover { background: #1e2130; }
[data-theme="dark"] input, [data-theme="dark"] select, [data-theme="dark"] textarea {
  background: var(--card); color: var(--text); border-color: var(--border);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', sans-serif;
       background: var(--bg); color: var(--text); line-height: 1.55; font-size: 14px; }

/* Layout */
.shell { display: flex; min-height: 100vh; }
.sidebar { width: 220px; background: #111827; color: #e5e7eb; padding: 20px 0;
           display: flex; flex-direction: column; flex-shrink: 0; }
.sidebar-brand { padding: 0 20px 20px; font-size: 15px; font-weight: 600;
                 color: #fff; letter-spacing: -0.3px; border-bottom: 1px solid #1f2937; }
.sidebar-brand span { color: #60a5fa; }
.sidebar-nav { padding: 12px 0; flex: 1; }
.sidebar-nav a { display: flex; align-items: center; gap: 10px; padding: 9px 20px;
                 color: #9ca3af; text-decoration: none; font-size: 13px; font-weight: 500;
                 transition: all 0.15s; cursor: pointer; }
.sidebar-nav a:hover { color: #fff; background: #1f2937; }
.sidebar-nav a.active { color: #fff; background: #1f2937; border-right: 2px solid #60a5fa; }
.sidebar-nav a svg { width: 18px; height: 18px; opacity: 0.7; }

.main { flex: 1; padding: 32px 40px; max-width: 960px; }
.main h1 { font-size: 22px; font-weight: 600; margin-bottom: 4px; letter-spacing: -0.3px; }
.main .page-desc { color: var(--text2); margin-bottom: 28px; font-size: 14px; }

/* Pages */
.page { display: none; }
.page.active { display: block; }

/* Cards */
.card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
        padding: 24px; margin-bottom: 16px; box-shadow: var(--shadow); }
.card-header { font-size: 15px; font-weight: 600; margin-bottom: 4px; }
.card-sub { color: var(--text2); font-size: 13px; margin-bottom: 16px; }

/* Upload zone */
.dropzone { border: 2px dashed var(--border); border-radius: var(--radius);
            padding: 52px 24px; text-align: center; cursor: pointer;
            transition: all 0.2s; background: var(--card); }
.dropzone:hover, .dropzone.over { border-color: var(--accent); background: var(--accent-light); }
.dropzone-icon { width: 48px; height: 48px; margin: 0 auto 12px; border-radius: 12px;
                 background: var(--accent-light); display: flex; align-items: center;
                 justify-content: center; color: var(--accent); }
.dropzone-text { font-size: 15px; font-weight: 500; margin-bottom: 4px; }
.dropzone-hint { color: var(--text3); font-size: 12px; }
.dropzone input { display: none; }

/* Schema groups */
.schema-group { margin-bottom: 20px; }
.schema-group-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.schema-group-dot { width: 8px; height: 8px; border-radius: 50%; }
.schema-group-label { font-size: 12px; font-weight: 600; text-transform: uppercase;
                      letter-spacing: 0.5px; color: var(--text2); }
.schema-group-count { font-size: 11px; color: var(--text3); }

.schema-row { display: flex; align-items: center; gap: 12px; padding: 8px 12px;
              border-radius: 6px; margin-bottom: 2px; transition: background 0.1s; }
.schema-row:hover { background: #f8f8f8; }
.schema-col-name { font-weight: 500; min-width: 160px; font-size: 13px; }
.schema-col-sample { flex: 1; color: var(--text3); font-size: 12px; overflow: hidden;
                     text-overflow: ellipsis; white-space: nowrap; }
.schema-col-type { min-width: 180px; }
.schema-col-type select { width: 100%; padding: 5px 8px; border: 1px solid var(--border);
                          border-radius: 6px; font-size: 12px; background: var(--card);
                          color: var(--text); cursor: pointer; }
.schema-col-type select:focus { border-color: var(--accent); outline: none;
                                 box-shadow: 0 0 0 2px var(--accent-light); }

/* Dataset name */
.name-field { margin-bottom: 20px; }
.name-field label { font-size: 12px; font-weight: 600; color: var(--text2); display: block;
                    margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
.name-field input { width: 100%; max-width: 400px; padding: 8px 12px; border: 1px solid var(--border);
                    border-radius: 6px; font-size: 14px; }
.name-field input:focus { border-color: var(--accent); outline: none;
                          box-shadow: 0 0 0 2px var(--accent-light); }

/* Stat pills */
.stats { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
.stat { background: #f3f4f6; border-radius: 8px; padding: 14px 18px; min-width: 120px; }
.stat-val { font-size: 22px; font-weight: 700; color: var(--accent); }
.stat-label { font-size: 11px; color: var(--text2); margin-top: 2px; }

/* Cost breakdown */
.cost-card { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: var(--radius);
             padding: 20px; font-size: 13px; }
.cost-line { display: flex; justify-content: space-between; padding: 4px 0;
             border-bottom: 1px solid #f1f5f9; }
.cost-line:last-child { border-bottom: none; }
.cost-total { font-weight: 600; border-top: 2px solid #e2e8f0; padding-top: 8px; margin-top: 4px; }
.cost-warn { background: var(--amber-light); color: #92400e; padding: 8px 12px;
             border-radius: 6px; font-size: 12px; margin-top: 12px; }

/* Progress */
.progress-track { background: #e5e7eb; border-radius: 8px; height: 8px; overflow: hidden;
                  margin: 16px 0 8px; }
.progress-fill { background: var(--accent); height: 100%; border-radius: 8px;
                 transition: width 0.4s ease; }
.progress-label { font-size: 12px; color: var(--text2); }

/* Buttons */
.btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; border: none;
       border-radius: 6px; font-size: 13px; font-weight: 500; cursor: pointer;
       transition: all 0.15s; }
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover { background: #1d4ed8; }
.btn-ghost { background: transparent; color: var(--text2); }
.btn-ghost:hover { background: #f3f4f6; color: var(--text); }
.btn-success { background: var(--green); color: #fff; }
.btn-group { display: flex; gap: 8px; margin-top: 20px; }

/* Dataset list */
.ds-card { display: flex; align-items: center; padding: 16px 20px; background: var(--card);
           border: 1px solid var(--border); border-radius: var(--radius); margin-bottom: 8px;
           box-shadow: var(--shadow); cursor: pointer; transition: all 0.15s; }
.ds-card:hover { border-color: var(--accent); }
.ds-icon { width: 40px; height: 40px; border-radius: 10px; background: var(--accent-light);
           display: flex; align-items: center; justify-content: center; margin-right: 14px;
           color: var(--accent); font-weight: 700; font-size: 16px; flex-shrink: 0; }
.ds-info { flex: 1; }
.ds-name { font-weight: 600; font-size: 14px; }
.ds-meta { color: var(--text3); font-size: 12px; margin-top: 2px; }
.ds-fields { display: flex; gap: 4px; margin-top: 6px; flex-wrap: wrap; }
.ds-field-tag { background: #f3f4f6; padding: 2px 8px; border-radius: 4px;
                font-size: 11px; color: var(--text2); }
.ds-actions { flex-shrink: 0; }

/* Profile list inside dataset detail */
.profile-row { display: flex; align-items: center; padding: 10px 16px;
               border-bottom: 1px solid #f3f4f6; font-size: 13px; }
.profile-row:hover { background: #fafafa; }
.profile-name { font-weight: 500; min-width: 180px; }
.profile-org { color: var(--text2); min-width: 160px; }
.profile-title { color: var(--text2); flex: 1; }
.profile-status { font-size: 11px; padding: 2px 8px; border-radius: 4px; }
.profile-status.enriched { background: var(--green-light); color: var(--green); }
.profile-status.pending { background: var(--amber-light); color: var(--amber); }
.profile-status.failed { background: var(--red-light); color: var(--red); }
.profile-status.skipped { background: #f3f4f6; color: var(--text3); }

/* Empty state */
.empty { text-align: center; padding: 48px; color: var(--text3); }
.empty-icon { font-size: 32px; margin-bottom: 8px; opacity: 0.3; }

/* Steps indicator */
.steps-bar { display: flex; gap: 0; margin-bottom: 24px; }
.step-dot { display: flex; align-items: center; gap: 8px; padding: 8px 16px;
            font-size: 12px; font-weight: 500; color: var(--text3); position: relative; }
.step-dot .num { width: 22px; height: 22px; border-radius: 50%; background: #e5e7eb;
                 display: flex; align-items: center; justify-content: center;
                 font-size: 11px; color: var(--text2); }
.step-dot.active .num { background: var(--accent); color: #fff; }
.step-dot.active { color: var(--text); }
.step-dot.done .num { background: var(--green); color: #fff; }
.step-dot.done { color: var(--green); }
.step-connector { width: 32px; height: 2px; background: #e5e7eb; align-self: center; }
.step-connector.done { background: var(--green); }

/* Transitions */
.fade-in { animation: fadeIn 0.2s ease; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }

/* Profile detail modal */
.modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,0.3); z-index: 100;
            display: none; align-items: center; justify-content: center; }
.modal-bg.open { display: flex; }
.modal { background: var(--card); border-radius: 12px; width: 640px; max-height: 80vh;
         overflow-y: auto; padding: 28px; box-shadow: 0 20px 60px rgba(0,0,0,0.15); }
.modal h2 { font-size: 18px; margin-bottom: 4px; }
.modal .subtitle { color: var(--text2); font-size: 13px; margin-bottom: 16px; }
.modal-section { margin-bottom: 16px; }
.modal-section h3 { font-size: 12px; font-weight: 600; color: var(--text2); text-transform: uppercase;
                    letter-spacing: 0.5px; margin-bottom: 6px; }
.modal-section p { font-size: 13px; line-height: 1.6; color: var(--text); }
.modal-close { position: absolute; top: 16px; right: 16px; background: none; border: none;
               font-size: 20px; cursor: pointer; color: var(--text3); }
</style>
</head>
<body>
<div class="shell">
  <nav class="sidebar">
    <div class="sidebar-brand">People <span>Search</span></div>
    <div class="sidebar-nav">
      <a onclick="showPage('search')" id="nav-search">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        Search
      </a>
      <a class="active" onclick="showPage('upload')" id="nav-upload">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        Upload
      </a>
      <a onclick="showPage('datasets')" id="nav-datasets">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
        Datasets
      </a>
      <a onclick="showPage('settings')" id="nav-settings">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
        Settings
      </a>
    </div>
    <div style="padding:12px 20px;border-top:1px solid #1f2937;font-size:11px;color:#6b7280;line-height:1.4;">
      Runs entirely on your machine. Your data never leaves your computer except for API calls you approve.
    </div>
  </nav>

  <div class="main" style="position:relative;">
    <button onclick="toggleTheme()" id="theme-toggle" style="position:absolute;top:8px;right:8px;background:none;border:1px solid var(--border);border-radius:6px;padding:4px 8px;cursor:pointer;font-size:14px;color:var(--text2);z-index:10;" title="Toggle dark/light mode">🌙</button>
    <!-- ═══ UPLOAD PAGE ═══ -->
    <div class="page active" id="page-upload">
      <h1>Upload Data</h1>
      <p class="page-desc">Drop a CSV or JSON of people. We'll detect the schema, show you what enrichment will cost, and make the data searchable.</p>

      <div class="dropzone" id="dropzone">
        <div class="dropzone-icon">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        </div>
        <div class="dropzone-text">Drop a file here or click to browse</div>
        <div class="dropzone-hint">CSV or JSON with name, email, LinkedIn URL, notes, or any other fields</div>
        <input type="file" id="fileInput" accept=".csv,.json,.jsonl">
      </div>

      <!-- Upload flow steps (hidden until file dropped) -->
      <div id="uploadFlow" style="display:none;">
        <div class="steps-bar" id="stepsBar">
          <div class="step-dot active" id="step-ind-1"><div class="num">1</div> Map columns</div>
          <div class="step-connector" id="step-conn-1"></div>
          <div class="step-dot" id="step-ind-2"><div class="num">2</div> Review cost</div>
          <div class="step-connector" id="step-conn-2"></div>
          <div class="step-dot" id="step-ind-3"><div class="num">3</div> Enrich</div>
          <div class="step-connector" id="step-conn-3"></div>
          <div class="step-dot" id="step-ind-4"><div class="num">4</div> Done</div>
        </div>

        <!-- Step 1: Schema -->
        <div class="card fade-in" id="step1" style="display:none;">
          <div class="name-field">
            <label>Dataset name</label>
            <input id="dsName" placeholder="e.g. Q1 Advisory Candidates">
          </div>

          <div id="schemaGroups"></div>

          <div class="btn-group">
            <button class="btn btn-primary" onclick="confirmSchema()">Continue</button>
            <button class="btn btn-ghost" onclick="resetUpload()">Cancel</button>
          </div>
        </div>

        <!-- Step 2: Cost -->
        <div class="card fade-in" id="step2" style="display:none;">
          <div class="card-header">Enrichment estimate</div>
          <div class="card-sub">What we found in your data and what enrichment will cost.</div>

          <div class="stats" id="costStats"></div>
          <div class="cost-card" id="costDetail"></div>

          <div class="btn-group">
            <button class="btn btn-primary" onclick="startEnrich()">Start enrichment</button>
            <button class="btn btn-ghost" onclick="skipEnrich()">Skip enrichment</button>
            <button class="btn btn-ghost" onclick="goStep(1)">Back</button>
          </div>
        </div>

        <!-- Step 3: Progress -->
        <div class="card fade-in" id="step3" style="display:none;">
          <div class="card-header">Enriching profiles</div>
          <div class="card-sub" id="progressLabel">Starting...</div>
          <div class="progress-track"><div class="progress-fill" id="progressFill" style="width:0"></div></div>
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <div class="progress-label" id="progressPct">0%</div>
            <button class="btn btn-ghost" style="color:var(--red);font-size:12px;" onclick="cancelEnrich()">Cancel</button>
          </div>
        </div>

        <!-- Step 4: Done -->
        <div class="card fade-in" id="step4" style="display:none;">
          <div class="card-header" style="color:var(--green);">Upload complete</div>
          <div class="stats" id="doneStats"></div>
          <div class="btn-group">
            <button class="btn btn-primary" onclick="resetUpload()">Upload another</button>
            <button class="btn btn-ghost" onclick="showPage('datasets')">View datasets</button>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══ DATASETS PAGE ═══ -->
    <div class="page" id="page-datasets">
      <h1>Datasets</h1>
      <p class="page-desc">All uploaded datasets. Click to view profiles.</p>
      <div id="dsList"></div>
    </div>

    <!-- ═══ DATASET DETAIL PAGE ═══ -->
    <div class="page" id="page-ds-detail">
      <div style="margin-bottom: 20px; display:flex; justify-content:space-between; align-items:center;">
        <button class="btn btn-ghost" onclick="showPage('datasets')" style="margin-left:-8px;">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
          Back
        </button>
        <div style="display:flex;gap:8px;">
          <button class="btn btn-primary" style="font-size:12px;" onclick="reenrichDataset(window._dsDetailId)">Re-enrich</button>
          <button class="btn btn-ghost" style="font-size:12px;color:var(--red);" onclick="deleteDataset(window._dsDetailId, document.getElementById('detailName').textContent)">Delete dataset</button>
        </div>
      </div>
      <h1 id="detailName"></h1>
      <p class="page-desc" id="detailMeta"></p>
      <div class="stats" id="detailStats"></div>
      <div class="card" style="padding:0; overflow:hidden;">
        <div style="padding:12px 16px; border-bottom: 1px solid var(--border); display:flex; align-items:center; gap:12px;">
          <input id="profileSearch" placeholder="Filter profiles..." style="flex:1; padding:6px 10px; border:1px solid var(--border); border-radius:6px; font-size:13px;">
          <span id="profileCount" style="font-size:12px; color:var(--text3);"></span>
        </div>
        <div id="profileList" style="max-height:500px; overflow-y:auto;"></div>
      </div>
    </div>

    <!-- ═══ SEARCH PAGE ═══ -->
    <div class="page" id="page-search">
      <h1>Search</h1>
      <p class="page-desc">Find people across your datasets using AI scoring.</p>

      <!-- Step 0: Search picker + global rules -->
      <div id="search-picker">
        <div class="card">
          <div class="card-header">Your Searches</div>
          <div class="card-sub">Pick an existing search or create a new one.</div>
          <div id="search-list"></div>
          <div style="display:flex;gap:8px;margin-top:8px;">
            <button class="btn primary" onclick="searchNewFlow()">+ Create New Search</button>
            <button class="btn" onclick="document.getElementById('search-import-file').click()">Import JSON</button>
            <input type="file" id="search-import-file" accept=".json" style="display:none" onchange="searchImportFile(this)">
          </div>
        </div>

        <div class="card" style="margin-top:16px;">
          <div style="display:flex;align-items:center;gap:6px;">
            <div class="card-header" style="margin-bottom:0;">Global Rules</div>
            <span style="display:inline-block;width:15px;height:15px;border-radius:50%;background:var(--border);text-align:center;font-size:10px;line-height:15px;color:var(--text2);cursor:help;" title="Global rules apply to EVERY search. They encode institutional knowledge — like what 'Republican' means for vetting. Format: 'When [condition], [rule].'">i</span>
          </div>
          <div class="card-sub">Apply across all searches. Format: "When [condition], [rule]."</div>
          <div id="global-rules-list" style="margin-bottom:10px;"></div>
          <div style="border-top:1px solid var(--border);padding-top:10px;">
            <textarea id="new-global-rule" placeholder='e.g. When anyone says "exceptional," they mean someone who has built and shipped real programs, not just written about wanting to.' style="width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;font-size:13px;min-height:50px;resize:vertical;"></textarea>
            <div style="margin-top:6px;">
              <button class="btn primary sm" onclick="addGlobalRule()">Add Rule</button>
            </div>
            <div style="font-size:11px;color:var(--text3);margin-top:6px;line-height:1.5;">
              Write rules the AI can understand. Before each search, AI reads all global rules and only injects the ones relevant to that specific search.<br>
              <strong>Good:</strong> "When evaluating 'practitioner,' look for shipping real programs — publications alone don't count."<br>
              <strong>Bad:</strong> "Prefer operators" <span style="color:var(--red);">(too vague)</span>, "This person is fluff" <span style="color:var(--red);">(search-level, not global)</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Step 1: New search + chat conversation -->
      <div id="search-new" style="display:none;">
        <div class="card" style="margin-bottom:12px;">
          <div class="card-header">New Search</div>
          <input type="text" id="search-name" placeholder="Name this search (e.g. BlueDot Head of Talent)" style="width:100%;padding:10px;border:1px solid var(--border);border-radius:6px;font-size:14px;margin-bottom:8px;">
          <div style="display:flex;gap:8px;align-items:center;">
            <select id="search-dataset" style="padding:8px;border:1px solid var(--border);border-radius:6px;font-size:13px;"><option value="">All datasets</option></select>
            <button class="btn" onclick="searchBackToPicker()">Cancel</button>
          </div>
        </div>

        <!-- Chat interface -->
        <div class="card" style="padding:0;overflow:hidden;">
          <div id="search-chat" style="max-height:400px;overflow-y:auto;padding:16px;">
            <!-- Messages appear here -->
          </div>
          <div style="border-top:1px solid var(--border);padding:12px;display:flex;gap:8px;">
            <textarea id="search-chat-input" placeholder="Describe who you're looking for..." style="flex:1;padding:10px;border:1px solid var(--border);border-radius:6px;font-size:14px;min-height:44px;max-height:120px;resize:vertical;" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();searchChatSend()}"></textarea>
            <button class="btn primary" onclick="searchChatSend()" id="search-chat-btn" style="align-self:flex-end;">Send</button>
          </div>
        </div>
        <button class="btn" onclick="searchSkipToScore()" id="search-skip-btn" style="margin-top:8px;display:none;">Skip questions &amp; search now</button>
      </div>

      <!-- Step 3: Progress -->
      <div id="search-progress" style="display:none;">
        <div class="card">
          <div style="font-size:13px;color:var(--text2);margin-bottom:4px;" id="search-progress-text">Scoring profiles...</div>
          <div style="background:var(--border);border-radius:4px;height:6px;"><div id="search-progress-fill" style="background:var(--accent);border-radius:4px;height:100%;width:0%;transition:width 0.3s;"></div></div>
        </div>
      </div>

      <!-- Step 4: Results + feedback -->
      <div id="search-results" style="display:none;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
          <div style="font-size:13px;color:var(--text2);" id="search-results-header"></div>
          <div style="display:flex;gap:8px;align-items:center;">
            <select id="search-dataset-results" style="padding:6px 8px;border:1px solid var(--border);border-radius:6px;font-size:12px;" onchange="switchSearchDataset()"><option value="">All datasets</option></select>
            <button class="btn" onclick="searchRerun()" id="btn-rerun">Re-run with feedback</button>
            <button class="btn" onclick="searchBackToPicker()">Back</button>
          </div>
        </div>
        <div id="search-rules-panel" class="card" style="display:none;padding:12px;margin-bottom:12px;">
          <div class="card-header" style="font-size:13px;">Search Rules</div>
          <div id="search-rules-list" style="font-size:12px;color:var(--text2);"></div>
        </div>
        <div id="search-proposal" class="card" style="display:none;background:var(--accent-light);padding:12px;margin-bottom:12px;">
          <div class="card-header" style="font-size:13px;">Proposed changes from feedback</div>
          <div id="search-proposal-content"></div>
          <div style="display:flex;gap:8px;margin-top:8px;">
            <button class="btn primary sm" onclick="searchAcceptProposal()">Accept &amp; Re-run</button>
            <button class="btn sm" onclick="searchRejectProposal()">Skip &amp; Re-run</button>
          </div>
        </div>
        <div id="search-results-list"></div>
      </div>
    </div>

    <!-- ═══ SETTINGS PAGE ═══ -->
    <div class="page" id="page-settings">
      <h1>Settings</h1>
      <p class="page-desc">API keys are saved locally and never sent anywhere except the services listed below.</p>

      <div class="card">
        <div class="card-header">API Keys</div>
        <div class="card-sub">These enable enrichment features. All are optional — the app works without them, but enrichment will be limited.</div>

        <div id="keyFields"></div>

        <div class="btn-group">
          <button class="btn btn-primary" onclick="saveKeys()">Save</button>
          <span id="keySaveStatus" style="font-size:12px;color:var(--green);align-self:center;"></span>
        </div>
      </div>

      <div class="card" style="background:#f8fafc;">
        <div class="card-header" style="font-size:13px;">How this app uses your data</div>
        <div style="font-size:13px;color:var(--text2);line-height:1.7;">
          <p style="margin-bottom:8px;"><strong>Everything runs locally on your machine.</strong> Your uploaded data is stored in a <code>datasets/</code> folder on disk as JSON files. Nothing is sent to any server unless you explicitly run enrichment.</p>
          <p style="margin-bottom:8px;"><strong>When you enrich:</strong> Names and emails are sent to Brave Search to find LinkedIn profiles. LinkedIn URLs are sent to EnrichLayer to pull public profile data. That's it.</p>
          <p style="margin-bottom:8px;"><strong>Suspicious?</strong> The code is fully inspectable. Ask Claude to explain what any file does, or read the source in <code>enrichment/</code>.</p>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Onboarding overlay (shown on first launch if keys missing) -->
<div class="modal-bg" id="onboardingModal" style="display:none;">
  <div class="modal" style="max-width:540px;">
    <h2>Welcome to People Search</h2>
    <div class="subtitle">Upload databases of people, enrich them, and search across them. Everything runs locally.</div>

    <div style="background:#f8fafc;border-radius:8px;padding:16px;margin-bottom:20px;font-size:13px;color:var(--text2);line-height:1.6;">
      Your data stays on your machine. The only external calls are to enrichment APIs (Brave Search, EnrichLayer) — and only when you click "Start enrichment."
      <br><br>
      Suspicious about what this code does? Ask Claude: <em>"explain what enrichment/pipeline.py does"</em>
    </div>

    <div style="font-size:13px;font-weight:600;margin-bottom:12px;">API keys <span style="color:var(--text3);font-weight:400;">(optional — you can add these later in Settings)</span></div>
    <div id="onboardingKeys"></div>

    <div class="btn-group" style="margin-top:20px;">
      <button class="btn btn-primary" onclick="saveOnboardingKeys()">Get started</button>
      <button class="btn btn-ghost" onclick="skipOnboarding()">Skip for now</button>
    </div>
  </div>
</div>

<!-- Profile detail modal -->
<div class="modal-bg" id="profileModal" onclick="if(event.target===this)closeModal()">
  <div class="modal" id="modalContent"></div>
</div>

<script>
// ── State ──
let file = null, mappings = null, datasetId = null;

// ── Navigation ──
function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.sidebar-nav a').forEach(a => a.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  const nav = document.getElementById('nav-' + name);
  if (nav) nav.classList.add('active');
  if (name === 'datasets') loadDatasets();
}

// ── Upload / Drop ──
const dz = document.getElementById('dropzone');
const fi = document.getElementById('fileInput');
dz.onclick = () => fi.click();
dz.ondragover = e => { e.preventDefault(); dz.classList.add('over'); };
dz.ondragleave = () => dz.classList.remove('over');
dz.ondrop = e => { e.preventDefault(); dz.classList.remove('over'); if(e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]); };
fi.onchange = () => { if(fi.files.length) handleFile(fi.files[0]); };

async function handleFile(f) {
  file = f;
  document.getElementById('dsName').value = f.name.replace(/\.(csv|json|jsonl)$/i, '');
  const fd = new FormData(); fd.append('file', f);
  const r = await fetch('/api/detect-schema', {method:'POST', body:fd});
  const d = await r.json();
  if (d.error) { alert(d.error); return; }
  mappings = d.mappings;
  document.getElementById('uploadFlow').style.display = 'block';
  dz.style.display = 'none';
  goStep(1);
  renderSchema(mappings);
}

// ── Steps ──
function goStep(n) {
  [1,2,3,4].forEach(i => {
    document.getElementById('step'+i).style.display = i===n ? 'block' : 'none';
    const ind = document.getElementById('step-ind-'+i);
    const conn = document.getElementById('step-conn-'+i);
    ind.className = 'step-dot' + (i < n ? ' done' : i === n ? ' active' : '');
    if(conn) conn.className = 'step-connector' + (i < n ? ' done' : '');
  });
}

function resetUpload() {
  file = null; mappings = null; datasetId = null;
  document.getElementById('uploadFlow').style.display = 'none';
  document.getElementById('dropzone').style.display = '';
  fi.value = '';
}

// ── Schema rendering (grouped) ──
function renderSchema(maps) {
  const groups = {
    identity: { label: 'Identity', color: '#2563eb', desc: 'Used to find and identify people', items: [] },
    links:    { label: 'Links', color: '#0891b2', desc: 'Social profiles, websites, resumes', items: [] },
    content:  { label: 'Searchable content', color: '#7c3aed', desc: 'Text that will be embedded for search', items: [] },
    metadata: { label: 'Metadata', color: '#6b7280', desc: 'Used for filtering', items: [] },
    ignore:   { label: 'Ignored', color: '#d1d5db', desc: 'Skipped during import', items: [] },
  };
  maps.forEach((m, i) => {
    const cat = m.field_type.startsWith('identity') ? 'identity'
              : m.field_type.startsWith('link_') ? 'links'
              : m.field_type === 'linkedin_text' ? 'identity'
              : m.field_type === 'content' ? 'content'
              : m.field_type === 'ignore' ? 'ignore' : 'metadata';
    groups[cat].items.push({...m, idx: i});
  });

  const types = [
    ['identity_name','Name'],['identity_email','Email'],['identity_linkedin','LinkedIn URL'],
    ['identity_org','Organization'],['identity_title','Title'],['identity_phone','Phone'],
    ['link_twitter','Twitter/X URL'],['link_website','Website URL'],['link_resume','Resume/CV link'],['link_other','Other link'],
    ['linkedin_text','Pre-enriched LinkedIn'],['content','Searchable text'],['metadata','Metadata'],['ignore','Ignore']
  ];
  const opts = types.map(([v,l]) => `<option value="${v}">${l}</option>`).join('');

  let html = '';
  for (const [key, g] of Object.entries(groups)) {
    if (!g.items.length) continue;
    // For metadata: separate empty fields and collapse them
    let mainItems = g.items;
    let emptyItems = [];
    if (key === 'metadata') {
      mainItems = g.items.filter(it => it.sample_values && it.sample_values.some(v => v && v.trim()));
      emptyItems = g.items.filter(it => !it.sample_values || !it.sample_values.some(v => v && v.trim()));
    }
    const displayCount = key === 'metadata' ? mainItems.length + (emptyItems.length ? ` + ${emptyItems.length} empty` : '') : g.items.length;
    html += `<div class="schema-group">
      <div class="schema-group-header">
        <div class="schema-group-dot" style="background:${g.color}"></div>
        <div class="schema-group-label">${g.label}</div>
        <div class="schema-group-count">${displayCount} field${g.items.length!==1?'s':''} &mdash; ${g.desc}</div>
      </div>`;
    for (const item of mainItems) {
      const sample = (item.sample_values||[]).slice(0,2).map(v => v.length > 50 ? v.slice(0,50)+'...' : v).join(', ');
      html += `<div class="schema-row">
        <div class="schema-col-name">${item.source_column}</div>
        <div class="schema-col-sample">${sample || '<em>empty</em>'}</div>
        <div class="schema-col-type">
          <select onchange="mappings[${item.idx}].field_type=this.value">${opts.replace('value="'+item.field_type+'"','value="'+item.field_type+'" selected')}</select>
        </div>
      </div>`;
    }
    if (emptyItems.length) {
      html += `<div style="padding:6px 12px;font-size:12px;color:var(--text3);cursor:pointer;" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'block':'none';this.textContent=this.nextElementSibling.style.display==='none'?'Show ${emptyItems.length} empty fields...':'Hide empty fields'"">Show ${emptyItems.length} empty fields...</div>`;
      html += `<div style="display:none;">`;
      for (const item of emptyItems) {
        html += `<div class="schema-row" style="opacity:0.5;">
          <div class="schema-col-name">${item.source_column}</div>
          <div class="schema-col-sample"><em>empty</em></div>
          <div class="schema-col-type">
            <select onchange="mappings[${item.idx}].field_type=this.value">${opts.replace('value="'+item.field_type+'"','value="'+item.field_type+'" selected')}</select>
          </div>
        </div>`;
      }
      html += `</div>`;
    }
    html += '</div>';
  }
  document.getElementById('schemaGroups').innerHTML = html;
}

// ── Confirm schema → cost ──
async function confirmSchema() {
  const fd = new FormData();
  fd.append('file', file);
  fd.append('mappings', JSON.stringify(mappings));
  fd.append('name', document.getElementById('dsName').value);
  const r = await fetch('/api/prepare', {method:'POST', body:fd});
  const d = await r.json();
  if (d.error) { alert(d.error); return; }
  datasetId = d.dataset_id;

  // Stats
  const s = d.stats;
  let statsHtml = `
    <div class="stat"><div class="stat-val">${s.total}</div><div class="stat-label">Profiles</div></div>
    <div class="stat"><div class="stat-val">${s.have_linkedin}</div><div class="stat-label">Have LinkedIn</div></div>
    <div class="stat"><div class="stat-val">${s.email_only}</div><div class="stat-label">Email only</div></div>
    <div class="stat"><div class="stat-val">${s.content_fields}</div><div class="stat-label">Content fields</div></div>
  `;
  if (s.skipped_rows > 0) {
    statsHtml += `<div class="stat"><div class="stat-val" style="color:var(--text3)">${s.skipped_rows}</div><div class="stat-label">Rows skipped (empty)</div></div>`;
  }
  document.getElementById('costStats').innerHTML = statsHtml;

  // Cost detail
  const c = d.cost;
  let costHtml = '';
  if (c.linkedin_enrichments) {
    costHtml += `<div class="cost-line"><span>LinkedIn enrichment (${c.linkedin_enrichments} profiles &times; $${c.linkedin_cost_per.toFixed(3)})</span><span>$${c.linkedin_total.toFixed(2)}</span></div>`;
  }
  if (c.identity_lookups) {
    costHtml += `<div class="cost-line"><span>Email &rarr; LinkedIn lookup (${c.identity_lookups} &times; $${c.identity_cost_per.toFixed(4)})</span><span>$${c.identity_total.toFixed(2)}</span></div>`;
  }
  costHtml += `<div class="cost-line"><span>Profile card generation (${c.embedding_profiles} profiles)</span><span>Free</span></div>`;
  costHtml += `<div class="cost-line cost-total"><span>Estimated total</span><span>$${c.total_cost.toFixed(2)}</span></div>`;
  if (c.total_cost > 10) costHtml += `<div class="cost-warn">Significant cost. Consider testing with a smaller sample first.</div>`;
  if (c.total_cost === 0 && c.linkedin_enrichments === 0 && c.identity_lookups === 0) {
    costHtml += `<div style="margin-top:8px;font-size:12px;color:var(--amber);">No LinkedIn URLs or emails found to enrich. Click "Skip enrichment" to build profile cards from the content fields you have.</div>`;
  }

  // Content fields info
  if (s.content_field_names && s.content_field_names.length) {
    costHtml += `<div style="margin-top:12px;font-size:12px;color:var(--text2);">Content fields: <strong>${s.content_field_names.join(', ')}</strong> &mdash; will be summarized into profile cards for LLM scoring.</div>`;
  }
  if (s.duplicates && s.duplicates > 0) {
    costHtml += `<div class="cost-warn">${s.duplicates} profile${s.duplicates>1?'s':''} already exist in other datasets. They will be included but won't be re-enriched.</div>`;
  }

  document.getElementById('costDetail').innerHTML = costHtml;
  goStep(2);
}

// ── Enrichment ──
async function startEnrich() {
  goStep(3);
  const r = await fetch('/api/enrich', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({dataset_id:datasetId})});
  const d = await r.json();
  if (d.error) { alert(d.error); return; }
  currentPollJobId = d.job_id;
  pollJob(d.job_id);
}

let currentPollJobId = null;

async function cancelEnrich() {
  if (currentPollJobId) {
    await fetch('/api/job/' + currentPollJobId + '/cancel', {method:'POST'});
  }
  currentPollJobId = null;
  // Delete the dataset so user can re-upload clean
  if (datasetId) {
    await fetch('/api/dataset/' + datasetId, {method:'DELETE'});
  }
  resetUpload();
  loadDatasets();
}

async function skipEnrich() {
  goStep(3);
  document.getElementById('progressLabel').textContent = 'Building profile cards (no enrichment)...';
  const r = await fetch('/api/embed-only', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({dataset_id:datasetId})});
  const d = await r.json();
  if (d.error) { alert(d.error); return; }
  currentPollJobId = d.job_id;
  pollJob(d.job_id);
}

async function pollJob(jobId) {
  if (currentPollJobId !== jobId) return;
  const r = await fetch('/api/job/' + jobId);
  const d = await r.json();
  const fill = document.getElementById('progressFill');
  const pct = document.getElementById('progressPct');
  const lbl = document.getElementById('progressLabel');

  if (d.status === 'running') {
    const p = Math.round((d.current / Math.max(d.total,1)) * 100);
    fill.style.width = p + '%'; pct.textContent = p + '%';
    lbl.textContent = d.message || 'Enriching...';
    setTimeout(() => pollJob(jobId), 800);
  } else if (d.status === 'embedding') {
    fill.style.width = '85%'; pct.textContent = '85%';
    lbl.textContent = 'Building profile cards...';
    setTimeout(() => pollJob(jobId), 1500);
  } else if (d.status === 'done') {
    fill.style.width = '100%'; fill.style.background = 'var(--green)';
    const st = d.stats || {};
    let doneHtml = '';
    if (st.resolved) doneHtml += `<div class="stat"><div class="stat-val" style="color:var(--accent)">${st.resolved}</div><div class="stat-label">LinkedIn found</div></div>`;
    doneHtml += `<div class="stat"><div class="stat-val" style="color:var(--green)">${st.enriched||0}</div><div class="stat-label">Enriched</div></div>`;
    doneHtml += `<div class="stat"><div class="stat-val">${st.skipped||0}</div><div class="stat-label">Skipped</div></div>`;
    if (st.failed || st.resolve_failed) doneHtml += `<div class="stat"><div class="stat-val">${(st.failed||0)+(st.resolve_failed||0)}</div><div class="stat-label">Failed</div></div>`;
    doneHtml += `<div class="stat"><div class="stat-val">$${(st.total_cost||0).toFixed(2)}</div><div class="stat-label">Cost</div></div>`;
    document.getElementById('doneStats').innerHTML = doneHtml;
    goStep(4);
  } else if (d.status === 'cancelled') {
    return; // stop polling, UI already reset by cancelEnrich()
  } else if (d.status === 'error') {
    lbl.textContent = 'Error: ' + d.message;
    fill.style.background = 'var(--red)';
  }
}

// ── Datasets ──
async function loadDatasets() {
  const r = await fetch('/api/datasets');
  const ds = await r.json();
  const el = document.getElementById('dsList');
  if (!ds.length) {
    el.innerHTML = '<div class="empty"><div class="empty-icon">&#x1F4C1;</div>No datasets yet. Upload one to get started.</div>';
    return;
  }
  el.innerHTML = ds.map(d => `
    <div class="ds-card">
      <div class="ds-icon" onclick="loadDatasetDetail('${d.id}')" style="cursor:pointer;">${(d.name||'?')[0].toUpperCase()}</div>
      <div class="ds-info" onclick="loadDatasetDetail('${d.id}')" style="cursor:pointer;">
        <div class="ds-name">${d.name}</div>
        <div class="ds-meta">${d.profiles} profiles &middot; ${d.created_at ? new Date(d.created_at).toLocaleDateString() : ''}</div>
        <div class="ds-fields">${(d.searchable_fields||[]).map(f=>'<span class="ds-field-tag">'+f+'</span>').join('')}</div>
      </div>
      <div class="ds-actions">
        <button class="btn btn-ghost" style="color:var(--red);font-size:12px;" onclick="event.stopPropagation();deleteDataset('${d.id}','${(d.name||'').replace(/'/g,"\\'")}')">Delete</button>
      </div>
    </div>
  `).join('');
}

async function loadDatasetDetail(id) {
  const r = await fetch('/api/dataset/' + id);
  const d = await r.json();
  if (d.error) { alert(d.error); return; }

  document.getElementById('detailName').textContent = d.name;
  document.getElementById('detailMeta').textContent = `${d.profiles.length} profiles &middot; Source: ${d.source_file.split('/').pop()} &middot; ${new Date(d.created_at).toLocaleDateString()}`;

  const enriched = d.profiles.filter(p=>p.enrichment_status==='enriched').length;
  const pending = d.profiles.filter(p=>p.enrichment_status==='pending').length;
  const hasContent = d.profiles.filter(p=>Object.keys(p.content_fields||{}).length>0).length;
  window._dsDetailId = id;
  document.getElementById('detailStats').innerHTML = `
    <div class="stat"><div class="stat-val">${d.profiles.length}</div><div class="stat-label">Total</div></div>
    <div class="stat"><div class="stat-val" style="color:var(--green)">${enriched}</div><div class="stat-label">Enriched</div></div>
    <div class="stat"><div class="stat-val">${hasContent}</div><div class="stat-label">Have content</div></div>
    <div class="stat"><div class="stat-val">${pending}</div><div class="stat-label">Pending</div></div>
  `;

  window._dsProfiles = d.profiles;
  renderProfiles(d.profiles);
  document.getElementById('profileCount').textContent = d.profiles.length + ' profiles';
  showPage('ds-detail');
}

function renderProfiles(profiles, filter) {
  let list = profiles;
  if (filter) {
    const q = filter.toLowerCase();
    list = profiles.filter(p => {
      const md = p.metadata || {};
      const loc = (md.app_what_is_your_nearest_city||md.city||'') + ' ' + (md.app_country_region_of_residence||md.country||'');
      const title = p.title || md.app_title || '';
      return (p.name||'').toLowerCase().includes(q) || (p.organization||'').toLowerCase().includes(q) || title.toLowerCase().includes(q) || (p.email||'').toLowerCase().includes(q) || loc.toLowerCase().includes(q);
    });
  }
  document.getElementById('profileCount').textContent = list.length + ' profiles';
  document.getElementById('profileList').innerHTML = list.map((p,i) => {
    const md = p.metadata || {};
    const loc = md.app_what_is_your_nearest_city || md.city || md.app_city || '';
    const country = md.app_country_region_of_residence || md.country || md.app_country || '';
    const locStr = [loc, country].filter(Boolean).join(', ');
    const title = p.title || md.app_title || '';
    return `
    <div class="profile-row" onclick="showProfile(${profiles.indexOf(p)})" style="cursor:pointer;">
      <div class="profile-name">${p.name || p.email || 'Unknown'}</div>
      <div class="profile-org">${p.organization || ''}</div>
      <div class="profile-title">${title}</div>
      <div style="color:var(--text3);font-size:12px;min-width:120px;">${locStr}</div>
      <div class="profile-status ${p.enrichment_status}">${p.enrichment_status}</div>
    </div>`;
  }).join('');
}

document.getElementById('profileSearch').oninput = function() {
  renderProfiles(window._dsProfiles, this.value);
};

// ── Profile modal ──
function showProfile(idx) {
  const p = window._dsProfiles[idx];
  let html = `<h2>${p.name || 'Unknown'}</h2>`;
  if (p._dataset_name) html += `<div style="font-size:11px;color:var(--text3);margin-bottom:4px;">From: ${p._dataset_name}</div>`;
  const links = [];
  if (p.email) links.push(p.email);
  if (p.linkedin_url) links.push('<a href="'+p.linkedin_url+'" target="_blank">LinkedIn</a>');
  if (p.twitter_url) links.push('<a href="'+p.twitter_url+'" target="_blank">Twitter/X</a>');
  if (p.website_url) links.push('<a href="'+p.website_url+'" target="_blank">Website</a>');
  if (p.resume_url) links.push('<a href="'+p.resume_url+'" target="_blank">Resume</a>');
  if (p.other_links && p.other_links.length) p.other_links.forEach(l => links.push('<a href="'+l+'" target="_blank">Link</a>'));
  html += `<div class="subtitle">${[p.title, p.organization].filter(Boolean).join(' at ')}${links.length ? ' &middot; ' + links.join(' &middot; ') : ''}</div>`;

  // Enriched LinkedIn data
  const li = p.linkedin_enriched || {};
  if (li.headline) html += `<div class="modal-section"><h3>Headline</h3><p>${li.headline}</p></div>`;
  if (li.summary) html += `<div class="modal-section"><h3>About</h3><p>${li.summary}</p></div>`;
  if (li.experience && li.experience.length) {
    html += `<div class="modal-section"><h3>Experience</h3>`;
    li.experience.forEach(e => {
      html += `<p style="margin-bottom:4px;"><strong>${e.title}</strong> at ${e.company}${e.years?' <span style="color:var(--text3);">('+e.years+')</span>':''}</p>`;
    });
    html += '</div>';
  }
  if (li.education && li.education.length) {
    html += `<div class="modal-section"><h3>Education</h3>`;
    li.education.forEach(e => {
      html += `<p>${e.degree} ${e.field_of_study} &mdash; ${e.school}${e.years?' ('+e.years+')':''}</p>`;
    });
    html += '</div>';
  }

  // Content fields
  const cf = p.content_fields || {};
  for (const [k, v] of Object.entries(cf)) {
    if (v && v.trim()) {
      const label = k.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
      html += `<div class="modal-section"><h3>${label}</h3><p>${v.replace(/\n/g,'<br>')}</p></div>`;
    }
  }

  // Metadata
  const md = p.metadata || {};
  const mdEntries = Object.entries(md).filter(([k,v]) => v);
  if (mdEntries.length) {
    html += `<div class="modal-section"><h3>Metadata</h3>`;
    mdEntries.forEach(([k,v]) => {
      const label = k.replace(/_/g,' ').replace(/\b\w/g, c => c.toUpperCase());
      html += `<p><strong>${label}:</strong> ${v}</p>`;
    });
    html += '</div>';
  }

  // Enrichment log
  const elog = p.enrichment_log || [];
  if (elog.length) {
    html += `<div class="modal-section"><h3>Enrichment Log</h3><div style="font-family:monospace;font-size:11px;line-height:1.6;color:var(--text2);background:#f8f9fa;padding:10px;border-radius:6px;max-height:200px;overflow-y:auto;">`;
    elog.forEach(line => { html += line.replace(/</g,'&lt;') + '<br>'; });
    html += '</div></div>';
  }

  // LinkedIn correction UI
  html += `<div class="modal-section" style="border-top:1px solid var(--border);padding-top:12px;margin-top:12px;">`;
  if (p.linkedin_url) {
    html += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
      <span style="font-size:12px;color:var(--text2);">LinkedIn: <a href="${p.linkedin_url}" target="_blank">${p.linkedin_url.split('/in/')[1] || p.linkedin_url}</a></span>
      <button class="btn btn-ghost" style="font-size:11px;color:var(--red);padding:2px 8px;" onclick="markWrongLinkedin('${p.id}')">Wrong LinkedIn</button>
    </div>`;
  }
  html += `<div id="linkedin-edit-${p.id}" style="${p.linkedin_url ? 'display:none;' : ''}">
    <div style="display:flex;gap:6px;align-items:center;">
      <input type="text" id="linkedin-input-${p.id}" placeholder="Paste LinkedIn URL..." style="flex:1;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font-size:12px;">
      <button class="btn btn-primary" style="font-size:11px;padding:4px 10px;" onclick="setLinkedin('${p.id}')">Set LinkedIn</button>
    </div>
  </div>`;
  html += `</div>`;

  html += `<div class="btn-group"><button class="btn btn-ghost" onclick="closeModal()">Close</button></div>`;
  document.getElementById('modalContent').innerHTML = html;
  document.getElementById('profileModal').classList.add('open');
}

async function markWrongLinkedin(profileId) {
  if (!confirm('Mark this LinkedIn as wrong? It will be cleared.')) return;
  const r = await fetch('/api/profile/' + profileId + '/linkedin', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({linkedin_url: ''})
  });
  const d = await r.json();
  if (d.error) { alert(d.error); return; }
  // Show the input field
  document.getElementById('linkedin-edit-' + profileId).style.display = '';
  // Remove the wrong link display
  const btn = event.target;
  btn.parentElement.innerHTML = '<span style="font-size:12px;color:var(--amber);">LinkedIn cleared. Enter correct URL below.</span>';
}

async function setLinkedin(profileId) {
  const input = document.getElementById('linkedin-input-' + profileId);
  const url = input.value.trim();
  if (!url || !url.includes('linkedin.com/in/')) {
    alert('Please paste a valid LinkedIn URL (must contain linkedin.com/in/)');
    return;
  }
  // Show loading state
  const btn = event.target;
  const origText = btn.textContent;
  btn.textContent = 'Enriching...';
  btn.disabled = true;
  input.disabled = true;
  btn.style.opacity = '0.6';

  try {
    const r = await fetch('/api/profile/' + profileId + '/linkedin', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({linkedin_url: url})
    });
    const d = await r.json();
    if (d.error) { alert(d.error); return; }
    // Refresh the profile modal
    if (d.profile) {
      window._dsProfiles = [d.profile];
      showProfile(0);
    }
  } finally {
    btn.textContent = origText;
    btn.disabled = false;
    input.disabled = false;
    btn.style.opacity = '';
  }
}

function closeModal() { document.getElementById('profileModal').classList.remove('open'); }

async function showSearchProfile(profileId) {
  const r = await fetch('/api/profile/' + profileId);
  const p = await r.json();
  if (p.error) { alert(p.error); return; }
  // Reuse the same modal as the dataset profile viewer
  window._dsProfiles = [p];
  showProfile(0);
}
document.onkeydown = e => { if(e.key==='Escape') closeModal(); };

async function deleteDataset(id, name) {
  if (!confirm(`Delete "${name}"? This cannot be undone.`)) return;
  await fetch('/api/dataset/' + id, {method:'DELETE'});
  loadDatasets();
  // If we were viewing this dataset's detail, go back
  if (document.getElementById('page-ds-detail').classList.contains('active')) {
    showPage('datasets');
  }
}

async function reenrichDataset(id) {
  showPage('upload');
  document.getElementById('uploadFlow').style.display = 'block';
  document.getElementById('dropzone').style.display = 'none';
  datasetId = id;
  currentPollJobId = null;

  // Fetch cost estimate and show step 2
  const r = await fetch('/api/reenrich-estimate', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({dataset_id:id})});
  const d = await r.json();
  if (d.error) { alert(d.error); return; }

  const s = d.stats;
  let statsHtml = `
    <div class="stat"><div class="stat-val">${s.total}</div><div class="stat-label">Profiles</div></div>
    <div class="stat"><div class="stat-val">${s.have_linkedin}</div><div class="stat-label">Have LinkedIn</div></div>
    <div class="stat"><div class="stat-val">${s.email_only}</div><div class="stat-label">Email only</div></div>
    <div class="stat"><div class="stat-val">${s.content_fields}</div><div class="stat-label">Content fields</div></div>
  `;
  document.getElementById('costStats').innerHTML = statsHtml;

  const c = d.cost;
  let costHtml = '';
  costHtml += `<div class="cost-line"><span>Identity resolution (${c.identity_lookups} profiles &times; ~$${c.identity_cost_per.toFixed(3)}/lookup)</span><span>$${c.identity_total.toFixed(2)}</span></div>`;
  costHtml += `<div class="cost-line"><span>LinkedIn enrichment (${c.linkedin_enrichments} profiles &times; $${c.linkedin_cost_per.toFixed(2)})</span><span>$${c.linkedin_total.toFixed(2)}</span></div>`;
  costHtml += `<div class="cost-line"><span>Profile card generation</span><span>Free</span></div>`;
  costHtml += `<div class="cost-line cost-total"><span>Estimated total</span><span>$${c.total_cost.toFixed(2)}</span></div>`;
  if (c.total_cost > 10) costHtml += `<div class="cost-warn">Significant cost. Consider testing with a smaller sample first.</div>`;
  if (s.content_field_names && s.content_field_names.length) {
    costHtml += `<div style="margin-top:12px;font-size:12px;color:var(--text2);">Content fields: <strong>${s.content_field_names.join(', ')}</strong></div>`;
  }
  document.getElementById('costDetail').innerHTML = costHtml;

  // Override the enrich button to call reenrich instead of enrich
  window._reenrichMode = true;
  window._reenrichDatasetId = id;

  goStep(2);
}

// Patch startEnrich to handle reenrich mode
const _origStartEnrich = startEnrich;
startEnrich = async function() {
  if (window._reenrichMode) {
    window._reenrichMode = false;
    // Reset progress UI
    document.getElementById('progressFill').style.width = '0%';
    document.getElementById('progressFill').style.background = 'var(--accent)';
    document.getElementById('progressPct').textContent = '0%';
    document.getElementById('progressLabel').textContent = 'Re-enriching...';
    goStep(3);

    const r = await fetch('/api/reenrich', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({dataset_id: window._reenrichDatasetId})});
    const d = await r.json();
    if (d.error) { alert(d.error); return; }
    currentPollJobId = d.job_id;
    pollJob(d.job_id);
  } else {
    _origStartEnrich();
  }
};

// ── Settings & Onboarding ──
const KEY_DEFS = [
  {id: 'GOOGLE_API_KEY', label: 'Google (Gemini)', hint: 'Powers search scoring, clarifying questions, and feedback synthesis. Get one at aistudio.google.com/apikey (free tier available)', required: true},
  {id: 'BRAVE_API_KEY', label: 'Brave Search', hint: 'For finding LinkedIn profiles from names/emails. Get one at brave.com/search/api (free tier: 2,000 queries/mo)', required: true},
  {id: 'SERPER_API_KEY', label: 'Serper (Google Search)', hint: 'Google search results for better LinkedIn resolution. serper.dev (free tier: 2,500 queries/mo)', required: true},
  {id: 'ENRICHLAYER_API_KEY', label: 'EnrichLayer', hint: 'For pulling full LinkedIn profiles (experience, education). $0.01/profile. enrichlayer.com', required: true},
  {id: 'ANTHROPIC_API_KEY', label: 'Anthropic (Claude)', hint: 'Optional. For LLM-based profile summarization. anthropic.com', required: false},
];

function renderKeyFields(containerId, values) {
  const el = document.getElementById(containerId);
  el.innerHTML = KEY_DEFS.map(k => `
    <div style="margin-bottom:14px;">
      <label style="font-size:12px;font-weight:600;color:var(--text2);display:flex;align-items:center;gap:6px;margin-bottom:3px;">
        ${k.label}
        ${values[k.id] ? '<span style="color:var(--green);font-weight:400;">Connected</span>' : k.required ? '<span style="color:var(--amber);font-weight:400;">Not set</span>' : '<span style="color:var(--text3);font-weight:400;">Optional</span>'}
      </label>
      <input id="key-${k.id}" type="password" value="${values[k.id]||''}" placeholder="${k.id}"
        style="width:100%;padding:7px 10px;border:1px solid var(--border);border-radius:6px;font-size:13px;font-family:monospace;">
      <div style="font-size:11px;color:var(--text3);margin-top:2px;">${k.hint}</div>
    </div>
  `).join('');
}

async function loadKeys() {
  const r = await fetch('/api/keys');
  return await r.json();
}

async function saveKeys() {
  const keys = {};
  KEY_DEFS.forEach(k => {
    const val = document.getElementById('key-' + k.id).value.trim();
    if (val) keys[k.id] = val;
  });
  const r = await fetch('/api/keys', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(keys)});
  const d = await r.json();
  if (d.ok) {
    document.getElementById('keySaveStatus').textContent = 'Saved!';
    setTimeout(() => document.getElementById('keySaveStatus').textContent = '', 2000);
    // Re-render to show Connected status
    const vals = await loadKeys();
    renderKeyFields('keyFields', vals);
  }
}

async function saveOnboardingKeys() {
  const keys = {};
  KEY_DEFS.forEach(k => {
    const el = document.getElementById('key-' + k.id);
    if (el) {
      const val = el.value.trim();
      if (val) keys[k.id] = val;
    }
  });
  if (Object.keys(keys).length) {
    await fetch('/api/keys', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(keys)});
  }
  document.getElementById('onboardingModal').style.display = 'none';
}

function skipOnboarding() {
  document.getElementById('onboardingModal').style.display = 'none';
}

async function initApp() {
  const keys = await loadKeys();

  // Settings page
  renderKeyFields('keyFields', keys);

  // Onboarding: show if no keys are set and no datasets exist
  const ds = await fetch('/api/datasets').then(r => r.json());
  const hasAnyKey = KEY_DEFS.some(k => keys[k.id]);
  if (!hasAnyKey && ds.length === 0) {
    renderKeyFields('onboardingKeys', keys);
    document.getElementById('onboardingModal').style.display = 'flex';
  }

  loadDatasets();
}

// ── Search ──
let sId = null, sPoll = null, sProposal = null;
let sChatSession = null, sChatQuery = '';

function sShow(step) {
  ['picker','new','progress','results'].forEach(s => {
    const el = document.getElementById('search-' + s);
    if (el) el.style.display = 'none';
  });
  const prop = document.getElementById('search-proposal');
  if (prop) prop.style.display = 'none';
  const el = document.getElementById('search-' + step);
  if (el) el.style.display = 'block';
}

async function loadSearchList() {
  const {searches} = await (await fetch('/api/search/searches')).json();
  const el = document.getElementById('search-list');
  if (!searches.length) { el.innerHTML = '<div style="color:var(--text3);font-size:13px;padding:8px 0;">No saved searches yet.</div>'; return; }
  el.innerHTML = searches.map(s => `
    <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 12px;border:1px solid var(--border);border-radius:8px;margin-bottom:6px;cursor:pointer;background:var(--card);" onclick="searchLoad('${s.id}')">
      <div><div style="font-weight:600;font-size:14px;">${s.name}</div>
      <div style="font-size:12px;color:var(--text3);">${s.query.substring(0,80)}${s.query.length>80?'...':''}</div></div>
      <div style="text-align:right;font-size:11px;color:var(--text3);">${s.feedback_count} feedback<br>${s.rule_count} rules${s.has_results?'<br><span style="color:var(--green);">has results</span>':''}<br><a href="#" onclick="event.stopPropagation();searchExport('${s.id}')" style="color:var(--accent);">export</a></div>
    </div>`).join('');
}

async function loadSearchDatasets() {
  const {datasets} = await (await fetch('/api/search/datasets')).json();
  const opts = '<option value="">All datasets</option>' + datasets.map(d => `<option value="${d.id}">${d.name} (${d.profiles})</option>`).join('');
  const sel = document.getElementById('search-dataset');
  if (sel) sel.innerHTML = opts;
  const sel2 = document.getElementById('search-dataset-results');
  if (sel2) sel2.innerHTML = opts;
}

function switchSearchDataset() {
  // Re-run the current search with the newly selected dataset
  if (!sId) return;
  searchRerun();
}

function searchNewFlow() {
  sId = null;
  sChatSession = crypto.randomUUID();
  sChatQuery = '';
  sShow('new');
  document.getElementById('search-name').value = '';
  document.getElementById('search-chat').innerHTML = '';
  document.getElementById('search-chat-input').value = '';
  document.getElementById('search-chat-input').placeholder = 'Describe who you\'re looking for...';
  document.getElementById('search-skip-btn').style.display = 'none';
  loadSearchDatasets();
}

function searchBackToPicker() { sShow('picker'); loadSearchList(); }

async function searchLoad(id) {
  const data = await (await fetch(`/api/search/searches/${id}`)).json();
  sId = id;
  if (data.cache?.scores && Object.keys(data.cache.scores).length) { await searchShowResults(id, data); }
  else { searchNewFlow(); sId = id; document.getElementById('search-name').value = data.name; }
}

function chatAppend(role, text) {
  const chat = document.getElementById('search-chat');
  const isUser = role === 'user';
  chat.innerHTML += `
    <div style="display:flex;justify-content:${isUser?'flex-end':'flex-start'};margin-bottom:10px;">
      <div style="max-width:80%;padding:10px 14px;border-radius:${isUser?'14px 14px 4px 14px':'14px 14px 14px 4px'};background:${isUser?'var(--accent)':'#f3f4f6'};color:${isUser?'white':'var(--text)'};font-size:13px;line-height:1.5;">${text}</div>
    </div>`;
  chat.scrollTop = chat.scrollHeight;
}

async function searchChatSend() {
  const input = document.getElementById('search-chat-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';

  chatAppend('user', text);

  // First message = the query
  const isFirstMessage = !sChatQuery;
  if (isFirstMessage) {
    sChatQuery = text;
    // Auto-set search name from first few words
    if (!document.getElementById('search-name').value) {
      document.getElementById('search-name').value = text.substring(0, 60);
    }
  }

  // Disable input while waiting
  input.disabled = true;
  document.getElementById('search-chat-btn').disabled = true;

  // Show thinking indicator
  chatAppend('assistant', '<span style="color:var(--text3);">Thinking...</span>');

  const res = await fetch('/api/search/chat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      query: sChatQuery,
      session_id: sChatSession,
      answer: isFirstMessage ? null : text,
      search_id: sId,
    })
  });
  const data = await res.json();

  // Remove thinking indicator
  const chat = document.getElementById('search-chat');
  if (chat.lastElementChild) chat.removeChild(chat.lastElementChild);

  if (data.done) {
    // DONE — disable input, show summary, run search
    input.disabled = true;
    document.getElementById('search-chat-btn').disabled = true;
    document.getElementById('search-skip-btn').style.display = 'none';
    chatAppend('assistant', 'Got it — running the search now.');
    searchRun(data.context || data.summary || '');
  } else {
    input.disabled = false;
    document.getElementById('search-chat-btn').disabled = false;
    chatAppend('assistant', data.question);
    input.placeholder = 'Your answer...';
    input.focus();
    document.getElementById('search-skip-btn').style.display = 'inline-block';
  }
}

function searchSkipToScore() {
  searchRun('');
}

async function searchRun(clarification) {
  const name = document.getElementById('search-name').value.trim() || 'Untitled';
  const query = sChatQuery || document.getElementById('search-query')?.value?.trim() || '';
  const dataset_id = document.getElementById('search-dataset-results')?.value || document.getElementById('search-dataset')?.value || undefined;
  if (!query) { alert('No query found'); sShow('new'); return; }
  sShow('progress'); document.getElementById('search-progress-fill').style.width = '0%';
  const data = await (await fetch('/api/search/score', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name, query, search_id:sId, dataset_id, clarification_context:clarification})})).json();
  if (data.error) { alert(data.error); sShow('new'); return; }
  sId = data.search_id;
  pollSearchProgress(sId);
}

function pollSearchProgress(id) {
  if (sPoll) clearInterval(sPoll);
  sPoll = setInterval(async () => {
    const data = await (await fetch(`/api/search/progress/${id}`)).json();
    const pct = data.total > 0 ? Math.round(data.done / data.total * 100) : 0;
    document.getElementById('search-progress-fill').style.width = pct + '%';
    document.getElementById('search-progress-text').textContent = `Scoring... ${data.done}/${data.total}`;
    if (data.status === 'done') { clearInterval(sPoll); searchShowResults(id); }
  }, 800);
}

async function searchShowResults(id, sd) {
  if (!sd) sd = await (await fetch(`/api/search/searches/${id}`)).json();
  const rp = document.getElementById('search-rules-panel');
  if (sd.search_rules?.length) { rp.style.display = 'block'; document.getElementById('search-rules-list').innerHTML = sd.search_rules.map(r => `<div style="padding:3px 0;font-size:12px;">• ${r}</div>`).join(''); }
  else rp.style.display = 'none';

  const {results} = await (await fetch(`/api/search/searches/${id}/results`)).json();
  document.getElementById('search-results-header').innerHTML = `<span id="search-name-display" style="font-weight:600;cursor:pointer;" onclick="searchEditName()" title="Click to rename">${sd.name} <span style="font-size:11px;opacity:0.5;">&#9998;</span></span> — ${results.length} scored (${sd.feedback_log?.length||0} feedback)`;
  document.getElementById('search-results-list').innerHTML = results.slice(0,50).map((r,i) => `
    <div class="card" style="padding:14px;">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <div><span style="font-weight:600;cursor:pointer;text-decoration:underline;text-decoration-color:var(--border);" onclick="showSearchProfile('${r.id}')">${i+1}. ${r.name}</span>${r.linkedin_url?` <a href="${r.linkedin_url}" target="_blank" style="font-size:11px;color:var(--accent);">LinkedIn</a>`:''}</div>
        <span style="font-size:22px;font-weight:700;color:${r.score>=70?'var(--green)':r.score>=50?'var(--amber)':'var(--red)'};">${r.score}</span>
      </div>
      <div style="font-size:12px;color:var(--text2);font-style:italic;margin:6px 0;">${r.reasoning||''}</div>
      <div style="display:flex;gap:4px;align-items:center;margin-top:6px;">
        <button class="btn sm" onclick="sRate('${r.id}',\`${(r.name||'').replace(/`/g,'')}\`,'strong_yes',this)">★</button>
        <button class="btn sm" onclick="sRate('${r.id}',\`${(r.name||'').replace(/`/g,'')}\`,'yes',this)">✓</button>
        <button class="btn sm" onclick="sRate('${r.id}',\`${(r.name||'').replace(/`/g,'')}\`,'no',this)">✗</button>
        <button class="btn sm" onclick="sRate('${r.id}',\`${(r.name||'').replace(/`/g,'')}\`,'strong_no',this)">✗✗</button>
        <button class="btn sm" onclick="sExclude('${r.id}')" title="Remove from results (not negative feedback)" style="margin-left:4px;opacity:0.5;">○</button>
        <input type="text" placeholder="Why?" style="flex:1;padding:4px 8px;border:1px solid var(--border);border-radius:4px;font-size:12px;" id="sr-${r.id}">
        <label style="font-size:10px;color:var(--text3);white-space:nowrap;cursor:pointer;" title="Check this if your feedback applies to ALL searches, not just this one."><input type="checkbox" id="sg-${r.id}"> global <span style="display:inline-block;width:13px;height:13px;border-radius:50%;background:var(--border);text-align:center;font-size:9px;line-height:13px;color:var(--text2);cursor:help;">i</span></label>
      </div>
    </div>`).join('');
  if (data.excluded_count > 0) {
    document.getElementById('search-results-list').innerHTML += `<div style="text-align:center;padding:12px;font-size:12px;color:var(--text3);cursor:pointer;" onclick="sShowExcluded()">${data.excluded_count} hidden profile${data.excluded_count>1?'s':''} — click to show</div>`;
  }
  sShow('results');
}

async function sExclude(profileId) {
  await fetch(\`/api/search/searches/\${sId}/exclude\`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({profile_id: profileId})
  });
  // Remove the card from the DOM
  loadSearchResults(sId);
}

async function sShowExcluded() {
  // Temporarily unexclude all and reload
  const s = await (await fetch(\`/api/search/searches/\${sId}\`)).json();
  const excluded = s.excluded_profile_ids || [];
  for (const pid of excluded) {
    await fetch(\`/api/search/searches/\${sId}/unexclude\`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({profile_id: pid})
    });
  }
  loadSearchResults(sId);
}

async function sRate(pid, pname, rating, btn) {
  const reason = document.getElementById('sr-'+pid)?.value||'';
  const scope = document.getElementById('sg-'+pid)?.checked ? 'global' : 'search';
  const wasSelected = btn.style.background && btn.style.background !== '';
  // Clear all buttons first
  btn.parentElement.querySelectorAll('.btn').forEach(b => b.style.background='');
  if (wasSelected) {
    // Clicking the same button again = unselect (no feedback sent)
    return;
  }
  btn.style.background = rating.includes('yes') ? 'var(--green-light)' : 'var(--red-light)';
  await fetch('/api/search/feedback', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({search_id:sId, profile_id:pid, profile_name:pname, rating, reason, scope})});
}

async function searchRerun() {
  document.getElementById('btn-rerun').textContent = 'Synthesizing...';
  const {proposal} = await (await fetch(`/api/search/searches/${sId}/synthesize`, {method:'POST'})).json();
  document.getElementById('btn-rerun').textContent = 'Re-run with feedback';
  if (proposal && (proposal.new_rules?.length || proposal.add_exemplars?.length || proposal.modified_rules?.length)) {
    sProposal = proposal;
    let h = '';
    if (proposal.new_rules?.length) {
      h += '<div style="font-weight:600;font-size:12px;">New rules:</div>';
      proposal.new_rules.forEach((r, i) => h += `<div style="margin:3px 0;"><input type="text" value="${r.replace(/"/g,'&quot;')}" id="prop-rule-${i}" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font-size:12px;"></div>`);
    }
    if (proposal.add_exemplars?.length) {
      h += '<div style="font-weight:600;font-size:12px;margin-top:4px;">New exemplars:</div>';
      proposal.add_exemplars.forEach(e => h += `<div style="font-size:12px;padding:4px 8px;background:var(--card);border-radius:4px;margin:3px 0;">Score ${e.score}: ${e.profile_name}</div>`);
    }
    if (proposal.notes) h += `<div style="font-size:11px;color:var(--text3);margin-top:4px;">${proposal.notes}</div>`;
    document.getElementById('search-proposal-content').innerHTML = h;
    document.getElementById('search-proposal').style.display = 'block';
    return;
  }
  doSearchRerun();
}

async function searchAcceptProposal() {
  // Read edited rule values from input fields
  if (sProposal.new_rules) {
    sProposal.new_rules = sProposal.new_rules.map((r, i) => {
      const el = document.getElementById(`prop-rule-${i}`);
      return el ? el.value.trim() : r;
    }).filter(r => r); // drop blanks (user cleared it = reject that rule)
  }
  await fetch(`/api/search/searches/${sId}/apply-proposal`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({proposal:sProposal})});
  document.getElementById('search-proposal').style.display = 'none'; sProposal = null; doSearchRerun();
}
function searchRejectProposal() { document.getElementById('search-proposal').style.display = 'none'; sProposal = null; doSearchRerun(); }

async function doSearchRerun() {
  sShow('progress'); document.getElementById('search-progress-fill').style.width = '0%';
  await fetch(`/api/search/searches/${sId}/rerun`, {method:'POST'});
  pollSearchProgress(sId);
}

async function searchEditName() {
  const newName = prompt('Rename search:', document.getElementById('search-name-display')?.textContent?.replace(/\s*✎/, '').trim());
  if (!newName || !sId) return;
  await fetch(`/api/search/searches/${sId}/rename`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name: newName})});
  searchShowResults(sId);
}

// ── Global Rules ──
async function loadGlobalRules() {
  const {rules} = await (await fetch('/api/search/global-rules')).json();
  const el = document.getElementById('global-rules-list');
  if (!rules.length) { el.innerHTML = '<div style="color:var(--text3);font-size:12px;">No global rules yet.</div>'; return; }
  el.innerHTML = rules.map(r => `
    <div style="padding:6px 0;border-bottom:1px solid var(--border);font-size:13px;">
      <div>${r.text}</div>
      <div style="font-size:11px;color:var(--text3);">${r.source === 'seed' ? 'Built-in' : r.source}</div>
    </div>`).join('');
}

async function addGlobalRule() {
  const text = document.getElementById('new-global-rule').value.trim();
  if (!text) return alert('Enter a rule');
  if (!text.toLowerCase().startsWith('when')) {
    if (!confirm('Global rules work best starting with "When [condition]..." so the AI knows when to apply them. Add anyway?')) return;
  }
  await fetch('/api/search/global-rules', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text})});
  document.getElementById('new-global-rule').value = '';
  loadGlobalRules();
}

// ── Search Export/Import ──
async function searchExport(id) {
  const data = await (await fetch(`/api/search/searches/${id}`)).json();
  const blob = new Blob([JSON.stringify(data, null, 2)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = (data.name || 'search').replace(/[^a-z0-9]/gi, '-').toLowerCase() + '.json';
  a.click();
}

function searchImportFile(input) {
  const file = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = async (e) => {
    try {
      const data = JSON.parse(e.target.result);
      await fetch('/api/search/import', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
      loadSearchList();
    } catch(err) { alert('Invalid search JSON: ' + err.message); }
  };
  reader.readAsText(file);
  input.value = '';
}

const _origShowPage = showPage;
showPage = function(name) { _origShowPage(name); if (name === 'search') { loadSearchList(); loadSearchDatasets(); loadGlobalRules(); } };

// Theme toggle
function toggleTheme() {
  const html = document.documentElement;
  const current = html.getAttribute('data-theme');
  const next = current === 'dark' ? 'light' : 'dark';
  html.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
  document.getElementById('theme-toggle').textContent = next === 'dark' ? '☀️' : '🌙';
}
// Apply saved theme on load
(function() {
  const saved = localStorage.getItem('theme') || 'light';
  if (saved === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark');
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = '☀️';
  }
})();

// Init
initApp();
</script>
</body>
</html>"""


# ── API Routes ───────────────────────────────────────────────

ENV_PATH = Path(__file__).parent.parent / ".env"

MANAGED_KEYS = ["GOOGLE_API_KEY", "BRAVE_API_KEY", "SERPER_API_KEY", "ENRICHLAYER_API_KEY", "ANTHROPIC_API_KEY"]


def _load_env() -> dict:
    """Load API keys from .env file."""
    keys = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k in MANAGED_KEYS:
                keys[k] = v
    return keys


def _save_env(keys: dict):
    """Save API keys to .env file and apply to environment."""
    lines = ["# People Search — API keys (auto-generated, do not commit)\n"]
    for k in MANAGED_KEYS:
        v = keys.get(k, "")
        if v:
            lines.append(f"{k}={v}")
            os.environ[k] = v
    ENV_PATH.write_text("\n".join(lines) + "\n")


def _apply_env():
    """Load .env and set environment variables on startup."""
    keys = _load_env()
    for k, v in keys.items():
        if v and not os.environ.get(k):
            os.environ[k] = v


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/keys")
def api_get_keys():
    """Return which keys are set (masked values)."""
    env_keys = _load_env()
    result = {}
    for k in MANAGED_KEYS:
        val = os.environ.get(k, "") or env_keys.get(k, "")
        if val:
            # Return masked version so UI shows "Connected" without exposing the key
            result[k] = val[:4] + "..." + val[-4:] if len(val) > 12 else "***"
        else:
            result[k] = ""
    return jsonify(result)


@app.route("/api/keys", methods=["POST"])
def api_save_keys():
    """Save API keys to .env and apply to environment."""
    data = request.json or {}
    # Merge with existing (don't overwrite keys not in this request)
    current = _load_env()
    for k in MANAGED_KEYS:
        if k in data and data[k]:
            # Don't save the masked placeholder back
            if "..." not in data[k] and "***" not in data[k]:
                current[k] = data[k]
    _save_env(current)
    # Reinitialize pipeline with new keys
    global PIPELINE
    PIPELINE = EnrichmentPipeline(
        data_dir=str(PIPELINE.data_dir),
        enrichlayer_api_key=os.environ.get("ENRICHLAYER_API_KEY", ""),
    )
    return jsonify({"ok": True})


@app.route("/api/detect-schema", methods=["POST"])
def api_detect_schema():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    filepath = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{f.filename}"
    f.save(str(filepath))

    try:
        ms = PIPELINE.detect_schema(filepath)
        return jsonify({
            "mappings": [m.to_dict() for m in ms],
            "filename": filepath.name,
            "rows": _count_rows(filepath),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/prepare", methods=["POST"])
def api_prepare():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    name = request.form.get("name", "")
    raw = json.loads(request.form.get("mappings", "[]"))

    filepath = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{f.filename}"
    f.save(str(filepath))

    try:
        fmaps = [FieldMapping(
            source_column=m["source_column"],
            field_type=FieldType(m["field_type"]),
            target_name=m.get("target_name", m["source_column"]),
            sample_values=m.get("sample_values", []),
            confidence=m.get("confidence", 0.5),
        ) for m in raw]

        dataset, cost = PIPELINE.prepare(filepath, fmaps, name=name)
        PIPELINE.save(dataset)

        have_li = sum(1 for p in dataset.profiles if is_valid_linkedin_url(p.linkedin_url))
        email_only = sum(1 for p in dataset.profiles if p.email and not is_valid_linkedin_url(p.linkedin_url))
        cfields = set()
        for p in dataset.profiles:
            cfields.update(p.content_fields.keys())

        return jsonify({
            "dataset_id": dataset.id,
            "stats": {
                "total": len(dataset.profiles),
                "have_linkedin": have_li,
                "email_only": email_only,
                "content_fields": len(cfields),
                "content_field_names": sorted(cfields),
                "skipped_rows": dataset.enrichment_stats.get("skipped_rows", 0),
                "duplicates": len(dataset.enrichment_stats.get("duplicates", [])),
            },
            "cost_summary": cost.summary(),
            "cost": cost.to_dict(),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 400


@app.route("/api/enrich", methods=["POST"])
def api_enrich():
    data = request.json or {}
    ds_id = data.get("dataset_id")
    if not ds_id:
        return jsonify({"error": "No dataset_id"}), 400

    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"status": "running", "current": 0, "total": 0, "message": "Starting...", "stats": {}}

    def run():
        def cancelled():
            return JOBS[job_id].get("cancel")

        try:
            ds = PIPELINE.load(ds_id)
            JOBS[job_id]["total"] = len(ds.profiles)

            def on_prog(cur, tot, msg):
                JOBS[job_id].update(current=cur, total=tot, message=msg)

            stats = PIPELINE.run_enrichment(ds, on_progress=on_prog)
            PIPELINE.save(ds)
            if cancelled(): return

            JOBS[job_id].update(status="running", message="Fetching link content...")
            link_stats = PIPELINE.fetch_links(ds, on_progress=on_prog)
            stats.update({f"links_{k}": v for k, v in link_stats.items()})
            PIPELINE.save(ds)
            if cancelled(): return

            JOBS[job_id].update(status="embedding", message="Building profile cards...")
            PIPELINE.build_profile_cards(ds)
            PIPELINE.save(ds)

            JOBS[job_id].update(status="done", stats=stats)
        except Exception as e:
            if not cancelled():
                JOBS[job_id].update(status="error", message=str(e))
            import traceback; traceback.print_exc()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/embed-only", methods=["POST"])
def api_embed_only():
    """Skip enrichment, just generate embeddings from uploaded content."""
    data = request.json or {}
    ds_id = data.get("dataset_id")
    if not ds_id:
        return jsonify({"error": "No dataset_id"}), 400

    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"status": "embedding", "current": 0, "total": 0, "message": "Building profile cards...", "stats": {}}

    def run():
        try:
            ds = PIPELINE.load(ds_id)
            # Mark all profiles as skipped (no enrichment attempted)
            from enrichment.models import EnrichmentStatus
            for p in ds.profiles:
                if p.enrichment_status == EnrichmentStatus.PENDING:
                    p.enrichment_status = EnrichmentStatus.SKIPPED

            # Still fetch links if available
            link_stats = PIPELINE.fetch_links(ds)

            # Build profile cards from whatever content we have
            PIPELINE.build_profile_cards(ds)
            PIPELINE.save(ds)
            JOBS[job_id].update(status="done", stats={"enriched": 0, "skipped": len(ds.profiles), "failed": 0, "total_cost": 0})
        except Exception as e:
            JOBS[job_id].update(status="error", message=str(e))
            import traceback; traceback.print_exc()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/job/<job_id>")
def api_job(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({k: v for k, v in job.items() if k != "cancel"})


@app.route("/api/job/<job_id>/cancel", methods=["POST"])
def api_job_cancel(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    job["cancel"] = True
    job["status"] = "cancelled"
    job["message"] = "Cancelled by user"
    return jsonify({"ok": True})


@app.route("/api/datasets")
def api_datasets():
    return jsonify(PIPELINE.list_datasets())


@app.route("/api/dataset/<ds_id>")
def api_dataset_detail(ds_id):
    try:
        ds = PIPELINE.load(ds_id)
        return jsonify({
            "id": ds.id,
            "name": ds.name,
            "created_at": ds.created_at,
            "source_file": ds.source_file,
            "total_rows": ds.total_rows,
            "searchable_fields": ds.searchable_fields,
            "enrichment_stats": ds.enrichment_stats,
            "profiles": [p.to_dict() for p in ds.profiles],
        })
    except FileNotFoundError:
        return jsonify({"error": "Dataset not found"}), 404


@app.route("/api/profile/<profile_id>")
def api_profile(profile_id):
    """Get a single profile by ID (searches across all datasets)."""
    for ds_info in PIPELINE.list_datasets():
        ds = PIPELINE.load(ds_info["id"])
        for p in ds.profiles:
            if p.id == profile_id:
                d = p.to_dict()
                d["_dataset_name"] = ds_info["name"]
                d["_dataset_id"] = ds_info["id"]
                return jsonify(d)
    return jsonify({"error": "Profile not found"}), 404


@app.route("/api/profile/<profile_id>/linkedin", methods=["POST"])
def api_profile_linkedin(profile_id):
    """Update a profile's LinkedIn URL. Optionally re-enriches."""
    from enrichment.models import EnrichmentStatus
    from enrichment.enrichers import normalize_linkedin_url

    data = request.json or {}
    new_url = data.get("linkedin_url", "").strip()

    # Find the profile and its dataset
    for ds_info in PIPELINE.list_datasets():
        ds = PIPELINE.load(ds_info["id"])
        for p in ds.profiles:
            if p.id == profile_id:
                if not new_url:
                    # Clear LinkedIn
                    p.linkedin_url = ""
                    p.linkedin_enriched = {}
                    p.enrichment_status = EnrichmentStatus.FAILED
                    p.enrichment_log.append("LinkedIn manually cleared by user")
                    p.profile_card = ""
                    p.build_raw_text()
                    PIPELINE.save(ds)
                    return jsonify({"status": "cleared"})

                # Set new URL and re-enrich
                p.linkedin_url = new_url
                p.enrichment_log.append(f"LinkedIn manually set to: {new_url}")

                # Enrich via EnrichLayer (skip verification — user provided this URL)
                url = normalize_linkedin_url(new_url)
                api_data = PIPELINE.enricher._call_api(url)

                if api_data and api_data != "OUT_OF_CREDITS":
                    parsed = PIPELINE.enricher._parse_response(api_data)
                    p.linkedin_enriched = parsed
                    p.enrichment_status = EnrichmentStatus.ENRICHED
                    exp_count = len(parsed.get("experience", []))
                    p.enrichment_log.append(f"LinkedIn enriched (manual): {exp_count} experiences, {parsed.get('headline', '')}")

                    # Backfill identity fields
                    if not p.name and parsed.get("full_name"):
                        p.name = parsed["full_name"]
                    if not p.organization and parsed.get("current_company"):
                        p.organization = parsed["current_company"]
                    if not p.title and parsed.get("current_title"):
                        p.title = parsed["current_title"]
                else:
                    p.enrichment_log.append("LinkedIn URL set but EnrichLayer returned no data")
                    p.enrichment_status = EnrichmentStatus.ENRICHED  # URL is correct, just no data

                # Rebuild profile card
                p.build_raw_text()
                PIPELINE.save(ds)
                return jsonify({"status": "enriched", "profile": p.to_dict()})

    return jsonify({"error": "Profile not found"}), 404


@app.route("/api/reenrich-estimate", methods=["POST"])
def api_reenrich_estimate():
    """Estimate costs for re-enriching an existing dataset."""
    data = request.json or {}
    ds_id = data.get("dataset_id")
    if not ds_id:
        return jsonify({"error": "No dataset_id"}), 400
    try:
        ds = PIPELINE.load(ds_id)
        from enrichment.costs import CostBreakdown
        # All profiles will be re-resolved and re-enriched
        have_li = sum(1 for p in ds.profiles if is_valid_linkedin_url(p.linkedin_url))
        have_email = sum(1 for p in ds.profiles if p.email and not is_valid_linkedin_url(p.linkedin_url))
        no_li_no_email = sum(1 for p in ds.profiles if not p.email and not is_valid_linkedin_url(p.linkedin_url))

        cost = CostBreakdown(
            linkedin_enrichments=len(ds.profiles),  # all will be re-enriched after resolution
            identity_lookups=len(ds.profiles),  # all will be re-resolved
            embedding_profiles=len(ds.profiles),
        )
        cfields = set()
        for p in ds.profiles:
            cfields.update(p.content_fields.keys())
        return jsonify({
            "stats": {
                "total": len(ds.profiles),
                "have_linkedin": have_li,
                "email_only": have_email,
                "no_signal": no_li_no_email,
                "content_fields": len(cfields),
                "content_field_names": sorted(cfields),
            },
            "cost": cost.to_dict(),
            "cost_summary": cost.summary(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/reenrich", methods=["POST"])
def api_reenrich():
    """Re-enrich an existing dataset: reset enrichment status and re-run."""
    data = request.json or {}
    ds_id = data.get("dataset_id")
    if not ds_id:
        return jsonify({"error": "No dataset_id"}), 400

    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"status": "running", "current": 0, "total": 0, "message": "Starting re-enrichment...", "stats": {}}

    def run():
        try:
            ds = PIPELINE.load(ds_id)
            # Reset all enrichment statuses to pending
            from enrichment.models import EnrichmentStatus
            for p in ds.profiles:
                p.enrichment_status = EnrichmentStatus.PENDING
                p.linkedin_enriched = {}
                p.profile_card = ""
                p.field_summaries = {}
                p.fetched_content = {}
            PIPELINE.save(ds)

            JOBS[job_id]["total"] = len(ds.profiles)

            def on_prog(cur, tot, msg):
                JOBS[job_id].update(current=cur, total=tot, message=msg)

            # Run full enrichment pipeline
            stats = PIPELINE.run_enrichment(ds, on_progress=on_prog)
            PIPELINE.save(ds)

            # Fetch links
            JOBS[job_id].update(message="Fetching link content...")
            link_stats = PIPELINE.fetch_links(ds, on_progress=on_prog)
            stats.update({f"links_{k}": v for k, v in link_stats.items()})
            PIPELINE.save(ds)

            # Build profile cards
            JOBS[job_id].update(status="embedding", message="Building profile cards...")
            PIPELINE.build_profile_cards(ds)
            PIPELINE.save(ds)

            JOBS[job_id].update(status="done", stats=stats)
        except Exception as e:
            JOBS[job_id].update(status="error", message=str(e))
            import traceback; traceback.print_exc()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/dataset/<ds_id>", methods=["DELETE"])
def api_delete_dataset(ds_id):
    try:
        path = PIPELINE.data_dir / f"{ds_id}.json"
        emb_path = PIPELINE.data_dir / f"{ds_id}_embeddings.npz"
        if path.exists():
            path.unlink()
        if emb_path.exists():
            emb_path.unlink()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


def _count_rows(filepath: Path) -> int:
    try:
        if filepath.suffix.lower() == ".json":
            with open(filepath) as f:
                data = json.load(f)
            return len(data) if isinstance(data, list) else 1
        else:
            with open(filepath) as f:
                return sum(1 for _ in f) - 1
    except Exception:
        return 0


# ── Main ─────────────────────────────────────────────────────

def main():
    global PIPELINE

    parser = argparse.ArgumentParser(description="People Search — Upload & Manage")
    parser.add_argument("--port", type=int, default=5556)
    parser.add_argument("--data-dir", type=str, default=str(Path(__file__).parent.parent / "datasets"))
    args = parser.parse_args()

    # Load saved API keys from .env (won't override env vars already set)
    _apply_env()

    PIPELINE = EnrichmentPipeline(
        data_dir=args.data_dir,
        enrichlayer_api_key=os.environ.get("ENRICHLAYER_API_KEY", ""),
    )

    # Initialize search
    init_search(PIPELINE)

    print(f"People Search: http://localhost:{args.port}")
    print(f"Data dir: {args.data_dir}")
    app.run(host="0.0.0.0", port=args.port, debug=True)


if __name__ == "__main__":
    main()
