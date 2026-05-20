"""drop utility_type enum, widen column to VARCHAR(50)

Revision ID: f2a9e1c3b057
Revises: 3b66afe6b62c
Create Date: 2026-05-20 00:01:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PgEnum


revision: str = 'f2a9e1c3b057'
down_revision: Union[str, None] = '3b66afe6b62c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Convert utility_bills.type from the utility_type Postgres ENUM to a plain
    # VARCHAR(50).  USING type::text is essential — it casts existing enum values
    # to their text representation rather than trying an implicit cast that Postgres
    # would reject.
    op.execute(
        "ALTER TABLE utility_bills ALTER COLUMN type TYPE VARCHAR(50) USING type::text"
    )
    # The enum type is now unreferenced; drop it to keep the schema clean.
    op.execute("DROP TYPE utility_type")


def downgrade() -> None:
    # Recreate the utility_type enum with its original members and restore the
    # column type.
    # NOTE: downgrade will fail if any rows contain custom (non-enum) values —
    # this is expected and acceptable; there is no safe automatic migration back
    # once custom types have been stored.
    utility_type_enum = PgEnum(
        "electricity", "internet", "gas", "water", "other",
        name="utility_type",
    )
    utility_type_enum.create(op.get_bind())
    op.execute(
        "ALTER TABLE utility_bills ALTER COLUMN type TYPE utility_type"
        " USING type::utility_type"
    )
