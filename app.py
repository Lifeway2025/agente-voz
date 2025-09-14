# app.py  — Lifeway Bot (voz conversacional + Monday + WhatsApp/Email + subitems)
import os, io, json, time, uuid, logging, smtplib
from typing import Any, Dict, Optional, List
from email.mime.text import MIMEText
from email.utils import formataddr
from datetime import datetime, timedelta

import requests
from flask import Flask, request, Response, abort, send_file
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient

# ------------------------ Utilidades ENV ------------------------
def _env_str(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default

def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except Exception:
        logging.warning("ENV %s inválida, usando %s", name, default)
        return default

# ------------------------ Configuración -------------------------
OPENAI_API_KEY   = _env_str("OPENAI_API_KEY")
OPENAI_MODEL     = _env_str("OPENAI_MODEL", "gpt-4o-mini")

TWILIO_ACCOUNT_SID   = _env_str("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN    = _env_str("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_E164    = _env_str("TWILIO_PHONE_E164") or _env_str("TWILIO_NUMBER", "+34930348966")
MESSAGING_SERVICE_SID= _env_str("MESSAGING_SERVICE_SID")  # opcional

ELEVEN_API_KEY   = _env_str("ELEVEN_API_KEY") or _env_str("ELEVENLABS_API_KEY")
ELEVEN_VOICE_ID  = _env_str("ELEVEN_VOICE_ID") or _env_str("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

MONDAY_API_KEY   = _env_str("MONDAY_API_KEY") or _env_str("monday_api")
MONDAY_API_URL   = "https://api.monday.com/v2"
# Por defecto, REOS BOT LIFEWAY. Puedes cambiar por ENV.
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

# --------------------- Mapping por tablero ----------------------
# REOS BOT LIFEWAY = 2147303762    |   CESIONES REMATE = 2068339939
BOARD_MAP: Dict[int, Dict[str, str]] = {
    2147303762: {  # REOS
        "name": "name",
        "direccion": "texto_mkmm1paw",
        "poblacion": "texto__1",
        "precio_main": "n_meros_mkmmx03j",   # PRECIO
        "precio_alt":  "",
        "enlace": "text_mkqrp4gr",
        "imagenes": "archivo8__1",          # Files
        "catastro": "texto_mkmm8w8t",
        "fecha_visita": "date_mkq9ggyk",    # Date
        "subitems": "subitems__1",          # (para referencia; no hace falta para crear)
    },
    2068339939: {  # CESIONES
        "name": "name",
        "direccion": "texto_mkmm1paw",      # ADRESS
        "poblacion": "texto__1",
        "precio_main": "numeric_mkptr6ge",  # PRECIO TOTAL
        "precio_alt":  "n_meros_mkmmx03j",  # PRECIO FONDO
        "enlace": "enlace_mkmmpkbk",        # ANUNCIO (link/text)
        "imagenes": "archivo8__1",
        "catastro": "texto_mkmm8w8t",
        "fecha_visita": "date_mkq9ggyk",
        "subitems": "subitems__1",
    },
}

# ------------------------ App / Twilio --------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lifeway")

AUDIO_STORE: Dict[str, bytes] = {}
_twilio: Optional[TwilioClient] = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    _twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ------------------------ OpenAI helper -------------------------
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

# ------------------------ ElevenLabs TTS ------------------------
def eleven_tts_to_bytes(text: str) -> bytes:
    if not ELEVEN_API_KEY:
        return b""
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}",
        headers={"xi-api-key": ELEVEN_API_KEY, "accept": "audio/mpeg", "content-type": "application/json"},
        json={"text": text, "model_id": "eleven_multilingual_v2",
              "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
        timeout=60
    )
    r.raise_for_status()
    return r.content

# ------------------------ Monday helpers ------------------------
def monday_query(query: str, variables: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    if not MONDAY_API_KEY:
        raise RuntimeError("MONDAY_API_KEY not configured")
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
    q = """query($item:Int!){ items(ids:[$item]){ id name column_values{ id text value } } }"""
    data = monday_query(q, {"item": item_id})
    items = data.get("items") or []
    return items[0] if items else {}

def monday_get_assets(asset_ids: List[int]) -> List[Dict[str, Any]]:
    if not asset_ids:
        return []
    q = "query($ids:[Int]){ assets(ids:$ids){ id name public_url } }"
    return monday_query(q, {"ids": asset_ids}).get("assets") or []

def monday_create_subitem(parent_item_id: int, subitem_name: str, column_values: Optional[Dict[str, Any]]=None) -> Optional[int]:
    """
    Crea un subelemento bajo el item padre.
    Nota: podemos pasar column_values como string JSON o no pasarlo si desconocemos las columnas del subitem.
    """
    if column_values is None:
        q = """mutation($pid:Int!, $name:String!){
          create_subitem(parent_item_id:$pid, item_name:$name){ id }
        }"""
        data = monday_query(q, {"pid": parent_item_id, "name": subitem_name})
    else:
        q = """mutation($pid:Int!, $name:String!, $cv: JSON!){
          create_subitem(parent_item_id:$pid, item_name:$name, column_values:$cv){ id }
        }"""
        # Monday acepta JSON nativo en GraphQL variable JSON
        data = monday_query(q, {"pid": parent_item_id, "name": subitem_name, "cv": column_values})
    try:
        return int(data["create_subitem"]["id"])
    except Exception:
        return None

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
    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    cv = _cv_map(item).get(m["imagenes"])
    ids = _parse_asset_ids(cv.get("value") if cv else None)
    assets = monday_get_assets(ids)
    urls = [a.get("public_url") for a in assets if a.get("public_url")]
    out, seen = [], set()
    for u in urls:
        if u and u not in seen:
            seen.add(u); out.append(u)
    return out[:5]

def _get_date(item: Dict[str, Any], col_id: str) -> Optional[str]:
    cv = _cv_map(item).get(col_id)
    if not cv: return None
    if cv.get("text"):
        t = cv["text"].strip()
        try:
            datetime.strptime(t, "%Y-%m-%d")
            return t
        except Exception:
            return t
    if cv.get("value"):
        try:
            data = json.loads(cv["value"])
            return data.get("date")
        except Exception:
            return None
    return None

def _fmt_fecha_humana(iso_date: Optional[str]) -> str:
    if not iso_date: return "pendiente de confirmar"
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        meses = ["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]
        return f"{dt.day} de {meses[dt.month-1]} de {dt.year}"
    except Exception:
        return iso_date

def build_property_summary(item: Dict[str, Any], board_id: int) -> str:
    if not item: return "No he encontrado esa propiedad."
    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    nombre    = item.get("name") or "Propiedad"
    direccion = _get_text(item, m["direccion"])
    poblacion = _get_text(item, m["poblacion"])
    precio    = _get_text(item, m["precio_main"]) or _get_text(item, m["precio_alt"])
    catastro  = _get_text(item, m["catastro"])
    fecha_raw = _get_date(item, m["fecha_visita"])
    fecha_leg = _fmt_fecha_humana(fecha_raw)

    parts = [
        f"{nombre}",
        f"Dirección: {direccion or '-'}",
        f"Población: {poblacion or '-'}",
        f"Precio: {precio or '-'}",
        f"Fecha de visita: {fecha_leg}",
    ]
    if catastro: parts.append(f"Ref. catastral: {catastro}")
    return "\n".join(parts)

# ------------------------ Email / ICS / WhatsApp ----------------
def send_email(to_email: str, subject: str, html: str) -> Dict[str, Any]:
    if not MAIL_SMTP_HOST:
        return {"ok": False, "error": "MAIL_SMTP_* not configured"}
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((MAIL_FROM_NAME, MAIL_FROM_EMAIL))
    msg["To"] = to_email
    try:
        with smtplib.SMTP(MAIL_SMTP_HOST, MAIL_SMTP_PORT, timeout=30) as s:
            if MAIL_SMTP_STARTTLS: s.starttls()
            if MAIL_SMTP_USER or MAIL_SMTP_PASS: s.login(MAIL_SMTP_USER, MAIL_SMTP_PASS)
            s.sendmail(MAIL_FROM_EMAIL, [to_email], msg.as_string())
        return {"ok": True}
    except Exception as e:
        logging.exception("SMTP error")
        return {"ok": False, "error": str(e)}

def create_ics(start_iso: str, title: str, description: str, duration_minutes: int = 30) -> str:
    dt_start = datetime.fromisoformat(start_iso)
    dt_end = dt_start + timedelta(minutes=duration_minutes)
    def _fmt(dt: datetime) -> str: return dt.strftime("%Y%m%dT%H%M%SZ")
    uid = f"{uuid.uuid4()}@lifeway"
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Lifeway//Visita//ES\nBEGIN:VEVENT\n"
        f"UID:{uid}\nDTSTAMP:{_fmt(datetime.utcnow())}\n"
        f"DTSTART:{_fmt(dt_start)}\nDTEND:{_fmt(dt_end)}\n"
        f"SUMMARY:{title}\nDESCRIPTION:{description}\nEND:VEVENT\nEND:VCALENDAR\n"
    )

def send_ics_email(to_email: str, subject: str, ics_text: str) -> Dict[str, Any]:
    html = f"<p>Tu cita está adjunta en formato iCalendar:</p><pre>{ics_text}</pre>"
    return send_email(to_email, subject, html)

def _twilio_params_base(to_e164: str) -> Dict[str, Any]:
    if _twilio is None:
        raise RuntimeError("Twilio credentials not configured")
    p = {"to": f"whatsapp:{to_e164}", "from_": f"whatsapp:{TWILIO_PHONE_E164}"}
    if MESSAGING_SERVICE_SID: p["messaging_service_sid"] = MESSAGING_SERVICE_SID
    return p

def send_whatsapp_text(to_e164: str, text: str) -> str:
    p = _twilio_params_base(to_e164)
    p["body"] = text
    return _twilio.messages.create(**p).sid

def send_whatsapp_images(to_e164: str, media_urls: List[str]) -> List[str]:
    sids=[]
    for u in media_urls[:3]:
        p = _twilio_params_base(to_e164)
        p["media_url"] = [u]
        sids.append(_twilio.messages.create(**p).sid)
    return sids

# ------------------------ Utils Flask --------------------------
def ok_json(data: Any, code: int = 200):
    return Response(json.dumps(data, ensure_ascii=False), status=code, mimetype="application/json")

def _board_from_request() -> int:
    bid = request.values.get("board_id") or (request.get_json(silent=True) or {}).get("board_id")
    try:
        if bid: return int(str(bid))
    except Exception:
        pass
    return MONDAY_DEFAULT_BOARD_ID

# ------------------------ Rutas -------------------------------
@app.get("/healthz")
def health(): return ok_json({"ok": True, "ts": int(time.time())})

@app.get("/audio/<audio_id>.mp3")
def serve_audio(audio_id: str):
    data = AUDIO_STORE.get(audio_id)
    if not data: abort(404)
    return send_file(io.BytesIO(data), mimetype="audio/mpeg", download_name=f"{audio_id}.mp3")

# --------- VOZ: conversacional con agenda ----------
@app.post("/voice")
def voice_inbound():
    vr = VoiceResponse()
    bienvenida = (f"Hola, soy el asistente de {BRAND_NAME}. "
                  "Dime el número de referencia o el nombre del inmueble que te interesa. "
                  "Puedo contarte sus características, la fecha de visita disponible y, si quieres, te la agendo.")
    a = eleven_tts_to_bytes(bienvenida)
    if a:
        aid = str(uuid.uuid4()); AUDIO_STORE[aid] = a
        vr.play(f"{request.url_root.rstrip('/')}/audio/{aid}.mp3")
    else:
        vr.say(bienvenida, language="es-ES")

    g = Gather(input="speech", timeout=7, action="/gather", speech_timeout="auto", language="es-ES")
    vr.append(g)
    vr.pause(length=1)
    vr.redirect("/voice")
    return Response(str(vr), mimetype="application/xml")

@app.post("/gather")
def gather_handler():
    speech = (request.values.get("SpeechResult") or "").strip()
    from_num = (request.values.get("From") or "").replace("whatsapp:", "")
    board_id = _board_from_request()
    vr = VoiceResponse()
    try:
        # Pedimos intención: buscar o agendar (con posibles nombre/email)
        sys = ("Eres un asistente inmobiliario. Devuelve SOLO JSON válido con:\n"
               "{'intent':'search_property|schedule_visit|chitchat',"
               " 'property_id':str|null, 'name':str|null, 'phone':str|null, 'email':str|null}")
        intent = json.loads(_openai_chat(
            [{"role":"system","content":sys},{"role":"user","content":speech}],
            temperature=0.2
        ))
        action = intent.get("intent") or "chitchat"
        prop_id_s = intent.get("property_id")
        client_name = intent.get("name") or ""
        client_phone = intent.get("phone") or (from_num if from_num.startswith("+") else "" )
        client_email = intent.get("email")

        if action == "search_property" and prop_id_s:
            item = monday_get_item(int(str(prop_id_s)))
            resumen = build_property_summary(item, board_id).replace("\n", ". ")
            texto = (resumen + ". Si te encaja, puedo agendar la visita para esa fecha. "
                     "Solo di: 'confirma la visita' o proponme otra.")
            a = eleven_tts_to_bytes(texto)
            if a:
                aid=str(uuid.uuid4()); AUDIO_STORE[aid]=a
                vr.play(f"{request.url_root.rstrip('/')}/audio/{aid}.mp3")
            else:
                vr.say(texto, language="es-ES")
            g = Gather(input="speech", timeout=6, action="/gather", speech_timeout="auto", language="es-ES")
            vr.append(g)

        elif action == "schedule_visit" and prop_id_s:
            item_id = int(str(prop_id_s))
            item = monday_get_item(item_id)
            m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
            fecha_iso = _get_date(item, m["fecha_visita"])
            # Si no hay fecha en Monday, pedimos confirmación alternativa:
            if not fecha_iso:
                msg = ("No tengo una fecha de visita guardada. "
                       "¿Quieres que te envíe la ficha por WhatsApp y coordinamos contigo?")
                a = eleven_tts_to_bytes(msg)
                if a:
                    aid=str(uuid.uuid4()); AUDIO_STORE[aid]=a
                    vr.play(f"{request.url_root.rstrip('/')}/audio/{aid}.mp3")
                else:
                    vr.say(msg, language="es-ES")
                g = Gather(input="speech", timeout=6, action="/gather", speech_timeout="auto", language="es-ES")
                vr.append(g)
                return Response(str(vr), mimetype="application/xml")

            # Creamos subelemento "Interesado - +34..." en la propiedad:
            sub_name = f"{client_name or 'Interesado'} - {client_phone or 's/tel'}"
            monday_create_subitem(item_id, sub_name)

            # Confirmación por WhatsApp + imágenes
            try:
                if client_phone.startswith("+"):
                    summary = build_property_summary(item, board_id)
                    send_whatsapp_text(client_phone, f"Visita confirmada para { _fmt_fecha_humana(fecha_iso) } a las 11:00.\n\n{summary}")
                    send_whatsapp_images(client_phone, extract_image_urls(item, board_id))
            except Exception:
                pass

            texto = (f"Perfecto. He anotado tu interés y la visita para el { _fmt_fecha_humana(fecha_iso) } a las once. "
                     "Te envié los detalles por WhatsApp.")
            a = eleven_tts_to_bytes(texto)
            if a:
                aid=str(uuid.uuid4()); AUDIO_STORE[aid]=a
                vr.play(f"{request.url_root.rstrip('/')}/audio/{aid}.mp3")
            else:
                vr.say(texto, language="es-ES")

            # (Opcional) email con .ics si lo dictó
            if client_email:
                start_iso = fecha_iso + "T11:00:00+00:00"
                ics = create_ics(start_iso, item.get("name","Visita inmueble"),
                                 "Visita a inmueble programada con Lifeway", 30)
                send_ics_email(client_email, f"[{BRAND_NAME}] Confirmación de visita", ics)

            vr.hangup()

        else:
            chat = _openai_chat(
                [{"role":"system","content":f"Eres asistente telefónico de {BRAND_NAME}. Sé directo y útil."},
                 {"role":"user","content": speech}]
            )
            a = eleven_tts_to_bytes(chat)
            if a:
                aid=str(uuid.uuid4()); AUDIO_STORE[aid]=a
                vr.play(f"{request.url_root.rstrip('/')}/audio/{aid}.mp3")
            else:
                vr.say(chat, language="es-ES")
            g = Gather(input="speech", timeout=6, action="/gather", speech_timeout="auto", language="es-ES")
            vr.append(g)

    except Exception:
        logging.exception("gather error")
        vr.say("Ha ocurrido un problema procesando tu petición. Probemos de nuevo.", language="es-ES")
        vr.redirect("/voice")

    return Response(str(vr), mimetype="application/xml")

# -------- WhatsApp inbound (texto) ----------
@app.post("/whatsapp")
def whatsapp_inbound():
    body  = (request.values.get("Body") or "").strip()
    board_id = _board_from_request()
    reply = MessagingResponse()
    try:
        sys = ("Eres un planificador. Devuelve SOLO JSON: "
               "{'intent':'chitchat|search_property|send_whatsapp|send_email|schedule_visit',"
               " 'property_id':str|null,'phone':str|null,'email':str|null,'message':str|null}")
        plan = json.loads(_openai_chat(
            [{"role":"system","content":sys},{"role":"user","content":body}],
            temperature=0.1
        ))
        intent = plan.get("intent","chitchat")

        if intent == "search_property":
            pid = int(str(plan.get("property_id") or "0"))
            item = monday_get_item(pid) if pid else {}
            reply.message(build_property_summary(item, board_id))

        elif intent == "send_email":
            email = plan.get("email"); pid = plan.get("property_id")
            if email and pid:
                item = monday_get_item(int(str(pid)))
                html = build_property_email_html(item, board_id)
                r = send_email(email, f"[{BRAND_NAME}] {item.get('name','Propiedad')}", html)
                reply.message(f"Ficha enviada a {email} (ok={r.get('ok')}).")
            else:
                reply.message("Dime: 'envía propiedad 123 a correo@...'")

        elif intent == "send_whatsapp":
            dest = plan.get("phone"); pid = plan.get("property_id")
            text = plan.get("message") or f"Hola, te escribe {BRAND_NAME}."
            if dest and pid:
                item = monday_get_item(int(str(pid)))
                summary = build_property_summary(item, board_id)
                imgs = extract_image_urls(item, board_id)
                sid_txt = send_whatsapp_text(dest, f"{summary}\n\n{text}")
                sids_img = send_whatsapp_images(dest, imgs)
                reply.message(f"Enviado a {dest} (texto {sid_txt} + {len(sids_img)} img).")
            elif dest:
                sid = send_whatsapp_text(dest, text); reply.message(f"WhatsApp enviado (SID {sid}).")
            else:
                reply.message("Dime: 'whatsapp a +34XXXXXXXXX: <mensaje>'")

        elif intent == "schedule_visit":
            pid = int(str(plan.get("property_id") or "0"))
            phone = plan.get("phone")
            name  = plan.get("name") or "Interesado"
            item = monday_get_item(pid) if pid else {}
            if not item:
                reply.message("No encuentro esa propiedad.")
            else:
                # subitem de registro
                monday_create_subitem(pid, f"{name} - {phone or 's/tel'}")
                # confirmación
                fecha = _fmt_fecha_humana(_get_date(item, BOARD_MAP[board_id]["fecha_visita"]))
                send_whatsapp_text(phone, f"Visita confirmada para {fecha}.")
                reply.message(f"Visita confirmada para {fecha}.")

        else:
            chat = _openai_chat(
                [{"role":"system","content":f"Eres asistente de {BRAND_NAME}. Responde breve y útil."},
                 {"role":"user","content": body}]
            )
            reply.message(chat)

    except Exception:
        logger.exception("whatsapp_inbound error")
        reply.message("Error procesando la solicitud.")
    return Response(str(reply), mimetype="application/xml")

# ------------------------ OPS (pruebas) ------------------------
def _require_ops():
    if not OPS_TOKEN or request.headers.get("X-Auth") != OPS_TOKEN:
        abort(403)

@app.post("/ops/send-prop-email")
def ops_send_prop_email():
    _require_ops()
    d = request.get_json(force=True)
    item_id  = int(d.get("item_id", 0))
    to_email = d.get("to")
    board_id = int(d.get("board_id") or MONDAY_DEFAULT_BOARD_ID)
    if not (item_id and to_email):
        return ok_json({"ok": False, "error": "Faltan item_id o to"}, 400)
    item = monday_get_item(item_id)
    # HTML con galería
    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    nombre    = item.get("name") or "Propiedad"
    direccion = _get_text(item, m["direccion"]); poblacion=_get_text(item,m["poblacion"])
    precio = _get_text(item, m["precio_main"]) or _get_text(item, m["precio_alt"])
    enlace = _get_text(item, m["enlace"])
    imgs   = extract_image_urls(item, board_id)
    rows = [("Dirección",direccion or "-"),("Población",poblacion or "-"),("Precio",precio or "-")]
    table="".join([f"<tr><td><strong>{k}</strong></td><td>{v}</td></tr>" for k,v in rows])
    if enlace: table += f"<tr><td><strong>Enlace</strong></td><td><a href='{enlace}' target='_blank'>Ver ficha</a></td></tr>"
    gallery="".join([f"<div><img src='{u}' style='max-width:100%;border-radius:8px'/></div>" for u in imgs])
    html=f"<h2>{nombre}</h2><table>{table}</table>{gallery}"
    res = send_email(to_email, f"[{BRAND_NAME}] {nombre}", html)
    return ok_json({"ok": res.get("ok", False)})

@app.post("/ops/send-prop-wa")
def ops_send_prop_wa():
    _require_ops()
    d = request.get_json(force=True)
    item_id = int(d.get("item_id", 0))
    to_e164 = d.get("to")
    board_id = int(d.get("board_id") or MONDAY_DEFAULT_BOARD_ID)
    if not (item_id and to_e164):
        return ok_json({"ok": False, "error": "Faltan item_id o to"}, 400)
    item = monday_get_item(item_id)
    sid_txt = send_whatsapp_text(to_e164, build_property_summary(item, board_id))
    sids_img = send_whatsapp_images(to_e164, extract_image_urls(item, board_id))
    return ok_json({"ok": True, "sid_text": sid_txt, "images_sent": len(sids_img)})

@app.post("/ops/book-visit")
def ops_book_visit():
    """Reserva rápida: crea subitem y confirma por WhatsApp (para tests)."""
    _require_ops()
    d = request.get_json(force=True)
    item_id = int(d.get("item_id", 0))
    board_id= int(d.get("board_id") or MONDAY_DEFAULT_BOARD_ID)
    name    = d.get("name") or "Interesado"
    phone   = d.get("phone") or ""
    email   = d.get("email")
    if not item_id:
        return ok_json({"ok": False, "error": "Falta item_id"}, 400)

    item = monday_get_item(item_id)
    if not item:
        return ok_json({"ok": False, "error": "Item no encontrado"}, 404)

    monday_create_subitem(item_id, f"{name} - {phone or 's/tel'}")

    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    fecha_iso = _get_date(item, m["fecha_visita"])
    fecha_leg = _fmt_fecha_humana(fecha_iso)
    if phone:
        send_whatsapp_text(phone, f"Visita confirmada para {fecha_leg}.")
        send_whatsapp_images(phone, extract_image_urls(item, board_id))
    if email and fecha_iso:
        ics = create_ics(fecha_iso+"T11:00:00+00:00", item.get("name","Visita"), "Cita programada", 30)
        send_ics_email(email, f"[{BRAND_NAME}] Confirmación de visita", ics)

    return ok_json({"ok": True, "fecha": fecha_leg})

# ------------------------ Main -------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=_env_int("PORT", 5000), debug=False)
