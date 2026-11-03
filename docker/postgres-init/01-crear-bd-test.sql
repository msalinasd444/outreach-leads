-- Solo para desarrollo local: BD separada para que los tests no toquen los datos de la app.
-- Postgres lo ejecuta únicamente cuando el volumen está vacío (primer arranque).
CREATE DATABASE outreach_test;
