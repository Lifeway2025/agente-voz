# app.py — Lifeway Conversational Agent (Twilio + ElevenLabs + Monday + WhatsApp)
import os, io, json, time, uuid, logging, re
from typing import Any, Dict, Optional, List
from datetime import datetime

import requests
from flask import Flask, request, Response, abort, send_file
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient

# ------------------ ENV / CONFIG ------------------
def _env_str(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default

def _env_int(name: str, default: int) -> int:
    try: return int(_env_str(name, str(default)))
    except: return default

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

# Subitems REOS (subboard 2147303765) — IDs que ya me diste
SUB_REOS_NAME_COL_ID   = _env_str("SUB_REOS_NAME_COL_ID")   # name
SUB_REOS_PHONE_COL_ID  = _env_str("SUB_REOS_PHONE_COL_ID")  # phone_mks7jjxp
SUB_REOS_EMAIL_COL_ID  = _env_str("SUB_REOS_EMAIL_COL_ID")  # email_mks7kagf
SUB_REOS_DATE_COL_ID   = _env_str("SUB_REOS_DATE_COL_ID")   # date0
SUB_REOS_NOLON_COL_ID  = _env_str("SUB_REOS_NOLON_COL_ID")  # text_mkvs87qt
SUB_REOS_STATUS_COL_ID = _env_str("SUB_REOS_STATUS_COL_ID") # color_mkvst8na

BRAND_NAME = _env_str("BRAND_NAME", "Lifeway")
OPS_TOKEN  = _env_str("OPS_TOKEN")

# Column mapping (padres)
BOARD_MAP: Dict[int, Dict[str, str]] = {
    2147303762: {  # REOS
        "name": "name",
        "direccion": "texto_mkmm1paw",
        "poblacion": "texto__1",
        "precio_main": "n_meros_mkmmx03j",
        "precio_alt":  "",
        "enlace": "text_mkqrp4gr",
        "imagenes": "archivo8__1",
        "catastro": "texto_mkmm8w8t",
        "fecha_visita": "date_mkq9ggyk",
        "nolon": "numeric_mkrfw72b",   # NOLON en padre
        "subitems": "subitems__1",
    },
    2068339939: {  # CESIONES (para cuando lo activemos)
        "name": "name",
        "direccion": "texto_mkmm1paw",
        "poblacion": "texto__1",
        "precio_main": "numeric_mkptr6ge",
        "precio_alt":  "n_meros_mkmmx03j",
        "enlace": "enlace_mkmmpkbk",
        "imagenes": "archivo8__1",
        "catastro": "texto_mkmm8w8t",
        "fecha_visita": "date_mkq9ggyk",
        "nolon": "texto5__1",  # REFERENCIA
        "subitems": "subitems__1",
    },
}

# ------------------ Flask / Twilio ------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lifeway")

_twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) else None
AUDIO_STORE: Dict[str, bytes] = {}

# Estado por llamada
SESS: Dict[str, Dict[str, Any]] = {}
def _sess(call_sid: str) -> Dict[str, Any]:
    s = SESS.get(call_sid) or {"history": []}
    s["ts"] = time.time()
    SESS[call_sid] = s
    # limpiezas
    now = time.time()
    for k,v in list(SESS.items()):
        if now - (v.get("ts") or now) > 900: SESS.pop(k, None)
    return s

# ------------------ OpenAI core ------------------
def _openai_chat(messages, temperature=0.4) -> str:
    if not OPENAI_API_KEY:
        return "Ahora mismo no puedo procesar tu petición. ¿Puedes repetir con dirección, población o referencia?"
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}","Content-Type":"application/json"},
        json={"model": OPENAI_MODEL, "messages": messages, "temperature": temperature},
        timeout=60
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

# ------------------ ElevenLabs TTS ------------------
def eleven_tts_to_bytes(text: str) -> bytes:
    if not ELEVEN_API_KEY: return b""
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}",
        headers={"xi-api-key": ELEVEN_API_KEY, "accept":"audio/mpeg", "content-type":"application/json"},
        json={"text": text, "model_id":"eleven_multilingual_v2",
              "voice_settings":{"stability":0.55, "similarity_boost":0.8}},
        timeout=60
    )
    r.raise_for_status()
    return r.content

