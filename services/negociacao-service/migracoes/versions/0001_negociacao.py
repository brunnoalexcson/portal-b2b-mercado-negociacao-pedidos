from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABELAS = ("processo_negociacao", "oferta_leilao", "lance", "proposta", "idempotency_keys",
           "processed_events", "outbox")


def upgrade():
    # tabelas de exemplo do kit, vazias e com nomes iguais aos deste serviço
    op.execute("""
        DROP TABLE IF EXISTS negociacao.negociacao, negociacao.lance,
          negociacao.idempotency_keys, negociacao.processed_events
    """)
    op.execute("""
        CREATE TABLE negociacao.processo_negociacao (
            id uuid PRIMARY KEY,
            modo text NOT NULL CHECK (modo IN ('LEILAO_DIRETO', 'VENDA_DIRETA')),
            status text NOT NULL CHECK (status IN
              ('AGUARDANDO_RESERVAS', 'ABERTO', 'ENCERRADO', 'DESERTO', 'RECUSADO', 'EXPIRADO')),
            produto_id varchar(64) NOT NULL,
            data_inicio timestamptz NOT NULL,
            data_fim timestamptz,
            quantidade_total numeric(12,2),
            valor_reserva numeric(18,4),
            incremento_minimo numeric(18,4),
            incremento_minimo_percentual numeric CHECK (incremento_minimo_percentual >= 0),
            melhor_lance_id uuid,
            total_lances integer NOT NULL DEFAULT 0 CHECK (total_lances >= 0),
            comprador_id varchar(64),
            fornecimento_id varchar(64),
            fornecedor_id varchar(64),
            quantidade numeric(12,2),
            preco_oferta numeric(18,4),
            preco_atual numeric(18,4),
            vez text CHECK (vez IN ('BUYER', 'SUPPLIER')),
            compra_imediata boolean NOT NULL DEFAULT false,
            expira_em timestamptz,
            reserva_situacao text CHECK (reserva_situacao IN ('PENDENTE', 'CONFIRMADA', 'RECUSADA')),
            pedido_id varchar(64),
            total_propostas integer NOT NULL DEFAULT 0 CHECK (total_propostas >= 0),
            correlation_id varchar(128) NOT NULL,
            criado_em timestamptz NOT NULL,
            encerrado_em timestamptz
        )
    """)
    op.execute("""
        CREATE TABLE negociacao.oferta_leilao (
            negociacao_id uuid NOT NULL REFERENCES negociacao.processo_negociacao(id),
            fornecimento_id varchar(64) NOT NULL,
            fornecedor_id varchar(64) NOT NULL,
            quantidade numeric(12,2) NOT NULL,
            preco_unitario numeric(18,4) NOT NULL,
            valor_item numeric(18,4) NOT NULL,
            participacao numeric(6,4) NOT NULL,
            ordem integer NOT NULL CHECK (ordem >= 1),
            situacao text NOT NULL CHECK (situacao IN ('PENDENTE', 'CONFIRMADA', 'RECUSADA')),
            pedido_id varchar(64),
            PRIMARY KEY (negociacao_id, fornecimento_id)
        )
    """)
    op.execute("""
        CREATE TABLE negociacao.lance (
            id uuid PRIMARY KEY,
            negociacao_id uuid NOT NULL REFERENCES negociacao.processo_negociacao(id),
            comprador_id varchar(64) NOT NULL,
            usuario_id varchar(128) NOT NULL,
            valor numeric(18,4) NOT NULL,
            demanda_id varchar(64),
            sequencia integer NOT NULL CHECK (sequencia >= 1),
            criado_em timestamptz NOT NULL,
            UNIQUE (negociacao_id, sequencia)
        )
    """)
    op.execute("""
        CREATE TABLE negociacao.proposta (
            id uuid PRIMARY KEY,
            negociacao_id uuid NOT NULL REFERENCES negociacao.processo_negociacao(id),
            autor text NOT NULL CHECK (autor IN ('BUYER', 'SUPPLIER')),
            empresa_id varchar(64) NOT NULL,
            preco_unitario numeric(18,4) NOT NULL,
            sequencia integer NOT NULL CHECK (sequencia >= 1),
            criado_em timestamptz NOT NULL,
            UNIQUE (negociacao_id, sequencia)
        )
    """)
    op.execute("""
        CREATE TABLE negociacao.idempotency_keys (
            escopo text PRIMARY KEY,
            hash_corpo text NOT NULL,
            status integer NOT NULL,
            corpo jsonb NOT NULL,
            location text,
            criado_em timestamptz NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE negociacao.processed_events (
            source text NOT NULL,
            event_id text NOT NULL,
            topico text NOT NULL,
            processado_em timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (source, event_id)
        )
    """)
    op.execute("""
        CREATE TABLE negociacao.outbox (
            event_id uuid PRIMARY KEY,
            -- ordem de gravação; criado_em empata entre eventos da mesma transação
            sequencia bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            topico text NOT NULL,
            chave text NOT NULL,
            envelope jsonb NOT NULL,
            tentativas integer NOT NULL DEFAULT 0,
            proximo_envio timestamptz NOT NULL DEFAULT now(),
            confirmado_em timestamptz,
            criado_em timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX outbox_pendente ON negociacao.outbox (chave, sequencia) WHERE confirmado_em IS NULL")
    op.execute(f"""
        GRANT SELECT, INSERT, UPDATE, DELETE ON {", ".join(f"negociacao.{t}" for t in TABELAS)}
        TO svc_negociacao
    """)
    op.execute("GRANT SELECT ON negociacao.alembic_version TO svc_negociacao")


def downgrade():
    op.execute(f"DROP TABLE {', '.join(f'negociacao.{t}' for t in TABELAS)}")
