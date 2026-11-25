"""Use the connection and transaction selected by the Taskboard operator command."""

from alembic import context


context.configure(
    connection=context.config.attributes["connection"],
    version_table=context.config.attributes["version_table"],
    transactional_ddl=True,
)
with context.begin_transaction():
    context.run_migrations()
