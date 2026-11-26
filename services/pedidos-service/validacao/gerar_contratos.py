"""Gera schemas JSON (2020-12) e exemplos dos contratos de Pedidos a partir da ESP-PED-001 v2.2, e valida cada exemplo."""
import json, sys
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker

OUT = Path(sys.argv[1])
BASE = "https://github.com/brunnoalexcson/portal-b2b-mercado-negociacao-pedidos/blob/main/docs/pedidos/contratos/"
UFS = ["AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA", "PB", "PR", "PE",
       "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"]

TXT = {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "\\S"}
UUID_IN = {"type": "string", "pattern": "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"}
UUID_OUT = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
QTD = {"type": "number", "exclusiveMinimum": 0, "maximum": 9999999999.99, "description": "Até 2 casas decimais."}
QTD0 = {"type": "number", "minimum": 0, "maximum": 9999999999.99, "description": "Até 2 casas decimais."}
BRL = {"type": "number", "exclusiveMinimum": 0, "maximum": 99999999999999.99, "description": "BRL, até 2 casas decimais."}
PESO = {"type": "number", "exclusiveMinimum": 0, "description": "Quilos, até 3 casas decimais."}
DT = {"type": "string", "format": "date-time", "description": "ISO 8601 com Z ou offset explícito."}
DATA = {"type": "string", "format": "date"}
NOME = {"type": "string", "minLength": 1, "maxLength": 255, "pattern": "\\S"}
LOCAL = {"type": "object", "required": ["cidade", "uf"], "additionalProperties": False,
         "properties": {"cidade": {"type": "string", "minLength": 1, "maxLength": 255, "pattern": "\\S"},
                        "uf": {"enum": UFS}}}
UNIDADE = {"enum": ["KG", "TON", "UN", "CX", "L"]}
TRANSPORTE = {"enum": ["GRANEL", "REFRIGERADO", "CARGA_SECA", "PERIGOSO"]}
STATUS = ["CRIADO", "VENDA_CONCLUIDA", "AGUARDANDO_FRETE", "FRETE_SELECIONADO", "ENTREGUE", "CANCELADO"]
RESERVA = {"enum": ["PENDENTE", "CONFIRMADA", "RECUSADA", "ENCERRADA"]}
MOTIVO = {"enum": ["ESTOQUE_INSUFICIENTE", "FORNECIMENTO_INEXISTENTE", "FORA_DO_LOTE", "NEGOCIACAO_SEM_VENDA"]}


def nulo(s):
    return {"anyOf": [s, {"type": "null"}]}


def obj(props, required=None, extra=True, **kw):
    return {"type": "object", "required": required if required is not None else list(props),
            "additionalProperties": extra, "properties": props, **kw}


def envelope(topico, source, chave, payload, desc):
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": BASE + topico + ".schema.json",
            "title": topico, "description": desc + f" Chave Kafka e aggregateId: {chave}.",
            "type": "object",
            "required": ["eventId", "eventType", "eventVersion", "timestamp", "source", "correlationId",
                         "aggregateId", "payload"],
            "additionalProperties": False,
            "properties": {"eventId": {"type": "string", "format": "uuid"}, "eventType": {"const": topico},
                           "eventVersion": {"const": "1.0"}, "timestamp": DT, "source": {"const": source},
                           "correlationId": {"type": "string", "minLength": 1, "maxLength": 128,
                                             "pattern": "^[A-Za-z0-9._-]+$"},
                           "aggregateId": {"type": "string", "minLength": 1, "maxLength": 128},
                           "payload": payload}}


ALOCACAO = obj({"fornecimentoId": TXT, "fornecedorId": TXT, "pedidoId": UUID_IN, "quantidade": QTD,
                "valorUnitario": BRL, "valorTotal": BRL})
FRETE = obj({"solicitacaoFreteId": TXT, "entregaId": TXT, "cotacaoId": TXT, "transportadoraId": TXT,
             "tentativa": {"type": "integer", "minimum": 1}, "preco": BRL,
             "prazoDias": {"type": "integer", "minimum": 1}, "dataEntregaPrevista": nulo(DATA)}, extra=False)
CARGA = ["compradorId", "fornecedorId", "produtoId", "produtoNome", "quantidade", "unidade", "pesoKg",
         "tipoTransporte", "valorTotal", "origem", "destino"]

E = "Consumido pelo pedidos-service (ESP-PED-001 v2.2, §7.3). Campos adicionais do produtor são aceitos e ignorados."
P = "Publicado pelo pedidos-service (ESP-PED-001 v2.2, §7.4)."

