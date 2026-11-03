#!/bin/sh
# Corre DENTRO de la imagen de producción (ver .github/workflows/tests.yml):
#   docker run --rm -i <imagen> sh /dev/stdin < scripts/verificar-imagen-produccion.sh
# Falla si la imagen trae tests o dependencias de desarrollo, si la app no se puede
# importar o si no corre con el usuario sin privilegios.
#
# Cada comprobación es un "if" que termina en exit 1. No se usa "! comando" ni
# "cond && falla": con set -e, sh no corta ante el fallo de esas formas.

falla() {
    echo "FALLA: $1"
    exit 1
}

for ruta in /srv/tests /srv/pytest.ini /srv/requirements-dev.txt; do
    if [ -e "$ruta" ]; then falla "la imagen trae $ruta"; fi
done
for modulo in pytest httpx2; do
    if python -c "import $modulo" 2>/dev/null; then falla "la imagen trae el paquete de desarrollo $modulo"; fi
done
if ! python -c "import app.main"; then falla "la app no se puede importar con las dependencias de producción"; fi
if [ "$(id -u)" != 10001 ]; then falla "la imagen no corre con el usuario sin privilegios"; fi

echo "OK: imagen de producción sin tests ni dependencias de desarrollo"
