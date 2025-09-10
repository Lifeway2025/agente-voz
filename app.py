import os
from flask import Flask, request, send_file, Response
from twilio.twiml.voice_response import VoiceResponse, Gather
from openai import OpenAI
from elevenlabs import set_api_key, generate, save

app = Flask(__name__)

# === Configura claves desde Render ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

# Comprueba las claves
if not OPENAI_API_KEY or not ELEVENLABS_API_KEY:
    raise EnvironmentError("Faltan OPENAI_API_KEY o ELEVENLABS_API_KEY en las variables de entorno.")

client = OpenAI(api_key=OPENAI_API_KEY)
set_api_key(ELEVENLABS_API_KEY)

# === Función para obtener respuesta de OpenAI ===
def obtener_respuesta_openai(pregunta):
    respuesta = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Eres un asistente virtual amable y profesional. Responde breve y claro."},
            {"role": "user", "content": pregunta}
        ]
    )
    return respuesta.choices[0].message.content.strip()

# === Función para generar audio con ElevenLabs ===
def sintetizar_respuesta(texto):
    audio = generate(
        text=texto,
        voice="Bella",  # Cambia por la voz que prefieras en ElevenLabs
        model="eleven_multilingual_v2"
    )
    filename = "/tmp/respuesta.mp3"
    save(audio, filename)
    return filename

# === Ruta raíz: saluda y pide input ===
@app.route("/voice", methods=['POST', 'GET'])
def voice():
    vr = VoiceResponse()
    gather = Gather(input="speech", action="/gather", method="POST", speechTimeout="auto")
    gather.say("Hola, soy tu asistente virtual. ¿En qué puedo ayudarte?", language="es-ES", voice="Polly.Miguel")
    vr.append(gather)
    vr.say("No escuché nada. Por favor, vuelve a intentarlo.", language="es-ES")
    return Response(str(vr), mimetype='text/xml')

# === Ruta gather: procesa lo que dice el usuario ===
@app.route("/gather", methods=['POST'])
def gather():
    user_input = request.values.get("SpeechResult", "").strip()
    if not user_input:
        vr = VoiceResponse()
        vr.say("No entendí nada. Por favor, vuelve a intentarlo.", language="es-ES")
        vr.redirect("/voice")
        return Response(str(vr), mimetype='text/xml')

    # Obtiene respuesta de OpenAI
    respuesta = obtener_respuesta_openai(user_input)

    # Genera audio con ElevenLabs
    audio_path = sintetizar_respuesta(respuesta)

    # Responde a Twilio reproduciendo el audio generado
    vr = VoiceResponse()
    vr.play(url=request.host_url + f"audio?file={os.path.basename(audio_path)}")
    return Response(str(vr), mimetype='text/xml')

# === Sirve el audio generado ===
@app.route("/audio", methods=['GET'])
def audio():
    file = request.args.get("file", "respuesta.mp3")
    filepath = os.path.join("/tmp", file)
    if os.path.exists(filepath):
        return send_file(filepath, mimetype="audio/mpeg")
    else:
        return "Archivo no encontrado", 404

# === Ruta para comprobar variables de entorno ===
@app.route("/envcheck")
def envcheck():
    openai_ok = bool(OPENAI_API_KEY)
    eleven_ok = bool(ELEVENLABS_API_KEY)
    return f"OPENAI_API_KEY presente: {openai_ok}<br>ELEVENLABS_API_KEY presente: {eleven_ok}"

# === Inicio local ===
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
