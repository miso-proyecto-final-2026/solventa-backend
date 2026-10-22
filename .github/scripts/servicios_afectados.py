"""Calcula qué servicios despliega `cicd/deploy-staging` según los archivos que cambia un PR.

Lee la lista de archivos (uno por línea) por la entrada estándar y el mapa carpeta -> servicio de
`.github/deploy-services.json`. `servicio` es a la vez el repositorio de ECR, el Deployment, el prefijo
del CronJob de migración y la clave del rol en `AWS_DEPLOY_ROLE_ARNS`; coincide con `name` en
`solventa-infra/environments/staging/services.yaml`. Escribe la matriz de GitHub Actions en JSON.
"""
import json
import sys
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "deploy-services.json"


def servicios_afectados(archivos, servicios):
    """Servicios cuyo directorio `services/<carpeta>/` contiene al menos un archivo cambiado."""
    por_carpeta = {s["carpeta"]: s for s in servicios}
    afectados = {}
    for archivo in archivos:
        partes = archivo.strip().split("/")
        if len(partes) > 2 and partes[0] == "services" and partes[1] in por_carpeta:
            afectados[partes[1]] = por_carpeta[partes[1]]
    return [afectados[c] for c in sorted(afectados)]


def main():
    servicios = json.loads(CONFIG.read_text(encoding="utf-8"))["servicios"]
    archivos = [linea for linea in sys.stdin.read().splitlines() if linea.strip()]
    print(json.dumps({"include": servicios_afectados(archivos, servicios)}, separators=(",", ":")))


if __name__ == "__main__":
    main()
