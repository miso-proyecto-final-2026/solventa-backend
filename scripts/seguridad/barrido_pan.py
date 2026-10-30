"""Detector de números de tarjeta (PAN) para el barrido de seguridad de HA05 (criterio 5, EC-S2.1).

Un PAN probable es una secuencia de 13 a 19 dígitos (con espacios o guiones entre grupos) que pasa el dígito de
control de Luhn **y** tiene el prefijo y el largo de una marca conocida. Esa segunda condición evita el ruido que
solo con Luhn sería frecuente: una marca de tiempo en milisegundos de 13 dígitos pasa Luhn una de cada diez veces.

Nunca imprime ni devuelve el número encontrado: solo la marca, el largo y dónde está.
"""

from __future__ import annotations

import os
import re
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Uso dentro de un pod (cada servicio usa sus propias credenciales, limitadas a su esquema):
#   kubectl -n staging exec -i deploy/<servicio> -- env ESQUEMA=<esquema> python - bd < barrido_pan.py
#   kubectl -n staging logs -l app.kubernetes.io/name=<servicio> --since=24h | python barrido_pan.py logs

# Marca -> (prefijos, largos válidos). Fuentes públicas de los rangos de cada marca.
_MARCAS: dict[str, tuple[tuple[str, ...], tuple[int, ...]]] = {
    "visa": (("4",), (13, 16, 19)),
    "mastercard": (tuple(str(p) for p in range(51, 56)) + tuple(str(p) for p in range(2221, 2721)), (16,)),
    "amex": (("34", "37"), (15,)),
    "diners": (("300", "301", "302", "303", "304", "305", "36", "38", "39"), (14, 16, 19)),
    "discover": (("6011", "65") + tuple(str(p) for p in range(644, 650)), (16, 19)),
    "jcb": (tuple(str(p) for p in range(3528, 3590)), (16, 19)),
}

# 13 a 19 dígitos que pueden venir en grupos separados por un espacio o un guion. Sin dígitos pegados a los lados.
_CANDIDATO = re.compile(r"(?<![\d])(?:\d[ -]?){12,18}\d(?![\d])")


@dataclass(frozen=True)
class Hallazgo:
    marca: str
    largo: int
    posicion: int
    ruta: str = ""

    def __repr__(self) -> str:  # nunca incluye el número
        return f"Hallazgo(marca={self.marca}, largo={self.largo}, posicion={self.posicion}, ruta={self.ruta!r})"


def luhn(digitos: str) -> bool:
    total, doble = 0, False
    for c in reversed(digitos):
        d = ord(c) - 48
        if doble:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        doble = not doble
    return total % 10 == 0


def marca_de(digitos: str) -> str | None:
    for marca, (prefijos, largos) in _MARCAS.items():
        if len(digitos) in largos and digitos.startswith(prefijos):
            return marca
    return None


def es_pan_probable(digitos: str) -> bool:
    return digitos.isdigit() and 13 <= len(digitos) <= 19 and luhn(digitos) and marca_de(digitos) is not None


def barrer_texto(texto: str, ruta: str = "") -> list[Hallazgo]:
    hallazgos = []
    for m in _CANDIDATO.finditer(texto):
        digitos = re.sub(r"[ -]", "", m.group(0))
        if es_pan_probable(digitos):
            hallazgos.append(Hallazgo(marca_de(digitos) or "", len(digitos), m.start(), ruta))
    return hallazgos


def barrer_json(valor: object, ruta: str = "") -> list[Hallazgo]:
    """Recorre un valor JSON (dict, list, str, int) y revisa cada hoja."""
    if isinstance(valor, dict):
        return [h for k, v in valor.items() for h in barrer_json(v, f"{ruta}.{k}" if ruta else str(k))]
    if isinstance(valor, list):
        return [h for i, v in enumerate(valor) for h in barrer_json(v, f"{ruta}[{i}]")]
    if isinstance(valor, bool) or valor is None:
        return []
    return barrer_texto(str(valor), ruta)


