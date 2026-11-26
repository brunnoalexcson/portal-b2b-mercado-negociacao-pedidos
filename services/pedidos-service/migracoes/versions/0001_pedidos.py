"""Schema pedidos da ESP-PED-001 v2.2 (pedido temporário, reserva, fechamento, destino e frete)."""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Tabelas de exemplo que o database/01-init.sql do kit cria no schema pedidos e tabelas da v1.1.
# Como no Mercado (DG-56) e na Negociação (D45): o dono do schema (portal_ddl) pode apagá-las.
ANTIGAS = ("pedido_historico", "audit_log", "processed_events", "outbox", "idempotency_keys", "pedido",
           "produto_referencia", "fornecimento_referencia", "resultado_negociacao")
TABELAS = ("produto", "oferta", "negociacao_encerrada", "solicitacao_pendente", "pedido", "pedido_historico",
           "audit_log", "solicitacao_frete", "entrega_pendente", "processed_events", "idempotency_keys", "outbox")


def upgrade():
    for nome in ANTIGAS:
        op.execute(f"DROP TABLE IF EXISTS pedidos.{nome} CASCADE")

    op.execute("""
        CREATE TABLE pedidos.produto (
            produto_id varchar(64) PRIMARY KEY,
            nome varchar(255) NOT NULL,
            unidade text NOT NULL CHECK (unidade IN ('KG','TON','UN','CX','L')),
            tipo_transporte text NOT NULL CHECK (tipo_transporte IN ('GRANEL','REFRIGERADO','CARGA_SECA','PERIGOSO')),
            peso_unitario_kg numeric(12,3) CHECK (peso_unitario_kg > 0),
            evento_em timestamptz NOT NULL
        )""")
    op.execute("""
        CREATE TABLE pedidos.oferta (
            fornecimento_id varchar(64) PRIMARY KEY,
            fornecedor_id varchar(64) NOT NULL,
            produto_id varchar(64) NOT NULL,
            unidade text NOT NULL,
            origem jsonb NOT NULL
        )""")
    op.execute("""
        CREATE TABLE pedidos.negociacao_encerrada (
            negociacao_id varchar(64) PRIMARY KEY,
            status text NOT NULL CHECK (status IN ('ENCERRADO','DESERTO','RECUSADO','EXPIRADO')),
            comprador_id varchar(64),
            alocacoes jsonb NOT NULL,
            recebido_em timestamptz NOT NULL
        )""")
    op.execute("""
        CREATE TABLE pedidos.solicitacao_pendente (
            negociacao_id varchar(64) NOT NULL,
            fornecimento_id varchar(64) NOT NULL,
            produto_id varchar(64) NOT NULL,
            payload jsonb NOT NULL,
            event_id uuid NOT NULL,
            correlation_id varchar(128) NOT NULL,
            causa text NOT NULL CHECK (causa IN ('PRODUTO_AUSENTE','OFERTA_AUSENTE','PESO_AUSENTE')),
            recebido_em timestamptz NOT NULL,
            PRIMARY KEY (negociacao_id, fornecimento_id)
        )""")
    op.execute("CREATE INDEX solicitacao_pendente_produto ON pedidos.solicitacao_pendente (produto_id, recebido_em)")
    op.execute("CREATE INDEX solicitacao_pendente_oferta ON pedidos.solicitacao_pendente (fornecimento_id, recebido_em)")
    op.execute("CREATE INDEX solicitacao_pendente_fila ON pedidos.solicitacao_pendente (recebido_em)")
    op.execute("""
        CREATE TABLE pedidos.pedido (
            pedido_id uuid PRIMARY KEY,
            negociacao_id varchar(64) NOT NULL,
            fornecimento_id varchar(64) NOT NULL,
            tipo_negociacao text NOT NULL CHECK (tipo_negociacao IN ('LEILAO','VENDA_DIRETA')),
            produto_id varchar(64) NOT NULL,
            fornecedor_id varchar(64) NOT NULL,
            comprador_id varchar(64),
            quantidade numeric(12,2) NOT NULL CHECK (quantidade > 0),
            unidade text NOT NULL,
            preco_unitario_oferta numeric(18,4) NOT NULL CHECK (preco_unitario_oferta >= 0),
            valor_unitario numeric(18,4) NOT NULL CHECK (valor_unitario >= 0),
            valor_total numeric(18,4) NOT NULL CHECK (valor_total >= 0),
            produto_nome varchar(255) NOT NULL,
            tipo_transporte text NOT NULL,
            peso_kg numeric(24,5) NOT NULL CHECK (peso_kg > 0),
            origem jsonb NOT NULL,
            destino jsonb,
            data_entrega_desejada date,
            status text NOT NULL CHECK (status IN ('CRIADO','VENDA_CONCLUIDA','AGUARDANDO_FRETE',
                                                   'FRETE_SELECIONADO','ENTREGUE','CANCELADO')),
            reserva_situacao text NOT NULL CHECK (reserva_situacao IN ('PENDENTE','CONFIRMADA','RECUSADA',
                                                                       'ENCERRADA')),
            motivo_cancelamento text,
            solicitacao_frete_vigente_id varchar(64),
            frete jsonb,
            ocorrencia_frete jsonb,
            versao integer NOT NULL CHECK (versao >= 1),
            criado_em timestamptz NOT NULL,
            atualizado_em timestamptz NOT NULL,
            vendido_em timestamptz,
            entregue_em timestamptz,
            correlation_id varchar(128) NOT NULL,
            UNIQUE (negociacao_id, fornecimento_id)
        )""")
    op.execute("CREATE INDEX pedido_comprador ON pedidos.pedido (comprador_id, criado_em DESC, pedido_id DESC)")
    op.execute("CREATE INDEX pedido_fornecedor ON pedidos.pedido (fornecedor_id, criado_em DESC, pedido_id DESC)")
    op.execute("CREATE INDEX pedido_recentes ON pedidos.pedido (criado_em DESC, pedido_id DESC)")
    op.execute("""
        CREATE TABLE pedidos.pedido_historico (
            pedido_id uuid NOT NULL REFERENCES pedidos.pedido (pedido_id),
            versao integer NOT NULL,
            status_anterior text,
            status text NOT NULL,
            reserva_situacao text NOT NULL,
            origem text NOT NULL,
            motivo text,
            event_id uuid,
            usuario varchar(128),
            correlation_id varchar(128) NOT NULL,
            ocorrido_em timestamptz NOT NULL,
            PRIMARY KEY (pedido_id, versao)
        )""")
    op.execute("""
        CREATE TABLE pedidos.audit_log (
            id bigserial PRIMARY KEY,
            acao text NOT NULL,
            instante timestamptz NOT NULL,
            detalhe jsonb NOT NULL,
            pedido_id varchar(64),
            negociacao_id varchar(64),
            usuario varchar(128),
            empresa varchar(64),
            event_id uuid,
            correlation_id varchar(128)
        )""")
    op.execute("CREATE INDEX audit_log_pedido ON pedidos.audit_log (pedido_id, instante)")
    op.execute("""
        CREATE TABLE pedidos.solicitacao_frete (
            solicitacao_frete_id varchar(64) PRIMARY KEY,
            pedido_id uuid NOT NULL REFERENCES pedidos.pedido (pedido_id),
            situacao text NOT NULL CHECK (situacao IN ('VIGENTE','SUBSTITUIDA')),
            indisponivel boolean NOT NULL DEFAULT false,
            motivo_indisponivel text,
            anterior_id varchar(64),
            selecoes jsonb NOT NULL DEFAULT '[]'
        )""")
    op.execute("""CREATE UNIQUE INDEX solicitacao_frete_vigente ON pedidos.solicitacao_frete (pedido_id)
                  WHERE situacao = 'VIGENTE'""")
    op.execute("""
        CREATE TABLE pedidos.entrega_pendente (
            pedido_id uuid NOT NULL REFERENCES pedidos.pedido (pedido_id),
            entrega_id varchar(64) NOT NULL,
            transportadora_id varchar(64) NOT NULL,
            entregue_em timestamptz NOT NULL,
            event_id uuid NOT NULL,
            correlation_id varchar(128) NOT NULL,
            recebido_em timestamptz NOT NULL,
            PRIMARY KEY (pedido_id, entrega_id)
        )""")
    op.execute("""
        CREATE TABLE pedidos.processed_events (
            source text NOT NULL,
            event_id uuid NOT NULL,
            topico text NOT NULL,
            processado_em timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (source, event_id)
        )""")
    op.execute("""
        CREATE TABLE pedidos.idempotency_keys (
            escopo text PRIMARY KEY,
            hash char(64) NOT NULL,
            status integer NOT NULL,
            corpo jsonb NOT NULL,
            expira_em timestamptz NOT NULL
        )""")
    op.execute("""
        CREATE TABLE pedidos.outbox (
            event_id uuid PRIMARY KEY,
            topico text NOT NULL,
            chave text NOT NULL,
            envelope jsonb NOT NULL,
            pedido_id uuid NOT NULL,
            versao integer NOT NULL,
            criado_em timestamptz NOT NULL DEFAULT now(),
            tentativas integer NOT NULL DEFAULT 0,
            proximo_envio timestamptz NOT NULL DEFAULT now(),
            confirmado_em timestamptz,
            UNIQUE (pedido_id, versao)
        )""")
    op.execute("CREATE INDEX outbox_pendente ON pedidos.outbox (criado_em) WHERE confirmado_em IS NULL")

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join('pedidos.' + t for t in TABELAS)} TO svc_pedidos")
    op.execute("GRANT USAGE ON SEQUENCE pedidos.audit_log_id_seq TO svc_pedidos")
    op.execute("GRANT SELECT ON pedidos.alembic_version TO svc_pedidos")


def downgrade():
    for nome in reversed(TABELAS):
        op.execute(f"DROP TABLE IF EXISTS pedidos.{nome} CASCADE")
