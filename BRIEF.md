# BRIEF: cómo dirigí al agente y cómo verifiqué

Construí el servicio con **Claude Code**. Para planificar y revisar sus
propuestas antes de aprobarlas usé además un asistente de chat (Claude).
Claude Code trabajó en **modo manual**: me mostraba cada archivo y cada
comando antes de ejecutarlo, y los commits, los push y los pull requests
los hice yo.

## 1. Cómo partí el problema y qué contexto le di

### El contexto fijo: `CLAUDE.md`

Antes de escribir código creé `CLAUDE.md`, que Claude Code lee al iniciar
cada sesión. Describe **qué** debe cumplir el sistema, no cómo
programarlo:

- **Objetivo y alcance:** los dos endpoints, el worker en segundo plano
  y lo que queda fuera (frontend, autenticación, enriquecimiento).
- **Decisiones ya tomadas**, con su motivo (ver abajo).
- **Requisitos de calidad:** validar y normalizar la entrada; tratar los
  datos del lead como datos en el prompt; validar la respuesta del
  modelo; timeouts y reintentos solo ante errores transitorios; motivos
  de error claros sin detalles internos; ningún secreto en el código,
  los logs ni el historial; todo con `docker compose up`.
- **Cómo trabajamos:** plan antes de código, pasos pequeños, mostrarme
  cada archivo antes de crearlo, no hacer commits, explicar cada
  decisión y decir qué no pudo verificar, y tests con el LLM simulado.

### Decisiones tomadas antes del primer prompt

|                             Decisión                        |                         Motivo                                   |
|                               ---                           |                          ---                                     | 
| Python, FastAPI, psycopg 3 con SQL directo y el SDK de Groq | Es el stack que mejor manejo |
| Worker como hilo dentro de la API, con la tabla como cola | En el plan gratuito de Render los background workers no son gratis |
| Estado intermedio `procesando` con la hora de la toma | Para retomar un lead que quede trabado |
| API en Render y base en Neon | El Postgres gratuito de Render expira a los 30 días; el de Neon no |

### Las fases y los prompts principales

Dividí el trabajo para separar persistencia y validación de lo que es
procesamiento en segundo plano. En cada fase pedí primero un plan, lo
revisé, y recién después el código.

**Fase 1, la base (sin worker ni LLM):**

> Empezamos por la fase 1: la base del proyecto, sin worker ni LLM
> todavía. […] No escribas código todavía. Propón un plan con la
> estructura de carpetas, el esquema de la tabla (cada columna y por
> qué), qué código HTTP devuelve el POST y por qué, cómo validas y
> normalizas la entrada, y cómo lo vamos a verificar.

