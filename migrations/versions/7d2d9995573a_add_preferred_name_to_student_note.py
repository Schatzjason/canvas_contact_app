"""add preferred_name to student_note

Revision ID: 7d2d9995573a
Revises: ed11b0f9566d
Create Date: 2026-09-09 13:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7d2d9995573a'
down_revision = 'ed11b0f9566d'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('student_note', schema=None) as batch_op:
        batch_op.add_column(sa.Column('preferred_name', sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table('student_note', schema=None) as batch_op:
        batch_op.drop_column('preferred_name')
