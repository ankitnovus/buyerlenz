"""activity log append only

Revision ID: 204cfc6298a3
Revises: 4e9640ec8c14
Create Date: 2026-10-27 09:34:44.847685

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.guards import ACTIVITY_LOG_APPEND_ONLY, REMOVE_ACTIVITY_LOG_APPEND_ONLY


# revision identifiers, used by Alembic.
revision: str = '204cfc6298a3'
down_revision: Union[str, Sequence[str], None] = '4e9640ec8c14'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(ACTIVITY_LOG_APPEND_ONLY)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(REMOVE_ACTIVITY_LOG_APPEND_ONLY)
