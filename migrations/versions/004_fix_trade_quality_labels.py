"""Fix Trade Quality labels - Grade A should be Aggressive, Grade C should be Conservative

Revision ID: 004_fix_trade_quality_labels
Revises: 003_add_risk_monitoring_and_websocket
Create Date: 2025-12-24

The original labels were backwards:
- Grade A (95% margin) was labeled 'conservative' but should be 'aggressive' (higher risk)
- Grade C (36% margin) was labeled 'aggressive' but should be 'conservative' (lower risk)
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '004_fix_trade_quality_labels'
down_revision = '003_add_risk_monitoring_and_websocket'
branch_labels = None
depends_on = None


def upgrade():
    """Fix incorrect risk_level labels in trade_qualities table"""
    # Fix Grade A: conservative -> aggressive
    op.execute("""
        UPDATE trade_qualities
        SET risk_level = 'aggressive',
            description = 'Aggressive approach - Uses 95% of available margin (higher risk)'
        WHERE quality_grade = 'A' AND risk_level = 'conservative'
    """)

    # Fix Grade C: aggressive -> conservative
    op.execute("""
        UPDATE trade_qualities
        SET risk_level = 'conservative',
            description = 'Conservative approach - Uses 36% of available margin (lower risk)'
        WHERE quality_grade = 'C' AND risk_level = 'aggressive'
    """)


def downgrade():
    """Revert to original (incorrect) labels"""
    # Revert Grade A: aggressive -> conservative
    op.execute("""
        UPDATE trade_qualities
        SET risk_level = 'conservative',
            description = 'Conservative approach - Uses 95% of available margin'
        WHERE quality_grade = 'A' AND risk_level = 'aggressive'
    """)

    # Revert Grade C: conservative -> aggressive
    op.execute("""
        UPDATE trade_qualities
        SET risk_level = 'aggressive',
            description = 'Aggressive approach - Uses 36% of available margin'
        WHERE quality_grade = 'C' AND risk_level = 'conservative'
    """)
