from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
import requests
import os
import re

app = Flask(__name__)

# --- Credenciales Twilio (para la llamada de voz) ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER      = os.getenv("TWILIO_NUMBER")  # E.164, p.ej. +19786378560
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# --- Credenciales WhatsApp Cloud API (Meta) ---
WA_TOKEN     = os.getenv("WA_TOKEN")      # access token (permanent o de larga duración)
WA_PHONE_ID  = os.getenv("WA_PHONE_ID")   # phone_number_id de tu cuenta de WhatsApp Business
WA_API_URL   = f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}/messages"

def format_e164(num: str) -> str:
    """Devuelve el número en E.164 (+346XXXXXXXX). Twilio ya lo da así en 'From',
    pero normalizamos por si llega con espacios, etc."""
    if not num:
        return ""
    num = re.sub(r"[^\d+]", "", num)
    if not num.startswith("+") and num.startswith("00"):
        num = "+" + num[2:]
    return num

def send_whatsapp(to_e164: str, text: str) -> bool:
    """Envía WhatsApp vía Meta Cloud API."""
    try:
        payload = {
            "messaging_product": "whatsapp",
            "to": to_e164.replace("+",""),  # Meta acepta sin '+'
            "type": "text",
            "text": {"body": text}
        }
        headers = {
            "Authorization": f"Bearer {WA_TOKEN}",
            "Content-Type": "application/json"
        }
        r = requests.post(WA_API_URL, json=payload, headers=headers, timeout=10)
        return r.status_code in (200, 201)
    except Exception:
        return False

@app.route("/")
def index():
    return "🚀 Hola, el agente de voz está en marcha!"

@app.route("/voice", methods=["POST"])
def voice():
    """Webhook de Twilio: atiende la llamada y envía WhatsApp al llamante."""
    caller = format_e164(request.form.get("From"))
    resp = VoiceResponse()

    # Mensaje de voz
    resp.say("¡Gracias por llamar! Tu agente de voz ya está funcionando.", language="es-ES")

    # **WhatsApp** al número que llama
    if caller and WA_TOKEN and WA_PHONE_ID:
        send_whatsapp(
            caller,
            "Gracias por tu llamada. Te contactaremos en breve desde Atención al Cliente."
        )

    # Si quieres además mandar un SMS fallback (opcional):
    # if caller:
    #     twilio_client.messages.create(
    #         body="Gracias por tu llamada. Mensaje de respaldo por SMS.",
    #         from_=TWILIO_NUMBER,
    #         to=caller
    #     )

    return Response(str(resp), mimetype="application/xml")

# Endpoint de prueba para WhatsApp (sin llamar)
@app.route("/test-wa", methods=["GET"])
def test_wa():
    to = format_e164(request.args.get("to", ""))
    ok = send_whatsapp(to, "Mensaje de prueba: WhatsApp Cloud API OK ✅")
    return {"ok": ok, "to": to}, (200 if ok else 400)
    import requests, os, re
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse

app = Flask(__name__)

WA_TOKEN     = os.getenv("WA_TOKEN")
WA_PHONE_ID  = os.getenv("WA_PHONE_ID")
WA_API_URL   = f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}/messages"
WA_TPL_NAME  = os.getenv("WA_TEMPLATE_NAME", "hello_world")
WA_TPL_LANG  = os.getenv("WA_TEMPLATE_LANG", "es")

def e164(num:str)->str:
    if not num: return ""
    num = re.sub(r"[^\d+]", "", num)
    if num.startswith("00"): num = "+"+num[2:]
    return num

def wa_send_text(to_e164:str, text:str)->(bool,int,str):
    payload = {
        "messaging_product":"whatsapp",
        "to": to_e164.replace("+",""),
        "type":"text",
        "text":{"body": text}
    }
    h={"Authorization":f"Bearer {WA_TOKEN}","Content-Type":"application/json"}
    r = requests.post(WA_API_URL, json=payload, headers=h, timeout=10)
    try:
        return (r.status_code in (200,201), r.status_code, r.text)
    except:
        return (False, 0, "")

def wa_send_template(to_e164:str, name:str, lang:str, components=None)->(bool,int,str):
    payload = {
        "messaging_product":"whatsapp",
        "to": to_e164.replace("+",""),
        "type":"template",
        "template":{
            "name": name,
            "language":{"code": lang},
            "components": components or []
        }
    }
    h={"Authorization":f"Bearer {WA_TOKEN}","Content-Type":"application/json"}
    r = requests.post(WA_API_URL, json=payload, headers=h, timeout=10)
    try:
        return (r.status_code in (200,201), r.status_code, r.text)
    except:
        return (False, 0, "")

@app.route("/voice", methods=["POST"])
def voice():
    caller = e164(request.form.get("From"))
    resp = VoiceResponse()
    resp.say("¡Gracias por llamar! Tu agente de voz ya está funcionando.",
             voice="alice", language="es-ES")

    if caller and WA_TOKEN and WA_PHONE_ID:
        ok, code, body = wa_send_text(
            caller,
            "Gracias por tu llamada. Te contactaremos en breve."
        )
        if not ok:
            # Error típico cuando no hay ventana 24h: 470/400 con details “Unsupported message type”
            # Reintenta con PLANTILLA
            comp = [{
                "type":"body",
                "parameters":[{"type":"text","text":"¡gracias por llamar!"}]
            }]
            ok2, code2, body2 = wa_send_template(caller, WA_TPL_NAME, WA_TPL_LANG, comp)
            print(f"[WA template] ok={ok2} code={code2} body={body2}")
        else:
            print(f"[WA text] ok={ok} code={code} body={body}")

    return Response(str(resp), mimetype="application/xml")

@app.route("/")
def root():
    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
