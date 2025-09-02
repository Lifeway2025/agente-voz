from twilio.request_validator import RequestValidator
import os

def is_valid_twilio_request(req) -> bool:
    # Necesita TWILIO_AUTH_TOKEN
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not token:
        return True  # si no tienes token, no bloquear (solo en pruebas)
    validator = RequestValidator(token)
    # URL pública de tu endpoint:
    url = request.url  # p.ej. https://agente-voz.onrender.com/voice
    # Todos los params form-data que Twilio envía:
    params = request.form.to_dict()
    signature = request.headers.get('X-Twilio-Signature', '')
    return validator.validate(url, params, signature)
