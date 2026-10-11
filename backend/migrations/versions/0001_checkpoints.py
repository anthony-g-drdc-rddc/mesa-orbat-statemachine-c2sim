"""Create durable runs and complete, immutable boundary checkpoints."""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("latest_seq", sa.Integer(), nullable=False),
        sa.Column("parent_run_id", sa.String(), sa.ForeignKey("runs.id")),
        sa.Column("parent_checkpoint_seq", sa.Integer()),
    )
    op.create_table(
        "checkpoints",
        sa.Column("run_id", sa.String(), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("state", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("checkpoints")
    op.drop_table("runs")
