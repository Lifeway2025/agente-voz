import os
import uuid
import time
import json
import traceback
from io import BytesIO
from typing import Dict, List

import requests
from flask import Flask, request, Response, send_file
from twilio.twiml.voice_response import VoiceResponse

app = Flask(__name__)

# =========================
# Configuración / Entorno
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL").strip()

MAX_TURNS = int(os.getenv("MAX_TURNS", "6"))

# Memoria simple en servidor (válido para pruebas)
SESSIONS: Dict[str, List[Dict[str, str]]] = {}  # CallSid -> [{role, content}, ...]
AUDIO_CACHE: Dict[str, bytes] = {}              # audio_id -> mp3 bytes
MONDAY_API_KEY = os.getenv("MONDAY_API_KEY", "").strip()
MONDAY_BOARD_PROPERTIES_ID = os.getenv("MONDAY_BOARD_PROPERTIES_ID", "").strip()
MONDAY_BOARD_VISITS_ID = os.getenv("MONDAY_BOARD_VISITS_ID", "").strip()
MONDAY_BOARD_LEADS_ID = os.getenv("MONDAY_BOARD_LEADS_ID", "").strip()

MONDAY_API_URL = "https://api.monday.com/v2"
# ==== Títulos de columnas en tus boards de Monday (AJUSTA ESTOS NOMBRES) ====
# Board de PROPIEDADES
COL_ZONA_TITLE   = "Zona"
COL_PRECIO_TITLE = "Precio"
COL_ESTADO_TITLE = "Estado"

# Board de VISITAS (si usas uno separado)
COL_VISITA_PROP_ID_TITLE = "Propiedad ID"
COL_VISITA_FECHA_TITLE   = "Fecha"
COL_VISITA_AGENTE_TITLE  = "Agente"
COL_VISITA_ESTADO_TITLE  = "Estado"

# Board de LEADS
COL_LEAD_TELEFONO_TITLE = "Teléfono"
COL_LEAD_NOTAS_TITLE    = "Notas"


# =========================
# Utilidades
# =========================
def log(*args):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}]", *args, flush=True)

def _safe_get(dct, *keys, default=None):
    cur = dct
    try:
        for k in keys:
            cur = cur[k]
        return cur
    except Exception:
        return default

def build_system_prompt() -> str:
    """Prompt base (rol del asistente)"""
    return (
        "Eres el asistente inmobiliario de Vekke/Lifeway. "
        "Respondes SIEMPRE en español, claro y profesional. "
        "Razona de forma implícita, entrega la respuesta en 1–3 frases y, si falta información, "
        "haz una pregunta útil para avanzar. "
        "Nunca digas simplemente ‘De acuerdo’. "
        "Si el usuario quiere visitar una propiedad, pide rango de fechas y horarios. "
        "Si pregunta por inventario, solicita zona y presupuesto y ofrece opciones."
    )

