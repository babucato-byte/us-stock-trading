import ast
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
from flask import Flask, flash, redirect, render_template, request, url_for

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from broker import BrokerConfig
from market_hours import get_us_market_session


CONFIG_DIR = BASE_DIR / "config"
RISK_CONFIG = BASE_DIR / "risk_config.py"
LOG_DIR = BASE_DIR / "logs"

CSV_FILES = {
    "candidates": "candidates.csv",
    "strong_candidates": "strong_candidates.csv",
    "order_candidates": "order_candidates.csv",
    "orders": "order_history.csv",
    "gpt": "gpt_candidate_analysis.csv",
}

EDITABLE_RISK_KEYS = {
    "MAX_DAILY_LOSS_RATE",
    "MAX_POSITION_RATE",
    "MAX_TRADES_PER_DAY",
    "MAX_OPEN_POSITIONS",
    "TAKE_PROFIT_RATE",
    "STOP_LOSS_RATE",
}


def create_app():
    app = Flask(__name__)
    app.secret_key = "local-dashboard-only"

    @app.route("/")
    def index():
        broker = BrokerConfig()
        counts = {key: len(read_csv(filename)) for key, filename in CSV_FILES.items()}
        return render_template(
            "index.html",
            counts=counts,
            broker=broker,
            market_session=get_us_market_session(),
            systemd_status=get_systemd_status(),
            cron_status=get_cron_status(),
        )

    @app.route("/table/<name>")
    def table(name):
        if name not in CSV_FILES:
            return redirect(url_for("index"))
        df = read_csv(CSV_FILES[name])
        return render_template("table.html", name=name, rows=df.to_dict("records"), columns=df.columns.tolist())

    @app.route("/logs")
    def logs():
        files = sorted(LOG_DIR.glob("*.log")) if LOG_DIR.exists() else []
        selected = request.args.get("file")
        content = ""
        if selected:
            path = (LOG_DIR / selected).resolve()
            if LOG_DIR.resolve() in path.parents and path.exists():
                content = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-300:])
        return render_template("logs.html", files=[f.name for f in files], selected=selected, content=content)

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        rules_path = CONFIG_DIR / "scanner_rules.json"
        presets_path = CONFIG_DIR / "scanner_presets.json"
        rules = read_json(rules_path, {})
        presets = read_json(presets_path, {})
        risk_values = read_risk_values()

        if request.method == "POST":
            section = request.form.get("section")
            if section == "rules":
                updated = coerce_rules(request.form, rules)
                write_json(rules_path, updated)
                flash("Scanner rules saved.")
            elif section == "preset":
                preset = request.form.get("active_preset")
                if preset in presets:
                    rules["active_preset"] = preset
                    write_json(rules_path, rules)
                    flash(f"Preset selected: {preset}")
            elif section == "risk":
                update_risk_config(request.form)
                flash("Risk settings saved. Live guardrails were not modified.")
            return redirect(url_for("settings"))

        return render_template("settings.html", rules=rules, presets=presets, risk_values=risk_values, broker=BrokerConfig())

    return app


def read_csv(filename):
    path = BASE_DIR / filename
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def read_json(path, fallback):
    if not path.exists():
        return fallback
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def coerce_rules(form, current):
    updated = current.copy()
    for key, value in form.items():
        if key == "section":
            continue
        if key == "ma200_required":
            updated[key] = value == "true"
        elif key in current:
            updated[key] = coerce_number(value)
    if "ma200_required" not in form:
        updated["ma200_required"] = False
    return updated


def coerce_number(value):
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except Exception:
        return value


def read_risk_values():
    values = {}
    tree = ast.parse(RISK_CONFIG.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in EDITABLE_RISK_KEYS:
                values[name] = ast.literal_eval(node.value)
    return values


def update_risk_config(form):
    lines = RISK_CONFIG.read_text(encoding="utf-8").splitlines()
    updated_lines = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key in EDITABLE_RISK_KEYS and key in form:
            suffix = ""
            if "#" in line:
                suffix = "  #" + line.split("#", 1)[1]
            updated_lines.append(f"{key} = {coerce_number(form[key])}{suffix}")
        else:
            updated_lines.append(line)
    RISK_CONFIG.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")


def run_command(command):
    try:
        result = subprocess.run(command, cwd=BASE_DIR, capture_output=True, text=True, timeout=5)
        output = (result.stdout or result.stderr).strip()
        return output[-1000:] if output else "No output"
    except Exception as exc:
        return f"Unavailable: {exc}"


def get_systemd_status():
    return run_command(["systemctl", "is-active", "order-monitor.service"])


def get_cron_status():
    return run_command(["crontab", "-l"])


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
