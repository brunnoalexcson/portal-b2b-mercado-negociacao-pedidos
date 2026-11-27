"""Duração do leilão escolhida pelo fornecedor no primeiro aceite (DG-70)."""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE mercado.sugestao_leilao ADD COLUMN IF NOT EXISTS duracao_leilao_horas integer "
               "CHECK (duracao_leilao_horas > 0)")


def downgrade():
    op.execute("ALTER TABLE mercado.sugestao_leilao DROP COLUMN IF EXISTS duracao_leilao_horas")
