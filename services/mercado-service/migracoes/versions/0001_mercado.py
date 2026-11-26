"""Schema `mercado` do §6.1 e tabelas técnicas (§5.1). Começa apagando as tabelas de exemplo do kit (DG-56)."""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABELAS = ("produto", "oferta", "demanda", "demanda_modelo", "cancelamento_antecipado", "sugestao_leilao",
           "aceite_leilao", "negociacao", "negociacao_item", "produto_lock", "processed_events", "idempotency_keys",
           "outbox")


def upgrade():
    for exemplo in ("match", "modo_negociacao", "processed_events"):
        op.execute(f"DROP TABLE IF EXISTS mercado.{exemplo}")

    op.execute("""
        CREATE TABLE mercado.produto (
            produto_id varchar(64) PRIMARY KEY,
            nome text NOT NULL,
            unidade varchar(3) NOT NULL,
            atualizado_em timestamptz NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE mercado.oferta (
            fornecimento_id varchar(64) PRIMARY KEY,
            fornecedor_id varchar(64) NOT NULL,
            produto_id varchar(64) NOT NULL,
            unidade varchar(3) NOT NULL,
            preco numeric(18,4) NOT NULL CHECK (preco >= 0),
            quantidade_disponivel numeric(12,2) NOT NULL CHECK (quantidade_disponivel >= 0),
            origem jsonb,
            criada_em timestamptz NOT NULL,
            dados_atualizados_em timestamptz NOT NULL,
            estoque_atualizado_em timestamptz NOT NULL
        )
    """)
    op.execute("CREATE INDEX oferta_produto ON mercado.oferta (produto_id)")
    op.execute("""
        CREATE TABLE mercado.demanda (
            demanda_id varchar(64) PRIMARY KEY,
            demanda_origem_id varchar(64),
            comprador_id varchar(64) NOT NULL,
            produto_id varchar(64) NOT NULL,
            unidade varchar(3) NOT NULL,
            quantidade numeric(12,2) NOT NULL CHECK (quantidade > 0),
            preco_maximo numeric(18,4),
            destino jsonb NOT NULL,
            prazo_desejado timestamptz,
            saldo numeric(12,2) NOT NULL CHECK (saldo >= 0),
            em_negociacao numeric(12,2) NOT NULL CHECK (em_negociacao >= 0),
            situacao varchar(16) NOT NULL CHECK (situacao IN ('AGUARDANDO', 'ATENDIDA', 'CANCELADA')),
            prazo_vencido boolean NOT NULL DEFAULT false,
            recebida_em timestamptz NOT NULL,
            encerrada_em timestamptz
        )
    """)
    op.execute("CREATE INDEX demanda_aberta ON mercado.demanda (produto_id, recebida_em, demanda_id) "
               "WHERE situacao = 'AGUARDANDO'")
    op.execute("CREATE INDEX demanda_origem ON mercado.demanda (demanda_origem_id)")
    op.execute("CREATE INDEX demanda_prazo ON mercado.demanda (prazo_desejado) "
               "WHERE situacao = 'AGUARDANDO' AND NOT prazo_vencido AND prazo_desejado IS NOT NULL")
    op.execute("""
        CREATE TABLE mercado.demanda_modelo (
            demanda_id varchar(64) PRIMARY KEY,
            comprador_id varchar(64) NOT NULL,
            produto_id varchar(64) NOT NULL,
            recebido_em timestamptz NOT NULL,
            cancelado boolean NOT NULL DEFAULT false
        )
    """)
    op.execute("""
        CREATE TABLE mercado.cancelamento_antecipado (
            demanda_id varchar(64) PRIMARY KEY,
            comprador_id varchar(64) NOT NULL,
            cancelado_em timestamptz NOT NULL,
            recebido_em timestamptz NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE mercado.sugestao_leilao (
            sugestao_id uuid PRIMARY KEY,
            produto_id varchar(64) NOT NULL,
            situacao varchar(16) NOT NULL CHECK (situacao IN ('ABERTA', 'EM_FORMACAO', 'CONVERTIDA', 'ENCERRADA')),
            excesso_demanda numeric(18,2) NOT NULL,
            janela_encerra_em timestamptz,
            negociacao_id uuid,
            correlation_id varchar(128),
            aberta_em timestamptz NOT NULL,
            encerrada_em timestamptz
        )
    """)
    op.execute("CREATE UNIQUE INDEX sugestao_ativa_por_produto ON mercado.sugestao_leilao (produto_id) "
               "WHERE situacao IN ('ABERTA', 'EM_FORMACAO')")
    op.execute("CREATE INDEX sugestao_janela ON mercado.sugestao_leilao (janela_encerra_em) "
               "WHERE situacao = 'EM_FORMACAO'")
    op.execute("""
        CREATE TABLE mercado.aceite_leilao (
            sugestao_id uuid NOT NULL REFERENCES mercado.sugestao_leilao(sugestao_id),
            fornecimento_id varchar(64) NOT NULL,
            fornecedor_id varchar(64) NOT NULL,
            quantidade numeric(12,2) NOT NULL CHECK (quantidade > 0),
            aceito_em timestamptz NOT NULL,
            PRIMARY KEY (sugestao_id, fornecimento_id)
        )
    """)
    op.execute("""
        CREATE TABLE mercado.negociacao (
            negociacao_id uuid PRIMARY KEY,
            modo varchar(16) NOT NULL CHECK (modo IN ('VENDA_DIRETA', 'LEILAO_DIRETO')),
            produto_id varchar(64) NOT NULL,
            unidade varchar(3) NOT NULL,
            situacao varchar(16) NOT NULL CHECK (situacao IN ('ABERTA', 'CONCLUIDA', 'SEM_ACORDO')),
            demanda_id varchar(64),
            comprador_id varchar(64),
            compra_imediata boolean NOT NULL DEFAULT false,
            quantidade_total numeric(12,2) NOT NULL,
            valor_referencia numeric(18,4) NOT NULL,
            preco_inicial numeric(18,4),
            incremento_minimo numeric(18,4),
            sugestao_id uuid,
            inicia_em timestamptz,
            encerra_em timestamptz,
            definida_em timestamptz NOT NULL,
            encerrada_em timestamptz,
            correlation_id varchar(128) NOT NULL
        )
    """)
    op.execute("CREATE INDEX negociacao_lista ON mercado.negociacao (definida_em DESC, negociacao_id DESC)")
    op.execute("CREATE INDEX negociacao_direta ON mercado.negociacao (demanda_id) "
               "WHERE modo = 'VENDA_DIRETA' AND situacao = 'ABERTA'")
    op.execute("""
        CREATE TABLE mercado.negociacao_item (
            negociacao_id uuid NOT NULL REFERENCES mercado.negociacao(negociacao_id),
            fornecimento_id varchar(64) NOT NULL,
            ordem integer NOT NULL,
            fornecedor_id varchar(64) NOT NULL,
            quantidade numeric(12,2) NOT NULL CHECK (quantidade > 0),
            preco_unitario numeric(18,4) NOT NULL,
            valor_item numeric(18,4) NOT NULL,
            participacao numeric(6,4) NOT NULL,
            origem jsonb,
            PRIMARY KEY (negociacao_id, fornecimento_id)
        )
    """)
    op.execute("CREATE INDEX item_fornecedor ON mercado.negociacao_item (fornecedor_id)")
    op.execute("""
        CREATE TABLE mercado.produto_lock (
            produto_id varchar(64) PRIMARY KEY,
            situacao varchar(20),
            situacao_desde timestamptz
        )
    """)
    op.execute("""
        CREATE TABLE mercado.processed_events (
            source text NOT NULL,
            event_id text NOT NULL,
            topico text NOT NULL,
            processado_em timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (source, event_id)
        )
    """)
    op.execute("""
        CREATE TABLE mercado.idempotency_keys (
            escopo text PRIMARY KEY,
            hash_corpo varchar(64) NOT NULL,
            status integer NOT NULL,
            corpo json NOT NULL,
            location text,
            criado_em timestamptz NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE mercado.outbox (
            id bigserial PRIMARY KEY,
            event_id uuid NOT NULL UNIQUE,
            topico text NOT NULL,
            chave varchar(128) NOT NULL,
            envelope json NOT NULL,
            tentativas integer NOT NULL DEFAULT 0,
            proximo_envio timestamptz NOT NULL DEFAULT now(),
            criado_em timestamptz NOT NULL DEFAULT now(),
            confirmado_em timestamptz
        )
    """)
    op.execute("CREATE INDEX outbox_pendente ON mercado.outbox (chave, id) WHERE confirmado_em IS NULL")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join('mercado.' + t for t in TABELAS)} TO svc_mercado")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE mercado.outbox_id_seq TO svc_mercado")
    op.execute("GRANT SELECT ON mercado.alembic_version TO svc_mercado")


def downgrade():
    for tabela in reversed(TABELAS):
        op.execute(f"DROP TABLE mercado.{tabela}")