**CI (PR #1):**

> CI en GitHub Actions que corra los tests contra un Postgres real en
> cada push y en cada pull request. Ojo: hoy la base `outreach_test` la
> crea un script de `docker-entrypoint-initdb.d` que solo existe en
> local; en el CI hay que resolverlo de otra forma. […] Propón un plan
> corto antes de escribir nada.

**Worker (PR #2).** El prompt listaba lo que debía cumplir, sin dictar
la solución:

> Un hilo dentro de la API […] toma leads "pendiente" de a uno (dos
> workers nunca deben tomar el mismo) […] llama al LLM fuera de
> cualquier transacción […] Retomar los leads trabados […] Llamadas con
> timeout; reintentos solo ante errores transitorios, respetando
> Retry-After; un único reintento si el contenido es inválido. Validar
> la salida del modelo […] Ojo con no rechazar texto legítimo en
> español, como "Estimad@s". […] Tests con el SDK real y un transporte
> HTTP simulado […] y que el POST no espera al LLM. Para cada decisión
> importante, explica el porqué y dime qué no pudiste verificar.

**Imagen de producción (PR #3):** que la imagen que construye Render no
incluya los tests ni las dependencias de desarrollo, y que el CI lo
compruebe.

## 2. Cuando la IA se equivocó

### Los tres más importantes

**1. La validación del correo contradecía a la de la entrada.** En el
plan del worker, Claude Code propuso rechazar en el correo cualquier
texto con forma de dominio (`palabra.tld`). Pero la API acepta nombres
de empresa como `Amazon.com` (hay un test que lo exige). Para esa
empresa, el modelo escribiría su nombre, la validación lo rechazaría, y
el lead terminaría siempre en `error`. Lo detecté comparando el plan con
lo que ya aceptaba la API, y pedí que la validación ignore el nombre de
la empresa del lead. Claude Code lo extendió a todas las reglas (para
que "100% Natural" no chocara con la regla del `%`), ignorando solo la
mención exacta: `Amazon.com/ofertas` se sigue rechazando.

**2. En producción, el modelo hablaba desde la empresa equivocada.**
En la primera prueba real con Groq, 2 de 3 correos decían "Me dirijo a
usted desde Acme SpA": el modelo confundía la empresa del destinatario
con la del remitente. Además, los 3 asuntos eran idénticos. Los tests no
podían detectarlo, porque validan la forma y no el sentido. Pedí
aclarar en el prompt quién escribe a quién y que el asunto se refiera al
área del cargo. En la segunda prueba, ningún correo repitió el error y
los asuntos cambiaron según el cargo. La misma prueba mostró que cada
correo gastaba unos 700 tokens y no los 455 estimados, así que bajé el
límite de llamadas por minuto (detalle en la sección 3).

**3. Un carácter invisible en un archivo de tests.** VS Code advirtió
que `tests/test_normalizacion.py` tenía separadores de línea Unicode. Lo
confirmé con `grep -nP '[\x{2028}\x{2029}]'`: en la línea 39 había un
U+2028 real en vez de la secuencia `\u2028`, y el caso se veía como
`"Acme Corp"`, un nombre válido que el test exigía rechazar. Los tests
pasaban igual, pero el archivo no se podía leer. Claude Code identificó
la causa (su herramienta de escritura convierte las secuencias
`\uXXXX` en el carácter real) y desde ahí escribió esos caracteres con
`chr(...)`.

### Otros momentos en que intervine

- **NFC en vez de NFKC.** El primer plan normalizaba la entrada con NFC,
  que deja pasar caracteres de ancho completo como `＜` o `＞`, útiles
  para disfrazar etiquetas en el prompt. Lo cambié a NFKC. En el mismo
  plan recorté complejidad que no aportaba al alcance: un sistema de
  migraciones, conversión a punycode y un filtro que nadie pidió.
- **Código difícil de leer.** Para un mensaje de error propuso
  `[", ".join(campos)] * bool(campos)`. Pedí reescribirlo con un `if`,
  porque el código tiene que poder explicarse sin herramientas.
- **Valores sin verificar.** Aceptaba `reasoning_effort` `none` y
  `default`, que son de los modelos Qwen y con `gpt-oss-20b` harían
  fallar cada lead con un 400; lo restringí a `low`, `medium` y `high`.
  Fijó 20 llamadas por minuto advirtiendo que no lo había verificado; lo
  recalculé con el límite de tokens del plan gratuito.
- **Un paso del plan que omitió.** En el plan del CI dijo que
  actualizaría el comentario del script de la base de tests ("Solo para
  desarrollo local"), porque ahora también lo usa el CI. Al implementar
  lo omitió; lo vi en el diff y pedí que lo corrigiera.
- **Un paquete de origen no verificado.** Propuso cambiar `httpx` por
  `httpx2` y advirtió que no había verificado su origen. Antes de
  aprobarlo lo confirmé en la documentación oficial de Pydantic y en el
  cambio correspondiente de Starlette, para descartar un paquete falso
  con nombre parecido.
- **Modo automático.** En una sesión nueva noté en la salida "Allowed by
  auto mode": Claude Code había ejecutado un script que modificó 5
  archivos sin pedirme permiso. Revisé el diff, que no hubiese nada incorrecto y volví a modo manual.

### Errores que detectaron los tests o el propio agente

- **Una condición de carrera en el apagado del worker**, encontrada por
  un test de punta a punta: si la app se detenía justo al arrancar el
  hilo, el aviso de parada se perdía y el worker dormía 10 minutos. Se
  corrigió el orden de las revisiones y se agregó un test que inicia y
  detiene el worker 30 veces.
- **El fencing necesitaba dos datos.** Identificar la toma solo por el
  número de intentos no bastaba: al devolver un lead sin contar la
  toma, el número se repite. Se agregó la hora de la toma.
- **Un test que se colgaba en vez de fallar** al quitar `SKIP LOCKED`.
  Se agregó un `statement_timeout` de 5 s a esas conexiones.
- **Una instrucción que inducía JSON inválido.** La nota del reintento
  decía "no escribas `\n`", y dentro de un JSON esa es la forma correcta
  de escribir un salto de línea. Se cambió para prohibir solo `\\n`.
- **`set -e` y `!` en un script.** El script que verifica la imagen de
  producción usaba `! python -c "import pytest"`, que `sh` no trata como
  error bajo `set -e`: el chequeo habría pasado aunque `pytest`
  estuviera instalado. Se cambió por `if` explícitos.

En cada paso, Claude Code rompió el código a propósito para confirmar
que los tests detectaban el error. Eso destapó tests que no probaban
nada, y se reescribieron.

## 3. Cómo comprobé que funciona

### Tests automáticos y CI

- **396 tests** con `docker compose run --rm --build api pytest`, contra
  un Postgres real y con el SDK real de Groq sobre un transporte HTTP
  simulado (sin red ni clave real). El detalle por archivo está en el
  README.
- **CI en verde en cada pull request:**
  - PR #1 (CI): ✓ en 35 s.
  - PR #2 (worker): ✓ en 41 s, con los 395 tests.
  - PR #3 (imagen de producción): ✓ en 36 s, incluida la verificación
    de la imagen.

### En local

Con la API levantada (`docker compose up -d --build`):

```
$ curl -s -i -X POST localhost:8000/leads -H "Content-Type: application/json" \
  -d '{"nombre_empresa":"Acme SpA","dominio":"https://www.acme.cl/","cargo_contacto":"Gerente de Operaciones"}'
HTTP/1.1 202 Accepted
{"id":1,"estado":"pendiente",...}

$ curl -s localhost:8000/leads
{"leads":[{"id":1,...,"dominio":"acme.cl",...,"estado":"pendiente","correo":null,...}],...}

$ docker compose exec db psql -U outreach -d outreach -c "SELECT id, dominio, estado FROM leads;"
 1 | acme.cl | pendiente
```

**Arranque desde cero** (`docker compose down -v` y `up -d --build`,
ejecutado por Claude Code y revisado por mí):

- El script de inicio creó la base de tests (`CREATE DATABASE` en los
  logs) y la app aplicó el esquema (`Esquema de base de datos
  aplicado`).
- `POST` con `"WWW.Acme.COM"` → 202, guardado como `acme.com`.
- Un dominio con `<script>` → 422, sin repetir lo enviado.
- Tras `docker compose restart api`, el esquema se reaplicó sin errores
  y el lead seguía ahí.
- En 79 líneas de logs de `api` y `db`: 0 apariciones de la contraseña y
  0 de `DATABASE_URL`.

### Con Groq real

|                                    | Primera prueba (Acme SpA) | Segunda prueba (Globex Chile) |
|              ---                   |             ---           |             ---               |
| Leads procesados al primer intento |           3 de 3          |           3 de 3              |
| "Desde la empresa del lead"        |      2 de 3 correos       |              0                |
| Asuntos                            |       Los 3 iguales       | Uno distinto por cargo        |
| Tokens de prompt medidos           |          565 a 567        |           655-657             |
| Tokens de respuesta                |          120-133          |           123-148             |
| Separación entre llamadas          | 4-5 s (15 por minuto)     | Unos 6 s (10 por minuto)      |

Con unos 800 tokens por correo y unos 8.000 por minuto en el plan
gratuito, caben unos 10 correos por minuto: de ahí `GROQ_RPM=10`.

### En producción

`https://outreach-leads-7m6i.onrender.com` (Render, con despliegue
automático solo después de que el CI pase):

- Logs del primer despliegue: `Esquema de base de datos aplicado`,
  `worker_iniciado` y `GET /health 200 OK`.
- `GET /health` → `{"estado":"ok"}`.
- `POST /leads` → `{"id":1,"estado":"pendiente",...}`.
- 15 segundos después, `GET /leads?limit=1` → el mismo lead en
  `procesado`, con el asunto "Optimizar la gestión comercial".

### Lo que no verifiqué

- El tiempo que da Render entre la señal de apagado y el corte. Si no
  alcanza, el lead queda en `procesando` y el rescate lo retoma a los 15
  minutos.
- Si Neon se suspende con la conexión mínima del pool abierta.
- El comportamiento con más de una instancia: `SKIP LOCKED` evita que
  dos tomen el mismo lead, pero el límite de llamadas es por proceso.

---

*Este BRIEF lo redacté con ayuda de un asistente de chat (Claude), a
partir de las notas que fuimos tomando en cada fase del desarrollo. Lo
revisé punto por punto y lo ajusté para que refleje lo que hice; cada
ejemplo y cada resultado corresponde a lo que ocurrió en este
repositorio.*  
