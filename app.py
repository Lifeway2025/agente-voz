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
from twilio.rest import Client

@app.route("/voice", methods=["POST"])
def voice():
    resp = VoiceResponse()
    resp.say("¡Gracias por llamar! Tu agente de voz ya está funcionando correctamente.", language="es-ES")

    # Enviar un WhatsApp al número verificado
    try:
        client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
        message = client.messages.create(
            from_="whatsapp:+14155238886",  # Sandbox de Twilio WhatsApp
            body="Hola 👋, tu llamada fue recibida correctamente. Este es un mensaje automático de tu agente de voz.",
            to="whatsapp:+34624467104"
        )
        print("WhatsApp enviado:", message.sid)
    except Exception as e:
        print("Error enviando WhatsApp:", e)

    return Response(str(resp), mimetype="application/xml")


if __name__ == "__main__":
   app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
