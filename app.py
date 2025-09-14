# app.py — Lifeway Bot (voz conversacional que escucha, multilenguaje, Monday, agenda y subitems con contacto)
import os, io, json, time, uuid, logging, smtplib, re
from typing import Any, Dict, Optional, List
from email.mime.text import MIMEText
from email.utils import formataddr
from datetime import datetime, timedelta

import requests
from flask import Flask, request, Response, abort, send_file
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient

# -------------------- ENV helpers --------------------
def _env_str(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default

def _env_int(name: str, default: int) -> int:
    try: return int(_env_str(name, str(default)))
    except Exception:
        logging.warning("ENV %s inválida, usando %s", name, default)
        return default

# -------------------- Config -------------------------
OPENAI_API_KEY   = _env_str("OPENAI_API_KEY")
OPENAI_MODEL     = _env_str("OPENAI_MODEL", "gpt-4o-mini")

TWILIO_ACCOUNT_SID = _env_str("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = _env_str("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_E164  = _env_str("TWILIO_PHONE_E164") or _env_str("TWILIO_NUMBER", "+34930348966")
MESSAGING_SERVICE_SID = _env_str("MESSAGING_SERVICE_SID")  # opcional

ELEVEN_API_KEY   = _env_str("ELEVEN_API_KEY") or _env_str("ELEVENLABS_API_KEY")
ELEVEN_VOICE_ID  = _env_str("ELEVEN_VOICE_ID") or _env_str("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

MONDAY_API_KEY   = _env_str("MONDAY_API_KEY") or _env_str("monday_api")
MONDAY_API_URL   = "https://api.monday.com/v2"
MONDAY_DEFAULT_BOARD_ID = _env_int("MONDAY_DEFAULT_BOARD_ID", 2147303762)  # REOS por defecto

# (NUEVO) Columnas del board de SUBITEMS para guardar contacto (opcionales pero recomendadas)
SUBITEM_NAME_COL_ID  = _env_str("SUBITEM_NAME_COL_ID")   # p.ej. "texto_nombre"
SUBITEM_PHONE_COL_ID = _env_str("SUBITEM_PHONE_COL_ID")  # p.ej. "telefono"
SUBITEM_EMAIL_COL_ID = _env_str("SUBITEM_EMAIL_COL_ID")  # p.ej. "email"

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

# -------------------- Mapeo por board -----------------
BOARD_MAP: Dict[int, Dict[str, str]] = {
    2147303762: {  # REOS BOT LIFEWAY
        "name": "name",
        "direccion": "texto_mkmm1paw",
        "poblacion": "texto__1",
        "precio_main": "n_meros_mkmmx03j",
        "precio_alt":  "",
        "enlace": "text_mkqrp4gr",
        "imagenes": "archivo8__1",
        "catastro": "texto_mkmm8w8t",
        "fecha_visita": "date_mkq9ggyk",
        "subitems": "subitems__1",
    },
    2068339939: {  # CESIONES REMATE
        "name": "name",
        "direccion": "texto_mkmm1paw",  # ADRESS
        "poblacion": "texto__1",
        "precio_main": "numeric_mkptr6ge",  # PRECIO TOTAL
        "precio_alt":  "n_meros_mkmmx03j",  # PRECIO FONDO
        "enlace": "enlace_mkmmpkbk",
        "imagenes": "archivo8__1",
        "catastro": "texto_mkmm8w8t",
        "fecha_visita": "date_mkq9ggyk",
        "subitems": "subitems__1",
    },
}

# -------------------- App / Twilio --------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lifeway")

AUDIO_STORE: Dict[str, bytes] = {}
_twilio: Optional[TwilioClient] = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) else None

# -------------------- OpenAI / NLU --------------------
def _openai_chat(messages, temperature=0.3) -> str:
    if not OPENAI_API_KEY: raise RuntimeError("OPENAI_API_KEY not configured")
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={"model": OPENAI_MODEL, "messages": messages, "temperature": temperature},
        timeout=60
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def nlu_extract(user_text: str) -> Dict[str, Any]:
    sys = (
        "Extrae SOLO JSON con claves:\n"
        "{'intent':'search|book|chitchat',"
        " 'property_id':str|null,'address':str|null,'city':str|null,'budget':float|null,"
        " 'name':str|null,'phone':str|null,'email':str|null,'lang':'es'|'ar'|'en'}\n"
        "Detecta árabe si el texto contiene árabe. Si dice un número tipo 2147… úsalo como property_id."
    )
    try:
        out = _openai_chat([{"role":"system","content":sys},{"role":"user","content":user_text}], temperature=0.1)
        return json.loads(out)
    except Exception:
        logger.exception("nlu_extract")
        return {"intent":"chitchat","property_id":None,"address":None,"city":None,"budget":None,"name":None,"phone":None,"email":None,"lang":"es"}

def translate(text: str, target_lang: str) -> str:
    if target_lang == "es": return text
    sys = f"Traduce al { 'árabe' if target_lang=='ar' else 'inglés' }, sin añadir nada extra."
    try: return _openai_chat([{"role":"system","content":sys},{"role":"user","content":text}], temperature=0)
    except Exception: return text

# -------------------- ElevenLabs TTS ------------------
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

def speak_tts(vr: VoiceResponse, text_es: str, lang: str, base_url: str):
    text = translate(text_es, lang) if lang != "es" else text_es
    try:
        a = eleven_tts_to_bytes(text)
        if a:
            aid = str(uuid.uuid4()); AUDIO_STORE[aid] = a
            vr.play(f"{base_url}/audio/{aid}.mp3")
            return
    except Exception:
        logger.exception("TTS error")
    # Fallback Twilio <Say> (no AR nativo, usamos ES)
    vr.say(text_es, language="es-ES")

# -------------------- Monday helpers -----------------
def monday_query(query: str, variables: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    if not MONDAY_API_KEY: raise RuntimeError("MONDAY_API_KEY not configured")
    r = requests.post(MONDAY_API_URL,
        headers={"Authorization": MONDAY_API_KEY, "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}}, timeout=60)
    r.raise_for_status()
    data = r.json()
    if "errors" in data: raise RuntimeError(f"Monday error: {data['errors']}")
    return data["data"]

def monday_get_item(item_id: int) -> Dict[str, Any]:
    q = """query($item:Int!){ items(ids:[$item]){ id name column_values{ id text value } } }"""
    d = monday_query(q, {"item": item_id})
    items = d.get("items") or []
    return items[0] if items else {}

def monday_get_assets(asset_ids: List[int]) -> List[Dict[str, Any]]:
    if not asset_ids: return []
    q = "query($ids:[Int]){ assets(ids:$ids){ id name public_url } }"
    return monday_query(q, {"ids": asset_ids}).get("assets") or []

def monday_create_subitem(parent_item_id: int, subitem_name: str, name_val: Optional[str]=None,
                          phone_val: Optional[str]=None, email_val: Optional[str]=None) -> Optional[int]:
    # 1) Crear el subitem con título (name)
    q1 = """mutation($pid:Int!, $name:String!){
      create_subitem(parent_item_id:$pid, item_name:$name){ id }
    }"""
    d1 = monday_query(q1, {"pid": parent_item_id, "name": subitem_name})
    try:
        sub_id = int(d1["create_subitem"]["id"])
    except Exception:
        return None

    # 2) Si tenemos columnas configuradas, las rellenamos
    cols = {}
    if SUBITEM_NAME_COL_ID and name_val:  cols[SUBITEM_NAME_COL_ID]  = name_val
    if SUBITEM_PHONE_COL_ID and phone_val: cols[SUBITEM_PHONE_COL_ID] = phone_val
    if SUBITEM_EMAIL_COL_ID and email_val: cols[SUBITEM_EMAIL_COL_ID] = email_val

    if cols:
        q2 = """mutation($item:Int!, $cv: JSON!){
          change_column_values(item_id:$item, column_values:$cv){ id }
        }"""
        monday_query(q2, {"item": sub_id, "cv": cols})
    return sub_id

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
        out=[]
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
        try: datetime.strptime(t, "%Y-%m-%d"); return t
        except Exception: return t
    if cv.get("value"):
        try: return json.loads(cv["value"]).get("date")
        except Exception: return None
    return None

def _fmt_fecha_humana(iso_date: Optional[str]) -> str:
    if not iso_date: return "pendiente de confirmar"
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        meses = ["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]
        return f"{dt.day} de {meses[dt.month-1]} de {dt.year}"
    except Exception: return iso_date

def build_property_summary(item: Dict[str, Any], board_id: int, lang: str="es") -> str:
    if not item:
        msg = "No he encontrado esa propiedad."
        return translate(msg, lang) if lang != "es" else msg
    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    nombre    = item.get("name") or "Propiedad"
    direccion = _get_text(item, m["direccion"])
    poblacion = _get_text(item, m["poblacion"])
    precio    = _get_text(item, m["precio_main"]) or _get_text(item, m["precio_alt"])
    catastro  = _get_text(item, m["catastro"])
    fecha_raw = _get_date(item, m["fecha_visita"])
    fecha_leg = _fmt_fecha_humana(fecha_raw)

    lines = [
        f"{nombre}",
        f"Dirección: {direccion or '-'}",
        f"Población: {poblacion or '-'}",
        f"Precio: {precio or '-'}",
        f"Fecha de visita: {fecha_leg}",
    ]
    if catastro: lines.append(f"Ref. catastral: {catastro}")
    text = "\n".join(lines)
    return translate(text, lang) if lang != "es" else text

# -------------------- WhatsApp / Email ----------------
def send_email(to_email: str, subject: str, html: str) -> Dict[str, Any]:
    if not MAIL_SMTP_HOST: return {"ok": False, "error": "MAIL_SMTP_* not configured"}
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
        logger.exception("SMTP error"); return {"ok": False, "error": str(e)}

def _twilio_params_base(to_e164: str) -> Dict[str, Any]:
    if _twilio is None: raise RuntimeError("Twilio not configured")
    p = {"to": f"whatsapp:{to_e164}", "from_": f"whatsapp:{TWILIO_PHONE_E164}"}
    if MESSAGING_SERVICE_SID: p["messaging_service_sid"] = MESSAGING_SERVICE_SID
    return p

def send_whatsapp_text(to_e164: str, text: str) -> str:
    p = _twilio_params_base(to_e164); p["body"]=text
    return _twilio.messages.create(**p).sid

def send_whatsapp_images(to_e164: str, media_urls: List[str]) -> List[str]:
    sids=[]
    for u in media_urls[:3]:
        p = _twilio_params_base(to_e164); p["media_url"]=[u]
        sids.append(_twilio.messages.create(**p).sid)
    return sids

# -------------------- Búsqueda local -------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9áéíóúüñ ]", " ", (s or "").lower())

def _price_to_float(s: str) -> Optional[float]:
    if not s: return None
    nums = re.findall(r"\d+", s.replace(".", "").replace(",", ""))
    if not nums: return None
    try: return float("".join(nums))
    except Exception: return None

def monday_fetch_items_for_search(board_id: int, max_items: int = 200) -> List[Dict[str, Any]]:
    items = []; cursor=None; remaining=max_items
    while remaining>0:
        limit=min(50,remaining)
        q = """
        query($ids:[ID!], $limit:Int!, $cursor:String){
          boards(ids:$ids){
            items_page(limit:$limit, cursor:$cursor){
              items{ id name column_values{ id text } }
              cursor
            }
          }
        }"""
        d = monday_query(q, {"ids":[board_id], "limit":limit, "cursor":cursor})
        page = (d.get("boards") or [{}])[0].get("items_page") or {}
        items += page.get("items") or []
        cursor = page.get("cursor")
        if not cursor: break
        remaining -= limit
    return items

def _score_item(item: Dict[str, Any], board_id: int, address: str, city: str, budget: Optional[float]) -> float:
    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    dir_ = _get_text(item, m["direccion"]); city_ = _get_text(item, m["poblacion"])
    price_txt = _get_text(item, m["precio_main"]) or _get_text(item, m["precio_alt"])
    price = _price_to_float(price_txt)
    score=0.0
    if address and _norm(address) in _norm(dir_): score+=3.0
    if city and _norm(city) in _norm(city_):     score+=2.0
    if address and _norm(address) in _norm(item.get("name","")): score+=1.0
    if budget and price and abs(price-budget) <= 0.2*budget: score+=2.0
    return score

def monday_search(board_id: int, address: Optional[str], city: Optional[str], budget: Optional[float]) -> Optional[Dict[str, Any]]:
    items = monday_fetch_items_for_search(board_id, 200)
    if not items: return None
    best, best_sc = None, -1
    for it in items:
        sc = _score_item(it, board_id, address or "", city or "", budget)
        if sc > best_sc: best_sc, best = sc, it
    return best if best_sc >= 2.0 else None

# -------------------- Flask utils ---------------------
def ok_json(data: Any, code: int = 200): 
    return Response(json.dumps(data, ensure_ascii=False), status=code, mimetype="application/json")

def _board_from_request() -> int:
    bid = request.values.get("board_id") or (request.get_json(silent=True) or {}).get("board_id")
    try:
        if bid: return int(str(bid))
    except Exception: pass
    return MONDAY_DEFAULT_BOARD_ID

# -------------------- Rutas ---------------------------
@app.get("/healthz")
def health(): return ok_json({"ok": True, "ts": int(time.time())})

@app.get("/audio/<audio_id>.mp3")
def serve_audio(audio_id: str):
    data = AUDIO_STORE.get(audio_id)
    if not data: abort(404)
    return send_file(io.BytesIO(data), mimetype="audio/mpeg", download_name=f"{audio_id}.mp3")

# ========== VOICE (ahora SÍ escucha y reintenta) ==========
WELCOME_ES = ("Hola, soy el asistente de " + BRAND_NAME +
              ". Dime la dirección, población o tu presupuesto y te digo qué tenemos. "
              "También puedo agendar la visita.")

def _new_gather():
    g = Gather(
        input="speech dtmf",
        method="POST",
        timeout=7,
        action="/gather",
        actionOnEmptyResult="true",
        speechTimeout="auto",
        language="es-ES"
    )
    return g

@app.post("/voice")
def voice_inbound():
    vr = VoiceResponse()
    speak_tts(vr, WELCOME_ES, "es", request.url_root.rstrip("/"))
    vr.append(_new_gather())  # escucha inmediatamente
    # NO hacemos redirect en bucle sin necesidad
    return Response(str(vr), mimetype="application/xml")

@app.post("/gather")
def gather_handler():
    base_url = request.url_root.rstrip("/")
    speech = (request.values.get("SpeechResult") or "").strip()
    digits = (request.values.get("Digits") or "").strip()
    caller = (request.values.get("From") or "").replace("whatsapp:", "")
    board_id = _board_from_request()
    vr = VoiceResponse()

    try:
        # Manejo robusto de silencio / sin resultados
        if not speech and not digits:
            speak_tts(vr, "No he podido escucharte bien. ¿Puedes repetir la dirección, la población o tu presupuesto?", "es", base_url)
            vr.append(_new_gather())
            return Response(str(vr), mimetype="application/xml")

        # NLU
        info = nlu_extract(speech or digits)
        intent = info.get("intent","chitchat")
        lang = info.get("lang","es")

        # 1) Caso: referencia directa
        if (intent in ("search","book")) and info.get("property_id"):
            item = monday_get_item(int(str(info["property_id"])))
            resumen = build_property_summary(item, board_id, lang).replace("\n",". ")
            speak_tts(vr, resumen + " ¿Quieres que agende esa visita?", lang, base_url)
            vr.append(_new_gather())
            return Response(str(vr), mimetype="application/xml")

        # 2) Búsqueda por dirección / población / precio
        if intent == "search":
            addr, city = info.get("address"), info.get("city")
            budget = float(info.get("budget") or 0) or None
            best = monday_search(board_id, addr, city, budget)
            if best:
                item = monday_get_item(int(best["id"]))
                resumen = build_property_summary(item, board_id, lang).replace("\n",". ")
                speak_tts(vr, resumen + " ¿Quieres que agende la visita?", lang, base_url)
            else:
                speak_tts(vr, "No he encontrado una coincidencia clara. ¿Quieres que te envíe opciones por WhatsApp?", lang, base_url)
            vr.append(_new_gather())
            return Response(str(vr), mimetype="application/xml")

        # 3) Agendar
        if intent == "book":
            # Si no hay ID, intentamos buscar
            item = None
            if info.get("property_id"):
                item = monday_get_item(int(str(info["property_id"])))
            else:
                best = monday_search(board_id, info.get("address"), info.get("city"), float(info.get("budget") or 0) or None)
                if best: item = monday_get_item(int(best["id"]))
            if not item:
                speak_tts(vr, "No localicé la propiedad. ¿Puedes repetir la referencia o la dirección?", lang, base_url)
                vr.append(_new_gather())
                return Response(str(vr), mimetype="application/xml")

            m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
            fecha_iso = _get_date(item, m["fecha_visita"])
            if not fecha_iso:
                speak_tts(vr, "Esa ficha no tiene fecha guardada. Puedo enviarte la información por WhatsApp y lo coordinamos.", lang, base_url)
                vr.append(_new_gather())
                return Response(str(vr), mimetype="application/xml")

            nombre = info.get("name") or "Interesado"
            telefono = info.get("phone") or (caller if caller.startswith("+") else "")
            correo = info.get("email")

            # Crear subitem con columnas (si están configuradas)
            monday_create_subitem(
                int(item["id"]),
                f"{nombre} - {telefono or 's/tel'}",
                name_val=nombre,
                phone_val=telefono or "",
                email_val=correo or ""
            )

            # Confirmación por WhatsApp con imágenes
            try:
                if telefono.startswith("+"):
                    summary = build_property_summary(item, board_id, lang)
                    send_whatsapp_text(telefono, translate(f"Visita confirmada para { _fmt_fecha_humana(fecha_iso) } a las 11:00.\n\n{summary}", lang))
                    send_whatsapp_images(telefono, extract_image_urls(item, board_id))
            except Exception:
                pass

            speak_tts(vr, f"Perfecto. He agendado tu visita para el { _fmt_fecha_humana(fecha_iso) } a las once. Te envié los detalles por WhatsApp.", lang, base_url)
            vr.hangup()
            return Response(str(vr), mimetype="application/xml")

        # 4) Charla breve si no hay intención clara
        chat_es = _openai_chat(
            [{"role":"system","content":f"Eres asistente telefónico de {BRAND_NAME}. Sé directo y útil."},
             {"role":"user","content": speech or digits}]
        )
        speak_tts(vr, chat_es, lang, base_url)
        vr.append(_new_gather())
        return Response(str(vr), mimetype="application/xml")

    except Exception:
        logger.exception("gather error")
        speak_tts(vr, "Ha ocurrido un problema procesando tu petición. Probemos de nuevo.", "es", base_url)
        vr.append(_new_gather())
        return Response(str(vr), mimetype="application/xml")

# -------------------- WhatsApp inbound ----------------
@app.post("/whatsapp")
def whatsapp_inbound():
    body  = (request.values.get("Body") or "").strip()
    board_id = _board_from_request()
    reply = MessagingResponse()
    try:
        info = nlu_extract(body)
        intent = info.get("intent","chitchat"); lang = info.get("lang","es")

        if intent == "search":
            item = None
            if info.get("property_id"):
                item = monday_get_item(int(str(info["property_id"])))
            else:
                best = monday_search(board_id, info.get("address"), info.get("city"), float(info.get("budget") or 0) or None)
                if best: item = monday_get_item(int(best["id"]))
            reply.message(build_property_summary(item, board_id, lang))

        elif intent == "book":
            item = None
            if info.get("property_id"):
                item = monday_get_item(int(str(info["property_id"])))
            else:
                best = monday_search(board_id, info.get("address"), info.get("city"), float(info.get("budget") or 0) or None)
                if best: item = monday_get_item(int(best["id"]))
            if not item:
                reply.message("No encuentro esa propiedad. Dime referencia o dirección.")
            else:
                m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
                fecha = _get_date(item, m["fecha_visita"])
                if not fecha:
                    reply.message("Esa ficha no tiene fecha. Te envío la info y te contactamos.")
                else:
                    nombre = info.get("name") or "Interesado"
                    tel = info.get("phone") or ""
                    correo = info.get("email") or ""
                    monday_create_subitem(int(item["id"]), f"{nombre} - {tel or 's/tel'}",
                                          name_val=nombre, phone_val=tel, email_val=correo)
                    reply.message(translate(f"Visita confirmada para { _fmt_fecha_humana(fecha) }.", lang))

        else:
            chat = _openai_chat(
                [{"role":"system","content":f"Eres asistente de {BRAND_NAME}. Responde breve y útil."},
                 {"role":"user","content": body}]
            )
            reply.message(translate(chat, lang))

    except Exception:
        logger.exception("whatsapp_inbound")
        reply.message("Error procesando la solicitud.")
    return Response(str(reply), mimetype="application/xml")

# -------------------- OPS (tests) ---------------------
def _require_ops():
    if not OPS_TOKEN or request.headers.get("X-Auth") != OPS_TOKEN: abort(403)

@app.get("/healthz")
def healthz_dup():  # alias
    return ok_json({"ok": True, "ts": int(time.time())})

@app.post("/ops/book-visit")
def ops_book_visit():
    _require_ops()
    d = request.get_json(force=True)
    item_id  = int(d.get("item_id", 0))
    board_id = int(d.get("board_id") or MONDAY_DEFAULT_BOARD_ID)
    name     = d.get("name") or "Interesado"
    phone    = d.get("phone") or ""
    email    = d.get("email") or ""
    if not item_id: return ok_json({"ok": False, "error":"Falta item_id"}, 400)
    item = monday_get_item(item_id)
    if not item: return ok_json({"ok": False, "error":"Item no encontrado"}, 404)
    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    fecha = _get_date(item, m["fecha_visita"]); fecha_leg = _fmt_fecha_humana(fecha)
    monday_create_subitem(item_id, f"{name} - {phone or 's/tel'}", name_val=name, phone_val=phone, email_val=email)
    if phone:
        try:
            send_whatsapp_text(phone, f"Visita confirmada para {fecha_leg}.")
            send_whatsapp_images(phone, extract_image_urls(item, board_id))
        except Exception:
            pass
    return ok_json({"ok": True, "fecha": fecha_leg})

# -------------------- Main ---------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=_env_int("PORT", 5000), debug=False)