def clip_messages(history: List[Dict[str, str]], max_turns: int) -> List[Dict[str, str]]:
    """Limita el contexto que enviamos a OpenAI."""
    system = [m for m in history if m["role"] == "system"][:1]
    rest = [m for m in history if m["role"] != "system"]
    return system + rest[-max_turns*2:]  # user+assistant por turno
    def monday_graphql(query: str, variables: dict | None = None) -> dict:
    if not MONDAY_API_KEY:
        raise RuntimeError("Falta MONDAY_API_KEY en el servidor.")
    r = requests.post(
        MONDAY_API_URL,
        headers={
            "Authorization": MONDAY_API_KEY,
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    if r.status_code >= 400:
        log("Monday HTTP error:", r.status_code, r.text[:800])
        raise RuntimeError(f"Monday error {r.status_code}")
    j = r.json()
    if "errors" in j:
        log("Monday GraphQL errors:", j["errors"])
        raise RuntimeError("Monday GraphQL errors")
    return j.get("data", {})
    def monday_search_props(zona: str | None, min_price: int | None, max_price: int | None, limit: int = 5):
    """
    Devuelve propiedades filtradas por zona y rango de precio (búsqueda simple con paginado básico).
    Ajusta los títulos de columna arriba (COL_*_TITLE).
    """
    if not MONDAY_BOARD_PROPERTIES_ID:
        return []

    q = """
    query($board_id: [Int]) {
      boards (ids: $board_id) {
        items_page (limit: 200) {
          items {
            id
            name
            column_values { title text }
          }
        }
      }
    }
    """
    data = monday_graphql(q, {"board_id": int(MONDAY_BOARD_PROPERTIES_ID)})

    items = data.get("boards", [{}])[0].get("items_page", {}).get("items", [])
    out = []
    for it in items:
        # Mapea: { "Zona": "Centro", "Precio": "300000", "Estado": "Disponible", ... }
        cols = {cv["title"]: (cv.get("text") or "") for cv in it.get("column_values", [])}

        zona_text   = cols.get(COL_ZONA_TITLE, "")
        precio_text = cols.get(COL_PRECIO_TITLE, "")

        # Normaliza precio a int
        precio = None
        try:
            precio = int(precio_text.replace(".", "").replace(",", "").strip() or "0")
        except Exception:
            pass

        ok = True
        if zona and zona.lower() not in zona_text.lower():
            ok = False
        if min_price is not None and (precio is None or precio < min_price):
            ok = False
        if max_price is not None and (precio is None or precio > max_price):
            ok = False

        if ok:
            out.append({
                "id": it["id"],
                "titulo": it["name"],
                "zona": zona_text or None,
                "precio": precio,
                "estado": cols.get(COL_ESTADO_TITLE) or None,
            })
            if len(out) >= limit:
                break

    return out


def monday_get_visits(property_id: str):
    """
    Obtiene visitas asociadas a una propiedad (si usas board de visitas).
    Filtra por una columna 'Propiedad ID' que contenga el id de la propiedad.
    Ajusta títulos de columnas arriba.
    """
    if not MONDAY_BOARD_VISITS_ID:
        return []

    q = """
    query($board_id: [Int]) {
      boards(ids: $board_id) {
        items_page(limit: 200) {
          items {
            id
            name
            column_values { title text }
          }
        }
      }
    }
    """
    data = monday_graphql(q, {"board_id": int(MONDAY_BOARD_VISITS_ID)})
    items = data.get("boards", [{}])[0].get("items_page", {}).get("items", [])

    visits = []
    for it in items:
        cols = {cv["title"]: (cv.get("text") or "") for cv in it.get("column_values", [])}
        if cols.get(COL_VISITA_PROP_ID_TITLE, "").strip() == str(property_id).strip():
            visits.append({
                "id": it["id"],
                "fecha": cols.get(COL_VISITA_FECHA_TITLE) or None,
                "agente": cols.get(COL_VISITA_AGENTE_TITLE) or None,
                "estado": cols.get(COL_VISITA_ESTADO_TITLE) or None,
            })
    return visits


def monday_create_lead(nombre: str, telefono: str, nota: str = "") -> str:
    """
    Crea un lead en Monday (ajusta board y columnas).
    Devuelve el id del item creado.
    """
    if not MONDAY_BOARD_LEADS_ID:
        raise RuntimeError("Falta MONDAY_BOARD_LEADS_ID")

    mutation = """
    mutation($board: Int!, $item: String!, $colvals: JSON!) {
      create_item (board_id: $board, item_name: $item, column_values: $colvals) {
        id
      }
    }
    """

    # Construye el dict usando los TÍTULOS de columna (no los IDs)
    colvals = {
        COL_LEAD_TELEFONO_TITLE: telefono,
        COL_LEAD_NOTAS_TITLE: nota
    }

    data = monday_graphql(mutation, {
        "board": int(MONDAY_BOARD_LEADS_ID),
        "item": nombre,
        "colvals": json.dumps(colvals),
    })
    return data.get("create_item", {}).get("id")


# =========================
# OpenAI (Chat Completions)
# =========================
def think_with_openai(user_text: str, history: List[Dict[str, str]]) -> str:
    if not OPENAI_API_KEY:
        return "Falta la clave de OpenAI en el servidor."

    messages = history.copy()
    if not messages or messages[0]["role"] != "system":
        messages.insert(0, {"role": "system", "content": build_system_prompt()})
    messages.append({"role": "user", "content": user_text})
    messages = clip_messages(messages, MAX_TURNS)

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "temperature": 0.5,
        "max_tokens": 300,
    }

    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=30,
        )
        if r.status_code >= 400:
            log("OpenAI HTTP error:", r.status_code, r.text[:800])
            return ("Estoy teniendo un problema técnico al pensar la respuesta. "
                    "¿Puedes reformularlo brevemente o darme un poco más de contexto?")
        j = r.json()
        content = _safe_get(j, "choices", 0, "message", "content", default="").strip()
        if not content:
            log("OpenAI empty content:", json.dumps(j)[:1200])
            content = ("Te he oído, pero necesito un detalle más para ayudarte. "
                       "¿Buscas información de alguna propiedad concreta o quieres agendar una visita?")
        if content.lower() in {"ok", "vale", "de acuerdo", "entendido"}:
            content = ("De acuerdo. ¿Te interesa información de alguna propiedad concreta "
                       "o prefieres que te proponga opciones y fechas de visita?")
        return content
    except Exception as e:
        log("OpenAI exception:", repr(e), traceback.format_exc())
        return ("Tu solicitud me llegó, pero tuve un problema al pensar la respuesta. "
                "¿Puedes repetirlo con otras palabras?")


