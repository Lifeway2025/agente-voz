# app.py
import os
import uuid
import requests
from io import BytesIO
from flask import Flask, request, Response, send_file
from twilio.twiml.voice_response import VoiceResponse

# ====== Config ======
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY") or os.getenv("eleven_labs_api", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://agente-voz.onrender.com")  # Cambia si tu URL es otra

# ====== App & store ======
app = Flask(__name__)
AUDIO_CACHE = {}  # id -> mp3 bytes (en memoria del proceso)

# ====== Utilidades ======
def think_with_openai(user_text: str) -> str:
    """Razona con OpenAI y devuelve una respuesta breve, clara y profesional."""
    if not OPENAI_API_KEY:
        return "Falta la clave de OpenAI en el servidor."
    if not user_text:
        return "No te he oído bien, ¿puedes repetirlo?"

    system_prompt = (
        "Eres el asistente inmobiliario de Vekke/Lifeway. "
        "Respondes SIEMPRE en español, breve, claro y profesional. "
        "Si falta información, pídela de forma natural. No inventes datos."
    )

    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-5",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 300,
            },
            timeout=30,
        )
        j = r.json()
        reply = (
            j.get("choices", [{}])[0]
             .get("message", {})
             .get("content", "")
            ).strip()
        return reply or "De acuerdo."
    except Exception as e:
        return "He tenido un problema al procesar tu petición. ¿Puedes repetirla?"

def tts_elevenlabs(text: str) -> bytes:
    """Convierte texto a voz (MP3) con ElevenLabs y devuelve los bytes."""
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("Falta ELEVENLABS_API_KEY en el servidor.")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    r = requests.post(
        url,
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
        },
        json={
            "text": text,
            "model_id": "eleven_turbo_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.8},
            "optimize_streaming_latency": 2,
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.content

# ====== Endpoints ======
@app.route("/", methods=["GET"])
def home():
    return "Servidor activo (voz).", 200

@app.route("/health", methods=["GET"])
def health():
    return "ok", 200

@app.route("/audio/<clip_id>", methods=["GET"])
def audio_clip(clip_id):
    data = AUDIO_CACHE.get(clip_id)
    if not data:
        return "not found", 404
    return send_file(BytesIO(data), mimetype="audio/mpeg")

@app.route("/voice", methods=["POST"])
def voice():
    vr = VoiceResponse()
    gather = vr.gather(
        input="speech",
        language="es-ES",
        speechTimeout="auto",
        action="/gather",
        method="POST",
    )
    gather.say(
        "Hola, soy el asistente virtual. ¿En qué puedo ayudarte?",
        voice="alice",
        language="es-ES",
    )
    # Si no habla, repetimos
    vr.redirect("/voice")
    return Response(str(vr), mimetype="text/xml")

@app.route("/gather", methods=["POST"])
def gather_handler():
    user_text = (request.form.get("SpeechResult") or "").strip()
    reply = think_with_openai(user_text)

    vr = VoiceResponse()
    try:
        # Generar audio con ElevenLabs
        mp3 = tts_elevenlabs(reply)
        clip_id = uuid.uuid4().hex[:12]
        AUDIO_CACHE[clip_id] = mp3
        audio_url = f"{PUBLIC_BASE_URL}/audio/{clip_id}"

        # Reproducir el MP3 con <Play> y volver a escuchar
        vr.play(audio_url)
        gather = vr.gather(
            input="speech",
            language="es-ES",
            speechTimeout="auto",
            action="/gather",
            method="POST",
        )
        # Fallback si por alguna razón no se reproduce el MP3
        gather.say("¿Algo más?", voice="alice", language="es-ES")
        return Response(str(vr), mimetype="text/xml")

    except Exception:
        # Si falla ElevenLabs, usamos TTS de Twilio como respaldo
        vr.say(reply, voice="alice", language="es-ES")
        vr.redirect("/voice")
        return Response(str(vr), mimetype="text/xml")

# ====== Run local (Render usa gunicorn vía Procfile) ======
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
    @app.route("/envcheck", methods=["GET"])
def envcheck():
    import os
    val = os.getenv("OPENAI_API_KEY", "")
    masked = (val[:7] + "…" + val[-4:]) if val else "(vacía)"
    return f"OPENAI_API_KEY presente: {bool(val)} | {masked}", 200
