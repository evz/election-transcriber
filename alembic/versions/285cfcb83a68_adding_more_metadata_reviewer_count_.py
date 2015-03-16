"""adding more metadata - reviewer_count, deadline, image_location

Revision ID: 285cfcb83a68
Revises: 36e20e3215bd
Create Date: 2015-03-13 10:19:18.302699

"""

# revision identifiers, used by Alembic.
revision = '285cfcb83a68'
down_revision = '36e20e3215bd'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.add_column('form_meta', sa.Column('deadline', sa.DateTime(timezone=True), nullable=True))
    op.add_column('form_meta', sa.Column('image_location', sa.String(), nullable=True))
    op.add_column('form_meta', sa.Column('reviewer_count', sa.Integer(), nullable=True))
    ### end Alembic commands ###


def downgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('form_meta', 'reviewer_count')
    op.drop_column('form_meta', 'image_location')
    op.drop_column('form_meta', 'deadline')
    ### end Alembic commands ###