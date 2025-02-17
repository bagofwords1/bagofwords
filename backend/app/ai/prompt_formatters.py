from pydantic import BaseModel


class TableColumn(BaseModel):
    name: str
    dtype: str | None


class ForeignKey(BaseModel):
    column: TableColumn
    references_name: str
    references_column: TableColumn


class Table(BaseModel):
    name: str
    columns: list[TableColumn] | None
    pks: list[TableColumn] | None
    fks: list[ForeignKey] | None


class ServiceFormatter:
    def __init__(self, tables: list[Table]) -> None:
        self.tables = tables
        self.table_str = self.format_tables(tables)
    
    def format_tables(self, tables: list[Table]) -> str:
        table_strs = []
        for table in tables:
            table_strs.append(self.format_table(table))
        return "\n\n".join(table_strs)
    
    def format_table(self, table: Table) -> str:
        table_strs = []
        table_title = f"table: {table.name}"
        table_strs.append(table_title)
        for col in table.columns or []:
            table_strs.append(f"column: {col.name} type: {col.dtype or 'any'}")
        
        return "\n".join(table_strs)


class TableFormatter:

    table_sep: str = "\n\n"

    def __init__(self, tables: list[Table]) -> None:
        self.tables = tables
        self.table_str = self.format_tables(tables)

    def format_table(self, table: Table) -> str:
        """Get table format."""
        table_fmt = []
        table_name = table.name
        for col in table.columns or []:
            table_fmt.append(f"    {col.name} {col.dtype or 'any'}")
        if table.pks:
            table_fmt.append(
                f"    primary key ({', '.join(pk.name for pk in table.pks)})"
            )
        for fk in table.fks or []:
            table_fmt.append(
                f"    foreign key ({fk.column.name}) references {fk.references_name}({fk.references_column.name})"  # noqa: E501
            )
        if table_fmt:
            all_cols = ",\n".join(table_fmt)
            create_tbl = f"CREATE TABLE {table_name} (\n{all_cols}\n)"
        else:
            create_tbl = f"CREATE TABLE {table_name}"
        return create_tbl

    def format_tables(self, tables: list[Table]) -> str:
        return self.table_sep.join(self.format_table(table) for table in tables)
