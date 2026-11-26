import ast
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
PERMITIDOS = {"fastapi", "starlette", "pydantic", "jwt", "cryptography", "simplejson", "prometheus_client", "app"}


def test_modulos_fora_dos_adaptadores_so_importam_o_permitido():
    proibidos = []
    for arquivo in APP.rglob("*.py"):
        if "adaptadores" in arquivo.relative_to(APP).parts:
            continue
        for no in ast.walk(ast.parse(arquivo.read_text(encoding="utf-8"))):
            if isinstance(no, ast.Import):
                nomes = [alias.name for alias in no.names]
            elif isinstance(no, ast.ImportFrom) and no.level == 0:
                nomes = [no.module]
            else:
                continue
            proibidos += [f"{arquivo.name}: {nome}" for nome in nomes
                          if nome.split(".")[0] not in PERMITIDOS | sys.stdlib_module_names]

    assert proibidos == []
