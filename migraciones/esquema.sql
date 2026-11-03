-- Esquema de la base de datos del servicio de outreach.
--
-- La app ejecuta este archivo completo en cada arranque, dentro de una sola
-- transacción. Por eso todo debe ser idempotente: correrlo de nuevo no cambia
-- nada ni falla.
--
-- Limitación: CREATE TABLE IF NOT EXISTS no modifica una tabla que ya existe.
-- Si más adelante cambia una columna o un CHECK, hay que agregar aquí abajo un
-- ALTER que también sea idempotente (p. ej. ADD COLUMN IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS leads (
    id              BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    nombre_empresa  TEXT        NOT NULL,
    dominio         TEXT        NOT NULL,
    cargo_contacto  TEXT        NOT NULL,
    estado          TEXT        NOT NULL DEFAULT 'pendiente',
    asunto          TEXT,
    cuerpo          TEXT,
    motivo_error    TEXT,
    intentos        INTEGER     NOT NULL DEFAULT 0,
    tomado_en       TIMESTAMPTZ,
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Segunda barrera: la API ya valida y normaliza, pero la BD no acepta datos
    -- que se salten esa validación (un script, un INSERT manual, un bug).
    CONSTRAINT leads_nombre_empresa_valido CHECK (
        char_length(nombre_empresa) BETWEEN 1 AND 120
        AND nombre_empresa = btrim(nombre_empresa)
        AND nombre_empresa !~ '[[:cntrl:]]'
    ),
    CONSTRAINT leads_cargo_contacto_valido CHECK (
        char_length(cargo_contacto) BETWEEN 1 AND 100
        AND cargo_contacto = btrim(cargo_contacto)
        AND cargo_contacto !~ '[[:cntrl:]]'
    ),
    -- Solo ASCII en minúsculas y al menos un punto (ajuste C: sin punycode).
    CONSTRAINT leads_dominio_valido CHECK (
        char_length(dominio) <= 253
        AND dominio ~ '^[a-z0-9-]+(\.[a-z0-9-]+)+$'
    ),

    CONSTRAINT leads_estado_valido CHECK (
        estado IN ('pendiente', 'procesando', 'procesado', 'error')
    ),
    CONSTRAINT leads_intentos_no_negativo CHECK (intentos >= 0),

    -- Coherencia entre estado y columnas: un estado imposible no se puede guardar.
    CONSTRAINT leads_procesando_con_hora CHECK (
        estado <> 'procesando' OR tomado_en IS NOT NULL
    ),
    CONSTRAINT leads_procesado_con_correo CHECK (
        estado <> 'procesado' OR (asunto IS NOT NULL AND cuerpo IS NOT NULL)
    ),
    CONSTRAINT leads_error_con_motivo CHECK (
        estado <> 'error' OR motivo_error IS NOT NULL
    )
);

-- GET /leads: más recientes primero; id desempata leads creados en el mismo instante.
CREATE INDEX IF NOT EXISTS leads_creado_en_idx
    ON leads (creado_en DESC, id DESC);

-- Cola del worker: solo filas pendientes, en orden de llegada.
CREATE INDEX IF NOT EXISTS leads_pendientes_idx
    ON leads (creado_en, id) WHERE estado = 'pendiente';

-- Rescate de leads trabados en "procesando".
CREATE INDEX IF NOT EXISTS leads_procesando_idx
    ON leads (tomado_en) WHERE estado = 'procesando';