TOPICOS = {
    "pedido_temporario_solicitado": ("negociacao-service", "negociacaoId", E, obj({
        "negociacaoId": TXT, "tipo": {"enum": ["LEILAO", "VENDA_DIRETA"]}, "fornecimentoId": TXT,
        "fornecedorId": TXT, "produtoId": TXT, "quantidade": QTD, "precoUnitario": BRL})),
    "negociacao_fechada": ("negociacao-service", "negociacaoId", E, obj({
        "negociacaoId": TXT, "tipo": {"enum": ["LEILAO", "VENDA_DIRETA"]},
        "status": {"enum": ["ENCERRADO", "DESERTO", "RECUSADO", "EXPIRADO"]}, "compradorId": nulo(TXT),
        "produtoId": TXT, "alocacoes": {"type": "array", "items": ALOCACAO}},
        allOf=[{"if": {"properties": {"status": {"const": "ENCERRADO"}}},
                "then": {"properties": {"compradorId": TXT, "alocacoes": {"minItems": 1}}},
                "else": {"properties": {"compradorId": {"type": "null"}, "alocacoes": {"maxItems": 0}}}}])),
    "produto_cadastrado": ("produtos-service", "produtoId", E, obj({
        "produtoId": TXT, "nome": NOME, "unidade": UNIDADE,
        "tipoTransporte": TRANSPORTE, "pesoUnitarioKg": nulo({"type": "number", "exclusiveMinimum": 0, "maximum": 999999999.999,
                                                                 "description": "Até 3 casas decimais."})},
        required=["produtoId", "nome", "unidade", "tipoTransporte"])),
    "fornecimento_criado": ("fornecimentos-service", "fornecimentoId", E, obj({
        "fornecimentoId": TXT, "fornecedorId": TXT, "produtoId": TXT, "unidade": UNIDADE, "origem": LOCAL})),
    "estoque_atualizado": ("fornecimentos-service", "fornecimentoId", E, obj({
        "fornecimentoId": TXT, "produtoId": TXT, "fornecedorId": TXT,
        "operacao": {"enum": ["RESERVA", "LIBERACAO", "BAIXA", "REPOSICAO", "ENCERRAMENTO"]},
        "quantidadeMovimentada": QTD, "quantidadeDisponivel": QTD0, "quantidadeReservada": QTD0,
        "pedidoId": nulo(UUID_IN), "atualizadoEm": DT},
        allOf=[{"if": {"properties": {"operacao": {"enum": ["RESERVA", "LIBERACAO", "BAIXA"]}}},
                "then": {"properties": {"pedidoId": UUID_IN}},
                "else": {"properties": {"pedidoId": {"type": "null"}}}}])),
    "reserva_estoque_recusada": ("fornecimentos-service", "pedidoId", E, obj({
        "pedidoId": UUID_IN, "fornecimentoId": TXT, "quantidadeSolicitada": QTD,
        "quantidadeDisponivel": nulo(QTD0), "motivo": {"enum": ["ESTOQUE_INSUFICIENTE", "FORNECIMENTO_INEXISTENTE"]},
        "recusadoEm": DT})),
    "solicitacao_frete_criada": ("logistica-service", "solicitacaoFreteId", E, obj({
        "solicitacaoFreteId": TXT, "pedidoId": UUID_IN, "status": {"const": "ABERTA"}})),
    "frete_selecionado": ("logistica-service", "pedidoId", E, obj({
        "pedidoId": UUID_IN, "solicitacaoFreteId": TXT, "entregaId": TXT, "cotacaoId": TXT, "transportadoraId": TXT,
        "tentativa": {"type": "integer", "minimum": 1}, "preco": BRL, "prazoDias": {"type": "integer", "minimum": 1},
        "dataEntregaPrevista": nulo(DATA), "status": {"const": "FRETE_SELECIONADO"}},
        required=["pedidoId", "solicitacaoFreteId", "entregaId", "cotacaoId", "transportadoraId", "tentativa",
                  "preco", "prazoDias", "status"])),
    "entrega_concluida": ("logistica-service", "pedidoId", E, obj({
        "pedidoId": UUID_IN, "entregaId": TXT, "transportadoraId": TXT, "entregueEm": DT})),
    "frete_indisponivel": ("logistica-service", "pedidoId", E, obj({
        "pedidoId": UUID_IN, "solicitacaoFreteId": TXT,
        "motivo": {"enum": ["SEM_COTACAO", "SEM_TRANSPORTADORA_DISPONIVEL"]}, "ocorridoEm": DT,
        "detalhe": nulo({"type": "string", "maxLength": 500})},
        required=["pedidoId", "solicitacaoFreteId", "motivo", "ocorridoEm"])),
    "pedido_criado": ("pedidos-service", "pedidoId", P + " Pedido temporário: compradorId e destino são null.", obj({
        "pedidoId": UUID_OUT, "negociacaoId": TXT, "compradorId": {"type": "null"}, "fornecedorId": TXT,
        "produtoId": TXT, "fornecimentoId": TXT, "produtoNome": NOME,
        "quantidade": QTD, "unidade": UNIDADE, "pesoKg": PESO, "tipoTransporte": TRANSPORTE, "valorTotal": BRL,
        "origem": LOCAL, "destino": {"type": "null"}, "status": {"const": "CRIADO"}}, extra=False)),
    "pedido_atualizado": ("pedidos-service", "pedidoId", P + " Um evento por versão nova do pedido, a partir da 2.", obj({
        "pedidoId": UUID_OUT, "negociacaoId": TXT, "fornecimentoId": TXT, "status": {"enum": STATUS},
        "statusAnterior": {"enum": STATUS}, "reservaSituacao": RESERVA, "versao": {"type": "integer", "minimum": 2},
        "motivo": MOTIVO, "compradorId": TXT, "fornecedorId": TXT, "produtoId": TXT,
        "produtoNome": NOME, "quantidade": QTD, "unidade": UNIDADE,
        "pesoKg": PESO, "tipoTransporte": TRANSPORTE, "valorUnitario": BRL, "valorTotal": BRL, "origem": LOCAL,
        "destino": LOCAL, "dataEntregaDesejada": DATA, "frete": FRETE, "entregueEm": DT},
        required=["pedidoId", "negociacaoId", "fornecimentoId", "status", "statusAnterior", "reservaSituacao",
                  "versao"], extra=False,
        allOf=[{"if": {"properties": {"status": {"const": s}}}, "then": {"required": r}} for s, r in [
            ("CANCELADO", ["motivo"]), ("VENDA_CONCLUIDA", ["compradorId", "valorUnitario", "valorTotal"]),
            ("AGUARDANDO_FRETE", CARGA), ("FRETE_SELECIONADO", ["frete"]), ("ENTREGUE", ["entregueEm"])]])),
    "pedidos.dlt": ("pedidos-service", "chave original ou topico:particao:offset",
                    "Publicado pelo pedidos-service com as mensagens rejeitadas (ESP-PED-001 v2.2, §7.4).", obj({
        "originalEvent": {"description": "Objeto original, string truncada em 64 KiB ou null."},
        "error": {"enum": ["EVENTO_INVALIDO", "CONTRATO_DIVERGENTE", "FECHAMENTO_INCOMPATIVEL", "TENTATIVAS_ESGOTADAS"]},
        "stackTrace": nulo({"type": "string"}),
        "failedAtAttempt": {"enum": [1, 3], "description": "3 em TENTATIVAS_ESGOTADAS; 1 nos demais."}},
        extra=False)),
}
TOPICOS["produto_atualizado"] = (*TOPICOS["produto_cadastrado"][:3], TOPICOS["produto_cadastrado"][3])

