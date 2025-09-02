import os
from flask import Flask, Response, request
from twilio.twiml.voice_response import VoiceResponse, Connect

app = Flask(__name__)

WS_URL = os.getenv("PROVIDER_WS_URL", "").strip()  # wss://... de tu proveedor
PREROLL_TTS = os.getenv("PREROLL_TTS", "").strip() # Mensaje inicial opcional
TTS_LANG = os.getenv("TTS_LANG", "es-ES")          # Idioma <Say>
TTS_VOICE = os.getenv("TTS_VOICE", "Polly.Conchita")  # Voz compatible con Twilio <Say>

@app.route("/", methods=["GET"])
def root():
    return "OK - Voice gateway running."

@app.route("/voice", methods=["POST"])
def voice():
    # (Opcional) podrías validar firma de Twilio aquí si quieres
    if not WS_URL:
        # Si faltara la URL del proveedor, devolvemos un aviso hablado
        r = VoiceResponse()
        r.say("Configuración incompleta. Falta la dirección del proveedor.", language="es-ES")
        return Response(str(r), mimetype="application/xml")

    r = VoiceResponse()

    # Mensaje de cortinilla opcional antes de conectar
    if PREROLL_TTS:
        r.say(PREROLL_TTS, language=TTS_LANG, voice=TTS_VOICE)

    connect = Connect()
    # Conecta ambos canales de audio
    stream = connect.stream(url=WS_URL, track="both_tracks")

    # Puedes enviar metadatos al proveedor (los leerá como parámetros iniciales)
    # Añade sólo los que tengas en entorno:
    for env_key in [
        "TWILIO_ACCOUNT_SID",
        "WA_PHONE_ID",
        "MONDAY_API",        # si quieres pasar tu token de Monday, etc.
        "MAIL_FROM",
        "MAIL_SMTP",
        "WHATSAPP_TEMPLATE",
    ]:
        val = os.getenv(env_key)
        if val:
            stream.parameter(name=env_key.lower(), value=val)

    r.append(connect)
    return Response(str(r), mimetype="application/xml")

# (Opcional) webhook de estados de llamada si lo configuras en Twilio
@app.route("/status", methods=["POST"])
def status():
    # Solo registro sencillo para depurar
    print("Call status:", dict(request.form))
    return ("", 204)

if __name__ == "__main__":
    # Para desarrollo local; en Render se ejecuta con gunicorn
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
