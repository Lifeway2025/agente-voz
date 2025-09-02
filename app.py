from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
import os
import requests

app = Flask(__name__)

# --- Configuración Twilio ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER")

# Cliente Twilio
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

@app.route("/")
def index():
    return "🚀 Servidor activo. Tu agente de voz está corriendo."

# ✅ Llamada entrante
@app.route("/voice", methods=["POST"])
def voice():
    resp = VoiceResponse()
    resp.say("¡Hola! Gracias por llamar. Tu agente de voz ya está funcionando en español.", language="es-ES")

    # Enviar WhatsApp de prueba
    try:
        twilio_client.messages.create(
            from_="whatsapp:" + TWILIO_NUMBER,
            to="whatsapp:+34624467104",   # <-- pon aquí tu número verificado en WhatsApp
            body="📲 Hola, este es un mensaje automático de prueba desde tu agente de voz."
        )
    except Exception as e:
        print("Error enviando WhatsApp:", e)

    return Response(str(resp), mimetype="application/xml")


if __name__ == "__main__":
   app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
