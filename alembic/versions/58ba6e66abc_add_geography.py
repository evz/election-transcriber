"""Add geography

Revision ID: 58ba6e66abc
Revises: 3b5bf4bf1e3
Create Date: 2017-09-12 16:25:06.346667

"""

# revision identifiers, used by Alembic.
revision = '58ba6e66abc'
down_revision = '3b5bf4bf1e3'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.create_table('project_geography',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('project', sa.String(), nullable=True, index=True),
    sa.Column('geo_type', sa.String(), nullable=True, index=True),
    sa.Column('code', sa.String(), nullable=True),
    sa.Column('name', sa.String(), nullable=True, index=True),
    sa.Column('voter_count', sa.Integer(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    ### end Alembic commands ###


def downgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('project_geography')
    ### end Alembic commands ###
