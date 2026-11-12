from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    for nome in ("produto_referencia", "fornecimento_referencia"):
        op.execute(f"""
            CREATE TABLE pedidos.{nome} (
                id varchar(64) PRIMARY KEY,
                dados jsonb NOT NULL,
                event_id uuid NOT NULL,
                atualizado_em timestamptz NOT NULL
            )
        """)
    op.execute("""
        CREATE TABLE pedidos.resultado_negociacao (
            negociacao_id varchar(64) PRIMARY KEY,
            status text NOT NULL CHECK (status IN ('ENCERRADO', 'DESERTO')),
            dados jsonb NOT NULL,
            fingerprint text NOT NULL,
            event_id uuid NOT NULL,
            correlation_id varchar(128) NOT NULL,
            recebido_em timestamptz NOT NULL DEFAULT now(),
            pendente boolean NOT NULL DEFAULT false,
            reavaliar_em timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX resultado_pendente
        ON pedidos.resultado_negociacao (reavaliar_em, negociacao_id) WHERE pendente
    """)
    op.execute("""
        CREATE TABLE pedidos.pedido (
            id uuid PRIMARY KEY,
            negociacao_id varchar(64) NOT NULL UNIQUE REFERENCES pedidos.resultado_negociacao(negociacao_id),
            comprador_id varchar(64) NOT NULL,
            fornecedor_id varchar(64) NOT NULL,
            fornecimento_id varchar(64) NOT NULL,
            produto_id varchar(64) NOT NULL,
            produto_nome varchar(255) NOT NULL,
            quantidade numeric(12,2) NOT NULL CHECK (quantidade > 0),
            unidade text NOT NULL,
            valor_unitario numeric(18,4) NOT NULL CHECK (valor_unitario > 0),
            valor_total numeric(18,4) NOT NULL CHECK (valor_total >= 0),
            peso_kg numeric(24,5) NOT NULL CHECK (peso_kg > 0),
            tipo_transporte text NOT NULL,
            origem jsonb NOT NULL,
            destino jsonb NOT NULL,
            data_entrega_desejada date,
            status text NOT NULL DEFAULT 'CRIADO',
            reserva_situacao text NOT NULL DEFAULT 'PENDENTE',
            versao integer NOT NULL DEFAULT 1,
            criado_em timestamptz NOT NULL DEFAULT now(),
            atualizado_em timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX pedido_comprador ON pedidos.pedido (comprador_id, criado_em DESC, id DESC)")
    op.execute("CREATE INDEX pedido_fornecedor ON pedidos.pedido (fornecedor_id, criado_em DESC, id DESC)")
    op.execute("""
        CREATE TABLE pedidos.pedido_historico (
            pedido_id uuid NOT NULL REFERENCES pedidos.pedido(id),
            versao integer NOT NULL,
            status_anterior text,
            status text NOT NULL,
            origem text NOT NULL,
            motivo text,
            ocorrido_em timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (pedido_id, versao)
        )
    """)
    op.execute("""
        CREATE TABLE pedidos.audit_log (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            pedido_id uuid REFERENCES pedidos.pedido(id),
            negociacao_id varchar(64),
            usuario_id varchar(128),
            origem text NOT NULL,
            event_id uuid,
            correlation_id varchar(128) NOT NULL,
            resultado text NOT NULL,
            ocorrido_em timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE pedidos.processed_events (
            source text NOT NULL,
            event_id uuid NOT NULL,
            fingerprint text NOT NULL,
            topico text NOT NULL,
            particao integer NOT NULL,
            "offset" bigint NOT NULL,
            resultado text NOT NULL,
            processado_em timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (source, event_id)
        )
    """)
    op.execute("""
        CREATE TABLE pedidos.outbox (
            event_id uuid PRIMARY KEY,
            pedido_id uuid NOT NULL REFERENCES pedidos.pedido(id),
            versao integer NOT NULL,
            topico text NOT NULL,
            chave text NOT NULL,
            envelope jsonb NOT NULL,
            tentativas integer NOT NULL DEFAULT 0,
            proximo_envio timestamptz NOT NULL DEFAULT now(),
            confirmado_em timestamptz,
            UNIQUE (pedido_id, versao)
        )
    """)
    op.execute("CREATE INDEX outbox_pronta ON pedidos.outbox (proximo_envio) WHERE confirmado_em IS NULL")
    op.execute("""
        CREATE TABLE pedidos.idempotency_keys (
            usuario_id varchar(128) NOT NULL,
            chave varchar(128) NOT NULL,
            negociacao_id varchar(64) NOT NULL,
            status integer,
            resposta jsonb,
            expira_em timestamptz NOT NULL DEFAULT now() + interval '24 hours',
            PRIMARY KEY (usuario_id, chave)
        )
    """)
    op.execute("""
        GRANT SELECT, INSERT, UPDATE, DELETE ON
          pedidos.produto_referencia, pedidos.fornecimento_referencia,
          pedidos.resultado_negociacao, pedidos.pedido, pedidos.pedido_historico,
          pedidos.audit_log, pedidos.processed_events, pedidos.outbox, pedidos.idempotency_keys
        TO svc_pedidos
    """)
    op.execute("GRANT SELECT ON pedidos.alembic_version TO svc_pedidos")


def downgrade():
    for nome in ("idempotency_keys", "outbox", "processed_events", "audit_log", "pedido_historico",
                 "pedido", "resultado_negociacao", "fornecimento_referencia", "produto_referencia"):
        op.execute(f"DROP TABLE pedidos.{nome}")