def speak(vr: VoiceResponse, text: str, lang: str, base_url: str):
    try:
        audio = eleven_tts_to_bytes(text)
        if audio:
            aid = str(uuid.uuid4()); AUDIO_STORE[aid] = audio
            vr.play(f"{base_url}/audio/{aid}.mp3"); return
    except Exception:
        log.exception("TTS")
    vr.say(text, language="es-ES")

# ------------------ Monday helpers ------------------
def monday_query(query: str, variables: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    if not MONDAY_API_KEY: raise RuntimeError("Falta MONDAY_API_KEY")
    r = requests.post(MONDAY_API_URL,
        headers={"Authorization": MONDAY_API_KEY, "Content-Type":"application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=60
    )
    r.raise_for_status()
    data=r.json()
    if "errors" in data: raise RuntimeError(str(data["errors"]))
    return data["data"]

def monday_get_item(item_id: int) -> Dict[str, Any]:
    q = """query($id:[ID!]){ items(ids:$id){ id name column_values{ id text value } } }"""
    d = monday_query(q, {"id":[item_id]})
    arr=d.get("items") or []
    return arr[0] if arr else {}

def monday_get_assets(asset_ids: List[int]) -> List[Dict[str, Any]]:
    if not asset_ids: return []
    q = "query($ids:[Int]){ assets(ids:$ids){ id name public_url } }"
    return monday_query(q, {"ids":asset_ids}).get("assets") or []

def _cv_map(item: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {cv["id"]: cv for cv in (item.get("column_values") or [])}

def _get_text(item: Dict[str, Any], col: str) -> str:
    cv=_cv_map(item).get(col); return (cv.get("text") or "").strip() if cv else ""

def _get_date(item: Dict[str, Any], col: str) -> Optional[str]:
    cv=_cv_map(item).get(col)
    if not cv: return None
    if cv.get("text") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", cv["text"].strip()):
        return cv["text"].strip()
    if cv.get("value"):
        try: return json.loads(cv["value"]).get("date")
        except: return None
    return None

def _parse_assets(value: Optional[str]) -> List[int]:
    if not value: return []
    try:
        data=json.loads(value)
        files=(data.get("files") or data.get("assets") or []) if isinstance(data, dict) else []
        out=[]
        for f in files:
            _id=f.get("id") or f.get("asset_id") or f.get("assetId")
            if _id is None: continue
            try: out.append(int(_id))
            except: pass
        return out
    except: return []

def extract_images(item: Dict[str, Any], board_id:int) -> List[str]:
    m=BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    cv=_cv_map(item).get(m["imagenes"])
    ids=_parse_assets(cv.get("value") if cv else None)
    assets=monday_get_assets(ids)
    urls=[]
    seen=set()
    for a in assets:
        u=a.get("public_url")
        if u and u not in seen:
            seen.add(u); urls.append(u)
    return urls[:5]

def human_date(iso: Optional[str]) -> str:
    if not iso: return "pendiente de confirmar"
    try:
        dt=datetime.strptime(iso,"%Y-%m-%d")
        meses=["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]
        return f"{dt.day} de {meses[dt.month-1]} de {dt.year}"
    except: return iso

def board_items_page(board_id:int, limit:int=200) -> List[Dict[str, Any]]:
    items=[]; cursor=None; remain=limit
    while remain>0:
        chunk=min(50,remain)
        q = """
        query($ids:[ID!], $limit:Int!, $cursor:String){
          boards(ids:$ids){
            items_page(limit:$limit, cursor:$cursor){
              items{ id name column_values{ id text } }
              cursor
            }
          }
        }"""
        d=monday_query(q, {"ids":[board_id],"limit":chunk,"cursor":cursor})
        page=(d.get("boards") or [{}])[0].get("items_page") or {}
        items+=page.get("items") or []
        cursor=page.get("cursor")
        if not cursor: break
        remain-=chunk
    return items

def _norm(s:str)->str: return re.sub(r"[^a-z0-9áéíóúüñ ]"," ",(s or "").lower())
def _digits(s:str)->str: return re.sub(r"\D","", s or "")
def _price(s:str)->Optional[float]:
    if not s: return None
    nums=re.findall(r"\d+", s.replace(".","").replace(",",""))
    if not nums: return None
    try: return float("".join(nums))
    except: return None

def score_item(item, m, address, budget)->float:
    dir_=_get_text(item, m["direccion"])
    price_txt=_get_text(item, m["precio_main"]) or _get_text(item, m["precio_alt"])
    price=_price(price_txt)
    sc=0.0
    if address and _norm(address) in _norm(dir_): sc+=3.0
    if budget and price and abs(price-budget)<=0.2*budget: sc+=2.0
    if _norm(address) in _norm(item.get("name","")): sc+=1.0
    return sc

def find_by_city(items, m, city)->List[Dict[str,Any]]:
    out=[]
    for it in items:
        if _norm(city) in _norm(_get_text(it, m["poblacion"])): out.append(it)
    return out

def find_by_nolon(board_id:int, nolon_digits:str)->Optional[Dict[str,Any]]:
    m=BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    col=m.get("nolon")
    if not col: return None
    for it in board_items_page(board_id, 300):
        if _digits(_get_text(it, col)) == nolon_digits:
            return it
    return None

# ------------------ WhatsApp ------------------
def _twilio_params_wa(to_e164: str) -> Dict[str, Any]:
    if _twilio is None: raise RuntimeError("Twilio no está configurado")
    return {"to": f"whatsapp:{to_e164}", "from_": f"whatsapp:{TWILIO_PHONE_E164}"}

def wa_text(to_e164: str, text: str):
    try:
        p=_twilio_params_wa(to_e164); p["body"]=text
        _twilio.messages.create(**p)
    except Exception: log.exception("wa_text")

def wa_images(to_e164: str, urls: List[str]):
    for u in urls[:3]:
        try:
            p=_twilio_params_wa(to_e164); p["media_url"]=[u]
            _twilio.messages.create(**p)
        except Exception: log.exception("wa_images")

# ------------------ Subitems (crear visita) ------------------
def sub_cols(board_id:int)->Dict[str, Optional[str]]:
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
    q1="""mutation($pid:Int!, $name:String!){
      create_subitem(parent_item_id:$pid, item_name:$name){ id }
    }"""
    d1=monday_query(q1, {"pid": parent_item_id, "name": title})
    try: sub_id=int(d1["create_subitem"]["id"])
    except: return None

    cols=sub_cols(board_id)
    payload={}
    if cols["name"]  and name:  payload[cols["name"]]  = name
    if cols["phone"] and phone: payload[cols["phone"]] = phone
    if cols["email"] and email: payload[cols["email"]] = email
    if cols["nolon"] and nolon_text: payload[cols["nolon"]] = nolon_text
    if cols["date"]  and date_iso: payload[cols["date"]]  = {"date": date_iso}
    if cols["status"]: payload[cols["status"]] = {"label": "CONFIRMADA"}

    if payload:
        q2="""mutation($item:Int!, $cv:JSON!){
          change_column_values(item_id:$item, column_values:$cv){ id }
        }"""
        monday_query(q2, {"item": sub_id, "cv": payload})
    return sub_id

# ------------------ Agent: tools + policy ------------------
AGENT_SYS = f"""
Eres {BRAND_NAME}, un asistente de voz inmobiliario amable y cercano.
Hablas español de forma natural; si detectas árabe o inglés, continúa en ese idioma.
Tu objetivo: entender lo que la persona quiere y ayudarla a encontrar la propiedad en Monday,
leyendo la ficha y ofreciendo anotar visita.

Herramientas disponibles (debes usarlas cuando tenga sentido):
- search_props(query) -> busca por dirección/ciudad/precio/NOLON.
- get_prop(item_id) -> devuelve ficha completa (dirección, población, precio, fecha visita).
- book_visit(item_id, name, phone, email) -> anota visita como subelemento y confirma.
- send_wa(item_id, phone) -> envía WhatsApp con ficha e imágenes (si hay).

Responde SIEMPRE en este JSON (y NADA MÁS):
{{
 "lang": "es|ar|en",
 "actions": [
   {{
     "type": "say",
     "text": "<lo que dices en voz natural, breve y empático>"
   }},
   {{
     "type": "tool",
     "name": "search_props|get_prop|book_visit|send_wa",
     "args": {{ ... }}
   }}
 ]
}}

Reglas:
- Si el cliente da una referencia tipo 597.444 o 597444, trátalo como NOLON y busca.
- Si hay ambigüedad, pregunta de forma amable (calle exacta, precio o referencia).
- Usa máximo 1-2 frases por turno.
- Tras buscar y elegir la propiedad, ofrece decir la fecha de visita y anotar.
- Si anotas, pide nombre, teléfono (si no lo tenemos) y email.
- Después de anotar, ofrece enviar ficha por WhatsApp.
"""

def agent_call(history: List[Dict[str,str]], tool_result: Optional[Dict[str,Any]]=None) -> Dict[str,Any]:
    msgs=[{"role":"system","content":AGENT_SYS}] + history
    if tool_result:
        msgs.append({"role":"system","content":"TOOL_RESULT:\n"+json.dumps(tool_result, ensure_ascii=False)})
    try:
        out=_openai_chat(msgs, temperature=0.5)
        return json.loads(out)
    except Exception:
        log.exception("agent_call")
        # fallback muy simple
        return {"lang":"es","actions":[{"type":"say","text":"¿Puedes darme dirección, población, precio o referencia para localizarla?"}]}

def tool_search_props(board_id:int, query:str)->Dict[str,Any]:
    # intenta NOLON directo
    nolon=re.search(r"\d[\d\.\s]{2,}\d", query or "")
    if nolon:
        item=find_by_nolon(board_id, _digits(nolon.group(0)))
        if item: return {"mode":"nolon","items":[item]}
    # heurística por dirección/ciudad/precio
    items=board_items_page(board_id, 300)
    m=BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])

    city=None; budget=None; address=None
    # heurística simple: si menciona palabras tipo "Tarragona", úsala como ciudad
    # (de verdad, el modelo ya trae el texto, esto solo complementa)
    city_match=re.search(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{3,}", query or "")
    if city_match: city=city_match.group(0)
    b=_price(query or ""); budget=b if b and b>=1000 else None
    if not city or not budget: address=query

    cand=[]
    if city:
        cand=find_by_city(items, m, city)
    if address or budget:
        best=None; best_sc=-1
        for it in items:
            sc=score_item(it, m, address or "", budget)
            if sc>best_sc: best_sc=sc; best=it
        if best and best_sc>=2.0:
            return {"mode":"best","items":[best]}
    return {"mode":"city" if city else "none","items":cand[:5]}

def tool_get_prop(board_id:int, item_id:int)->Dict[str,Any]:
    it=monday_get_item(item_id)
    m=BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    return {
        "id": item_id,
        "name": it.get("name"),
        "direccion": _get_text(it, m["direccion"]),
        "poblacion": _get_text(it, m["poblacion"]),
        "precio": _get_text(it, m["precio_main"]) or _get_text(it, m["precio_alt"]),
        "fecha_iso": _get_date(it, m["fecha_visita"]),
        "fecha_legible": human_date(_get_date(it, m["fecha_visita"])),
        "nolon": _get_text(it, m.get("nolon","")),
        "images": extract_images(it, board_id),
        "enlace": _get_text(it, m["enlace"]),
    }

def tool_book_visit(board_id:int, item_id:int, name:str, phone:str, email:str)->Dict[str,Any]:
    it=monday_get_item(item_id)
    m=BOARD_MAP.get(board_id, BOARD_MAP[MONDAY_DEFAULT_BOARD_ID])
    fecha_iso=_get_date(it, m["fecha_visita"])
    nolon_txt=_get_text(it, m.get("nolon",""))
    sub_id=create_subitem_contact(item_id, board_id,
                                  title=f"{name} - {phone or 's/tel'} - {email or 's/email'}",
                                  name=name, phone=phone, email=email,
                                  date_iso=fecha_iso, nolon_text=nolon_txt)
    return {"ok": bool(sub_id), "subitem_id": sub_id, "fecha": human_date(fecha_iso)}

def tool_send_wa(board_id:int, item_id:int, phone:str)->Dict[str,Any]:
    it=monday_get_item(item_id)
    info=tool_get_prop(board_id, item_id)
    summary=f"{info['name']}\nDirección: {info['direccion']}\nPoblación: {info['poblacion']}\nPrecio: {info['precio']}\nVisita: {info['fecha_legible']}\n{info.get('enlace') or ''}".strip()
    if phone.startswith("+"):
        wa_text(phone, summary)
        imgs=info.get("images") or []
        if imgs: wa_images(phone, imgs)
        return {"ok": True, "sent_images": len(imgs)}
    return {"ok": False, "error":"phone_not_e164"}

# ------------------ Twilio Voice ------------------
def _new_gather():
    return Gather(input="speech", method="POST", timeout=8,
                  action="/gather", speechTimeout="auto",
                  language="es-ES", actionOnEmptyResult="true")

WELCOME = "Hola, gracias por llamar a Lifeway. ¿En qué puedo ayudarte?"

@app.post("/voice")
def voice_in():
    vr=VoiceResponse(); base=request.url_root.rstrip("/")
    speak(vr, WELCOME, "es", base)
    vr.append(_new_gather())
    return Response(str(vr), mimetype="application/xml")

@app.post("/gather")
def gather():
    vr=VoiceResponse(); base=request.url_root.rstrip("/")
    call_sid=request.values.get("CallSid") or str(uuid.uuid4())
    text=(request.values.get("SpeechResult") or "").strip()
    from_num=(request.values.get("From") or "").replace("whatsapp:","")
    board_id=MONDAY_DEFAULT_BOARD_ID
    st=_sess(call_sid)

    try:
        if not text:
            speak(vr, "No te he escuchado bien, ¿puedes repetirlo?", "es", base)
            vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")

        # Añadimos turno de usuario al historial
        st["history"].append({"role":"user","content":text})

        # Llamamos al agente (puede responder y/o pedir herramientas)
        result = agent_call(st["history"])

        # Ejecutamos herramientas si las hay, y re-llamamos al agente con TOOL_RESULT
        tool_res=None
        for act in result.get("actions", []):
            if act.get("type")=="tool":
                name=act.get("name"); args=act.get("args") or {}
                if name=="search_props":
                    tool_res={"tool":"search_props","data": tool_search_props(board_id, args.get("query",""))}
                elif name=="get_prop":
                    tool_res={"tool":"get_prop","data": tool_get_prop(board_id, int(args.get("item_id",0)))}
                elif name=="book_visit":
                    namev=args.get("name") or "Interesado"
                    phonev=args.get("phone") or (from_num if from_num.startswith("+") else "")
                    emailv=args.get("email") or ""
                    tool_res={"tool":"book_visit","data": tool_book_visit(board_id, int(args.get("item_id",0)), namev, phonev, emailv)}
                elif name=="send_wa":
                    tool_res={"tool":"send_wa","data": tool_send_wa(board_id, int(args.get("item_id",0)), args.get("phone",""))}

        if tool_res:
            result = agent_call(st["history"], tool_result=tool_res)

        # Ahora decimos lo que corresponda (mayor naturalidad)
        lang=result.get("lang","es")
        for act in result.get("actions", []):
            if act.get("type")=="say" and act.get("text"):
                txt=act["text"]
                st["history"].append({"role":"assistant","content":txt})
                speak(vr, txt, lang, base)

        vr.append(_new_gather())
        return Response(str(vr), mimetype="application/xml")

    except Exception:
        log.exception("gather")
        speak(vr, "Se me fue el santo al cielo. ¿Repetimos?", "es", base)
        vr.append(_new_gather()); return Response(str(vr), mimetype="application/xml")

# ------------------ WhatsApp inbound (simple) ------------------
@app.post("/whatsapp")
def whatsapp_in():
    body=(request.values.get("Body") or "").strip()
    reply=MessagingResponse()
    reply.message("Hola, dime dirección, población, precio o referencia y te paso la ficha por aquí.")
    return Response(str(reply), mimetype="application/xml")

# ------------------ Audio cache ------------------
@app.get("/audio/<aid>.mp3")
def audio(aid:str):
    data=AUDIO_STORE.get(aid)
    if not data: abort(404)
    return send_file(io.BytesIO(data), mimetype="audio/mpeg", download_name=f"{aid}.mp3")

# ------------------ OPS de prueba ------------------
def _require_ops():
    if not OPS_TOKEN or request.headers.get("X-Auth") != OPS_TOKEN: abort(403)

@app.post("/ops/book-visit")
def ops_book():
    _require_ops()
    d=request.get_json(force=True)
    item_id=int(d["item_id"]); name=d.get("name","Test"); phone=d.get("phone","+34600000000"); email=d.get("email","test@lifeway.es")
    out=tool_book_visit(MONDAY_DEFAULT_BOARD_ID, item_id, name, phone, email)
    return Response(json.dumps(out, ensure_ascii=False), mimetype="application/json")

@app.get("/healthz")
def health(): return Response(json.dumps({"ok":True}), mimetype="application/json")

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","5000")))
