from flask import Flask, request, jsonify
from curl_cffi import requests as cf
from datetime import datetime, timezone
import hashlib, base64, json, re

app = Flask(__name__)

EDUSP_HEADERS = {
    "accept": "*/*",
    "accept-language": "pt-BR,pt;q=0.9,en;q=0.8",
    "content-type": "application/json",
    "origin": "https://sed.pca.inf.br",
    "priority": "u=1, i",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
    "sec-gpc": "1",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "x-api-platform": "webclient",
    "x-api-realm": "edusp",
}


def edusp_headers(auth_token):
    h = dict(EDUSP_HEADERS)
    h["x-api-key"] = auth_token
    return h


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def auto_answer(lesson_info):
    questions = lesson_info.get("questions") or []
    answers = {}

    for q in questions:
        qid = q.get("id")
        qtype = q.get("type", "")
        options = q.get("options")

        if qtype == "info" or not q.get("required", False) and options is None:
            continue

        if qtype == "text_ai":
            opts = options or {}
            keywords = opts.get("ai_grading_keywords") or []
            min_chars = opts.get("min_text_count") or 1
            base = " ".join(keywords) if keywords else "resposta"
            text = base
            while len(text) < min_chars:
                text += " " + base
            answers[str(qid)] = text[:2000]

        elif qtype == "fill-words":
            if not isinstance(options, dict):
                continue
            items = options.get("items") or []
            phrase = options.get("phrase") or []
            result = []
            for part in phrase:
                if part.get("type") in ("select", "blank"):
                    ans = part.get("answer")
                    if ans is not None and isinstance(ans, int):
                        result.append(ans)
                    elif items:
                        result.append(0)
            answers[str(qid)] = result if result else [0]

        elif qtype == "order-sentences":
            if not isinstance(options, dict):
                continue
            incorrects = options.get("incorrects") or []
            if isinstance(incorrects, list):
                answers[str(qid)] = [item.get("id") for item in incorrects if isinstance(item, dict) and "id" in item]
            else:
                answers[str(qid)] = []

        elif qtype == "true-false":
            if not isinstance(options, dict):
                continue
            result = {}
            for key in sorted(options.keys(), key=lambda x: int(x) if x.isdigit() else 0):
                opt = options[key]
                if not isinstance(opt, dict):
                    continue
                opt_id = opt.get("id")
                if opt_id is None:
                    continue
                is_correct = opt.get("is_correct") or opt.get("correct") or opt.get("isCorrect")
                result[opt_id] = bool(is_correct)
            if not any(result.values()):
                first_key = next(iter(result), None)
                if first_key:
                    result[first_key] = True
            answers[str(qid)] = result

        elif qtype in ("single", "multi", "cloud"):
            if not isinstance(options, dict):
                continue
            correct_ids = []
            all_ids = []
            for key in sorted(options.keys(), key=lambda x: int(x) if x.isdigit() else 0):
                opt = options[key]
                if not isinstance(opt, dict):
                    continue
                opt_id = opt.get("id")
                if opt_id is None:
                    continue
                all_ids.append(opt_id)
                if opt.get("is_correct") or opt.get("correct") or opt.get("isCorrect"):
                    correct_ids.append(opt_id)
            chosen = correct_ids if correct_ids else (all_ids[:1] if all_ids else [])
            answers[str(qid)] = chosen

        else:
            if isinstance(options, dict) and options:
                first = next(iter(options.values()), None)
                if isinstance(first, dict) and first.get("id"):
                    answers[str(qid)] = [first["id"]]
            elif isinstance(options, list) and options:
                first = options[0]
                opt_id = first.get("id") if isinstance(first, dict) else first
                answers[str(qid)] = [opt_id]

    return answers


@app.route("/tms/apply_direto", methods=["POST"])
def apply_direto():
    body = request.get_json(force=True) or {}
    auth_token = body.get("auth_token")
    task_id = body.get("task_id")
    room_code = body.get("room_code")

    if not all([auth_token, task_id, room_code]):
        return jsonify({"success": False, "error": "auth_token, task_id e room_code obrigatorios"}), 400

    url = f"https://edusp-api.ip.tv/tms/task/{task_id}/apply/?preview_mode=false&room_code={room_code}"
    try:
        res = cf.get(url, headers=edusp_headers(auth_token), impersonate="chrome")
        if not res.ok:
            return jsonify({"success": False, "error": f"HTTP {res.status_code}: {res.text[:300]}"}), res.status_code
        lesson_info = res.json()
        return jsonify({"success": True, "lesson_info": lesson_info})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 502


@app.route("/tms/completar_direto", methods=["POST"])
def completar_direto():
    body = request.get_json(force=True) or {}
    auth_token = body.get("auth_token")
    task_id = body.get("task_id")
    room_code = body.get("room_code")
    lesson_info = body.get("lesson_info")

    if not all([auth_token, task_id, room_code, lesson_info]):
        return jsonify({"success": False, "error": "auth_token, task_id, room_code e lesson_info obrigatorios"}), 400

    answers = auto_answer(lesson_info)
    ts = now_iso()
    payload = {
        "draft": False,
        "time_spent": lesson_info.get("min_execution_time") or 60,
        "answers": answers,
        "accessed_on": ts,
        "executed_on": ts,
    }

    answer_id = (lesson_info.get("answer") or {}).get("id") if isinstance(lesson_info.get("answer"), dict) else None
    if answer_id and answer_id > 0:
        url = f"https://edusp-api.ip.tv/tms/task/{task_id}/answer/{answer_id}?room_code={room_code}"
        method = "PUT"
    else:
        url = f"https://edusp-api.ip.tv/tms/task/{task_id}/answer?room_code={room_code}"
        method = "POST"

    try:
        res = cf.request(
            method,
            url,
            headers=edusp_headers(auth_token),
            data=json.dumps(payload).encode("utf-8"),
            impersonate="chrome",
        )
        if not res.ok:
            return jsonify({"success": False, "error": f"HTTP {res.status_code}: {res.text[:300]}"}), res.status_code
        try:
            result = res.json()
        except Exception:
            result = {"raw": res.text}
        return jsonify({"success": True, "result": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 502


@app.route("/", methods=["GET"])
def health():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
