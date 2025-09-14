# app.py (multi-board Monday + WhatsApp + Email + ElevenLabs)
import os, io, json, time, uuid, logging, smtplib
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any, Dict, Optional, List

import requests
from flask import Flask, request, Response, abort, send_file
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client as TwilioClient

# ---------- ENV helpers ----------
def _env_str(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default

def _env_int(name: str, default: int) -> int:
    try: return int(_env_str(name, str(default)))
    except Exception:
        logging.warning("ENV %s inválida, usando %s", name, default)
        return default

# ---------- Config ----------
OPENAI_API_KEY  = _env_str("OPENAI_API_KEY")
OPENAI_MODEL    = _env_str("OPENAI_MODEL", "gpt-4o-mini")

TWILIO_ACCOUNT_SID = _env_str("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = _env_str("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_E164  = _env_str("TWILIO_PHONE_E164") or _env_str("TWILIO_NUMBER", "+34930348966")
MESSAGING_SERVICE_SID = _env_str("MESSAGING_SERVICE_SID")

ELEVEN_API_KEY  = _env_str("ELEVEN_API_KEY") or _env_str("ELEVENLABS_API_KEY")
ELEVEN_VOICE_ID = _env_str("ELEVEN_VOICE_ID") or _env_str("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

MONDAY_API_KEY  = _env_str("MONDAY_API_KEY") or _env_str("monday_api")
MONDAY_API_URL  = "https://api.monday.com/v2"
# Board por defecto (puedes cambiarlo por ENV):
MONDAY_DEFAULT_BOARD_ID = _env_int("MONDAY_DEFAULT_BOARD_ID", 2147303762)

# SMTP
MAIL_SMTP_HOST     = _env_str("MAIL_SMTP_HOST")
MAIL_SMTP_PORT     = _env_int("MAIL_SMTP_PORT", 587)
MAIL_SMTP_USER     = _env_str("MAIL_SMTP_USER")
MAIL_SMTP_PASS     = _env_str("MAIL_SMTP_PASS")
MAIL_SMTP_STARTTLS = _env_str("MAIL_SMTP_STARTTLS", "true").lower() == "true"
MAIL_FROM_EMAIL    = _env_str("MAIL_FROM_EMAIL", "no-reply@gcaconsulting.es")
MAIL_FROM_NAME     = _env_str("MAIL_FROM_NAME", "GCA Consulting")

BRAND_NAME = _env_str("BRAND_NAME", "GCA Consulting")
OPS_TOKEN  = _env_str("OPS_TOKEN")

# ---------- App ----------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lifeway")
AUDIO_STORE: Dict[str, bytes] = {}

_twilio: Optional[TwilioClient] = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    _twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ---------- OpenAI ----------
def _openai_chat(messages, temperature=0.2) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured")
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={"model": OPENAI_MODEL, "messages": messages, "temperature": temperature},
        timeout=60
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def plan_intent(user_text: str) -> Dict[str, Any]:
    sys = ("Eres un planificador. Devuelve SOLO JSON:\n"
           "{'intent':'chitchat|search_property|send_whatsapp|send_email|help',"
           " 'property_id':str|null,'phone':str|null,'email':str|null,'message':str|null,"
           " 'board_id':str|null}")
    try:
        out = _openai_chat(
            [{"role":"system","content":sys},{"role":"user","content":user_text}],
            temperature=0.1
        )
        return json.loads(out)
    except Exception:
        logger.exception("plan_intent")
        return {"intent":"chitchat","message":user_text,"property_id":None,"phone":None,"email":None,"board_id":None}

# ---------- ElevenLabs ----------
def eleven_tts_to_bytes(text: str) -> bytes:
    if not ELEVEN_API_KEY: return b""
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}",
        headers={"xi-api-key": ELEVEN_API_KEY, "accept": "audio/mpeg", "content-type": "application/json"},
        json={"text": text, "model_id": "eleven_multilingual_v2",
              "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
        timeout=60
    )
    r.raise_for_status()
    return r.content

# ---------- Monday (multi-board) ----------
# Config por board (id -> mapping de columnas)
BOARD_MAP: Dict[int, Dict[str, str]] = {
    # REOS BOT LIFEWAY (2147303762)
    2147303762: {
        "name": "name",
        "direccion": "texto_mkmm1paw",
        "poblacion": "texto__1",
        "precio_main": "n_meros_mkmmx03j",    # PRECIO
        "precio_alt":  "",                     # no aplica
        "enlace": "text_mkqrp4gr",
        "imagenes": "archivo8__1",
        "catastro": "texto_mkmm8w8t"
    },
    # CESIONES REMATE (2068339939)
    2068339939: {
        "name": "name",
        "direccion": "texto_mkmm1paw",         # ADRESS
        "poblacion": "texto__1",
        "precio_main": "numeric_mkptr6ge",     # PRECIO TOTAL (principal)
        "precio_alt":  "n_meros_mkmmx03j",     # PRECIO FONDO (alternativo)
        "enlace": "enlace_mkmmpkbk",           # ANUNCIO (link/text)
        "imagenes": "archivo8__1",
        "catastro": "texto_mkmm8w8t"
    }
}

def _board_from_request() -> int:
    # Permite sobreescribir por query/body (p.ej. ?board_id=2068339939)
    bid = request.values.get("board_id") or (request.get_json(silent=True) or {}).get("board_id")
    try:
        if bid: return int(str(bid))
    except Exception:
        pass
    return MONDAY_DEFAULT_BOARD_ID

def monday_query(query: str, variables: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    if not MONDAY_API_KEY: raise RuntimeError("MONDAY_API_KEY not configured")
    r = requests.post(
        MONDAY_API_URL,
        headers={"Authorization": MONDAY_API_KEY, "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=60
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"Monday error: {data['errors']}")
    return data["data"]

def monday_get_item(item_id: int) -> Dict[str, Any]:
    q = """query($item:Int!){ items(ids: [$item]){ id name column_values{ id text value } } }"""
    data = monday_query(q, {"item": item_id})
    items = data.get("items") or []
    return items[0] if items else {}

def monday_get_assets(asset_ids: List[int]) -> List[Dict[str, Any]]:
    if not asset_ids: return []
    q = "query($ids:[Int]){ assets(ids:$ids){ id name public_url } }"
    data = monday_query(q, {"ids": asset_ids})
    return data.get("assets") or []

def _cv_map(item: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {cv["id"]: cv for cv in (item.get("column_values") or [])}

def _get_text(item: Dict[str, Any], col_id: str) -> str:
    cv = _cv_map(item).get(col_id)
    return (cv.get("text") or "").strip() if cv else ""

def _parse_asset_ids(value: Optional[str]) -> List[int]:
    if not value: return []
    try:
        data = json.loads(value)
        files = (data.get("files") or data.get("assets") or []) if isinstance(data, dict) else []
        out = []
        for f in files:
            _id = f.get("id") or f.get("asset_id") or f.get("assetId")
            if _id is None: continue
            try: out.append(int(_id))
            except Exception: continue
        return out
    except Exception:
        return []

def extract_image_urls(item: Dict[str, Any], board_id: int) -> List[str]:
    col = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])["imagenes"]
    cv = _cv_map(item).get(col)
    ids = _parse_asset_ids(cv.get("value") if cv else None)
    assets = monday_get_assets(ids)
    urls = [a.get("public_url") for a in assets if a.get("public_url")]
    out, seen = [], set()
    for u in urls:
        if u and u not in seen:
            seen.add(u); out.append(u)
    return out[:5]

def build_property_summary(item: Dict[str, Any], board_id: int) -> str:
    if not item: return "No he encontrado esa propiedad."
    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    nombre    = item.get("name") or "Propiedad"
    direccion = _get_text(item, m["direccion"])
    poblacion = _get_text(item, m["poblacion"])
    precio    = _get_text(item, m["precio_main"]) or _get_text(item, m["precio_alt"])
    catastro  = _get_text(item, m["catastro"])
    parts = [f"{nombre}", f"Dirección: {direccion or '-'}", f"Población: {poblacion or '-'}", f"Precio: {precio or '-'}"]
    if catastro: parts.append(f"Ref. catastral: {catastro}")
    return "\n".join(parts)

def build_property_email_html(item: Dict[str, Any], board_id: int) -> str:
    if not item: return "<p>No he encontrado esa propiedad.</p>"
    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    nombre    = item.get("name") or "Propiedad"
    direccion = _get_text(item, m["direccion"])
    poblacion = _get_text(item, m["poblacion"])
    precio    = _get_text(item, m["precio_main"]) or _get_text(item, m["precio_alt"])
    enlace    = _get_text(item, m["enlace"])
    catastro  = _get_text(item, m["catastro"])
    imgs      = extract_image_urls(item, board_id)

    rows = [("Dirección", direccion or "-"), ("Población", poblacion or "-"), ("Precio", precio or "-")]
    if catastro: rows.append(("Ref. catastral", catastro))
    table = "".join([f"<tr><td><strong>{k}</strong></td><td>{v}</td></tr>" for k,v in rows])
    if enlace: table += f"<tr><td><strong>Enlace</strong></td><td><a href='{enlace}' target='_blank'>Ver ficha</a></td></tr>"
    gallery = "".join([f"<div style='margin:8px 0'><img src='{u}' style='max-width:100%;border-radius:8px'/></div>" for u in imgs])

    return f"""
    <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;line-height:1.45">
      <h2 style="margin:0 0 12px">{nombre}</h2>
      <table cellpadding="6" cellspacing="0" style="border-collapse:collapse">{table}</table>
      {gallery}
      <p style="margin-top:16px">Si necesitas más detalles, responde a este correo o por WhatsApp.</p>
      <p style="color:#888;font-size:12px;margin-top:24px">{BRAND_NAME}</p>
    </div>"""

# ---------- Email / WhatsApp ----------
def send_email(to_email: str, subject: str, html: str) -> Dict[str, Any]:
    if not MAIL_SMTP_HOST: return {"ok": False, "error": "MAIL_SMTP_* not configured"}
    msg = MIMEText(html, "html", "utf-8"); msg["Subject"]=subject
    msg["From"]=formataddr((MAIL_FROM_NAME, MAIL_FROM_EMAIL)); msg["To"]=to_email
    try:
        with smtplib.SMTP(MAIL_SMTP_HOST, MAIL_SMTP_PORT, timeout=30) as s:
            if MAIL_SMTP_STARTTLS: s.starttls()
            if MAIL_SMTP_USER or MAIL_SMTP_PASS: s.login(MAIL_SMTP_USER, MAIL_SMTP_PASS)
            s.sendmail(MAIL_FROM_EMAIL, [to_email], msg.as_string())
        return {"ok": True}
    except Exception as e:
        logging.exception("SMTP error"); return {"ok": False, "error": str(e)}

def _twilio_params_base(to_e164: str) -> Dict[str, Any]:
    if _twilio is None: raise RuntimeError("Twilio not configured")
    p = {"to": f"whatsapp:{to_e164}", "from_": f"whatsapp:{TWILIO_PHONE_E164}"}
    if MESSAGING_SERVICE_SID: p["messaging_service_sid"] = MESSAGING_SERVICE_SID
    return p

def send_whatsapp_text(to_e164: str, text: str) -> str:
    p = _twilio_params_base(to_e164); p["body"]=text
    return _twilio.messages.create(**p).sid

def send_whatsapp_images(to_e164: str, media_urls: List[str]) -> List[str]:
    sids=[]; 
    for u in media_urls[:3]:
        p = _twilio_params_base(to_e164); p["media_url"]=[u]
        sids.append(_twilio.messages.create(**p).sid)
    return sids

# ---------- Routes ----------
def ok_json(data: Any, code: int = 200): 
    return Response(json.dumps(data, ensure_ascii=False), status=code, mimetype="application/json")

@app.get("/healthz")
def health(): return ok_json({"ok": True, "ts": int(time.time())})

@app.get("/audio/<audio_id>.mp3")
def serve_audio(audio_id: str):
    data = AUDIO_STORE.get(audio_id)
    if not data: abort(404)
    return send_file(io.BytesIO(data), mimetype="audio/mpeg", download_name=f"{audio_id}.mp3")

# --- WhatsApp entrante ---
@app.post("/whatsapp")
def whatsapp_inbound():
    body = (request.values.get("Body") or "").strip()
    board_id = _board_from_request()
    reply = MessagingResponse()
    try:
        plan = plan_intent(body)
        board_id = int(plan.get("board_id") or board_id)

        if plan.get("intent") == "search_property":
            prop_id = int(str(plan.get("property_id") or "0"))
            item = monday_get_item(prop_id) if prop_id else {}
            reply.message(build_property_summary(item, board_id))

        elif plan.get("intent") == "send_email":
            email = plan.get("email"); prop = plan.get("property_id")
            if email and prop:
                item = monday_get_item(int(str(prop)))
                html = build_property_email_html(item, board_id)
                r = send_email(email, f"[{BRAND_NAME}] {item.get('name','Propiedad')}", html)
                reply.message(f"Ficha enviada a {email} (ok={r.get('ok')}).")
            else:
                reply.message("Dime: 'envía propiedad 123 a correo@...'")
        elif plan.get("intent") == "send_whatsapp":
            dest = plan.get("phone"); prop = plan.get("property_id")
            text = plan.get("message") or f"Hola, te escribe {BRAND_NAME}."
            if dest and prop:
                item = monday_get_item(int(str(prop)))
                summary = build_property_summary(item, board_id)
                imgs = extract_image_urls(item, board_id)
                sid_txt = send_whatsapp_text(dest, f"{summary}\n\n{text}")
                sids_img = send_whatsapp_images(dest, imgs)
                reply.message(f"Enviado a {dest} (texto {sid_txt} + {len(sids_img)} img).")
            elif dest:
                sid = send_whatsapp_text(dest, text); reply.message(f"WhatsApp enviado (SID {sid}).")
            else:
                reply.message("Dime: 'whatsapp a +34XXXXXXXXX: <mensaje>'")
        else:
            chat = _openai_chat(
                [{"role":"system","content":f"Eres asistente de {BRAND_NAME}. Sé breve."},
                 {"role":"user","content": body}]
            )
            reply.message(chat)
    except Exception:
        logger.exception("whatsapp_inbound")
        reply.message("Error procesando la solicitud.")
    return Response(str(reply), mimetype="application/xml")

# --- Voice ---
@app.post("/voice")
def voice_inbound():
    vr = VoiceResponse()
    # Para grabar la llamada (código Meta), descomenta:
    # vr.record(max_length=30, play_beep=False)

    t = f"Bienvenido a {BRAND_NAME}. Después del tono, cuéntame qué necesitas."
    a = eleven_tts_to_bytes(t)
    if a:
        aid=str(uuid.uuid4()); AUDIO_STORE[aid]=a
        vr.play(f"{request.url_root.rstrip('/')}/audio/{aid}.mp3")
    else:
        vr.say(t, language="es-ES")

    g = Gather(input="speech dtmf", timeout=6, num_digits=1, action="/gather",
               speech_timeout="auto", language="es-ES")
    vr.append(g); vr.pause(length=1); vr.redirect("/voice")
    return Response(str(vr), mimetype="application/xml")

@app.post("/gather")
def gather_handler():
    speech = (request.values.get("SpeechResult") or "").strip()
    board_id = _board_from_request()
    vr = VoiceResponse()
    try:
        plan = plan_intent(speech or ""); board_id = int(plan.get("board_id") or board_id)
        txt = ""
        if plan.get("intent") == "search_property":
            pid = int(str(plan.get("property_id") or "0"))
            item = monday_get_item(pid) if pid else {}
            txt = "He encontrado: " + build_property_summary(item, board_id).replace("\n",". ")
        else:
            txt = _openai_chat(
                [{"role":"system","content":f"Eres asistente telefónico de {BRAND_NAME}."},
                 {"role":"user","content": speech}]
            )
        a = eleven_tts_to_bytes(txt or "De acuerdo.")
        if a:
            aid=str(uuid.uuid4()); AUDIO_STORE[aid]=a
            vr.play(f"{request.url_root.rstrip('/')}/audio/{aid}.mp3")
        else:
            vr.say(txt or "De acuerdo.", language="es-ES")
    except Exception:
        logger.exception("gather error"); vr.say("Ha ocurrido un error.", language="es-ES")
    vr.hangup(); return Response(str(vr), mimetype="application/xml")

# --- OPS ---
def _require_ops():
    if not OPS_TOKEN or request.headers.get("X-Auth") != OPS_TOKEN: abort(403)

@app.post("/ops/send-prop-email")
def ops_send_prop_email():
    _require_ops()
    data = request.get_json(force=True)
    item_id  = int(data.get("item_id", 0))
    to_email = data.get("to")
    board_id = int(data.get("board_id") or MONDAY_DEFAULT_BOARD_ID)
    if not (item_id and to_email): return ok_json({"ok": False, "error":"Faltan item_id o to"}, 400)
    item = monday_get_item(item_id)
    res = send_email(to_email, f"[{BRAND_NAME}] {item.get('name','Propiedad')}",
                     build_property_email_html(item, board_id))
    return ok_json({"ok": res.get("ok", False)})

@app.post("/ops/send-prop-wa")
def ops_send_prop_wa():
    _require_ops()
    data = request.get_json(force=True)
    item_id = int(data.get("item_id", 0))
    to_e164 = data.get("to")
    board_id = int(data.get("board_id") or MONDAY_DEFAULT_BOARD_ID)
    if not (item_id and to_e164): return ok_json({"ok": False, "error":"Faltan item_id o to"}, 400)
    item = monday_get_item(item_id)
    sid_txt = send_whatsapp_text(to_e164, build_property_summary(item, board_id))
    sids_img = send_whatsapp_images(to_e164, extract_image_urls(item, board_id))
    return ok_json({"ok": True, "sid_text": sid_txt, "images_sent": len(sids_img)})

# --- main ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=_env_int("PORT", 5000), debug=False)
