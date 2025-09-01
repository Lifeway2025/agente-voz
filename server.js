import express from "express";

const app = express();
app.use(express.urlencoded({ extended: false }));
app.use(express.json());

// Página raíz
app.get("/", (_req, res) => {
  res.send("✅ Backend de voz activo. Endpoint: POST /voice");
});

// Webhook de Twilio (Voice)
app.post("/voice", (req, res) => {
  const twiml = `<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="es-ES" voice="Polly.Conchita">
    ¡Gracias por ver el video!
  </Say>
</Response>`;
  res.type("text/xml").send(twiml);
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`✅ Servidor escuchando en ${PORT}`));
