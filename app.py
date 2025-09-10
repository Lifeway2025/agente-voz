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
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()

# Voz por defecto de ElevenLabs (puedes cambiarla por la tuya)
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")

# Modelo de OpenAI (estable y razonable)
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Número máximo de turnos de memoria por llamada
MAX_TURNS = int(os.getenv("MAX_TURNS", "6"))

# Almacenamiento en memoria (válido para pruebas / free tier)
SESSIONS: Dict[str, List[Dict[str, str]]] = {}      # CallSid -> [ {role, content}, ... ]
AUDIO_CACHE: Dict[str, bytes] = {}                   # audio_id -> mp3 data

# =========================
# Utilidades
# =========================

def log(*args):
    """Imprime en logs de Render con prefijo de tiempo."""
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
    """Prompt base para ‘razonamiento’ útil y tono profesional."""
    return (
        "Eres el asistente inmobiliario de Vekke/Lifeway. "
        "Respondes SIEMPRE en español, claro y profesional. "
        "Razona de forma implícita y entrega la respuesta en 1–3 frases. "
        "Si falta información, haz una pregunta útil para avanzar. "
        "Nunca digas simplemente ‘De acuerdo’. "
        "Si el usuario quiere visitar una propiedad, pide rango de fechas/horarios. "
        "Si pregunta por inventario, solicita zona/presupuesto y ofrece opciones."
    )

def clip_messages(history: List[Dict[str, str]], max_turns: int) -> List[Dict[str, str]]:
    """Corta la historia para no pasar demasiados tokens a OpenAI."""
    # Mantén el primer mensaje de sistema + últimos turnos
    system = [m for m in history if m["role"] == "system"][:1]
    rest = [m for m in history if m["role"] != "system"]
    return system + rest[-max_turns*2:]  # 2 por turno (user+assistant)

# =========================
# OpenAI via requests (robusto)
# =========================
def think_with_openai(user_text: str, history: List[Dict[str, str]]) -> str:
    """Llama a OpenAI Chat Completions con manejo de errores y fallback."""
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
            log("OpenAI HTTP error:", r.status_code, r.text)
            return ("Estoy teniendo un problema técnico con el análisis. "
                    "¿Puedes reformularlo brevemente o darme un poco más de contexto?")
        j = r.json()
        content = _safe_get(j, "choices", 0, "message", "content", default="").strip()
        if not content:
            log("OpenAI empty content:", json.dumps(j)[:1200])
            content = ("Te he oído, pero necesito un detalle más para ayudarte mejor. "
                       "¿Buscas información de alguna propiedad concreta o quieres agendar una visita?")
        # Evita respuestas demasiado vacías
        low = content.lower()
        if low in {"ok", "vale", "de acuerdo", "entendido"}:
            content = ("De acuerdo. ¿Te interesa información de alguna propiedad concreta "
                       "o prefieres que te proponga opciones y fechas de visita?")
        return content
    except Exception as e:
        log("OpenAI exception:", repr(e), traceback.format_exc())
        return ("Tu solicitud me llegó, pero tuve un problema al pensar la respuesta. "
                "¿Puedes repetirlo con otras palabras?")

# =========================
# ElevenLabs TTS (HTTP)
# =========================
def tts_elevenlabs(text: str) -> bytes:
    """Convierte texto a audio MP3 con ElevenLabs. Lanza excepción si falla."""
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
    # Evitar caching del lado de Twilio/CDN si vas variando respuestas
    resp.headers["Cache-Control"] = "no-store"
    return resp

# =========================
# Flujo de llamada (Twilio)
# =========================
@app.route("/voice", methods=["POST", "GET"])
def voice():
    """Primer saludo y gather de voz."""
    call_sid = request.values.get("CallSid", "unknown")
    # Inicia historia si no existe
    if call_sid not in SESSIONS:
        SESSIONS[call_sid] = [{"role": "system", "content": build_system_prompt()}]

    vr = VoiceResponse()
    gather = vr.gather(
        input="speech",
        language="es-ES",
        speechTimeout="auto",
        action="/gather",
        method="POST",
    )
    gather.say("Hola, soy tu asistente virtual. ¿En qué puedo ayudarte?", voice="alice", language="es-ES")
    # Si no contesta, reintenta
    vr.redirect("/voice")
    return Response(str(vr), mimetype="text/xml")

@app.route("/gather", methods=["POST"])
def gather_handler():
    """Recibe lo dicho, llama a OpenAI y responde con voz (ElevenLabs o Polly)."""
    call_sid = request.values.get("CallSid", "unknown")
    user_text = (request.values.get("SpeechResult") or "").strip()
    log(f"gather: CallSid={call_sid}, user='{user_text}'")

    # Obtiene conversación previa
    history = SESSIONS.get(call_sid, [{"role": "system", "content": build_system_prompt()}])

    # Razonamiento con OpenAI
    reply_text = think_with_openai(user_text or "No he entendido nada.", history)

    # Actualiza memoria
    history.append({"role": "user", "content": user_text or "(vacío)"})
    history.append({"role": "assistant", "content": reply_text})
    SESSIONS[call_sid] = clip_messages(history, MAX_TURNS)

    vr = VoiceResponse()
    try:
        # Intenta ElevenLabs TTS
        mp3 = tts_elevenlabs(reply_text)
        audio_id = uuid.uuid4().hex[:12]
        AUDIO_CACHE[audio_id] = mp3
        audio_url = (request.url_root.rstrip("/") + f"/audio/{audio_id}")
        vr.play(audio_url)
    except Exception as e:
        # Fallback a voz de Twilio (Polly)
        log("TTS fallback (Polly):", repr(e))
        vr.say(reply_text, voice="alice", language="es-ES")

    # Vuelve a escuchar al usuario para continuar
    gather = vr.gather(
        input="speech",
        language="es-ES",
        speechTimeout="auto",
        action="/gather",
        method="POST",
    )
    gather.say("¿Algo más?", voice="alice", language="es-ES")
    return Response(str(vr), mimetype="text/xml")


# =========================
# Main (local)
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
