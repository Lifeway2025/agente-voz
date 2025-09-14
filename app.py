# app.py — Lifeway Voice Bot
# Conversación natural (ES/AR/EN), búsqueda en Monday, agenda visitas,
# creación de subelementos con contacto y confirmación, WhatsApp con imágenes.

import os, io, json, time, uuid, logging, re
from typing import Any, Dict, Optional, List
from datetime import datetime

import requests
from flask import Flask, request, Response, abort, send_file

from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient

# ============ Utils ENV ============
def _env_str(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default

def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except Exception:
        logging.warning("ENV %s inválida, usando %s", name, default)
        return default

# ============ Config ============
OPENAI_API_KEY   = _env_str("OPENAI_API_KEY")
OPENAI_MODEL     = _env_str("OPENAI_MODEL", "gpt-4o-mini")

TWILIO_ACCOUNT_SID = _env_str("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = _env_str("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_E164  = _env_str("TWILIO_PHONE_E164") or _env_str("TWILIO_NUMBER")

ELEVEN_API_KEY   = _env_str("ELEVEN_API_KEY") or _env_str("ELEVENLABS_API_KEY")
ELEVEN_VOICE_ID  = _env_str("ELEVEN_VOICE_ID") or _env_str("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

# Monday
MONDAY_API_KEY   = _env_str("MONDAY_API_KEY") or _env_str("monday_api")
MONDAY_API_URL   = "https://api.monday.com/v2"
MONDAY_DEFAULT_BOARD_ID = _env_int("MONDAY_DEFAULT_BOARD_ID", 2147303762)  # REOS como default

# Subitems REOS (board 2147303765)
SUB_REOS_NAME_COL_ID   = _env_str("SUB_REOS_NAME_COL_ID")   # name
SUB_REOS_PHONE_COL_ID  = _env_str("SUB_REOS_PHONE_COL_ID")  # phone_mks7jjxp
SUB_REOS_EMAIL_COL_ID  = _env_str("SUB_REOS_EMAIL_COL_ID")  # email_mks7kagf
SUB_REOS_DATE_COL_ID   = _env_str("SUB_REOS_DATE_COL_ID")   # date0
SUB_REOS_NOLON_COL_ID  = _env_str("SUB_REOS_NOLON_COL_ID")  # text_mkvs87qt
SUB_REOS_STATUS_COL_ID = _env_str("SUB_REOS_STATUS_COL_ID") # color_mkvst8na

# (Si más adelante usamos CESIONES, añadimos aquí sus SUB_CES_*)

BRAND_NAME = _env_str("BRAND_NAME", "Lifeway")
OPS_TOKEN  = _env_str("OPS_TOKEN")

# ============ Column mapping por tablero ============
# IMPORTANTÍSIMO: los IDs de columnas DEBEN coincidir con tu Monday
BOARD_MAP: Dict[int, Dict[str, str]] = {
    2147303762: {  # REOS BOT LIFEWAY (board padre)
        "name": "name",                   # nombre item
        "direccion": "texto_mkmm1paw",    # DIRECCIÓN
        "poblacion": "texto__1",          # POBLACIÓN
        "precio_main": "n_meros_mkmmx03j",# PRECIO
        "precio_alt":  "",                # alternativa (vacío)
        "enlace": "text_mkqrp4gr",        # ENLACE
        "imagenes": "archivo8__1",        # IMAGENES (file)
        "catastro": "texto_mkmm8w8t",     # R. CATASTRAL
        "fecha_visita": "date_mkq9ggyk",  # Fecha (día de visita)
        "nolon": "numeric_mkrfw72b",      # NOLON ID del padre
        "subitems": "subitems__1",
    },
    2068339939: {  # CESIONES REMATE (board padre) — para el futuro
        "name": "name",
        "direccion": "texto_mkmm1paw",    # ADRESS
        "poblacion": "texto__1",
        "precio_main": "numeric_mkptr6ge",# PRECIO TOTAL
        "precio_alt":  "n_meros_mkmmx03j",# PRECIO FONDO
        "enlace": "enlace_mkmmpkbk",
        "imagenes": "archivo8__1",
        "catastro": "texto_mkmm8w8t",
        "fecha_visita": "date_mkq9ggyk",
        "nolon": "texto5__1",             # REFERENCIA (si quieres)
        "subitems": "subitems__1",
    },
}

# ============ App / Twilio ============
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lifeway")

AUDIO_STORE: Dict[str, bytes] = {}
_twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) else None

# Memoria simple por llamada (CallSid)
SESS: Dict[str, Dict[str, Any]] = {}
SESSION_TTL_SECONDS = 900
def _sess(call_sid: str) -> Dict[str, Any]:
    s = SESS.get(call_sid) or {}
    s["ts"] = time.time()
    SESS[call_sid] = s
    # purge
    now = time.time()
    for k,v in list(SESS.items()):
        if now - (v.get("ts") or now) > SESSION_TTL_SECONDS:
            SESS.pop(k, None)
    return s

# ============ OpenAI ============
def _openai_chat(messages, temperature=0.3) -> str:
    if not OPENAI_API_KEY:
        # Si no hay OpenAI, devolvemos algo mínimo
        return "Ahora mismo no puedo entenderte, ¿puedes repetirlo con dirección, población o precio?"
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"},
        json={"model": OPENAI_MODEL, "messages": messages, "temperature": temperature},
        timeout=60
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def nlu_extract(user_text: str) -> Dict[str, Any]:
    """
    Extrae intención y pistas: ciudad/calle/precio, confirmaciones y posibles datos de contacto.
    """
    sys = (
        "Devuelve SOLO JSON válido:\n"
        "{'intent':'search|narrow|confirm|book|contact|chitchat',"
        " 'property_id':str|null, 'address':str|null, 'city':str|null, 'budget':float|null,"
        " 'name':str|null, 'phone':str|null, 'email':str|null, 'yesno':'yes'|'no'|null,"
        " 'lang':'es'|'ar'|'en'}\n"
        "Si menciona una ciudad (p.ej. Tarragona) ponla en 'city'. "
        "Si da calle o referencia, en 'address' o 'property_id'. Si da precio, en 'budget'. "
        "Detecta árabe si el texto contiene escritura árabe."
    )
    try:
        out = _openai_chat([{"role":"system","content":sys},{"role":"user","content":user_text}], temperature=0.1)
        return json.loads(out)
    except Exception:
        logger.exception("nlu_extract")
        return {"intent":"chitchat","property_id":None,"address":None,"city":None,"budget":None,"name":None,"phone":None,"email":None,"yesno":None,"lang":"es"}

def translate(text: str, target_lang: str) -> str:
    if target_lang == "es": return text
    try:
        sys = f"Traduce al { 'árabe' if target_lang=='ar' else 'inglés' }, sin añadir nada extra."
        return _openai_chat([{"role":"system","content":sys},{"role":"user","content":text}], temperature=0)
    except Exception:
        return text

# ============ ElevenLabs TTS ============
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

def speak_tts(vr: VoiceResponse, text_es: str, lang: str, base_url: str):
    text = translate(text_es, lang) if lang != "es" else text_es
    try:
        data = eleven_tts_to_bytes(text)
        if data:
            aid = str(uuid.uuid4()); AUDIO_STORE[aid] = data
            vr.play(f"{base_url}/audio/{aid}.mp3")
            return
    except Exception:
        logger.exception("TTS error")
    # Fallback
    vr.say(text_es, language="es-ES")

# ============ Monday helpers ============
def monday_query(query: str, variables: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    if not MONDAY_API_KEY: raise RuntimeError("MONDAY_API_KEY (o monday_api) no configurada")
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
    q = """query($id:[ID!]){ items(ids:$id){ id name column_values{ id text value } } }"""
    d = monday_query(q, {"id": [item_id]})
    arr = d.get("items") or []
    return arr[0] if arr else {}

def monday_get_assets(asset_ids: List[int]) -> List[Dict[str, Any]]:
    if not asset_ids: return []
    q = "query($ids:[Int]){ assets(ids:$ids){ id name public_url } }"
    return monday_query(q, {"ids": asset_ids}).get("assets") or []

def _cv_map(item: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {cv["id"]: cv for cv in (item.get("column_values") or [])}

def _get_text(item: Dict[str, Any], col_id: str) -> str:
    cv = _cv_map(item).get(col_id)
    return (cv.get("text") or "").strip() if cv else ""

def _get_date(item: Dict[str, Any], col_id: str) -> Optional[str]:
    cv = _cv_map(item).get(col_id)
    if not cv: return None
    # intentar text (ya viene "YYYY-MM-DD")
    if cv.get("text"):
        t = cv["text"].strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t): return t
        return t
    # o value json
    if cv.get("value"):
        try:
            return json.loads(cv["value"]).get("date")
        except Exception:
            return None
    return None

def _parse_asset_ids(value: Optional[str]) -> List[int]:
    if not value: return []
    try:
        data = json.loads(value)
        files = (data.get("files") or data.get("assets") or []) if isinstance(data, dict) else []
        ids=[]
        for f in files:
            _id = f.get("id") or f.get("asset_id") or f.get("assetId")
            if _id is None: continue
            try: ids.append(int(_id))
            except: pass
        return ids
    except Exception:
        return []

def extract_image_urls(item: Dict[str, Any], board_id: int) -> List[str]:
    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    cv = _cv_map(item).get(m["imagenes"])
    ids = _parse_asset_ids(cv.get("value") if cv else None)
    assets = monday_get_assets(ids)
    out=[]
    seen=set()
    for a in assets:
        u = a.get("public_url")
        if u and u not in seen:
            seen.add(u); out.append(u)
    return out[:5]

def _fmt_fecha_humana(iso_date: Optional[str]) -> str:
    if not iso_date: return "pendiente de confirmar"
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        meses = ["enero","febrero","marzo","abril","mayo","junio","julio",
                 "agosto","septiembre","octubre","noviembre","diciembre"]
        return f"{dt.day} de {meses[dt.month-1]} de {dt.year}"
    except Exception:
        return iso_date or ""

def build_property_summary(item: Dict[str, Any], board_id: int, lang: str="es") -> str:
    if not item:
        msg = "No he encontrado esa propiedad."
        return translate(msg, lang) if lang != "es" else msg
    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    nombre    = item.get("name") or "Propiedad"
    direccion = _get_text(item, m["direccion"])
    poblacion = _get_text(item, m["poblacion"])
    precio    = _get_text(item, m["precio_main"]) or _get_text(item, m["precio_alt"])
    fecha_leg = _fmt_fecha_humana(_get_date(item, m["fecha_visita"]))
    lines = [
        f"{nombre}",
        f"Dirección: {direccion or '-'}",
        f"Población: {poblacion or '-'}",
        f"Precio: {precio or '-'}",
        f"Fecha de visita: {fecha_leg}",
    ]
    txt = "\n".join(lines)
    return translate(txt, lang) if lang != "es" else txt

# ============ Búsqueda ============
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9áéíóúüñ ]", " ", (s or "").lower())

def _price_to_float(s: str) -> Optional[float]:
    if not s: return None
    nums = re.findall(r"\d+", s.replace(".", "").replace(",", ""))
    if not nums: return None
    try: return float("".join(nums))
    except: return None

def monday_fetch_items_for_search(board_id: int, max_items: int = 200) -> List[Dict[str, Any]]:
    items=[]; cursor=None; remaining=max_items
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

def _score_item_for_city(item: Dict[str, Any], board_id: int, city: str) -> float:
    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    city_ = _get_text(item, m["poblacion"])
    return 1.0 if _norm(city) in _norm(city_) else 0.0

def _score_item(item: Dict[str, Any], board_id: int, address: str, budget: Optional[float]) -> float:
    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    dir_ = _get_text(item, m["direccion"])
    price_txt = _get_text(item, m["precio_main"]) or _get_text(item, m["precio_alt"])
    price = _price_to_float(price_txt)
    sc = 0.0
    if address and _norm(address) in _norm(dir_): sc += 3.0
    if budget and price and abs(price - budget) <= 0.2*(budget): sc += 2.0
    if _norm(address) in _norm(item.get("name","")): sc += 1.0
    return sc

# ============ WhatsApp ============
def _twilio_params_base(to_e164: str) -> Dict[str, Any]:
    if _twilio is None: raise RuntimeError("Twilio no configurado")
    return {"to": f"whatsapp:{to_e164}", "from_": f"whatsapp:{TWILIO_PHONE_E164}"}

def send_whatsapp_text(to_e164: str, text: str) -> Optional[str]:
    try:
        p = _twilio_params_base(to_e164); p["body"]=text
        return _twilio.messages.create(**p).sid
    except Exception:
        logger.exception("send_whatsapp_text")
        return None

def send_whatsapp_images(to_e164: str, media_urls: List[str]) -> List[str]:
    sids=[]
    for u in media_urls[:3]:
        try:
            p = _twilio_params_base(to_e164); p["media_url"]=[u]
            sids.append(_twilio.messages.create(**p).sid)
        except Exception:
            logger.exception("send_whatsapp_images")
    return sids

# ============ Subitems ============
def _sub_cols_for_board(board_id: int) -> Dict[str, Optional[str]]:
    # Por ahora solo REOS (CESIONES se añadirá si pasas los IDs)
    return {
        "name":   SUB_REOS_NAME_COL_ID,
        "phone":  SUB_REOS_PHONE_COL_ID,
        "email":  SUB_REOS_EMAIL_COL_ID,
        "date":   SUB_REOS_DATE_COL_ID,
        "nolon":  SUB_REOS_NOLON_COL_ID,
        "status": SUB_REOS_STATUS_COL_ID,
    }

def monday_create_subitem_with_contact(
    parent_item_id: int,
    board_id: int,
    subitem_title: str,
    name_val: Optional[str],
    phone_val: Optional[str],
    email_val: Optional[str],
    date_val_iso: Optional[str],
    nolon_val_text: Optional[str],
) -> Optional[int]:
    # 1) crear subitem
    q1 = """mutation($pid:Int!, $name:String!){
      create_subitem(parent_item_id:$pid, item_name:$name){ id }
    }"""
    d1 = monday_query(q1, {"pid": parent_item_id, "name": subitem_title})
    try:
        sub_id = int(d1["create_subitem"]["id"])
    except Exception:
        return None

    # 2) set columnas
    cols_ids = _sub_cols_for_board(board_id)
    payload = {}
    if cols_ids.get("name") and name_val:
        payload[cols_ids["name"]] = name_val
    if cols_ids.get("phone") and phone_val:
        payload[cols_ids["phone"]] = phone_val
    if cols_ids.get("email") and email_val:
        payload[cols_ids["email"]] = email_val
    if cols_ids.get("nolon") and nolon_val_text:
        payload[cols_ids["nolon"]] = nolon_val_text
    if cols_ids.get("date") and date_val_iso:
        payload[cols_ids["date"]] = {"date": date_val_iso}
    if cols_ids.get("status"):
        # Escribe el label "CONFIRMADA" si existe en tu status
        payload[cols_ids["status"]] = {"label": "CONFIRMADA"}

    if payload:
        q2 = """mutation($item:Int!, $cv: JSON!){
          change_column_values(item_id:$item, column_values:$cv){ id }
        }"""
        monday_query(q2, {"item": sub_id, "cv": payload})
    return sub_id

# ============ Flask helpers ============
def ok_json(data: Any, code: int = 200):
    return Response(json.dumps(data, ensure_ascii=False), status=code, mimetype="application/json")

def _board_from_request() -> int:
    bid = request.values.get("board_id") or (request.get_json(silent=True) or {}).get("board_id")
    try:
        if bid: return int(str(bid))
    except: pass
    return MONDAY_DEFAULT_BOARD_ID

def _new_gather():
    return Gather(
        input="speech",
        method="POST",
        timeout=8,
        action="/gather",
        actionOnEmptyResult="true",
        speechTimeout="auto",
        language="es-ES"
    )

# ============ Rutas ============
@app.get("/healthz")
def health(): return ok_json({"ok": True})

@app.get("/audio/<aid>.mp3")
def audio(aid: str):
    data = AUDIO_STORE.get(aid)
    if not data: abort(404)
    return send_file(io.BytesIO(data), mimetype="audio/mpeg", download_name=f"{aid}.mp3")

WELCOME = "Hola, gracias por llamar a Lifeway. ¿En qué puedo ayudarte?"

@app.post("/voice")
def voice_inbound():
    vr = VoiceResponse()
    base = request.url_root.rstrip("/")
    speak_tts(vr, WELCOME, "es", base)
    vr.append(_new_gather())
    return Response(str(vr), mimetype="application/xml")

@app.post("/gather")
def gather_handler():
    base = request.url_root.rstrip("/")
    call_sid = request.values.get("CallSid") or str(uuid.uuid4())
    state = _sess(call_sid)
    speech = (request.values.get("SpeechResult") or "").strip()
    from_num = (request.values.get("From") or "").replace("whatsapp:", "")
    board_id = _board_from_request()
    vr = VoiceResponse()

    try:
        if not speech:
            speak_tts(vr, "No te he escuchado bien. ¿Puedes repetirlo?", state.get("lang","es"), base)
            vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")

        info = nlu_extract(speech)
        lang = info.get("lang") or state.get("lang") or "es"
        state["lang"] = lang

        # Si estamos pidiendo detalle para desambiguar
        if state.get("step") == "need_detail":
            address = info.get("address")
            budget  = float(info.get("budget") or 0) or None
            prop_id = info.get("property_id")
            items   = state.get("cand_items") or []
            if prop_id:
                state["item_id"] = int(str(prop_id)); state["step"] = "offer_day"
            else:
                best, best_sc = None, -1.0
                for it in items:
                    sc = _score_item(it, board_id, address or "", budget)
                    if sc > best_sc: best_sc, best = sc, it
                if best and best_sc >= 2.0:
                    state["item_id"] = int(best["id"]); state["step"] = "offer_day"
                else:
                    speak_tts(vr, "No estoy seguro de cuál es. ¿Recuerdas la calle exacta o la referencia?", lang, base)
                    vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")

        # Disparo inicial
        if state.get("step") is None:
            if info.get("property_id"):
                state["item_id"] = int(str(info["property_id"])); state["step"]="offer_day"
            else:
                city = info.get("city")
                address = info.get("address")
                budget = float(info.get("budget") or 0) or None
                if city and not (address or budget):
                    all_items = monday_fetch_items_for_search(board_id, 200)
                    cand = [it for it in all_items if _score_item_for_city(it, board_id, city) >= 1.0]
                    if not cand:
                        speak_tts(vr, f"No he encontrado inmuebles en {city}. ¿Quieres que busque otra zona?", lang, base)
                        vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")
                    state["cand_items"] = cand; state["step"]="need_detail"
                    speak_tts(vr, f"Tenemos varias opciones en {city}. ¿Recuerdas la calle, la referencia o el precio aproximado?", lang, base)
                    vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")
                if address or budget:
                    all_items = monday_fetch_items_for_search(board_id, 200)
                    best, best_sc = None, -1.0
                    for it in all_items:
                        sc = _score_item(it, board_id, address or "", budget)
                        if sc > best_sc: best_sc, best = sc, it
                    if best and best_sc >= 2.0:
                        state["item_id"] = int(best["id"]); state["step"]="offer_day"
                    else:
                        speak_tts(vr, "No tengo claro cuál es. ¿Podrías indicarme la calle exacta o la referencia?", lang, base)
                        vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")
                if state.get("step") is None:
                    speak_tts(vr, "Puedo ayudarte con dirección, población o presupuesto. ¿Cuál te interesa?", lang, base)
                    vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")

        # Ofrecer día de visita
        if state.get("step") == "offer_day":
            item = monday_get_item(state["item_id"])
            resumen = build_property_summary(item, board_id, lang).replace("\n", ". ")
            speak_tts(vr, resumen + " ¿Quiere que le diga el día de visita previsto para esa vivienda?", lang, base)
            state["step"] = "await_yes_day"
            vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")

        # Esperar confirmación para leer el día
        if state.get("step") == "await_yes_day":
            yesno = info.get("yesno")
            item = monday_get_item(state["item_id"])
            m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
            fecha_iso = _get_date(item, m["fecha_visita"])
            fecha_leg = _fmt_fecha_humana(fecha_iso)
            if yesno == "no":
                speak_tts(vr, "De acuerdo. ¿Quiere más información de la vivienda o prefiere otra zona?", lang, base)
                state["step"] = None; vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")
            speak_tts(vr, f"El día previsto de visita es {fecha_leg}. Las visitas se realizan por orden de llegada de 10 a 17 horas. ¿Quiere que le anote para esa fecha?", lang, base)
            state["step"] = "await_book_confirm"; vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")

        # Confirmación para anotar
        if state.get("step") == "await_book_confirm":
            if info.get("yesno") == "no":
                speak_tts(vr, "Perfecto, no le anoto por ahora. ¿En qué más puedo ayudarle?", lang, base)
                state["step"]=None; vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")
            state["step"]="collect_contact"
            speak_tts(vr, "Para anotarle, ¿me indica su nombre, teléfono y correo electrónico?", lang, base)
            vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")

        # Recibir contacto y crear subitem
        if state.get("step") == "collect_contact":
            name  = info.get("name")  or "Interesado"
            phone = info.get("phone") or (from_num if from_num.startswith("+") else "")
            email = info.get("email") or ""
            item_id = state.get("item_id")
            if not item_id:
                speak_tts(vr, "Se me perdió la referencia. ¿Puede repetir la dirección o la referencia de la vivienda?", lang, base)
                state["step"]=None; vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")

            item = monday_get_item(item_id)
            m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
            fecha_iso = _get_date(item, m["fecha_visita"])
            nolon_txt = _get_text(item, m.get("nolon",""))

            monday_create_subitem_with_contact(
                parent_item_id=int(item_id),
                board_id=board_id,
                subitem_title=f"{name} - {phone or 's/tel'} - {email or 's/email'}",
                name_val=name, phone_val=phone, email_val=email,
                date_val_iso=fecha_iso, nolon_val_text=nolon_txt
            )

            # WhatsApp de confirmación + imágenes
            try:
                if phone.startswith("+") and _twilio is not None:
                    summary = build_property_summary(item, board_id, lang)
                    send_whatsapp_text(phone, translate("Reserva anotada. Te enviamos la ficha por aquí.", lang) + "\n\n" + summary)
                    send_whatsapp_images(phone, extract_image_urls(item, board_id))
            except Exception:
                logger.exception("whatsapp confirm")

            speak_tts(vr, "Perfecto, queda anotado. ¿Puedo ayudarte en algo más?", lang, base)
            state["step"]=None; vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")

        # fallback charla
        chat_es = _openai_chat(
            [{"role":"system","content":f"Eres asistente telefónico de {BRAND_NAME}. Sé natural, breve y útil."},
             {"role":"user","content": speech}]
        )
        speak_tts(vr, chat_es, lang, base)
        vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")

    except Exception:
        logger.exception("gather error")
        speak_tts(vr, "Ha ocurrido un problema. Probemos otra vez.", state.get("lang","es"), base)
        vr.append(_new_gather())
        return Response(str(vr), mimetype="application/xml")

# WhatsApp entrante (opcional)
@app.post("/whatsapp")
def whatsapp_inbound():
    body  = (request.values.get("Body") or "").strip()
    board_id = _board_from_request()
    reply = MessagingResponse()
    try:
        info = nlu_extract(body)
        lang = info.get("lang","es")
        if info.get("property_id"):
            item = monday_get_item(int(str(info["property_id"])))
            reply.message(build_property_summary(item, board_id, lang))
        else:
            reply.message("Dime dirección, población o presupuesto y te digo opciones.")
    except Exception:
        logger.exception("whatsapp_inbound"); reply.message("Error procesando la solicitud.")
    return Response(str(reply), mimetype="application/xml")

# ============ OPS de prueba ============
def _require_ops():
    if not OPS_TOKEN or request.headers.get("X-Auth") != OPS_TOKEN:
        abort(403)

@app.post("/ops/book-visit")
def ops_book_visit():
    _require_ops()
    d = request.get_json(force=True)
    item_id  = int(d.get("item_id", 0))
    board_id = int(d.get("board_id", MONDAY_DEFAULT_BOARD_ID))
    name     = d.get("name") or "Interesado"
    phone    = d.get("phone") or ""
    email    = d.get("email") or ""
    if not item_id:
        return ok_json({"ok": False, "error":"Falta item_id"}, 400)

    item = monday_get_item(item_id)
    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    fecha_iso = _get_date(item, m["fecha_visita"])
    nolon_txt = _get_text(item, m.get("nolon",""))

    sub_id = monday_create_subitem_with_contact(
        parent_item_id=item_id, board_id=board_id,
        subitem_title=f"{name} - {phone or 's/tel'} - {email or 's/email'}",
        name_val=name, phone_val=phone, email_val=email,
        date_val_iso=fecha_iso, nolon_val_text=nolon_txt
    )

    return ok_json({"ok": True, "subitem_id": sub_id})

# ============ Main ============
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
