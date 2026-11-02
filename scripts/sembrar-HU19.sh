#!/usr/bin/env bash
# Crea los casos de prueba de HU19 (cobros fallidos) por la API real de MS Pagos. Guía: docs/como-probar-HU19.md.
#
# Uso:
#   scripts/sembrar-HU19.sh --local                    # levanta services/pagos/docker-compose.yml y siembra
#   scripts/sembrar-HU19.sh --staging [--si]           # siembra en el clúster del contexto actual de kubectl
#   scripts/sembrar-HU19.sh --local|--staging --pasarela <MODO>    # solo cambia el modo del simulador
#   scripts/sembrar-HU19.sh --staging --usuarios       # además crea el asesor y el operador demo (ver la guía)
#
# Pasos de la siembra:
#   1. Pagos en modo RECHAZAR.
#   2. Tres cobros de prima por la ruta interna POST /v1/pagos/cobros (en staging, por port-forward), con la clave de
#      servicio leída del secreto. La clave nunca se imprime.
#   3. Gracia ajustada por SQL: A normal (3 días), B próximo a vencer (6 h) y C vencido (hace 1 h).
#   4. Pagos de vuelta en modo APROBAR (también si algo falla a mitad).
#   5. Muestra los tres casos.
# Cada corrida crea casos nuevos (sufijo con la fecha y hora), así que se puede repetir.
set -euo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash en Windows: no convertir /tmp/... ni /v1/... en rutas de Windows

RAIZ=$(cd "$(dirname "$0")/.." && pwd)
# Docker para Windows no entiende /c/...: en Git Bash se le pasa la ruta como C:/...
if command -v cygpath > /dev/null; then COMPOSE_DIR=$(cygpath -m "$RAIZ/services/pagos"); else COMPOSE_DIR="$RAIZ/services/pagos"; fi
ENV_LOCAL="$COMPOSE_DIR/.env"
KUBECTL=${KUBECTL:-kubectl}
NAMESPACE=${NAMESPACE:-staging}
DEPLOY=solventa-pagos
PUERTO_TUNEL=${PUERTO_TUNEL:-18005}
MODOS="APROBAR RECHAZAR TARDAR NO_DISPONIBLE PERDER_RESPUESTA"

AMBIENTE="" PASARELA="" SI="" USUARIOS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --local) AMBIENTE=local ;;
    --staging) AMBIENTE=staging ;;
    --pasarela) PASARELA=$(echo "${2:-}" | tr '[:lower:]' '[:upper:]'); shift ;;
    --si) SI=1 ;;
    --usuarios) USUARIOS=1 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "Opción desconocida: $1 (use --help)" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$AMBIENTE" ] || { echo "Indique --local o --staging (use --help)." >&2; exit 2; }
if [ -n "$PASARELA" ] && ! echo " $MODOS " | grep -q " $PASARELA "; then
  echo "Modo de pasarela no válido: $PASARELA (use uno de: $MODOS)" >&2; exit 2
fi

paso() { printf '\n== %s\n' "$*"; }
falla() { echo "ERROR: $*" >&2; exit 1; }
aleatorio() { od -An -tx1 -N"$1" /dev/urandom | tr -d ' \n'; }

# --- Operaciones por ambiente -------------------------------------------------------------------------------------

compose() { docker compose --project-directory "$COMPOSE_DIR" -f "$COMPOSE_DIR/docker-compose.yml" "$@"; }
kc() { "$KUBECTL" -n "$NAMESPACE" "$@"; }

# Corre en un contenedor de Pagos el Python que llega por la entrada estándar (usa la conexión del propio servicio).
python_en_pagos() {
  if [ "$AMBIENTE" = local ]; then compose exec -T pagos python - "$@"
  else kc exec -i "deploy/$DEPLOY" -- python - "$@"; fi
}

