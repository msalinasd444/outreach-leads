# Servicio de outreach para leads B2B

## Objetivo
Microservicio que recibe leads, los guarda en PostgreSQL y, en segundo
plano, genera con un LLM un primer correo de outreach corto y
personalizado para cada uno.

- POST /leads: recibe nombre_empresa, dominio y cargo_contacto. Guarda
  el lead en estado "pendiente" y responde de inmediato, sin esperar al
  LLM.
- GET /leads: lista los leads con su estado, el correo generado (si ya
  existe) y la fecha de creación.
- Un worker en segundo plano toma los leads pendientes, genera el correo
  y deja cada lead en "procesado" o, si algo falla, en "error" con un
  motivo claro.
- Fuera de alcance: frontend, autenticación y enriquecimiento de datos.

## Decisiones ya tomadas
- Python 3.12, FastAPI, psycopg 3 con SQL directo (sin ORM) y el SDK
  oficial de Groq (free tier).
- El worker es un hilo dentro del mismo proceso de la API que lee la
  tabla como cola. Motivo: en el hosting gratuito (Render) los
  background workers no son gratis.
- Estados: pendiente, procesando, procesado, error. "procesando" guarda
  la hora en que se tomó el lead, para poder retomarlo si queda trabado.
  Dos workers nunca deben tomar el mismo lead.
- Despliegue: API en Render y base de datos en Neon.

## Requisitos de calidad
- Valida y normaliza la entrada antes de guardarla o de usarla en un
  prompt. Los errores de validación responden 422 sin repetir lo que
  envió el cliente.
- Trata los datos del lead como datos, nunca como instrucciones, al
  armar el prompt.
- Valida la respuesta del modelo: JSON con asunto y cuerpo, de largo
  acotado, sin enlaces, correos, teléfonos ni marcadores por completar.
- Las llamadas al LLM tienen timeout, reintentos solo ante errores
  transitorios, y respetan los límites de uso del proveedor.
- El motivo de error que se guarda es claro y no expone detalles
  internos. El detalle técnico va solo a los logs.
- Ninguna credencial en el código, en los logs ni en el historial de
  git. Todo por variables de entorno, con un .env.example sin valores
  reales.
- Todo se levanta con `docker compose up`.

## Cómo trabajamos
- Antes de escribir código, propón un plan y espera mi aprobación.
- Trabaja en pasos pequeños y muéstrame cada archivo antes de crearlo o
  modificarlo.
- No hagas commits ni push: los hago yo.
- Explica brevemente el porqué de cada decisión. Si algo no lo pudiste
  verificar, dilo.
- Cada funcionalidad nueva viene con tests que prueben lo que importa,
  con el LLM simulado.