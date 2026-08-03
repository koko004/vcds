import difflib
from extractor import extraer_texto, extraer_csv, extraer_dni, extraer_nombre, extraer_fecha, extraer_no_consta


def normalizar(texto: str) -> str:
    return " ".join(texto.split()).strip()


def comparar_texto(texto_usuario: str, texto_original: str) -> dict:
    t1 = normalizar(texto_usuario)
    t2 = normalizar(texto_original)

    if t1 == t2:
        return {"coincide": True, "ratio": 1.0, "diferencias": []}

    ratio = difflib.SequenceMatcher(None, t1, t2).ratio()

    diff = list(difflib.unified_diff(
        t1.split(), t2.split(),
        fromfile="usuario", tofile="original",
        lineterm=""
    ))

    return {"coincide": ratio > 0.95, "ratio": round(ratio, 4), "diferencias": diff[:50]}


def comparar_datos(datos_usuario: dict, datos_original: dict) -> dict:
    campos = ["nombre", "dni", "csv", "fecha_emision", "no_consta"]
    resultados = {}
    todo_ok = True
    for campo in campos:
        v1 = datos_usuario.get(campo)
        v2 = datos_original.get(campo)
        if isinstance(v1, bool) or isinstance(v2, bool):
            ok = v1 == v2
            if not ok:
                todo_ok = False
            resultados[campo] = {"coincide": ok, "usuario": v1, "original": v2}
        elif v1 and v2:
            ok = v1.strip().upper() == v2.strip().upper()
            if not ok:
                todo_ok = False
            resultados[campo] = {"coincide": ok, "usuario": v1, "original": v2}
        elif v1 and not v2:
            todo_ok = False
            resultados[campo] = {"coincide": False, "usuario": v1, "original": None}
        else:
            resultados[campo] = {"coincide": True, "usuario": v1, "original": v2}
    return {"todo_ok": todo_ok, "campos": resultados}


def comparar(pdf_usuario: str, pdf_original: str, datos_usuario_editados: dict | None = None) -> dict:
    texto_usuario = extraer_texto(pdf_usuario)
    texto_original = extraer_texto(pdf_original)

    if datos_usuario_editados:
        datos_usuario = dict(datos_usuario_editados)
    else:
        datos_usuario = {
            "nombre": extraer_nombre(texto_usuario),
            "dni": extraer_dni(texto_usuario),
            "csv": extraer_csv(texto_usuario),
            "fecha_emision": extraer_fecha(texto_usuario),
            "no_consta": extraer_no_consta(texto_usuario),
        }
    datos_original = {
        "nombre": extraer_nombre(texto_original),
        "dni": extraer_dni(texto_original),
        "csv": extraer_csv(texto_original),
        "fecha_emision": extraer_fecha(texto_original),
        "no_consta": extraer_no_consta(texto_original),
    }

    comp_texto = comparar_texto(texto_usuario, texto_original)
    comp_datos = comparar_datos(datos_usuario, datos_original)

    if comp_datos["todo_ok"] and comp_texto["coincide"]:
        veredicto = "original"
        mensaje = "El documento es ORIGINAL. Coincide con el registro del Ministerio."
    elif comp_datos["todo_ok"] and not comp_texto["coincide"]:
        veredicto = "original"
        mensaje = "El documento es ORIGINAL. Los datos coinciden con el registro del Ministerio. (El texto tiene pequeñas diferencias por OCR)"
    elif not comp_datos["todo_ok"]:
        veredicto = "manipulado"
        mensaje = "El documento está MANIPULADO. Los datos no coinciden con el registro oficial."
    else:
        veredicto = "dudoso"
        mensaje = "No se pudo determinar con certeza. Revisa manualmente."

    return {
        "veredicto": veredicto,
        "mensaje": mensaje,
        "comparacion_texto": comp_texto,
        "comparacion_datos": comp_datos,
        "datos_usuario": datos_usuario,
        "datos_original": datos_original,
    }
