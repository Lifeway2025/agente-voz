# app.py — Lifeway Voice Agent (razonador + herramientas)
# Twilio (voz) + ElevenLabs TTS + Monday (REOS) + WhatsApp + subelementos
#
# ENV necesarios:
# OPENAI_API_KEY
# OPENAI_MODEL (opcional, por defecto "gpt-4o-mini")
# ELEVEN_API_KEY (o ELEVENLABS_API_KEY)
# ELEVEN_VOICE_ID (o ELEVENLABS_VOICE_ID)   # voz de Eleven
# TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_E164  (p.ej. "+34930348966")
# MONDAY_API_KEY (o monday_api)
# MONDAY_DEFAULT_BOARD_ID=2147303762   # REOS BOT LIFEWAY (padre)
# SUB_REOS_NAME_COL_ID=name
# SUB_REOS_NOLON_COL_ID=text_mkvs87qt
# SUB_REOS_DATE_COL_ID=date0
# SUB_REOS_PHONE_COL_ID=phone_mks7jjxp
# SUB_REOS_EMAIL_COL_ID=email_mks7kagf
# SUB_REOS_STATUS_COL_ID=color_mkvst8na
# OPS_TOKEN  (para /ops/book-visit)

import os, io, json, time, uuid, logging, re
from typing import Any, Dict, Optional, List
from datetime import datetime

import requests
from flask import Flask, request, Response, abort, send_file
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient

