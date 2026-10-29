"""Immutable approval packets; preserve all existing consent and order evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_mvp037_privacy"
down_revision = "0007_mvp029_chat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "consent_publications",
        sa.Column("consent_version", sa.Text(), nullable=False),
        sa.Column("bundle_checksum", sa.Text(), nullable=False),
        sa.Column("bundle", postgresql.JSONB(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["consent_version"], ["app.consent_texts.version"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("consent_version"),
        sa.CheckConstraint(
            "bundle_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_consent_publications_checksum",
        ),
        schema="app",
    )
    op.execute("""
        CREATE FUNCTION app.protect_consent_publication() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'Consent publication is immutable'
                USING ERRCODE = '23514';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER consent_publication_immutable BEFORE UPDATE OR DELETE
        ON app.consent_publications FOR EACH ROW
        EXECUTE FUNCTION app.protect_consent_publication()
    """)
    op.execute("REVOKE ALL ON FUNCTION app.protect_consent_publication() FROM PUBLIC")
    # Publication/retirement requires protected migrator CLI credentials in addition
    # to staff admin authentication. Ordinary API/SQLAdmin cannot modify documents.
    op.execute(
        "REVOKE INSERT, UPDATE, DELETE ON app.consent_publications "
        "FROM akbrizcase_runtime"
    )
    # Existing synthetic fixture tooling is retained only on owner-marked databases.
    # Production has no runtime consent-write privilege, including retired_at.
    op.execute("""
        DO $$ BEGIN
            IF coalesce(shobj_description((SELECT oid FROM pg_database
                WHERE datname=current_database()), 'pg_database'), '')
                NOT IN ('akbrizcase:synthetic-seed:demo',
                        'akbrizcase:synthetic-seed:test') THEN
                REVOKE INSERT, UPDATE, DELETE ON app.consent_texts
                    FROM akbrizcase_runtime;
                -- Row SHARE locks need UPDATE on a column. Identity trigger
                -- forbids changing this column; no retirement privilege is granted.
                GRANT UPDATE(version) ON app.consent_texts TO akbrizcase_runtime;
            ELSE
                GRANT INSERT ON app.consent_publications TO akbrizcase_runtime;
            END IF;
        END $$
    """)

    # Fixed synthetic fixture only. Owners may mark a demo DB after upgrading;
    # that must not require granting production document-write privileges to API.
    op.execute("""
        CREATE FUNCTION app.seed_demo_consent(packet jsonb, packet_checksum text)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        BEGIN
            IF coalesce(shobj_description((SELECT oid FROM pg_database
                WHERE datname=current_database()), 'pg_database'), '')
                NOT IN ('akbrizcase:synthetic-seed:demo',
                        'akbrizcase:synthetic-seed:test')
                OR packet->>'approval_status' IS DISTINCT FROM 'SYNTHETIC ONLY'
                OR packet->'consent'->>'version'
                    IS DISTINCT FROM 'SYNTHETIC-DEMO-ONLY-v1'
                OR packet->'consent'->>'checksum' IS DISTINCT FROM
                    encode(sha256(convert_to(packet->'consent'->>'text','UTF8')),'hex')
            THEN
                RAISE EXCEPTION 'Synthetic seed requires an owner-marked database'
                    USING ERRCODE = '23514';
            END IF;
            INSERT INTO app.consent_texts(version,purpose,text,checksum,active_from)
            VALUES ('SYNTHETIC-DEMO-ONLY-v1', packet->'consent'->>'purpose',
                packet->'consent'->>'text', packet->'consent'->>'checksum',
                '2026-01-01T00:00:00Z') ON CONFLICT (version) DO NOTHING;
            INSERT INTO app.consent_publications
                (consent_version,bundle_checksum,bundle,published_at)
            VALUES ('SYNTHETIC-DEMO-ONLY-v1',packet_checksum,packet,
                '2026-01-01T00:00:00Z') ON CONFLICT (consent_version) DO NOTHING;
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION app.seed_demo_consent(jsonb,text) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.seed_demo_consent(jsonb,text) "
        "TO akbrizcase_runtime"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION app.seed_demo_consent(jsonb,text)")
    op.execute(
        "GRANT INSERT, UPDATE, DELETE ON app.consent_texts TO akbrizcase_runtime"
    )
    op.drop_table("consent_publications", schema="app")
    op.execute("DROP FUNCTION app.protect_consent_publication()")