P1, P3 = "32000000-0000-4000-8000-000000000001", "32000000-0000-4000-8000-000000000003"
N, F1, F2, F3 = ("31000000-0000-4000-8000-000000000001", "34000000-0000-4000-8000-000000000001",
                 "34000000-0000-4000-8000-000000000002", "34000000-0000-4000-8000-000000000003")
PROD, A1, A2, COMP = ("35000000-0000-4000-8000-000000000001", "a1000000-0000-4000-8000-000000000001",
                      "a1000000-0000-4000-8000-000000000002", "b2000000-0000-4000-8000-000000000002")
GO = lambda c: {"cidade": c, "uf": "GO"}
FRETE_EX = {"solicitacaoFreteId": "sol-go-21", "entregaId": "ent-go-31", "cotacaoId": "cot-go-18",
            "transportadoraId": "tra-go-8", "tentativa": 1, "preco": 150.00, "prazoDias": 2,
            "dataEntregaPrevista": "2026-11-23"}
CARGA_EX = {"compradorId": COMP, "fornecedorId": A1, "produtoId": PROD, "produtoNome": "Milho em grão",
            "quantidade": 5.00, "unidade": "TON", "pesoKg": 5000, "tipoTransporte": "GRANEL", "valorTotal": 650.00,
            "origem": GO("Rio Verde"), "destino": GO("Anápolis"), "dataEntregaDesejada": "2026-11-27"}
BASE_ATU = {"pedidoId": P1, "negociacaoId": N, "fornecimentoId": F1}

