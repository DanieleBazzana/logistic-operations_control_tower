"""Make exception history append-only at the database boundary.

Revision ID: 20250827_02
Revises: 20250827_01
Create Date: 2026-08-27
"""

from typing import Sequence

from alembic import op

revision: str = "20250827_02"
down_revision: str | None = "20250827_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_FK_NAME = "fk_exception_history_exception_id_exceptions"
_TRIGGER_NAME = "trg_exception_history_append_only"
_TRUNCATE_TRIGGER_NAME = "trg_exception_history_append_only_truncate"
_FUNCTION_NAME = "prevent_exception_history_mutation"


def upgrade() -> None:
    """Prevent deleting an exception from deleting its audit history."""
    op.execute(f"ALTER TABLE exception_history DROP CONSTRAINT {_FK_NAME}")
    op.execute(
        f"ALTER TABLE exception_history ADD CONSTRAINT {_FK_NAME} "
        "FOREIGN KEY (exception_id) REFERENCES exceptions (id) ON DELETE RESTRICT"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_FUNCTION_NAME}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'exception_history is append-only';
        END;
        $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_TRIGGER_NAME}
        BEFORE UPDATE OR DELETE ON exception_history
        FOR EACH ROW EXECUTE FUNCTION {_FUNCTION_NAME}();
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_TRUNCATE_TRIGGER_NAME}
        BEFORE TRUNCATE ON exception_history
        FOR EACH STATEMENT EXECUTE FUNCTION {_FUNCTION_NAME}();
        """
    )


def downgrade() -> None:
    """Remove append-only enforcement and restore the M01 cascade."""
    op.execute(f"DROP TRIGGER IF EXISTS {_TRUNCATE_TRIGGER_NAME} ON exception_history")
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME} ON exception_history")
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION_NAME}()")
    op.execute(f"ALTER TABLE exception_history DROP CONSTRAINT {_FK_NAME}")
    op.execute(
        f"ALTER TABLE exception_history ADD CONSTRAINT {_FK_NAME} "
        "FOREIGN KEY (exception_id) REFERENCES exceptions (id) ON DELETE CASCADE"
    )