modo_pasarela() {
  if [ "$AMBIENTE" = local ]; then
    PASARELA_SIM_MODO="$1" compose up -d --no-deps --wait pagos > "$SALIDA" 2>&1 \
      || { tail -5 "$SALIDA" >&2; falla "Pagos no quedó sano con PASARELA_SIM_MODO=$1 (docker compose logs pagos)"; }
  else
    kc set env "deploy/$DEPLOY" PASARELA_SIM_MODO="$1" > /dev/null
    kc rollout status "deploy/$DEPLOY" --timeout=300s > /dev/null || falla "el despliegue de Pagos no terminó"
  fi
  echo "Pagos con PASARELA_SIM_MODO=$1"
}

preparar_local() {
  command -v docker > /dev/null || falla "falta docker"
  if [ ! -f "$ENV_LOCAL" ]; then
    {
      echo "# Generado por scripts/sembrar-HU19.sh: claves locales al azar. No se versiona."
      echo "POSTGRES_PASSWORD=$(aleatorio 16)"
      echo "ASESOR_JWT_SECRETO=$(aleatorio 32)"
      echo "PAGOS_SEED_KEY=$(aleatorio 24)"
      echo "ASESOR_DEMO_PASSWORD=Demo-$(aleatorio 8)"
      echo "OPERADOR_DEMO_PASSWORD=Demo-$(aleatorio 8)"
    } > "$ENV_LOCAL"
    echo "Claves locales generadas en services/pagos/.env"
  fi
  set -a; . "$ENV_LOCAL"; set +a
  echo "Levantando PostgreSQL, Kafka, Identidad y Pagos (la primera vez construye las imágenes)..."
  compose up -d --build --wait > "$SALIDA" 2>&1 \
    || { tail -15 "$SALIDA" >&2; falla "el entorno no quedó sano (docker compose ps / logs)"; }
}

preparar_staging() {
  command -v "$KUBECTL" > /dev/null || falla "falta kubectl"
  local contexto
  contexto=$("$KUBECTL" config current-context 2>/dev/null) || falla "kubectl no tiene un contexto activo"
  kc get "deploy/$DEPLOY" > /dev/null || falla "no se encontró deploy/$DEPLOY en el namespace $NAMESPACE"
  echo "Contexto de kubectl: $contexto (namespace $NAMESPACE)"
  if [ -z "$SI" ]; then
    printf '¿Modificar Pagos en este clúster? [s/N] '
    read -r respuesta
    [ "$respuesta" = s ] || [ "$respuesta" = S ] || falla "cancelado"
  fi
}

# Asesor y operador demo con services/identidad/scripts/sembrar_demo.py (idempotente). La imagen no trae scripts/.
usuarios_demo() {
  # cp con ruta relativa: una ruta C:/... se leería como «contenedor C».
  local dir_script="$RAIZ/services/identidad/scripts"
  [ -n "${ASESOR_DEMO_PASSWORD:-}" ] && [ -n "${OPERADOR_DEMO_PASSWORD:-}" ] \
    || falla "defina ASESOR_DEMO_PASSWORD y OPERADOR_DEMO_PASSWORD"
  local vars=(PYTHONPATH=/srv ASESOR_DEMO_EMAIL=asesor@solventa.test ASESOR_DEMO_PASSWORD="$ASESOR_DEMO_PASSWORD"
    OPERADOR_DEMO_EMAIL=operador@solventa.test OPERADOR_DEMO_PASSWORD="$OPERADOR_DEMO_PASSWORD")
  if [ "$AMBIENTE" = local ]; then
    (cd "$dir_script" && compose cp sembrar_demo.py identidad:/tmp/sembrar_demo.py > /dev/null)
    compose exec -T identidad env "${vars[@]}" python /tmp/sembrar_demo.py
  else
    local pod
    pod=$(kc get pods -l app.kubernetes.io/name=solventa-identidad --field-selector=status.phase=Running \
      -o jsonpath='{.items[0].metadata.name}')
    (cd "$dir_script" && kc cp sembrar_demo.py "$pod:/tmp/sembrar_demo.py" > /dev/null)
    kc exec "$pod" -- env "${vars[@]}" python /tmp/sembrar_demo.py
  fi
}