EXEMPLOS = {
    "pedido_temporario_solicitado": [("", N, {"negociacaoId": N, "tipo": "LEILAO", "fornecimentoId": F1, "fornecedorId": A1,
                                               "produtoId": PROD, "quantidade": 5.00, "precoUnitario": 120.00})],
    "negociacao_fechada": [
        ("", N, {"negociacaoId": N, "tipo": "LEILAO", "status": "ENCERRADO", "compradorId": COMP, "produtoId": PROD,
                 "demandaId": "d4000000-0000-4000-8000-000000000001", "alocacoes": [{"fornecimentoId": F1, "fornecedorId": A1, "pedidoId": P1, "quantidade": 5.00,
                                "valorUnitario": 130.00, "valorTotal": 650.00},
                               {"fornecimentoId": F2, "fornecedorId": A2, "pedidoId": "32000000-0000-4000-8000-000000000002",
                                "quantidade": 15.00, "valorUnitario": 130.00, "valorTotal": 1950.00}]}),
        (".deserto", N, {"negociacaoId": N, "tipo": "LEILAO", "status": "DESERTO", "compradorId": None,
                         "produtoId": PROD, "demandaId": None, "alocacoes": []})],
    "produto_cadastrado": [("", PROD, {"produtoId": PROD, "nome": "Milho em grão", "unidade": "TON",
                                       "tipoTransporte": "GRANEL", "pesoUnitarioKg": None})],
    "produto_atualizado": [("", PROD, {"produtoId": PROD, "nome": "Milho em grão tipo 1", "unidade": "TON",
                                       "tipoTransporte": "GRANEL", "pesoUnitarioKg": None})],
    "fornecimento_criado": [("", F1, {"fornecimentoId": F1, "fornecedorId": A1, "produtoId": PROD, "unidade": "TON",
                                      "origem": GO("Rio Verde")})],
    "estoque_atualizado": [
        ("", F1, {"fornecimentoId": F1, "produtoId": PROD, "fornecedorId": A1, "operacao": "RESERVA",
                  "quantidadeMovimentada": 5.00, "quantidadeDisponivel": 95.00, "quantidadeReservada": 5.00,
                  "pedidoId": P1, "atualizadoEm": "2026-11-20T12:00:04.000Z"}),
        (".reposicao", F1, {"fornecimentoId": F1, "produtoId": PROD, "fornecedorId": A1, "operacao": "REPOSICAO",
                            "quantidadeMovimentada": 10.00, "quantidadeDisponivel": 105.00, "quantidadeReservada": 5.00,
                            "pedidoId": None, "atualizadoEm": "2026-11-20T13:00:00.000Z"})],
    "reserva_estoque_recusada": [("", P1, {"pedidoId": P1, "fornecimentoId": F1, "quantidadeSolicitada": 5.00,
                                           "quantidadeDisponivel": 2.00, "motivo": "ESTOQUE_INSUFICIENTE",
                                           "recusadoEm": "2026-11-20T12:00:04.000Z"})],
    "solicitacao_frete_criada": [("", "sol-go-21", {"solicitacaoFreteId": "sol-go-21", "pedidoId": P1, "status": "ABERTA"})],
    "frete_selecionado": [("", P1, {"pedidoId": P1, **{k: v for k, v in FRETE_EX.items()}, "criterio": "ESCOLHA_COMPRADOR",
                                    "status": "FRETE_SELECIONADO"})],
    "entrega_concluida": [("", P1, {"pedidoId": P1, "entregaId": "ent-go-31", "transportadoraId": "tra-go-8",
                                    "entregueEm": "2026-11-23T16:00:00.000Z", "prazoPrometido": "2026-11-23T21:00:00.000Z",
                                    "noPrazo": True, "atrasoHoras": 0})],
    "frete_indisponivel": [("", P1, {"pedidoId": P1, "solicitacaoFreteId": "sol-go-21", "motivo": "SEM_COTACAO",
                                     "detalhe": "Prazo de cotação expirado.", "ocorridoEm": "2026-11-21T02:00:00.000Z"})],
    "pedido_criado": [("", P1, {"pedidoId": P1, "negociacaoId": N, "compradorId": None, "fornecedorId": A1,
                                "produtoId": PROD, "fornecimentoId": F1, "produtoNome": "Milho em grão", "quantidade": 5.00,
                                "unidade": "TON", "pesoKg": 5000, "tipoTransporte": "GRANEL", "valorTotal": 600.00,
                                "origem": GO("Rio Verde"), "destino": None, "status": "CRIADO"})],
    "pedido_atualizado": [
        ("", P1, {**BASE_ATU, "status": "CRIADO", "statusAnterior": "CRIADO", "reservaSituacao": "CONFIRMADA", "versao": 2}),
        (".venda_concluida", P1, {**BASE_ATU, "status": "VENDA_CONCLUIDA", "statusAnterior": "CRIADO",
                                  "reservaSituacao": "CONFIRMADA", "versao": 3, "compradorId": COMP,
                                  "valorUnitario": 130.00, "valorTotal": 650.00}),
        (".aguardando_frete", P1, {**BASE_ATU, "status": "AGUARDANDO_FRETE", "statusAnterior": "VENDA_CONCLUIDA",
                                   "reservaSituacao": "CONFIRMADA", "versao": 4, **CARGA_EX}),
        (".frete_selecionado", P1, {**BASE_ATU, "status": "FRETE_SELECIONADO", "statusAnterior": "AGUARDANDO_FRETE",
                                    "reservaSituacao": "CONFIRMADA", "versao": 5, "frete": FRETE_EX}),
        (".entregue", P1, {**BASE_ATU, "status": "ENTREGUE", "statusAnterior": "FRETE_SELECIONADO",
                           "reservaSituacao": "CONFIRMADA", "versao": 6, "entregueEm": "2026-11-23T16:00:00.000Z"}),
        (".cancelado", P3, {"pedidoId": P3, "negociacaoId": N, "fornecimentoId": F3, "status": "CANCELADO",
                            "statusAnterior": "CRIADO", "reservaSituacao": "ENCERRADA", "versao": 3,
                            "motivo": "FORA_DO_LOTE"})],
    "pedidos.dlt": [("", P1, {"originalEvent": {"eventId": "37000000-0000-4000-8000-000000000201",
                                                "eventType": "frete_selecionado", "eventVersion": "1.0",
                                                "timestamp": "2026-11-20T14:00:00.000Z", "source": "logistica-service",
                                                "correlationId": "teste-ped-01", "aggregateId": P1},
                              "error": "EVENTO_INVALIDO", "stackTrace": "ValidationError: payload ausente",
                              "failedAtAttempt": 1})],
}
FONTE = {"pedido_criado": "pedidos-service", "pedido_atualizado": "pedidos-service", "pedidos.dlt": "pedidos-service"}

