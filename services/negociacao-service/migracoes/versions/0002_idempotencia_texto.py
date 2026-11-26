from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    # jsonb reordena as chaves, e o replay da idempotência precisa devolver os mesmos bytes da resposta original
    op.execute("ALTER TABLE negociacao.idempotency_keys ALTER COLUMN corpo TYPE text USING corpo::text")


def downgrade():
    op.execute("ALTER TABLE negociacao.idempotency_keys ALTER COLUMN corpo TYPE jsonb USING corpo::jsonb")
