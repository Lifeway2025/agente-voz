# app.py
import os
import io
import json
import time
import uuid
import logging
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any, Dict, Optional

import requests
from flask import Flask, request, Response, abort, send_file, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client as TwilioClient

# ======================
#   CONFIG / ENV VARS
# ======================

# OpenAI
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Twilio (WhatsApp + Voice)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "")
# Prioriza el +34; compatibilidad con TWILIO_NUMBER antiguo
TWILIO_PHONE_E164  = os.getenv("TWILIO_PHONE_E164") or os.getenv("TWILIO_NUMBER", "+34930348966")
MESSAGING_SERVICE_SID = os.getenv("MESSAGING_SERVICE_SID", "")  # MG...

# ElevenLabs (compatibles con nombres previos)
ELEVEN_API_KEY  = os.getenv("ELEVEN_API_KEY") or os.getenv("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID") or os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

# Monday.com (compatibles con nombres previos)
MONDAY_API_KEY  = os.getenv("MONDAY_API_KEY") or os.getenv("monday_api", "")
MONDAY_API_URL  = "https://api.monday.com/v2"
MONDAY_BOARD_ID = int(os.getenv("MONDAY_BOARD_ID", "0") or "0")

# Email por SMTP (OVH)
MAIL_SMTP_HOST     = env_int("MAIL_SMTP_PORT", 587)     # ej: ssl0.ovh.net
MAIL_SMTP_PORT     = int(os.getenv("MAIL_SMTP_PORT", "587"))
MAIL_SMTP_USER     = os.getenv("MAIL_SMTP_USER", "")     # ej: usuario@dominio
MAIL_SMTP_PASS     = os.getenv("MAIL_SMTP_PASS", "")
MAIL_SMTP_STARTTLS = (os.getenv("MAIL_SMTP_STARTTLS", "true").lower() == "true")
MAIL_FROM_EMAIL    = os.getenv("MAIL_FROM_EMAIL", "no-reply@gcaconsulting.es")
MAIL_FROM_NAME     = os.getenv("MAIL_FROM_NAME", "GCA Consulting")

# Marca / seguridad
BRAND_NAME = os.getenv("BRAND_NAME", "GCA Consulting")
OPS_TOKEN  = os.getenv("OPS_TOKEN", "")  # para endpoints /ops/*

# ======================
#   APP & GLOBALS
# ======================

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gca")

AUDIO_STORE: Dict[str, bytes] = {}  # cache de audios TTS
_twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ======================
#   HELPERS
# ======================

def ok_json(data: Any, code: int = 200):
    return Response(json.dumps(data, ensure_ascii=False), status=code, mimetype="application/json")