# ------------- ENV & CONFIG -------------
def _env_str(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default

def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except Exception:
        logging.warning("ENV %s inválida, usando %s", name, default)
        return default

OPENAI_API_KEY   = _env_str("OPENAI_API_KEY")
OPENAI_MODEL     = _env_str("OPENAI_MODEL", "gpt-4o-mini")

ELEVEN_API_KEY   = _env_str("ELEVEN_API_KEY") or _env_str("ELEVENLABS_API_KEY")
ELEVEN_VOICE_ID  = _env_str("ELEVEN_VOICE_ID") or _env_str("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

TWILIO_ACCOUNT_SID = _env_str("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = _env_str("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_E164  = _env_str("TWILIO_PHONE_E164") or _env_str("TWILIO_NUMBER")

MONDAY_API_KEY   = _env_str("MONDAY_API_KEY") or _env_str("monday_api")
MONDAY_API_URL   = "https://api.monday.com/v2"
MONDAY_DEFAULT_BOARD_ID = _env_int("MONDAY_DEFAULT_BOARD_ID", 2147303762)  # REOS por defecto

# Subitems REOS (board 2147303765) — IDs proporcionados
SUB_REOS_NAME_COL_ID   = _env_str("SUB_REOS_NAME_COL_ID")   # name
SUB_REOS_PHONE_COL_ID  = _env_str("SUB_REOS_PHONE_COL_ID")  # phone_mks7jjxp
SUB_REOS_EMAIL_COL_ID  = _env_str("SUB_REOS_EMAIL_COL_ID")  # email_mks7kagf
SUB_REOS_DATE_COL_ID   = _env_str("SUB_REOS_DATE_COL_ID")   # date0
SUB_REOS_NOLON_COL_ID  = _env_str("SUB_REOS_NOLON_COL_ID")  # text_mkvs87qt
SUB_REOS_STATUS_COL_ID = _env_str("SUB_REOS_STATUS_COL_ID") # color_mkvst8na

BRAND_NAME = _env_str("BRAND_NAME", "Lifeway")
OPS_TOKEN  = _env_str("OPS_TOKEN")

# Columnas tablero padre (REOS y CESIONES opcional)
BOARD_MAP: Dict[int, Dict[str, str]] = {
    2147303762: {  # REOS BOT LIFEWAY (padre)
        "name": "name",
        "direccion": "texto_mkmm1paw",    # DIRECCIÓN
        "poblacion": "texto__1",          # POBLACIÓN
        "precio_main": "n_meros_mkmmx03j",# PRECIO
        "precio_alt":  "",
        "enlace": "text_mkqrp4gr",
        "imagenes": "archivo8__1",
        "catastro": "texto_mkmm8w8t",
        "fecha_visita": "date_mkq9ggyk",  # Fecha (día de visita)
        "nolon": "numeric_mkrfw72b",      # NOLON ID (padre)
        "subitems": "subitems__1",
    },
    2068339939: {  # CESIONES REMATE (si lo activas después)
        "name": "name",
        "direccion": "texto_mkmm1paw",
        "poblacion": "texto__1",
        "precio_main": "numeric_mkptr6ge",
        "precio_alt":  "n_meros_mkmmx03j",
        "enlace": "enlace_mkmmpkbk",
        "imagenes": "archivo8__1",
        "catastro": "texto_mkmm8w8t",
        "fecha_visita": "date_mkq9ggyk",
        "nolon": "texto5__1",
        "subitems": "subitems__1",
    },
}

# ------------- Flask/Twilio app -------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lifeway")

_twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) else None
AUDIO_STORE: Dict[str, bytes] = {}

# Memoria por llamada
SESS: Dict[str, Dict[str, Any]] = {}   # {CallSid: { "ts":..., "lang":"es", "history":[...]} }
SESSION_TTL = 900

def _sess(call_sid: str) -> Dict[str, Any]:
    s = SESS.get(call_sid) or {"history": []}
    s["ts"] = time.time()
    SESS[call_sid] = s
    now = time.time()
    for k, v in list(SESS.items()):
        if now - (v.get("ts") or now) > SESSION_TTL:
            SESS.pop(k, None)
    s["history"] = (s.get("history") or [])[-20:]
    return s

# ------------- OpenAI (rápido) -------------
def _openai_chat(messages, temperature=0.3) -> str:
    if not OPENAI_API_KEY:
        return ""
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type":"application/json"},
        json={"model": OPENAI_MODEL, "messages": messages, "temperature": temperature},
        timeout=40
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def nlu_extract(user_text: str) -> Dict[str, Any]:
    """Parser ligero y ROBUSTO: devuelve dict válido pase lo que pase."""
    sys = (
        "Devuelve SOLO JSON válido con claves:"
        "{'city':str|null,'address':str|null,'budget':float|null,'yesno':'yes'|'no'|null,"
        "'name':str|null,'phone':str|null,'email':str|null,'lang':'es'|'ar'|'en'}."
        " Si no sabes, usa null."
    )
    fallback_lang = "ar" if re.search(r"[\u0600-\u06FF]", user_text or "") else "es"
    fallback = {"city":None,"address":None,"budget":None,"yesno":None,"name":None,"phone":None,"email":None,"lang":fallback_lang}
    try:
        out = _openai_chat([{"role":"system","content":sys},{"role":"user","content":user_text}], temperature=0.1)
        if not out or not out.strip():
            return fallback
        if out.strip()[0] not in "{[":
            return fallback
        parsed = json.loads(out)
        if not isinstance(parsed, dict):
            return fallback
        for k in fallback.keys():
            if k not in parsed:
                parsed[k] = fallback[k]
        return parsed
    except Exception:
        log.exception("nlu_extract")
        return fallback

# ===================== AGENTE RAZONADOR (FUNCTION-CALLING) =====================
def _tool_defs():
    return [
        {
            "type": "function",
            "function": {
                "name": "search_properties",
                "description": "Busca inmuebles por texto libre (calle, ciudad, precio, referencia NOLON). Devuelve mejor candidato si lo hay.",
                "parameters": {
                    "type":"object",
                    "properties":{
                        "query":{"type":"string"},
                        "board_id":{"type":"integer"}
                    },
                    "required":["query","board_id"]
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name":"get_property_summary",
                "description":"Lee resumen de una propiedad (dirección, población, precio, día de visita).",
                "parameters":{
                    "type":"object",
                    "properties":{"item_id":{"type":"integer"}},
                    "required":["item_id"]
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name":"book_visit_subitem",
                "description":"Crea subelemento (reserva) con nombre/teléfono/email. Usa fecha del padre.",
                "parameters":{
                    "type":"object",
                    "properties":{
                        "item_id":{"type":"integer"},
                        "name":{"type":"string"},
                        "phone":{"type":"string"},
                        "email":{"type":"string"}
                    },
                    "required":["item_id","name"]
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name":"send_whatsapp_brief",
                "description":"Envía por WhatsApp el resumen y hasta 3 imágenes de la propiedad.",
                "parameters":{
                    "type":"object",
                    "properties":{
                        "phone":{"type":"string"},
                        "item_id":{"type":"integer"},
                        "board_id":{"type":"integer"}
                    },
                    "required":["phone","item_id","board_id"]
                },
            },
        },
    ]

def _run_tool(name:str, args:Dict[str,Any], call_sid:str, lang:str) -> Dict[str,Any]:
    try:
        if name == "search_properties":
            board_id = int(args["board_id"])
            q = str(args["query"])
            hit = search_flexible(board_id, q)
            if not hit:
                return {"ok": False, "reason":"no_match"}
            return {"ok":True, "item_id": int(hit["id"]), "name": hit.get("name")}

        elif name == "get_property_summary":
            it = monday_get_item(int(args["item_id"]))
            if not it: return {"ok":False,"reason":"not_found"}
            text = say_summary(it, MONDAY_DEFAULT_BOARD_ID, lang)
            return {"ok":True,"summary":text}

        elif name == "book_visit_subitem":
            item_id = int(args["item_id"])
            name    = (args.get("name") or "Interesado").strip()
            phone   = (args.get("phone") or "").strip()
            email   = (args.get("email") or "").strip()
            it = monday_get_item(item_id)
            if not it: return {"ok":False,"reason":"item_missing"}
            m = BOARD_MAP.get(MONDAY_DEFAULT_BOARD_ID, BOARD_MAP[2147303762])
            fecha_iso = _get_date(it, m["fecha_visita"])
            nolon_txt = _get_text(it, m.get("nolon",""))
            sid = create_subitem_contact(item_id, MONDAY_DEFAULT_BOARD_ID,
                                         title=f"{name} - {phone or 's/tel'} - {email or 's/email'}",
                                         name=name, phone=phone, email=email,
                                         date_iso=fecha_iso, nolon_text=nolon_txt)
            return {"ok": bool(sid), "subitem_id": sid}

        elif name == "send_whatsapp_brief":
            if _twilio is None: return {"ok":False,"reason":"twilio_not_configured"}
            phone   = str(args["phone"])
            item_id = int(args["item_id"])
            board   = int(args["board_id"])
            it = monday_get_item(item_id)
            if not it: return {"ok":False,"reason":"item_missing"}
            resumen = say_summary(it, board, "es")
            try:
                if phone.startswith("+"):
                    wa_text(phone, "Ficha y visita:\n\n"+resumen)
                    imgs = extract_images(it, board)
                    if imgs: wa_images(phone, imgs)
                return {"ok":True}
            except Exception:
                log.exception("send_whatsapp")
                return {"ok":False,"reason":"wa_error"}

        else:
            return {"ok":False,"reason":"unknown_tool"}
    except Exception:
        log.exception("tool_error %s", name)
        return {"ok":False,"reason":"exception"}

def reason_and_act(history:List[Dict[str,str]], call_sid:str, lang:str, board_id:int)->str:
    sys = (
        f"Eres un agente de {BRAND_NAME}, cercano, rápido y útil. "
        "Tu objetivo es ayudar con inmuebles (ubicación, precio, día de visita), "
        "aclarar dudas sencillas y, si procede, reservar visitas y enviar la ficha por WhatsApp. "
        "NO inventes datos de inventario: usa funciones. Si el usuario pregunta algo general, responde breve y vuelve a ofrecer ayuda con la propiedad."
    )
    messages = [{"role":"system","content":sys}] + history[-12:]
    tools = _tool_defs()

    while True:
        try:
            r = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type":"application/json"},
                json={
                    "model": OPENAI_MODEL,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice":"auto",
                    "temperature":0.3
                },
                timeout=40
            )
            r.raise_for_status()
            data = r.json()
            choice = data["choices"][0]
            msg = choice["message"]

            if "tool_calls" in msg:
                messages.append({"role":"assistant","tool_calls":msg["tool_calls"],"content":None})
                for tc in msg["tool_calls"]:
                    name = tc["function"]["name"]
                    args = json.loads(tc["function"]["arguments"] or "{}")
                    res = _run_tool(name, args, call_sid, lang)
                    messages.append({
                        "role":"tool",
                        "tool_call_id": tc["id"],
                        "name": name,
                        "content": json.dumps(res, ensure_ascii=False)
                    })
                continue

            return msg.get("content") or "¿En qué más puedo ayudarte?"

        except Exception:
            log.exception("reason_and_act")
            return "Se me fue un cable, pero ya está. ¿Me repites lo que necesitas?"

# ------------- ElevenLabs TTS -------------
def eleven_tts_to_bytes(text: str) -> bytes:
    if not ELEVEN_API_KEY:
        return b""
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}",
        headers={"xi-api-key": ELEVEN_API_KEY, "accept":"audio/mpeg", "content-type":"application/json"},
        json={"text": text, "model_id":"eleven_multilingual_v2",
              "voice_settings":{"stability":0.5, "similarity_boost":0.8}},
        timeout=40
    )
    r.raise_for_status()
    return r.content

def speak(vr: VoiceResponse, text: str, lang: str, base_url: str):
    speak_text = text
    if lang == "en":
        try:
            speak_text = _openai_chat(
                [{"role":"system","content":"Traduce al inglés con tono conversacional."},
                 {"role":"user","content":text}], temperature=0
            )
        except Exception:
            pass
    elif lang == "ar":
        try:
            speak_text = _openai_chat(
                [{"role":"system","content":"Traduce al árabe con tono cercano y claro."},
                 {"role":"user","content":text}], temperature=0
            )
        except Exception:
            pass
    try:
        audio = eleven_tts_to_bytes(speak_text)
        if audio:
            aid = str(uuid.uuid4()); AUDIO_STORE[aid] = audio
            vr.play(f"{base_url}/audio/{aid}.mp3")
            return
    except Exception:
        log.exception("TTS error")
    vr.say(text, language="es-ES")

# ------------- Monday helpers -------------
def monday_query(query: str, variables: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    if not MONDAY_API_KEY: raise RuntimeError("Falta MONDAY_API_KEY (o monday_api)")
    r = requests.post(MONDAY_API_URL,
                      headers={"Authorization": MONDAY_API_KEY, "Content-Type":"application/json"},
                      json={"query": query, "variables": variables or {}},
                      timeout=40)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(str(data["errors"]))
    return data["data"]

def monday_get_item(item_id: int) -> Dict[str, Any]:
    q = """query($id:[ID!]){ items(ids:$id){ id name column_values{ id text value } } }"""
    d = monday_query(q, {"id":[item_id]})
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
    if cv.get("text") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", cv["text"].strip()):
        return cv["text"].strip()
    if cv.get("value"):
        try: return json.loads(cv["value"]).get("date")
        except Exception: return None
    return None

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
            except: pass
        return out
    except Exception:
        return []

def extract_images(item: Dict[str, Any], board_id: int) -> List[str]:
    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    cv = _cv_map(item).get(m["imagenes"])
    ids = _parse_asset_ids(cv.get("value") if cv else None)
    assets = monday_get_assets(ids)
    urls=[]; seen=set()
    for a in assets:
        u=a.get("public_url")
        if u and u not in seen:
            seen.add(u); urls.append(u)
    return urls[:5]

def human_date(iso: Optional[str]) -> str:
    if not iso: return "pendiente de confirmar"
    try:
        dt = datetime.strptime(iso, "%Y-%m-%d")
        meses = ["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]
        return f"{dt.day} de {meses[dt.month-1]} de {dt.year}"
    except Exception:
        return iso or ""

# ------------- Búsqueda rápida y FLEXIBLE -------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9áéíóúüñ ]"," ", (s or "").lower())

def _digits(s: str) -> str:
    return re.sub(r"\D","", s or "")

def _price(s: str) -> Optional[float]:
    if not s: return None
    nums = re.findall(r"\d+", s.replace(".","").replace(",",""))
    if not nums: return None
    try: return float("".join(nums))
    except: return None

def board_items_page(board_id: int, limit: int = 200) -> List[Dict[str, Any]]:
    items=[]; cursor=None; remaining=limit
    while remaining>0:
        chunk=min(50, remaining)
        q = """
        query($ids:[ID!], $limit:Int!, $cursor:String){
          boards(ids:$ids){
            items_page(limit:$limit, cursor:$cursor){
              items{ id name column_values{ id text } }
              cursor
            }
          }
        }"""
        d = monday_query(q, {"ids":[board_id], "limit":chunk, "cursor":cursor})
        page=(d.get("boards") or [{}])[0].get("items_page") or {}
        items += page.get("items") or []
        cursor = page.get("cursor")
        if not cursor: break
        remaining -= chunk
    return items

def extract_nolon_candidate(text: str) -> Optional[str]:
    if not text: return None
    for m in re.finditer(r"\d[\d\.\s]{2,}\d", text):
        cand=_digits(m.group(0))
        if 5 <= len(cand) <= 9:
            return cand
    only=_digits(text)
    if 5 <= len(only) <= 9: return only
    return None

def find_by_nolon(board_id: int, nolon_digits: str) -> Optional[Dict[str, Any]]:
    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    col = m.get("nolon")
    if not col: return None
    for it in board_items_page(board_id, 300):
        if _digits(_get_text(it, col)) == nolon_digits:
            return it
    return None

def score_item(item, m, address, budget) -> float:
    dir_   = _get_text(item, m["direccion"])
    name_  = item.get("name","")
    pob_   = _get_text(item, m["poblacion"])
    price_txt = _get_text(item, m["precio_main"]) or _get_text(item, m["precio_alt"])
    price = _price(price_txt)

    s = 0.0
    adr = _norm(address or "")
    if adr:
        if adr in _norm(dir_):  s += 2.5
        if adr in _norm(name_): s += 1.0
        if adr in _norm(pob_):  s += 1.0
        toks = [t for t in re.split(r"\s+", adr) if len(t) >= 4]
        hit = sum(1 for t in toks if t in _norm(dir_) or t in _norm(name_) or t in _norm(pob_))
        s += 0.5 * min(hit, 3)
    if budget and price:
        if abs(price - budget) <= 0.25 * budget:
            s += 1.5
    return s

def find_by_city(items, m, city) -> List[Dict[str, Any]]:
    out=[]
    for it in items:
        if _norm(city) in _norm(_get_text(it, m["poblacion"])):
            out.append(it)
    return out

def search_flexible(board_id:int, text:str)->Optional[Dict[str,Any]]:
    if not text: return None
    nolon = extract_nolon_candidate(text)
    log.info("search_flexible: nolon=%s", nolon)
    if nolon:
        it = find_by_nolon(board_id, nolon)
        if it: return it

    items = board_items_page(board_id, 300)
    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    budget = _price(text)
    address = text
    best=None; best_sc=-1.0
    for it in items:
        sc = score_item(it, m, address, budget)
        if sc > best_sc:
            best_sc, best = sc, it
    return best if (best and best_sc >= 1.0) else None

# ------------- WhatsApp -------------
def _twilio_params_wa(to_e164: str) -> Dict[str, Any]:
    if _twilio is None: raise RuntimeError("Twilio no está configurado")
    return {"to": f"whatsapp:{to_e164}", "from_": f"whatsapp:{TWILIO_PHONE_E164}"}

def wa_text(to_e164: str, text: str):
    try:
        p = _twilio_params_wa(to_e164); p["body"]=text
        _twilio.messages.create(**p)
    except Exception:
        log.exception("wa_text")

def wa_images(to_e164: str, urls: List[str]):
    for u in urls[:3]:
        try:
            p = _twilio_params_wa(to_e164); p["media_url"]=[u]
            _twilio.messages.create(**p)
        except Exception:
            log.exception("wa_images")

# ------------- Subitems (crear visita) -------------
def sub_cols_reos() -> Dict[str, Optional[str]]:
    return {
        "name":   SUB_REOS_NAME_COL_ID,
        "phone":  SUB_REOS_PHONE_COL_ID,
        "email":  SUB_REOS_EMAIL_COL_ID,
        "date":   SUB_REOS_DATE_COL_ID,
        "nolon":  SUB_REOS_NOLON_COL_ID,
        "status": SUB_REOS_STATUS_COL_ID,
    }

def create_subitem_contact(parent_item_id:int, board_id:int,
                           title:str, name:str, phone:str, email:str,
                           date_iso:Optional[str], nolon_text:Optional[str]) -> Optional[int]:
    q1 = """mutation($pid:Int!, $name:String!){
      create_subitem(parent_item_id:$pid, item_name:$name){ id }
    }"""
    d1 = monday_query(q1, {"pid": parent_item_id, "name": title})
    try:
        sub_id = int(d1["create_subitem"]["id"])
    except Exception:
        return None

    cols = sub_cols_reos()
    payload={}
    if cols.get("name") and name:   payload[cols["name"]]  = name
    if cols.get("phone") and phone: payload[cols["phone"]] = phone
    if cols.get("email") and email: payload[cols["email"]] = email
    if cols.get("nolon") and nolon_text: payload[cols["nolon"]] = nolon_text
    if cols.get("date") and date_iso: payload[cols["date"]] = {"date": date_iso}
    if cols.get("status"): payload[cols["status"]] = {"label": "CONFIRMADA"}

    if payload:
        q2 = """mutation($item:Int!, $cv: JSON!){
          change_column_values(item_id:$item, column_values:$cv){ id }
        }"""
        monday_query(q2, {"item": sub_id, "cv": payload})
    return sub_id

# ------------- Textos -------------
def say_intro(lang: str) -> str:
    if lang=="ar": return "مرحبًا، شكرًا لاتصالك بـ لايفواي. كيف أقدر أساعدك؟"
    if lang=="en": return "Hi, thanks for calling Lifeway. How can I help?"
    return "Hola, gracias por llamar a Lifeway. ¿En qué puedo ayudarte?"

def say_summary(item: Dict[str,Any], board_id:int, lang:str) -> str:
    m = BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    nombre = item.get("name") or "Propiedad"
    dir_   = _get_text(item, m["direccion"]) or "-"
    pob    = _get_text(item, m["poblacion"]) or "-"
    precio = _get_text(item, m["precio_main"]) or _get_text(item, m["precio_alt"]) or "-"
    fecha  = human_date(_get_date(item, m["fecha_visita"]))
    if lang=="ar":
        return f"{nombre}. العنوان: {dir_}. المنطقة: {pob}. السعر: {precio}. يوم الزيارة: {fecha}."
    if lang=="en":
        return f"{nombre}. Address: {dir_}. City: {pob}. Price: {precio}. Visit day: {fecha}."
    return f"{nombre}. Dirección: {dir_}. Población: {pob}. Precio: {precio}. Día de visita: {fecha}."

# ------------- Flask helpers -------------
def ok_json(data: Any, code: int = 200):
    return Response(json.dumps(data, ensure_ascii=False), status=code, mimetype="application/json")

def _board_from_request() -> int:
    bid = request.values.get("board_id") or (request.get_json(silent=True) or {}).get("board_id")
    try:
        if bid: return int(str(bid))
    except Exception:
        pass
    return MONDAY_DEFAULT_BOARD_ID

def _new_gather():
    return Gather(
        input="speech",
        method="POST",
        timeout=10,
        action="/gather",
        actionOnEmptyResult="true",
        speechTimeout="auto",
        language="es-ES"
    )

# ------------- Rutas -------------
@app.get("/healthz")
def health(): return ok_json({"ok": True})

@app.get("/audio/<aid>.mp3")
def audio(aid: str):
    data = AUDIO_STORE.get(aid)
    if not data: abort(404)
    return send_file(io.BytesIO(data), mimetype="audio/mpeg", download_name=f"{aid}.mp3")

WELCOME = "Hola, gracias por llamar a Lifeway. ¿En qué puedo ayudarte?"

@app.post("/voice")
def voice_in():
    vr = VoiceResponse(); base = request.url_root.rstrip("/")
    speak(vr, WELCOME, "es", base)
    vr.append(_new_gather())
    return Response(str(vr), mimetype="application/xml")

@app.post("/gather")
def gather():
    vr = VoiceResponse(); base = request.url_root.rstrip("/")
    call_sid = request.values.get("CallSid") or str(uuid.uuid4())
    st = _sess(call_sid)
    board_id = _board_from_request()
    from_num = (request.values.get("From") or "").replace("whatsapp:","")
    speech = (request.values.get("SpeechResult") or "").strip()

    try:
        if not speech:
            speak(vr, "No te he escuchado bien. ¿Puedes repetirlo?", st.get("lang","es"), base)
            vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")

        # Detección rápida de idioma y NLU (por si hace falta)
        lang = "ar" if re.search(r"[\u0600-\u06FF]", speech) else (st.get("lang") or "es")
        info = nlu_extract(speech)
        if info.get("lang"): lang = info["lang"]
        st["lang"] = lang

        # ====== AGENTE GENERAL CON HERRAMIENTAS ======
        st["history"].append({"role":"user","content":speech})
        st["history"] = st["history"][-20:]

        # si hay contacto completo y ya se identificó la propiedad, el agente podrá llamar a book_visit_subitem;
        # para comodidad, dejamos el teléfono detectado en historial también si viene de Twilio
        if from_num.startswith("+") and not info.get("phone"):
            st["history"].append({"role":"system","content":f"Teléfono que llama: {from_num}"})

        agent_reply = reason_and_act(st["history"], call_sid, lang, board_id)

        st["history"].append({"role":"assistant","content":agent_reply})

        speak(vr, agent_reply, lang, base)
        vr.append(_new_gather())
        return Response(str(vr), mimetype="application/xml")

    except Exception:
        log.exception("gather error")
        speak(vr, "Se me fue un cable, pero ya está. ¿Me repites por favor?", st.get("lang","es"), base)
        vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")

# WhatsApp entrante (simple)
@app.post("/whatsapp")
def whatsapp_in():
    body  = (request.values.get("Body") or "").strip()
    reply = MessagingResponse()
    reply.message("Dime dirección, población, precio o referencia NOLON y te paso la ficha.")
    return Response(str(reply), mimetype="application/xml")

# OPS de prueba (crear subitem sin llamada)
def _require_ops():
    if not OPS_TOKEN or request.headers.get("X-Auth") != OPS_TOKEN: abort(403)

@app.post("/ops/book-visit")
def ops_book_visit():
    _require_ops()
    d = request.get_json(force=True)
    item_id = int(d.get("item_id", 0))
    name = d.get("name") or "Interesado"
    phone = d.get("phone") or "+34600000000"
    email = d.get("email") or "test@example.com"
    if not item_id: return ok_json({"ok": False, "error":"Falta item_id"}, 400)

    it = monday_get_item(item_id)
    m = BOARD_MAP.get(MONDAY_DEFAULT_BOARD_ID, BOARD_MAP[2147303762])
    fecha_iso = _get_date(it, m["fecha_visita"])
    nolon_text = _get_text(it, m.get("nolon",""))
    sub_id = create_subitem_contact(item_id, MONDAY_DEFAULT_BOARD_ID,
                                    title=f"{name} - {phone} - {email}",
                                    name=name, phone=phone, email=email,
                                    date_iso=fecha_iso, nolon_text=nolon_text)
    return ok_json({"ok": True, "subitem_id": sub_id})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","5000")))
