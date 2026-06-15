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
from daily_candidate_scanner import SUPPORTED_FIELDS, SUPPORTED_OPERATORS, normalize_rules
from market_hours import get_us_market_session
from performance_analytics import generate_performance_report


CONFIG_DIR = BASE_DIR / "config"
RISK_CONFIG = BASE_DIR / "risk_config.py"
LOG_DIR = BASE_DIR / "logs"

CSV_FILES = {
    "candidates": "candidates.csv",
    "strong_candidates": "strong_candidates.csv",
    "order_candidates": "order_candidates.csv",
    "orders": "order_history.csv",
    "gpt": "gpt_candidate_analysis.csv",
    "performance_summary": "performance_summary.csv",
    "performance_trades": "performance_trades.csv",
    "strategy_performance": "strategy_performance.csv",
}

UI_LABELS = {
    "candidates": "후보 종목",
    "strong_candidates": "수급 강한 후보",
    "order_candidates": "주문 검토 후보",
    "orders": "주문 내역",
    "gpt": "AI 분석",
    "performance_summary": "성과 요약",
    "performance_trades": "성과 거래 내역",
    "strategy_performance": "전략 성과",
    "Broker": "거래 모드",
    "Market": "시장 상태",
    "systemd": "서비스 상태",
    "Live Guard": "실거래 보호",
    "Win Rate": "승률",
    "Profit Factor": "손익비",
    "Daily Return": "일일 수익률",
    "Open P/L": "미실현 손익",
    "Open Positions": "보유 종목",
    "Total Orders": "총 주문 수",
    "Filled Orders": "체결 주문",
    "Canceled Orders": "취소 주문",
    "Rejected Orders": "거절 주문",
    "trend": "Trend",
    "trend_score": "Trend Score",
    "momentum_score": "Momentum Score",
    "breakout_score": "Breakout Score",
    "final_score": "Final Score",
}

VALUE_LABELS = {
    "PAPER": "모의투자",
    "LIVE_DRY_RUN": "실거래 예행연습",
    "LIVE_DISABLED": "실거래 비활성",
    "LIVE_ENABLED": "실거래 활성",
    "premarket": "프리마켓",
    "regular": "정규장",
    "afterhours": "애프터마켓",
    "closed": "장 마감",
    "active": "정상 실행",
    "inactive": "중지",
    "failed": "오류",
    "Locked": "실거래 차단",
}

TABLE_HINTS = {
    "candidates": "현재 조건을 통과한 종목 목록입니다.",
    "strong_candidates": "수급 조건이 강하게 포착된 후보 종목입니다.",
    "order_candidates": "실제 주문 전 최종 검토 대상입니다.",
    "orders": "Paper Trading 주문 기록입니다.",
    "gpt": "AI 분석 결과입니다.",
    "performance_summary": "Paper Trading 성과 요약입니다.",
    "performance_trades": "성과 분석에 사용된 거래 목록입니다.",
}