def _openai_chat(messages, temperature=0.2, response_format: Optional[Dict]=None) -> str:
    """Llama a OpenAI Chat Completions (HTTP simple)."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured")
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": OPENAI_MODEL, "messages": messages, "temperature": temperature}
    if response_format:
        payload["response_format"] = response_format
    r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def plan_intent(user_text: str) -> Dict[str, Any]:
    """Planner → devuelve JSON con intent y slots."""
    sys = (
        "Eres un planificador. Devuelve SOLO un JSON válido con:\n"
        "{'intent':'chitchat|search_property|send_whatsapp|send_email|help',"
        " 'property_id':str|null,'phone':str|null,'email':str|null,'message':str|null}"
    )
    try:
        out = _openai_chat(
            [{"role": "system", "content": sys},
             {"role": "user", "content": user_text}],
            temperature=0.1
        )
        return json.loads(out)
    except Exception as e:
        logger.exception("plan_intent error: %s", e)
        return {"intent": "chitchat", "message": user_text, "property_id": None, "phone": None, "email": None}

def eleven_tts_to_bytes(text: str) -> bytes:
    """Genera audio MP3 con ElevenLabs (si hay API Key)."""
    if not ELEVEN_API_KEY:
        return b""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    headers = {"xi-api-key": ELEVEN_API_KEY, "accept": "audio/mpeg", "content-type": "application/json"}
    payload = {"text": text, "model_id": "eleven_multilingual_v2",
               "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    return r.content

def monday_query(query: str, variables: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    if not MONDAY_API_KEY:
        raise RuntimeError("MONDAY_API_KEY not configured")
    headers = {"Authorization": MONDAY_API_KEY, "Content-Type": "application/json"}
    body = {"query": query, "variables": variables or {}}
    r = requests.post(MONDAY_API_URL, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"Monday error: {data['errors']}")
    return data["data"]

def monday_get_property_by_id(board_id: int, item_id: int) -> Dict[str, Any]:
    q = """
    query($board:Int!, $item:Int!) {
      items(ids: [$item]) {
        id
        name
        column_values { id text value }
        board { id name }
      }
    }"""
    data = monday_query(q, {"board": board_id, "item": item_id})
    items = data.get("items") or []
    return items[0] if items else {}

def build_property_summary(item: Dict[str, Any]) -> str:
    if not item:
        return "No he encontrado esa propiedad."
    kv = {cv["id"]: cv.get("text") for cv in (item.get("column_values") or [])}
    nombre = item.get("name") or "Propiedad"
    precio = kv.get("price") or kv.get("precio") or "—"
    direccion = kv.get("direccion") or kv.get("address") or "—"
    metros = kv.get("metros") or kv.get("sqm") or "—"
    return f"{nombre}\nDirección: {direccion}\nPrecio: {precio}\nSuperficie: {metros} m²"

def send_whatsapp(to_e164: str, body: Optional[str]=None,
                  content_sid: Optional[str]=None,
                  content_vars: Optional[Dict[str,Any]]=None) -> str:
    """Envía WhatsApp con texto libre o plantilla."""
    params = {"to": f"whatsapp:{to_e164}", "from_": f"whatsapp:{TWILIO_PHONE_E164}"}
    if MESSAGING_SERVICE_SID:
        params["messaging_service_sid"] = MESSAGING_SERVICE_SID
    if content_sid:
        params["content_sid"] = content_sid
        if content_vars:
            params["content_variables"] = json.dumps(content_vars, ensure_ascii=False)
    else:
        params["body"] = body or ""
    msg = _twilio.messages.create(**params)
    return msg.sid

def send_email(to_email: str, subject: str, html: str) -> Dict[str, Any]:
    """Envío de email por SMTP (OVH)."""
    if not MAIL_SMTP_HOST:
        raise RuntimeError("MAIL_SMTP_* no configurado")
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((MAIL_FROM_NAME, MAIL_FROM_EMAIL))
    msg["To"] = to_email
    try:
        with smtplib.SMTP(MAIL_SMTP_HOST, MAIL_SMTP_PORT, timeout=30) as s:
            if MAIL_SMTP_STARTTLS:
                s.starttls()
            if MAIL_SMTP_USER or MAIL_SMTP_PASS:
                s.login(MAIL_SMTP_USER, MAIL_SMTP_PASS)
            s.sendmail(MAIL_FROM_EMAIL, [to_email], msg.as_string())
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ======================
#   ROUTES
# ======================

@app.get("/healthz")
def health():
    return ok_json({"ok": True, "ts": int(time.time())})

@app.get("/audio/<audio_id>.mp3")
def serve_audio(audio_id: str):
    data = AUDIO_STORE.get(audio_id)
    if not data:
        abort(404)
    return send_file(io.BytesIO(data), mimetype="audio/mpeg", download_name=f"{audio_id}.mp3")

# ---------- WhatsApp / SMS (Entrante) ----------
@app.post("/whatsapp")
def whatsapp_inbound():
    from_ = request.values.get("From", "")
    body  = (request.values.get("Body") or "").strip()
    logger.info("WHATSAPP INBOUND from=%s body=%s", from_, body)

    reply = MessagingResponse()
    try:
        plan = plan_intent(body)
        logger.info("plan=%s", plan)

        if plan.get("intent") == "search_property":
            prop_id = int(str(plan.get("property_id") or "0"))
            item = monday_get_property_by_id(MONDAY_BOARD_ID, prop_id) if (MONDAY_BOARD_ID and prop_id) else {}
            reply.message(build_property_summary(item))

        elif plan.get("intent") == "send_whatsapp":
            dest = plan.get("phone")
            text = plan.get("message") or f"Hola, te escribe {BRAND_NAME}."
            if dest:
                sid = send_whatsapp(dest, body=text)
                reply.message(f"WhatsApp enviado a {dest} (SID {sid}).")
            else:
                reply.message("Dime: 'enviar whatsapp a +34XXXXXXXXX: <mensaje>'")

        elif plan.get("intent") == "send_email":
            email = plan.get("email")
            text  = plan.get("message") or "Hola,"
            if email:
                r = send_email(email, f"[{BRAND_NAME}] Información solicitada", f"<p>{text}</p>")
                reply.message(f"Email a {email} (ok={r.get('ok')}).")
            else:
                reply.message("Dime: 'email a correo@dominio.com: <mensaje>'")

        else:
            chat = _openai_chat(
                [{"role":"system","content":f"Eres asistente de {BRAND_NAME}. Responde breve y útil."},
                 {"role":"user","content": body}]
            )
            reply.message(chat)

    except Exception:
        logger.exception("error whatsapp_inbound")
        reply.message("Error procesando la solicitud. Probemos en un momento.")
    return Response(str(reply), mimetype="application/xml")

# ---------- Voice (Entrante) ----------
@app.post("/voice")
def voice_inbound():
    vr = VoiceResponse()
    text = (f"Bienvenido a {BRAND_NAME}. Después del tono, cuéntame qué necesitas. "
            "También puedes pulsar uno para recibir por WhatsApp la ficha de una propiedad.")
    audio_bytes = eleven_tts_to_bytes(text)
    if audio_bytes:
        audio_id = str(uuid.uuid4()); AUDIO_STORE[audio_id] = audio_bytes
        vr.play(f"{request.url_root.rstrip('/')}/audio/{audio_id}.mp3")
    else:
        vr.say(text, language="es-ES")
    g = Gather(input="speech dtmf", timeout=6, num_digits=1, action="/gather",
               speech_timeout="auto", language="es-ES")
    vr.append(g)
    vr.pause(length=1)
    vr.redirect("/voice")
    return Response(str(vr), mimetype="application/xml")

@app.post("/gather")
def gather_handler():
    digit = request.values.get("Digits")
    speech = (request.values.get("SpeechResult") or "").strip()
    vr = VoiceResponse()
    try:
        if digit == "1":
            msg = "Perfecto. Te enviaremos un WhatsApp con la ficha de la última propiedad destacada."
            audio_bytes = eleven_tts_to_bytes(msg)
            if audio_bytes:
                audio_id = str(uuid.uuid4()); AUDIO_STORE[audio_id] = audio_bytes
                vr.play(f"{request.url_root.rstrip('/')}/audio/{audio_id}.mp3")
            else:
                vr.say(msg, language="es-ES")
            vr.hangup()
            return Response(str(vr), mimetype="application/xml")

        plan = plan_intent(speech or "")
        logger.info("voice plan=%s", plan)

        answer_text = ""
        if plan.get("intent") == "search_property":
            prop_id = int(str(plan.get("property_id") or "0"))
            item = monday_get_property_by_id(MONDAY_BOARD_ID, prop_id) if (MONDAY_BOARD_ID and prop_id) else {}
            answer_text = "He encontrado lo siguiente: " + build_property_summary(item).replace("\n", ". ")

        elif plan.get("intent") == "send_whatsapp":
            dest = plan.get("phone"); text = plan.get("message") or f"Hola, te escribe {BRAND_NAME}."
            if dest:
                send_whatsapp(dest, body=text)
                answer_text = f"He enviado el WhatsApp a {dest}."
            else:
                answer_text = "No he detectado el número destino."

        elif plan.get("intent") == "send_email":
            email = plan.get("email"); text = plan.get("message") or "Hola,"
            if email:
                send_email(email, f"[{BRAND_NAME}] Información solicitada", f"<p>{text}</p>")
                answer_text = f"He enviado el correo a {email}."
            else:
                answer_text = "No he detectado el correo."

        else:
            chat = _openai_chat(
                [{"role":"system","content":f"Eres asistente telefónico de {BRAND_NAME}. Sé conciso."},
                 {"role":"user","content": speech}]
            )
            answer_text = chat or "De acuerdo, lo gestiono ahora mismo."

        audio_bytes = eleven_tts_to_bytes(answer_text)
        if audio_bytes:
            audio_id = str(uuid.uuid4()); AUDIO_STORE[audio_id] = audio_bytes
            vr.play(f"{request.url_root.rstrip('/')}/audio/{audio_id}.mp3")
        else:
            vr.say(answer_text, language="es-ES")

    except Exception:
        logger.exception("gather error")
        vr.say("Ha ocurrido un error procesando tu petición.", language="es-ES")

    vr.hangup()
    return Response(str(vr), mimetype="application/xml")

# ---------- OPS / utilidades ----------
def _require_ops():
    if request.headers.get("X-Auth") != OPS_TOKEN:
        abort(403)

@app.post("/ops/send-whatsapp")
def ops_send_whatsapp():
    _require_ops()
    data = request.get_json(force=True)
    sid = send_whatsapp(
        data["to"],
        body=data.get("body"),
        content_sid=data.get("content_sid"),
        content_vars=data.get("content_vars")
    )
    return ok_json({"sid": sid})

@app.post("/ops/send-email")
def ops_send_email():
    _require_ops()
    data = request.get_json(force=True)
    r = send_email(data["to"], data["subject"], data["html"])
    return ok_json(r)

@app.post("/ops/test-email")
def ops_test_email():
    _require_ops()
    to = request.args.get("to") or request.json.get("to")
    if not to:
        return ok_json({"ok": False, "error": "Parámetro 'to' requerido"}, 400)
    r = send_email(to, f"[{BRAND_NAME}] Test SMTP", "<p>Test OK</p>")
    return ok_json(r)

@app.post("/ops/test-wa")
def ops_test_wa():
    _require_ops()
    to = request.args.get("to") or request.json.get("to")
    text = (request.args.get("text") or request.json.get("text") or
            f"Mensaje de prueba desde {BRAND_NAME}")
    if not to:
        return ok_json({"ok": False, "error": "Parámetro 'to' requerido"}, 400)
    sid = send_whatsapp(to, body=text)
    return ok_json({"ok": True, "sid": sid})

# ======================
#   MAIN
# ======================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
