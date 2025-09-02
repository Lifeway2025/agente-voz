from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
import os

app = Flask(__name__)

# ⚡ Cargamos credenciales desde las variables de entorno en Render
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER")

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

@app.route("/")
def index():
    return "🚀 Hola, el agente de voz está en marcha!"

@app.route("/voice", methods=["POST"])
def voice():
    """Responde cuando entra una llamada"""
    resp = VoiceResponse()
    resp.say("¡Gracias por llamar! Tu agente de voz ya está funcionando.", language="es-ES")

    # ⚡ EJEMPLO: enviar un SMS automático al número que llama
    from_number = request.form.get("From")  # número del cliente
    if from_number:
        client.messages.create(
            body="Gracias por tu llamada. Te contactaremos pronto.",
            from_=TWILIO_NUMBER,
            to=from_number
        )

    return Response(str(resp), mimetype="application/xml")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
