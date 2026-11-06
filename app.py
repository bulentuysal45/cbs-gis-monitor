import datetime
import os
import threading
import time
from flask import Flask, jsonify, render_template, request, send_from_directory

from main import finalize_and_send_email, load_state, scan_since_last_email
from manage_rules import add_rule, read_all, remove_rule, write_all

app = Flask(__name__)

SCHEDULED_SLOTS = {"08:00", "13:00", "18:00"}
_last_triggered_slot = ""


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


def background_scheduler_loop():
    global _last_triggered_slot
    while True:
        try:
            now_tr = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
            hh_mm = now_tr.strftime("%H:%M")
            day_key = now_tr.strftime("%Y-%m-%d_") + hh_mm
            if hh_mm in SCHEDULED_SLOTS and _last_triggered_slot != day_key:
                _last_triggered_slot = day_key
                finalize_and_send_email(slot_label=f"Otomatik {hh_mm} Raporu")
        except Exception as exc:
            print(f"[ZAMANLAYICI HATASI] {exc}")
        time.sleep(30)


@app.route("/")
def index():
    return send_from_directory("templates", "index.html")


@app.route("/api/agent-reports", methods=["GET"])
def api_agent_reports():
    import json
    if os.path.exists("agent_reports.json"):
        try:
            with open("agent_reports.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                return jsonify({"ok": True, "reports": data.get("reports", [])})
        except Exception:
            pass
    return jsonify({"ok": True, "reports": []})


@app.route("/api/state", methods=["GET"])
def api_state():
    state = load_state()
    rules = read_all()
    return jsonify({
        "ok": True,
        "last_email_at": state.get("last_email_at", "Henüz gönderilmedi"),
        "last_query_at": state.get("last_query_at", "Henüz sorgulanmadı"),
        "items": state.get("pending_items", []),
        "rules": rules,
    })


@app.route("/api/scan", methods=["POST", "OPTIONS"])
def api_scan():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    payload = request.get_json(silent=True) or {}
    konum = (payload.get("konum") or "").strip()
    kelime = (payload.get("kelime") or "").strip()

    items, state = scan_since_last_email(selected_konum=konum, selected_kelime=kelime)
    return jsonify({
        "ok": True,
        "count": len(items),
        "last_email_at": state.get("last_email_at"),
        "last_query_at": state.get("last_query_at"),
        "items": state.get("pending_items", []),
    })


@app.route("/api/send-email-now", methods=["POST", "OPTIONS"])
def api_send_email_now():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    sent, count = finalize_and_send_email(slot_label="Web Arayüzünden Manuel Gönderim")
    state = load_state()
    return jsonify({
        "ok": True,
        "sent": sent,
        "count": count,
        "last_email_at": state.get("last_email_at"),
    })


@app.route("/api/rules/add", methods=["POST", "OPTIONS"])
def api_add_rule():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or request.form
    kelime = (data.get("kelime") or "").strip()
    dil = (data.get("dil") or "TÜRKÇE").strip()
    konum = (data.get("konum") or "TÜRKİYE").strip()
    kural_tipi = (data.get("kural_tipi") or "TAM_ESLESTIRME").strip()
    baglam = (data.get("baglam_kelimeleri") or "").strip()
    haric = (data.get("haric_kelimeler") or "").strip()

    if kelime:
        add_rule(kelime, dil, konum, kural_tipi, baglam, haric)
    return jsonify({"ok": True, "rules": read_all()})


@app.route("/api/rules/toggle/<int:idx>", methods=["POST", "OPTIONS"])
def api_toggle_rule(idx: int):
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    rows = read_all()
    if 1 <= idx <= len(rows):
        curr = rows[idx - 1].get("aktif", "1").strip()
        rows[idx - 1]["aktif"] = "0" if curr == "1" else "1"
        write_all(rows)
    return jsonify({"ok": True, "rules": rows})


@app.route("/api/rules/delete/<int:idx>", methods=["POST", "OPTIONS"])
def api_delete_rule(idx: int):
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    remove_rule(idx)
    return jsonify({"ok": True, "rules": read_all()})


if __name__ == "__main__":
    scheduler_thread = threading.Thread(target=background_scheduler_loop, daemon=True)
    scheduler_thread.start()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
