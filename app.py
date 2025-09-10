from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Servidor activo (voz).", 200

@app.route("/health", methods=["GET"])
def health():
    return "ok", 200

# Primer saludo + escucha (STT de Twilio)
@app.route("/voice", methods=["POST"])
def voice():
    vr = VoiceResponse()
    gather = vr.gather(
        input="speech",
        language="es-ES",
        speechTimeout="auto",
        action="/gather",
        method="POST"
    )
    gather.say("Hola, soy el asistente virtual. ¿En qué puedo ayudarte?",
               voice="alice", language="es-ES")
    # Si no habla, repetimos
    vr.redirect("/voice")
    return Response(str(vr), mimetype="text/xml")

# Respuesta simple: repite lo que entendió y vuelve a escuchar
@app.route("/gather", methods=["POST"])
def gather_handler():
    said = (request.form.get("SpeechResult") or "").strip()
    vr = VoiceResponse()
    if said:
        vr.say(f"Has dicho: {said}. ¿Algo más?",
               voice="alice", language="es-ES")
    else:
        vr.say("No te he oído bien. Vamos a intentarlo de nuevo.",
               voice="alice", language="es-ES")
    vr.redirect("/voice")
    return Response(str(vr), mimetype="text/xml")
