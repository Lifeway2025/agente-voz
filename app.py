from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse
import os

app = Flask(__name__)

@app.route("/")
def index():
    return "🚀 Hola, el agente de voz está en marcha!"

@app.route("/voice", methods=["POST"])
def voice():
    resp = VoiceResponse()
    resp.say("¡Gracias por llamar! Tu agente de voz ya está funcionando.", language="es-ES")
    return Response(str(resp), mimetype="application/xml")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