# Deja en CLAVE la clave de servicio de Pagos y en URL_PAGOS su dirección. En staging abre el túnel.
clave_y_url() {
  if [ "$AMBIENTE" = local ]; then
    CLAVE=$PAGOS_SEED_KEY
    URL_PAGOS=http://localhost:8005
    return
  fi
  # External Secrets entrega PAGOS_API_KEYS al pod como «clave» o «nombre=clave[,...]»: se toma la primera.
  CLAVE=$(kc get secret "$DEPLOY-connection" -o jsonpath='{.data.PAGOS_API_KEYS}' | base64 -d | cut -d, -f1)
  CLAVE=${CLAVE#*=}
  [ -n "$CLAVE" ] || falla "no se pudo leer PAGOS_API_KEYS del secreto $DEPLOY-connection"
  kc port-forward "svc/$DEPLOY" "$PUERTO_TUNEL:80" > /dev/null 2>&1 &
  TUNEL=$!
  URL_PAGOS=http://localhost:$PUERTO_TUNEL
  for _ in $(seq 1 20); do
    curl -sf "$URL_PAGOS/health" > /dev/null && return
    sleep 1
  done
  falla "el túnel a svc/$DEPLOY no respondió"
}

# --- Siembra ------------------------------------------------------------------------------------------------------

TUNEL="" RESTAURAR="" SALIDA=$(mktemp)
limpiar() {
  rm -f "$SALIDA"
  [ -n "$TUNEL" ] && kill "$TUNEL" 2> /dev/null
  if [ -n "$RESTAURAR" ]; then
    echo "Algo falló: devolviendo Pagos a APROBAR." >&2
    modo_pasarela APROBAR >&2 || echo "No se pudo devolver Pagos a APROBAR: hágalo con --pasarela APROBAR." >&2
  fi
}
trap limpiar EXIT

if [ "$AMBIENTE" = local ]; then preparar_local; else preparar_staging; fi

if [ -n "$PASARELA" ]; then
  modo_pasarela "$PASARELA"
  exit 0
fi

if [ "$AMBIENTE" = local ] || [ -n "$USUARIOS" ]; then
  paso "Asesor y operador demo"
  usuarios_demo
fi

RUN=$(date -u +%y%m%d%H%M%S)
SUS_A="hu19-normal-$RUN"
SUS_B="hu19-por-vencer-$RUN"
SUS_C="hu19-vencido-$RUN"

paso "1. Pagos en modo RECHAZAR"
RESTAURAR=1
modo_pasarela RECHAZAR

paso "2. Tres cobros de prima rechazados (POST /v1/pagos/cobros)"
clave_y_url
cobrar() {  # suscripción, cliente demo, token de la pasarela simulada, monto
  local cuerpo codigo estado
  cuerpo=$(curl -s -w '\n%{http_code}' -X POST "$URL_PAGOS/v1/pagos/cobros" \
    -H "X-Service-Key: $CLAVE" -H 'Content-Type: application/json' \
    -d "{\"suscripcion_id\":\"$1\",\"cliente_id\":\"$2\",\"metodo_pago\":\"$3\",\"monto\":\"$4\",\"periodicidad_pago\":\"MENSUAL\"}")
  codigo=${cuerpo##*$'\n'}
  estado=$(echo "$cuerpo" | sed -n 's/.*"estado":"\([A-Z_]*\)".*/\1/p' | head -1)
  [ "$codigo" = 201 ] && [ "$estado" = RECHAZADA ] || falla "cobro de $1: HTTP $codigo, estado ${estado:-?}"
  echo "$1  HTTP $codigo  $estado"
}
# Clientes demo de sembrar_demo.py. B usa el prefijo tok_sim_rechazar_: su reintento se rechaza siempre (flujo 05).
cobrar "$SUS_A" 00000000-0000-4000-8000-0000000000a1 "tok_sim_hu19a$RUN"          48500.00
cobrar "$SUS_B" 00000000-0000-4000-8000-0000000000b2 "tok_sim_rechazar_hu19b$RUN" 72300.00
cobrar "$SUS_C" 00000000-0000-4000-8000-0000000000c3 "tok_sim_hu19c$RUN"          39900.00

paso "3. Gracia: A normal (3 días), B próximo a vencer (6 h), C vencido (hace 1 h)"
# PERIODO_GRACIA_DIAS va en días enteros: B y C se ajustan en la base de Pagos.
python_en_pagos gracia "$RUN" << 'PY'
import os, sys
import psycopg

_, run = sys.argv[1], sys.argv[2]
url = (os.environ.get("PAGOS_DATABASE_URL") or os.environ["DATABASE_URL"]).replace("postgresql+psycopg://", "postgresql://")
with psycopg.connect(url) as c:
    for sus, delta in ((f"hu19-por-vencer-{run}", "6 hours"), (f"hu19-vencido-{run}", "-1 hour")):
        for tabla in ("pagos.cobros_fallidos", "pagos.transacciones"):
            n = c.execute(
                f"UPDATE {tabla} SET gracia_hasta = now() + %s::interval WHERE suscripcion_id = %s", (delta, sus)
            ).rowcount
            if n != 1:
                sys.exit(f"{tabla}: se esperaba 1 fila de {sus} y hubo {n}")
        print(f"{sus}  gracia = ahora {'+' if not delta.startswith('-') else ''}{delta}")
PY

paso "4. Pagos de vuelta en modo APROBAR"
modo_pasarela APROBAR
RESTAURAR=""

paso "5. Casos creados"
python_en_pagos mostrar "$RUN" << 'PY'
import os, sys
from datetime import UTC, datetime
import psycopg

_, run = sys.argv[1], sys.argv[2]
url = (os.environ.get("PAGOS_DATABASE_URL") or os.environ["DATABASE_URL"]).replace("postgresql+psycopg://", "postgresql://")
casos = {f"hu19-normal-{run}": "A", f"hu19-por-vencer-{run}": "B", f"hu19-vencido-{run}": "C"}
with psycopg.connect(url) as c:
    filas = c.execute(
        "SELECT suscripcion_id, monto, estado, gracia_hasta, operador_notificado_en IS NOT NULL"
        " FROM pagos.cobros_fallidos WHERE suscripcion_id = ANY(%s) ORDER BY gracia_hasta",
        (list(casos),),
    ).fetchall()
if len(filas) != 3:
    sys.exit(f"Se esperaban 3 casos y hay {len(filas)}")
print(f"{'':2}{'suscripción':<30}{'monto':>10}  {'estado':<10}{'gracia hasta (UTC)':<19}{'horas':>7}")
for sus, monto, estado, gracia, avisado in filas:
    horas = (gracia - datetime.now(UTC)).total_seconds() / 3600
    aviso = "  operador avisado" if avisado else ""
    print(f"{casos[sus]:<2}{sus:<30}{monto:>10}  {estado:<10}{gracia:%Y-%m-%d %H:%M}{horas:>8.1f}{aviso}")
PY

echo
echo "Listo. C pasa a VENCIDO y avisa al operador en la siguiente vuelta del detector (PAGOS_DETECTOR_GRACIA_S)."
if [ "$AMBIENTE" = local ]; then
  echo "Consola: npx ng serve en solventa-web y http://localhost:4200, con asesor@solventa.test u operador@solventa.test"
  echo "(las contraseñas están en services/pagos/.env). Los 8 flujos: docs/como-probar-HU19.md."
fi
