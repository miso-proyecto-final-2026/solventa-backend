"""Cambios que rompen el contrato de `/partners/v1` (HA07, criterio 3; ADR-0030).

Compara dos versiones del OpenAPI con oasdiff (`breaking --fail-on ERR`): un cambio incompatible para un socio que ya
integró (quitar un campo o una ruta, volver obligatorio un parámetro, cambiar un tipo...) hace fallar; uno compatible
(agregar un campo opcional, una ruta nueva) no.

    python scripts/contrato/compatibilidad.py contra <openapi-base.json>   # el CI, contra la rama base del PR
    python scripts/contrato/compatibilidad.py casos                        # demuestra que distingue ambos tipos

`casos` aplica cada parche de `scripts/contrato/casos/*.json` (JSON Patch: add, remove, replace) al contrato actual y
exige el resultado esperado, sin excepciones. `contra` aplica las de `excepciones.txt`: cambios que oasdiff marca pero que
no cambian lo que recibe el socio, cada uno justificado. Usa el binario `oasdiff` si está en el PATH; si no, la imagen de Docker fijada abajo.
Solo necesita la biblioteca estándar.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
CONTRATO = RAIZ / "docs" / "api" / "openapi.json"
CASOS = Path(__file__).resolve().parent / "casos"
EXCEPCIONES = Path(__file__).resolve().parent / "excepciones.txt"
IMAGEN = "tufin/oasdiff:v1.35.0"


def _oasdiff(base: Path, revision: Path, excepciones: Path | None = None) -> subprocess.CompletedProcess[str]:
    """`oasdiff breaking` con `--fail-on ERR`: código 1 si hay al menos un cambio que rompe y no está exceptuado.

    Los tres archivos se pasan desde la misma carpeta, que es la que se monta si se usa Docker.
    """
    carpeta = base.parent
    assert revision.parent == carpeta, "base y revisión deben estar en la misma carpeta"
    argumentos = ["breaking", "--fail-on", "ERR", "--format", "text"]
    if excepciones is not None:
        shutil.copy(excepciones, carpeta / "excepciones.txt")
        argumentos += ["--err-ignore", "{c}/excepciones.txt"]
    if shutil.which("oasdiff"):
        prefijo, comando = str(carpeta), ["oasdiff"]
    else:
        prefijo, comando = "/c", ["docker", "run", "--rm", "-v", f"{carpeta}:/c:ro", IMAGEN]
    rutas = [a.format(c=prefijo) for a in argumentos] + [f"{prefijo}/{base.name}", f"{prefijo}/{revision.name}"]
    return subprocess.run([*comando, *rutas], capture_output=True, text=True, encoding="utf-8", check=False)


def _puntero(documento: dict, ruta: str) -> tuple[object, str]:
    partes = [p.replace("~1", "/").replace("~0", "~") for p in ruta.lstrip("/").split("/")]
    padre: object = documento
    for parte in partes[:-1]:
        padre = padre[int(parte)] if isinstance(padre, list) else padre[parte]  # type: ignore[index]
    return padre, partes[-1]


def aplicar_parche(documento: dict, operaciones: list[dict]) -> dict:
    """JSON Patch mínimo (RFC 6902: add, remove, replace); falla si una ruta no existe, para que un caso no quede vacío."""
    resultado = copy.deepcopy(documento)
    for op in operaciones:
        padre, clave = _puntero(resultado, op["path"])
        if op["op"] == "remove":
            if isinstance(padre, list):
                del padre[int(clave)]
            else:
                del padre[clave]  # type: ignore[attr-defined]
        elif op["op"] in ("add", "replace"):
            if isinstance(padre, list):
                if clave == "-":
                    padre.append(op["value"])
                elif op["op"] == "add":
                    padre.insert(int(clave), op["value"])
                else:
                    padre[int(clave)] = op["value"]
            else:
                if op["op"] == "replace" and clave not in padre:  # type: ignore[operator]
                    raise KeyError(op["path"])
                padre[clave] = op["value"]  # type: ignore[index]
        else:
            raise ValueError(f"Operación no soportada: {op['op']}")
    return resultado


def contra(base: Path) -> int:
    with tempfile.TemporaryDirectory() as carpeta:
        destino = Path(carpeta)
        shutil.copy(base, destino / "base.json")
        shutil.copy(CONTRATO, destino / "revision.json")
        r = _oasdiff(destino / "base.json", destino / "revision.json", EXCEPCIONES)
    print(r.stdout or "Sin cambios que rompan el contrato.", r.stderr, sep="\n")
    return r.returncode


def casos() -> int:
    actual = json.loads(CONTRATO.read_text(encoding="utf-8"))
    errores = 0
    with tempfile.TemporaryDirectory() as carpeta:
        destino = Path(carpeta)
        (destino / "base.json").write_text(json.dumps(actual), encoding="utf-8")
        for archivo in sorted(CASOS.glob("*.json")):
            caso = json.loads(archivo.read_text(encoding="utf-8"))
            (destino / "revision.json").write_text(json.dumps(aplicar_parche(actual, caso["parche"])), encoding="utf-8")
            r = _oasdiff(destino / "base.json", destino / "revision.json")
            if r.returncode not in (0, 1):
                print(f"ERROR   {archivo.name}: oasdiff terminó con {r.returncode}\n{r.stderr}")
                errores += 1
                continue
            rompe = r.returncode == 1
            correcto = rompe == (caso["esperado"] == "rompe")
            errores += not correcto
            print(f"{'ok' if correcto else 'FALLA':7} {archivo.name}: {caso['descripcion']} -> {'rompe' if rompe else 'compatible'}")
            if not correcto:
                print(r.stdout)
    print(f"\n{errores} caso(s) con un resultado distinto del esperado.")
    return 1 if errores else 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "contra":
        sys.exit(contra(Path(sys.argv[2]).resolve()))
    if len(sys.argv) == 2 and sys.argv[1] == "casos":
        sys.exit(casos())
    print(__doc__)
    sys.exit(2)