FILTER_FIELDS = sorted(SUPPORTED_FIELDS)
FILTER_OPERATORS = [">=", "<=", ">", "<", "==", "!=", "between", "in", "not_in"]

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

    @app.context_processor
    def inject_ui_helpers():
        return {
            "ui_label": ui_label,
            "value_label": value_label,
        }

    @app.route("/")
    def index():
        broker = BrokerConfig()
        counts = {key: len(read_csv(filename)) for key, filename in CSV_FILES.items()}
        performance_summary = read_latest_performance_summary()
        return render_template(
            "index.html",
            counts=counts,
            broker=broker,
            market_session=get_us_market_session(),
            systemd_status=get_systemd_status(),
            cron_status=get_cron_status(),
            performance_summary=performance_summary,
        )

    @app.route("/table/<name>")
    def table(name):
        if name not in CSV_FILES:
            return redirect(url_for("index"))
        df = read_csv(CSV_FILES[name])
        if name == "gpt":
            df = ensure_ai_provider_columns(df)
        return render_template(
            "table.html",
            name=name,
            title=ui_label(name),
            hint=TABLE_HINTS.get(name, ""),
            rows=df.to_dict("records"),
            columns=df.columns.tolist(),
        )

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

    @app.route("/performance")
    def performance():
        api_error = ""
        try:
            summary, trades = generate_performance_report()
            api_error = summary.get("api_error", "")
        except Exception as exc:
            api_error = str(exc)
            summary = read_latest_performance_summary()
            trades = read_csv("performance_trades.csv")
        has_data = bool(summary)
        if not trades.empty and "status" in trades.columns:
            trades = trades[trades["status"].astype(str).str.lower() == "filled"]
        recent_trades = trades.head(25).to_dict("records") if not trades.empty else []
        columns = trades.columns.tolist() if not trades.empty else []
        return render_template(
            "performance.html",
            summary=summary,
            trades=recent_trades,
            columns=columns,
            trend_stats=summary.get("trend_stats", []),
            momentum_stats=summary.get("momentum_stats", []),
            breakout_stats=summary.get("breakout_stats", []),
            has_data=has_data,
            api_error=api_error,
            broker=BrokerConfig(),
        )

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
                flash("스캐너 규칙이 저장되었습니다.")
            elif section == "preset":
                preset = request.form.get("active_preset")
                if preset in presets:
                    selected = presets[preset].copy()
                    selected.pop("description", None)
                    rules.update(selected)
                    rules["active_preset"] = preset
                    write_json(rules_path, rules)
                    flash(f"프리셋이 선택되었습니다: {preset}")
            elif section == "filter_add":
                rules = normalize_rules(rules)
                rules["filters"].append(parse_filter_form(request.form))
                write_json(rules_path, rules)
                flash("스캐너 필터가 추가되었습니다.")
            elif section == "filter_update":
                rules = normalize_rules(rules)
                index = int(request.form.get("filter_index", -1))
                if 0 <= index < len(rules["filters"]):
                    rules["filters"][index] = parse_filter_form(request.form)
                    write_json(rules_path, rules)
                    flash("스캐너 필터가 수정되었습니다.")
            elif section == "filter_delete":
                rules = normalize_rules(rules)
                index = int(request.form.get("filter_index", -1))
                if 0 <= index < len(rules["filters"]):
                    rules["filters"].pop(index)
                    write_json(rules_path, rules)
                    flash("스캐너 필터가 삭제되었습니다.")
            elif section == "risk":
                update_risk_config(request.form)
                flash("리스크 설정이 저장되었습니다. 실거래 보호 설정은 변경되지 않았습니다.")
            return redirect(url_for("settings"))

        rules = normalize_rules(rules)
        return render_template(
            "settings.html",
            rules=rules,
            presets=presets,
            risk_values=risk_values,
            broker=BrokerConfig(),
            filter_fields=FILTER_FIELDS,
            filter_operators=FILTER_OPERATORS,
        )

    return app


def ui_label(value):
    return UI_LABELS.get(value, str(value).replace("_", " "))


def value_label(value):
    return VALUE_LABELS.get(str(value), str(value))


def read_csv(filename):
    path = BASE_DIR / filename
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def ensure_ai_provider_columns(df):
    if df.empty:
        for column in ["provider", "model"]:
            if column not in df.columns:
                df[column] = []
        return df
    if "provider" not in df.columns:
        df["provider"] = "fallback"
    df["provider"] = df["provider"].fillna("fallback").replace("", "fallback")
    if "model" not in df.columns:
        df["model"] = "fallback"
    df["model"] = df["model"].fillna("fallback").replace("", "fallback")
    return df


def read_json(path, fallback):
    if not path.exists():
        return fallback
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def read_latest_performance_summary():
    df = read_csv("performance_summary.csv")
    if df.empty:
        return {}
    return df.tail(1).to_dict("records")[0]


def coerce_rules(form, current):
    updated = normalize_rules(current).copy()
    for key, value in form.items():
        if key == "section":
            continue
        if key in {"active_preset", "filters"}:
            continue
        if key in updated:
            updated[key] = coerce_number(value)
    return updated


def parse_filter_form(form):
    operator = form.get("operator")
    rule_filter = {
        "field": form.get("field", "").strip(),
        "operator": operator,
    }
    if operator == "between":
        min_value = coerce_optional_number(form.get("min", ""))
        max_value = coerce_optional_number(form.get("max", ""))
        if min_value is not None:
            rule_filter["min"] = min_value
        if max_value is not None:
            rule_filter["max"] = max_value
    else:
        value = form.get("value", "")
        if operator in {"in", "not_in"}:
            rule_filter["value"] = [item.strip() for item in value.split(",") if item.strip()]
        elif value.lower() in {"true", "false"}:
            rule_filter["value"] = value.lower() == "true"
        else:
            rule_filter["value"] = coerce_number(value)
    return rule_filter


def coerce_optional_number(value):
    if value is None or str(value).strip() == "":
        return None
    return coerce_number(value)


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
        return output[-1000:] if output else "출력 없음"
    except Exception as exc:
        return f"사용 불가: {exc}"


def get_systemd_status():
    return run_command(["systemctl", "is-active", "order-monitor.service"])


def get_cron_status():
    return run_command(["crontab", "-l"])


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
