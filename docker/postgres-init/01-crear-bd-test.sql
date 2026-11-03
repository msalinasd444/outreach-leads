-- Desarrollo local y CI: BD separada para que los tests no toquen los datos de la app.
-- Postgres lo ejecuta únicamente cuando el volumen está vacío (primer arranque);
-- en el CI el volumen siempre es nuevo. En producción (Neon) no se usa.
CREATE DATABASE outreach_test;