# =========================
# ElevenLabs TTS
# =========================
def tts_elevenlabs(text: str) -> bytes:
    """Convierte texto a MP3 usando ElevenLabs."""
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("Falta ELEVENLABS_API_KEY en el servidor.")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    payload = {
        "text": text,
        "model_id": "eleven_turbo_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.8},
        "optimize_streaming_latency": 2,
    }
    r = requests.post(
        url,
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    if r.status_code >= 400:
        log("ElevenLabs HTTP error:", r.status_code, r.text[:800])
        raise RuntimeError(f"ElevenLabs error {r.status_code}")
    return r.content

def play_tts_to(container, text: str) -> None:
    """
    Genera audio con ElevenLabs y lo añade al 'container' (VoiceResponse o Gather).
    Si ElevenLabs falla, usamos <Say> de Twilio como último recurso para no dejar la llamada muda.
    """
    try:
        mp3 = tts_elevenlabs(text)
        audio_id = uuid.uuid4().hex[:12]
        AUDIO_CACHE[audio_id] = mp3
        audio_url = (request.url_root.rstrip("/") + f"/audio/{audio_id}")
        container.play(audio_url)
    except Exception as e:
        log("TTS fallback (Polly) en play_tts_to:", repr(e))
        container.say(text, voice="alice", language="es-ES")


# =========================
# Rutas auxiliares
# =========================
@app.route("/", methods=["GET"])
def home():
    return "Servidor de voz activo.", 200

@app.route("/health", methods=["GET"])
def health():
    return {"ok": True}, 200

@app.route("/envcheck", methods=["GET"])
def envcheck():
    oa = OPENAI_API_KEY
    el = ELEVENLABS_API_KEY
    oa_mask = (oa[:7] + "…" + oa[-4:]) if oa else "(vacía)"
    el_mask = (el[:7] + "…" + el[-4:]) if el else "(vacía)"
    return (
        f"OPENAI_API_KEY: {bool(oa)} | {oa_mask}\n"
        f"ELEVENLABS_API_KEY: {bool(el)} | {el_mask}\n"
        f"MODEL: {OPENAI_MODEL}\n"
        f"VOICE_ID: {ELEVENLABS_VOICE_ID}"
    ), 200

@app.route("/audio/<clip_id>", methods=["GET"])
def audio_clip(clip_id):
    data = AUDIO_CACHE.get(clip_id)
    if not data:
        return "not found", 404
    resp = send_file(BytesIO(data), mimetype="audio/mpeg")
    resp.headers["Cache-Control"] = "no-store"
    return resp
# =========================
# Flujo de llamada (Twilio)
# =========================
@app.route("/voice", methods=["POST", "GET"])
def voice():
    """Saludo + primer gather (todo con ElevenLabs)."""
    call_sid = request.values.get("CallSid", "unknown")
    if call_sid not in SESSIONS:
        SESSIONS[call_sid] = [{"role": "system", "content": build_system_prompt()}]

    vr = VoiceResponse()

    # Importante: el saludo va DENTRO del gather
    gather = vr.gather(
        input="speech",
        language="es-ES",
        speechTimeout="auto",
        action="/gather",
        actionOnEmptyResult="true",
        method="POST",
    )
    play_tts_to(gather, "Hola, gracias por llamar a Lifeway, ¿en qué puedo ayudarte?")

    # Por si no hay entrada, reintenta
    vr.pause(length=2)
    vr.redirect("/voice")

    return Response(str(vr), mimetype="text/xml")


@app.route("/gather", methods=["POST"])
def gather_handler():
    """Recibe lo que dijo el usuario, razona con OpenAI y responde con ElevenLabs."""
    call_sid = request.values.get("CallSid", "unknown")
    user_text = (request.values.get("SpeechResult") or "").strip()
    log(f"gather: CallSid={call_sid}, user='{user_text}'")

    # Historial
    history = SESSIONS.get(call_sid, [{"role": "system", "content": build_system_prompt()}])

    # Pensar con OpenAI
    reply_text = think_with_openai(user_text or "No he entendido nada.", history)

    # Actualiza memoria
    history.append({"role": "user", "content": user_text or "(vacío)"})
    history.append({"role": "assistant", "content": reply_text})
    SESSIONS[call_sid] = clip_messages(history, MAX_TURNS)

    vr = VoiceResponse()

    # Respuesta principal (ElevenLabs)
    play_tts_to(vr, reply_text)

    # Nuevo turno (gather) y repregunta (también con Eleven)
    gather = vr.gather(
        input="speech",
        language="es-ES",
        speechTimeout="auto",
        action="/gather",
        actionOnEmptyResult="true",
        method="POST",
    )
    play_tts_to(vr, "¿Algo más?")

    # fallback por si no entra nada
    vr.pause(length=2)
    vr.redirect("/voice")

    return Response(str(vr), mimetype="text/xml")


# =========================
# Main (local)
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