n = 0
for i, (topico, (source, chave, desc, payload)) in enumerate(sorted(TOPICOS.items())):
    schema = envelope(topico, source, chave, payload, desc)
    Draft202012Validator.check_schema(schema)
    (OUT / f"{topico}.schema.json").write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    val = Draft202012Validator(schema, format_checker=FormatChecker())
    for j, (sufixo, agg, corpo) in enumerate(EXEMPLOS[topico]):
        ex = {"eventId": f"37000000-0000-4000-8000-{i:06d}{j:06d}", "eventType": topico, "eventVersion": "1.0",
              "timestamp": "2026-11-20T12:00:00.000Z", "source": source, "correlationId": "teste-ped-01",
              "aggregateId": agg, "payload": corpo}
        erros = [e.message for e in val.iter_errors(ex)]
        assert not erros, (topico, sufixo, erros)
        (OUT / f"{topico}{sufixo}.exemplo.json").write_text(json.dumps(ex, ensure_ascii=False, indent=2) + "\n",
                                                             encoding="utf-8")
        n += 1
# self-check: os validadores rejeitam casos que a ESP manda para a DLT
ruins = [("pedido_criado", {"status": "VENDA_CONCLUIDA"}), ("negociacao_fechada", {"status": "DESERTO", "compradorId": COMP}),
         ("solicitacao_frete_criada", {"status": "FECHADA"}), ("pedido_atualizado", {"status": "CANCELADO"}),
         ("estoque_atualizado", {"operacao": "RESERVA", "pedidoId": None}),
         ("estoque_atualizado.reposicao", {"pedidoId": "32000000-0000-4000-8000-000000000001"}),
         ("produto_cadastrado", {"nome": "   "}), ("pedidos.dlt", {"failedAtAttempt": 2})]
for topico, troca in ruins:
    ex = json.loads((OUT / f"{topico}.exemplo.json").read_text(encoding="utf-8"))
    topico = topico.split(".")[0] if topico != "pedidos.dlt" else topico
    ex["payload"].update(troca)
    if topico == "pedido_atualizado":
        ex["payload"].pop("motivo", None)
    schema = json.loads((OUT / f"{topico}.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(ex)), topico
print(len(TOPICOS), "schemas,", n, "exemplos válidos; casos inválidos rejeitados")
