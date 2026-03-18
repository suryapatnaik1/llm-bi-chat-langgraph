"""Dashboard HTML rendering and report file management."""
import json
import logging
import re
import time
from pathlib import Path

from config import REPORTS_DIR

_logger = logging.getLogger(__name__)

# Load Chart.js once at import time so it's embedded inline in every report.
# This avoids CDN dependency — critical for rendering inside Streamlit's sandboxed iframe.
_CHARTJS_PATH = Path(__file__).parent.parent / "static" / "chart.umd.min.js"
_CHARTJS_SRC = _CHARTJS_PATH.read_text(encoding="utf-8") if _CHARTJS_PATH.exists() else ""

CHART_COLORS = [
    "rgba(99,102,241,0.85)",
    "rgba(16,185,129,0.85)",
    "rgba(245,158,11,0.85)",
    "rgba(239,68,68,0.85)",
    "rgba(59,130,246,0.85)",
    "rgba(168,85,247,0.85)",
    "rgba(20,184,166,0.85)",
    "rgba(251,146,60,0.85)",
]

_DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <script>__CHARTJS__</script>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         background:#f0f2f5;padding:24px 16px;color:#1e293b}
    .page{max-width:1100px;margin:0 auto}
    header{margin-bottom:24px}
    header h1{font-size:1.4rem;font-weight:700;color:#1e293b}
    header p{font-size:.875rem;color:#64748b;margin-top:4px}
    .kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-bottom:24px}
    .kpi{background:#fff;border-radius:10px;padding:16px 20px;
         box-shadow:0 1px 6px rgba(0,0,0,.07)}
    .kpi-label{font-size:.75rem;color:#64748b;text-transform:uppercase;letter-spacing:.05em}
    .kpi-value{font-size:1.6rem;font-weight:700;margin:4px 0}
    .kpi-change{font-size:.8rem;font-weight:500}
    .kpi-change.up{color:#16a34a}
    .kpi-change.down{color:#dc2626}
    .charts{display:grid;grid-template-columns:repeat(auto-fill,minmax(460px,1fr));gap:16px}
    .chart-card{background:#fff;border-radius:10px;padding:20px 24px;
                box-shadow:0 1px 6px rgba(0,0,0,.07)}
    .chart-card h2{font-size:.95rem;font-weight:600;margin-bottom:16px;color:#334155}
    .chart-wrap{position:relative;height:280px}
  </style>
</head>
<body>
<div class="page">
  <header>
    <h1 id="dash-title"></h1>
    <p id="dash-subtitle"></p>
  </header>
  <div class="kpis" id="kpi-row"></div>
  <div class="charts" id="chart-grid"></div>
</div>
<script>
const dash = __DASHBOARD_JSON__;
const palette = __PALETTE_JSON__;

document.getElementById("dash-title").textContent = dash.title || "BI Dashboard";
document.getElementById("dash-subtitle").textContent = dash.subtitle || "";

const kpiRow = document.getElementById("kpi-row");
(dash.kpis || []).forEach(k => {
  const up = k.up !== false && !String(k.change || "").startsWith("-");
  kpiRow.innerHTML += `
    <div class="kpi">
      <div class="kpi-label">${k.label}</div>
      <div class="kpi-value">${k.value}</div>
      ${k.change ? `<div class="kpi-change ${up?"up":"down"}">${up?"▲":"▼"} ${k.change}</div>` : ""}
    </div>`;
});

const grid = document.getElementById("chart-grid");
const charts = dash.charts || [];
grid.innerHTML = charts.map((cfg, idx) =>
  '<div class="chart-card">' +
  '<h2>' + (cfg.title || "") + '</h2>' +
  '<div class="chart-wrap"><canvas id="chart-' + idx + '"></canvas></div>' +
  '</div>'
).join("");

charts.forEach((cfg, idx) => {
  const isPie = cfg.type === "pie" || cfg.type === "doughnut";
  cfg.data.datasets.forEach((ds, di) => {
    if (isPie) {
      ds.backgroundColor = palette;
      ds.borderColor = "#fff";
      ds.borderWidth = 2;
    } else {
      ds.backgroundColor = ds.backgroundColor || palette[di % palette.length];
      ds.borderColor = palette[di % palette.length].replace("0.85","1");
      ds.borderWidth = 1;
    }
  });
  new Chart(document.getElementById("chart-" + idx), {
    type: cfg.type || "bar",
    data: cfg.data,
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: isPie ? "right" : "top" },
        tooltip: { mode: isPie ? "point" : "index", intersect: false }
      },
      scales: isPie ? {} : {
        x: { title: { display: !!cfg.x_label, text: cfg.x_label||"" }, grid: { display:false } },
        y: { title: { display: !!cfg.y_label, text: cfg.y_label||"" }, beginAtZero:true,
             grid: { color:"rgba(0,0,0,.05)" } }
      }
    }
  });
});
</script>
</body>
</html>"""


def render_dashboard(dashboard_json: dict) -> str:
    """Render dashboard JSON into a complete, self-contained HTML string.

    Chart.js is embedded inline so the HTML works inside Streamlit's
    sandboxed iframe (no CDN dependency).
    """
    return (
        _DASHBOARD_TEMPLATE
        .replace("__CHARTJS__", _CHARTJS_SRC)
        .replace("__TITLE__", dashboard_json.get("title", "BI Dashboard"))
        .replace("__DASHBOARD_JSON__", json.dumps(dashboard_json))
        .replace("__PALETTE_JSON__", json.dumps(CHART_COLORS))
    )


def save_report(html: str) -> str:
    """Save HTML to disk and return a URL path."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"report_{int(time.time())}.html"
    (REPORTS_DIR / filename).write_text(html, encoding="utf-8")
    _logger.info("Saved report %s", filename)
    return f"/app/static/reports/{filename}"


def parse_dashboard_response(text: str) -> tuple[dict | None, str]:
    """Extract dashboard-data JSON block from Claude's response.

    Returns:
        (dashboard_dict, remaining_text) if found, else (None, original_text).
    """
    match = re.search(r"```dashboard-data\s*(\{.*\})\s*```", text, re.DOTALL)
    if match:
        try:
            dash = json.loads(match.group(1))
            summary = text[match.end():].strip()
            return dash, summary
        except json.JSONDecodeError as exc:
            _logger.warning("Dashboard JSON parse error: %s", exc)
    return None, text