def resumen_texto(hallazgos: list[Hallazgo]) -> str:
    if not hallazgos:
        return "0 hallazgos"
    return f"{len(hallazgos)} hallazgos: " + ", ".join(f"{h.marca}/{h.largo} en {h.ruta or 'texto'}@{h.posicion}" for h in hallazgos)


@dataclass(frozen=True)
class HallazgoLinea:
    linea: int
    marca: str
    largo: int

    def __repr__(self) -> str:
        return f"HallazgoLinea(linea={self.linea}, marca={self.marca}, largo={self.largo})"


def barrer_logs(lineas: Iterable[str]) -> list[HallazgoLinea]:
    """Cada línea con un PAN probable cuenta una vez; nunca se devuelve el texto de la línea."""
    return [
        HallazgoLinea(n, h.marca, h.largo)
        for n, linea in enumerate(lineas, start=1)
        for h in barrer_texto(linea)[:1]
    ]


@dataclass(frozen=True)
class ResultadoColumna:
    tabla: str
    columna: str
    filas: int
    coincidencias: int
    marcas: tuple[tuple[str, int], ...] = ()

    def __repr__(self) -> str:
        return f"{self.tabla}.{self.columna} filas={self.filas} coincidencias={self.coincidencias}"


def barrer_base(engine, esquema: str | None = None) -> list[ResultadoColumna]:
    """Revisa todas las columnas de texto, JSON y numéricas de un esquema, fila por fila (en el servidor no se filtra)."""
    import sqlalchemy as sa

    inspector = sa.inspect(engine)
    preparador = engine.dialect.identifier_preparer
    resultados: list[ResultadoColumna] = []
    for tabla in sorted(inspector.get_table_names(schema=esquema)):
        for columna in inspector.get_columns(tabla, schema=esquema):
            tipo = columna["type"]
            if not isinstance(tipo, (sa.String, sa.Text, sa.JSON, sa.Integer, sa.Numeric)):
                continue
            origen = (preparador.quote_schema(esquema) + "." if esquema else "") + preparador.quote(tabla)
            consulta = sa.text(f"SELECT {preparador.quote(columna['name'])} FROM {origen}")
            filas, marcas = 0, Counter()
            with engine.connect() as c:
                for (valor,) in c.execution_options(stream_results=True).execute(consulta):
                    filas += 1
                    if valor is None:
                        continue
                    for h in barrer_json(valor) if isinstance(valor, (dict, list)) else barrer_texto(str(valor)):
                        marcas[h.marca] += 1
            resultados.append(ResultadoColumna(tabla, columna["name"], filas, sum(marcas.values()), tuple(sorted(marcas.items()))))
    return resultados


def _imprimir_base(resultados: Sequence[ResultadoColumna], esquema: str | None) -> int:
    total = sum(r.coincidencias for r in resultados)
    print(f"Esquema {esquema or '(por defecto)'}: {len(resultados)} columnas, {sum(r.filas for r in resultados)} valores revisados")
    for r in resultados:
        if r.coincidencias:
            print(f"  COINCIDENCIA {r.tabla}.{r.columna}: {r.coincidencias} ({', '.join(f'{m}:{n}' for m, n in r.marcas)})")
    print(f"TOTAL: {total} coincidencias")
    return 1 if total else 0


def main(argumentos: Sequence[str] | None = None) -> int:
    argumentos = list(sys.argv[1:] if argumentos is None else argumentos)
    modo = argumentos[0] if argumentos else ""
    if modo == "logs":
        hallazgos = barrer_logs(sys.stdin)
        print(f"{len(hallazgos)} línea{'s' if len(hallazgos) != 1 else ''} con PAN probable")
        for h in hallazgos[:50]:
            print(f"  línea {h.linea}: {h.marca}/{h.largo}")
        return 1 if hallazgos else 0
    if modo == "bd":
        import sqlalchemy as sa

        url = os.environ.get("DATABASE_URL") or os.environ.get("SOLVENTA_DATABASE_URL")
        if not url:
            print("Falta DATABASE_URL", file=sys.stderr)
            return 2
        esquema = os.environ.get("ESQUEMA") or None
        return _imprimir_base(barrer_base(sa.create_engine(url), esquema), esquema)
    print("Uso: barrido_pan.py {logs|bd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
