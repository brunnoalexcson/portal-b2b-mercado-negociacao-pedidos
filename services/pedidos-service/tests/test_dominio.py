import unittest
from decimal import Decimal

from app.contratos import Fechamento, Fornecimento, Produto
from app.dominio import montar_carga


class TestCarga(unittest.TestCase):
    def setUp(self):
        self.resultado = Fechamento.model_validate({
            "negociacaoId": "neg-1", "status": "ENCERRADO",
            "compradorId": "emp-1", "fornecedorId": "emp-2",
            "produtoId": "prod-1", "fornecimentoId": "forn-1",
            "quantidade": Decimal("1.25"), "valorUnitario": Decimal("8.00"),
            "destino": {"cidade": "Anápolis", "uf": "GO"},
        })
        self.produto = Produto.model_validate({
            "produtoId": "prod-1", "nome": "Milho", "unidade": "TON", "tipoTransporte": "GRANEL",
        })
        self.oferta = Fornecimento.model_validate({
            "fornecimentoId": "forn-1", "fornecedorId": "emp-2", "produtoId": "prod-1",
            "unidade": "TON", "origem": {"cidade": "Rio Verde", "uf": "GO"},
        })

    def test_calcula_valor_e_peso_sem_ponto_flutuante(self):
        carga = montar_carga(self.resultado, self.produto, self.oferta)
        self.assertEqual(carga["pesoKg"], Decimal("1250"))
        self.assertEqual(carga["valorTotal"], Decimal("10.00"))
        self.assertEqual(carga["fornecimentoId"], "forn-1")

    def test_oferta_de_outro_fornecedor_nao_gera_carga(self):
        divergente = self.oferta.model_copy(update={"fornecedorId": "outro"})
        self.assertIsNone(montar_carga(self.resultado, self.produto, divergente))

    def test_unidade_sem_peso_fica_pendente(self):
        produto = self.produto.model_copy(update={"unidade": "UN"})
        oferta = self.oferta.model_copy(update={"unidade": "UN"})
        self.assertIsNone(montar_carga(self.resultado, produto, oferta))

    def test_arredonda_total_para_duas_casas(self):
        resultado = self.resultado.model_copy(update={"quantidade": Decimal("0.05"), "valorUnitario": Decimal("0.10")})
        carga = montar_carga(resultado, self.produto, self.oferta)
        self.assertEqual(carga["valorTotal"], Decimal("0.01"))
