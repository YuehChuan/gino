import asyncio
import inspect
import itertools
import time
import warnings

import aiomysql
from sqlalchemy import util, exc, sql
from sqlalchemy.dialects.mysql import (JSON, json)
# from sqlalchemy.dialects.postgresql import (  # noqa: F401
#     ARRAY,
#     CreateEnumType,
#     DropEnumType,
#     JSON,
#     JSONB,
#     json,
# )
from sqlalchemy.dialects.mysql.base import (
    MySQLCompiler,
    MySQLDialect,
    MySQLExecutionContext,
)
from sqlalchemy.sql import sqltypes

from . import base

class AiomysqlDBAPI(base.BaseDBAPI):
    pass
    # Error = asyncpg.PostgresError, asyncpg.InterfaceError


# class AiomysqlCompiler(PGCompiler):
#     @property
#     def bindtemplate(self):
#         return self._bindtemplate
#
#     @bindtemplate.setter
#     def bindtemplate(self, val):
#         # noinspection PyAttributeOutsideInit
#         self._bindtemplate = val.replace(":", "$")
#
#     def _apply_numbered_params(self):
#         if hasattr(self, "string"):
#             return super()._apply_numbered_params()


# noinspection PyAbstractClass
class AiomysqlExecutionContext(base.ExecutionContextOverride,
                               MySQLExecutionContext):
    async def _execute_scalar(self, stmt, type_):
        conn = self.root_connection
        if (
            isinstance(stmt, util.text_type)
            and not self.dialect.supports_unicode_statements
        ):
            stmt = self.dialect._encoder(stmt)[0]

        if self.dialect.positional:
            default_params = self.dialect.execute_sequence_format()
        else:
            default_params = {}

        conn._cursor_execute(self.cursor, stmt, default_params, context=self)
        r = await self.cursor.async_execute(stmt, None, default_params, 1)
        r = r[0][0]
        if type_ is not None:
            # apply type post processors to the result
            proc = type_._cached_result_processor(
                self.dialect, self.cursor.description[0][1]
            )
            if proc:
                return proc(r)
        return r


class AiomysqlIterator:
    def __init__(self, context, iterator):
        self._context = context
        self._iterator = iterator

    async def __anext__(self):
        row = await self._iterator.__anext__()
        return self._context.process_rows([row])[0]


class AiomysqlCursor(base.Cursor):
    def __init__(self, context, cursor):
        self._context = context
        self._cursor = cursor

    async def many(self, n, *, timeout=base.DEFAULT):
        if timeout is base.DEFAULT:
            timeout = self._context.timeout
        rows = await self._cursor.fetch(n, timeout=timeout)
        return self._context.process_rows(rows)

    async def next(self, *, timeout=base.DEFAULT):
        if timeout is base.DEFAULT:
            timeout = self._context.timeout
        row = await self._cursor.fetchrow(timeout=timeout)
        if not row:
            return None
        return self._context.process_rows([row])[0]

    async def forward(self, n, *, timeout=base.DEFAULT):
        if timeout is base.DEFAULT:
            timeout = self._context.timeout
        await self._cursor.forward(n, timeout=timeout)


class PreparedStatement(base.PreparedStatement):
    def __init__(self, prepared, clause=None):
        super().__init__(clause)
        self._prepared = prepared

    def _get_iterator(self, *params, **kwargs):
        return AiomysqlIterator(
            self.context, self._prepared.cursor(*params, **kwargs).__aiter__()
        )

    async def _get_cursor(self, *params, **kwargs):
        iterator = await self._prepared.cursor(*params, **kwargs)
        return AiomysqlCursor(self.context, iterator)

    async def _execute(self, params, one):
        if one:
            rv = await self._prepared.fetchrow(*params)
            if rv is None:
                rv = []
            else:
                rv = [rv]
        else:
            rv = await self._prepared.fetch(*params)
        return self._prepared.get_statusmsg(), rv


class DBAPICursor(base.DBAPICursor):
    def __init__(self, dbapi_conn):
        self._conn = dbapi_conn
        self._cursor_description = None
        self._status = None

    async def prepare(self, context, clause=None):
        timeout = context.timeout
        if timeout is None:
            conn = await self._conn.acquire(timeout=timeout)
        else:
            before = time.monotonic()
            conn = await self._conn.acquire(timeout=timeout)
            after = time.monotonic()
            timeout -= after - before
        prepared = await conn.prepare(context.statement, timeout=timeout)
        try:
            self._cursor_description = prepared.get_attributes()
        except TypeError:  # asyncpg <= 0.12.0
            self._cursor_description = []
        rv = PreparedStatement(prepared, clause)
        rv.context = context
        return rv

    async def async_execute(self, query, timeout, args, limit=0, many=False):
        if timeout is None:
            conn = await self._conn.acquire(timeout=timeout)
        else:
            before = time.monotonic()
            conn = await self._conn.acquire(timeout=timeout)
            after = time.monotonic()
            timeout -= after - before

        if args is not None:
            query = query % self._escape_args(args, conn)
        await conn.query(query)
        # noinspection PyProtectedMember
        result = conn._result
        self._cursor_description = result.description
        self._status = result.affected_rows
        return result.rows

    def _escape_args(self, args, conn):
        if isinstance(args, (tuple, list)):
            return tuple(conn.escape(arg) for arg in args)
        elif isinstance(args, dict):
            return dict((key, conn.escape(val)) for (key, val) in args.items())
        else:
            # If it's not a dictionary let's try escaping it anyways.
            # Worst case it will throw a Value error
            return conn.escape(args)

    @property
    def description(self):
        return self._cursor_description

    def get_statusmsg(self):
        return self._status


class Pool(base.Pool):
    def __init__(self, url, loop, **kwargs):
        self._url = url
        self._loop = loop
        self._kwargs = kwargs
        self._pool = None

    async def _init(self):
        args = self._kwargs.copy()
        args.update(
            loop=self._loop,
            host=self._url.host,
            port=self._url.port,
            user=self._url.username,
            db=self._url.database,
            password=self._url.password,
        )
        self._pool = await aiomysql.create_pool(**args)
        return self

    def __await__(self):
        return self._init().__await__()

    @property
    def raw_pool(self):
        return self._pool

    async def acquire(self, *, timeout=None):
        # TODO: add timeout
        return await self._pool.acquire()

    async def release(self, conn):
        await self._pool.release(conn)

    async def close(self):
        self._pool.close()
        await self._pool.wait_closed()

    # TODO: add repr()


class Transaction(base.Transaction):
    def __init__(self, tx):
        self._tx = tx

    @property
    def raw_transaction(self):
        return self._tx

    async def begin(self):
        await self._tx.start()

    async def commit(self):
        await self._tx.commit()

    async def rollback(self):
        await self._tx.rollback()


class AiomysqlJSONPathType(json.JSONPathType):
    def bind_processor(self, dialect):
        super_proc = self.string_bind_processor(dialect)

        def process(value):
            assert isinstance(value, util.collections_abc.Sequence)
            if super_proc:
                return [super_proc(util.text_type(elem)) for elem in value]
            else:
                return [util.text_type(elem) for elem in value]

        return process


# noinspection PyAbstractClass
class AiomysqlDialect(MySQLDialect, base.AsyncDialectMixin):
    driver = "aiomysql"
    supports_native_decimal = True
    dbapi_class = AiomysqlDBAPI
    statement_compiler = MySQLCompiler
    execution_ctx_cls = AiomysqlExecutionContext
    cursor_cls = DBAPICursor
    init_kwargs = set(
        itertools.chain(
            *[
                inspect.getfullargspec(f).args
                for f in [aiomysql.create_pool, aiomysql.connect]
            ]
        )
    ) - {'echo'}  # use SQLAlchemy's echo instead
    # colspecs = util.update_copy(
    #     PGDialect.colspecs,
    #     {
    #         ENUM: AsyncEnum,
    #         sqltypes.Enum: AsyncEnum,
    #         sqltypes.NullType: GinoNullType,
    #         sqltypes.JSON.JSONPathType: AsyncpgJSONPathType,
    #     },
    # )

    def __init__(self, *args, **kwargs):
        self._pool_kwargs = {}
        for k in self.init_kwargs:
            if k in kwargs:
                self._pool_kwargs[k] = kwargs.pop(k)
        super().__init__(*args, **kwargs)
        self._init_mixin()

    async def init_pool(self, url, loop, pool_class=None):
        if pool_class is None:
            pool_class = Pool
        return await pool_class(url, loop, **self._pool_kwargs)

    # noinspection PyMethodMayBeStatic
    def transaction(self, raw_conn, args, kwargs):
        return Transaction(raw_conn.transaction(*args, **kwargs))

    # def on_connect(self):
    #     if self.isolation_level is not None:
    #
    #         async def connect(conn):
    #             await self.set_isolation_level(conn, self.isolation_level)
    #
    #         return connect
    #     else:
    #         return None

    async def set_isolation_level(self, connection, level):
        """
        Given an asyncpg connection, set its isolation level.

        """
        level = level.replace("_", " ")
        if level not in self._isolation_lookup:
            raise exc.ArgumentError(
                "Invalid value '%s' for isolation_level. "
                "Valid isolation levels for %s are %s"
                % (level, self.name, ", ".join(self._isolation_lookup))
            )
        await connection.execute(
            "SET SESSION CHARACTERISTICS AS TRANSACTION " "ISOLATION LEVEL %s" % level
        )
        await connection.execute("COMMIT")

    async def get_isolation_level(self, connection):
        """
        Given an asyncpg connection, return its isolation level.

        """
        val = await connection.fetchval("show transaction isolation level")
        return val.upper()

    # async def has_schema(self, connection, schema):
    #     row = await connection.first(
    #         sql.text(
    #             "select nspname from pg_namespace " "where lower(nspname)=:schema"
    #         ).bindparams(
    #             sql.bindparam(
    #                 "schema", util.text_type(schema.lower()), type_=sqltypes.Unicode,
    #             )
    #         )
    #     )
    #
    #     return bool(row)

    # async def has_table(self, connection, table_name, schema=None):
    #     # seems like case gets folded in pg_class...
    #     if schema is None:
    #         row = await connection.first(
    #             sql.text(
    #                 "select relname from pg_class c join pg_namespace n on "
    #                 "n.oid=c.relnamespace where "
    #                 "pg_catalog.pg_table_is_visible(c.oid) "
    #                 "and relname=:name"
    #             ).bindparams(
    #                 sql.bindparam(
    #                     "name", util.text_type(table_name), type_=sqltypes.Unicode
    #                 ),
    #             )
    #         )
    #     else:
    #         row = await connection.first(
    #             sql.text(
    #                 "select relname from pg_class c join pg_namespace n on "
    #                 "n.oid=c.relnamespace where n.nspname=:schema and "
    #                 "relname=:name"
    #             ).bindparams(
    #                 sql.bindparam(
    #                     "name", util.text_type(table_name), type_=sqltypes.Unicode,
    #                 ),
    #                 sql.bindparam(
    #                     "schema", util.text_type(schema), type_=sqltypes.Unicode,
    #                 ),
    #             )
    #         )
    #     return bool(row)
    #
    # async def has_sequence(self, connection, sequence_name, schema=None):
    #     if schema is None:
    #         row = await connection.first(
    #             sql.text(
    #                 "SELECT relname FROM pg_class c join pg_namespace n on "
    #                 "n.oid=c.relnamespace where relkind='S' and "
    #                 "n.nspname=current_schema() "
    #                 "and relname=:name"
    #             ).bindparams(
    #                 sql.bindparam(
    #                     "name", util.text_type(sequence_name), type_=sqltypes.Unicode,
    #                 )
    #             )
    #         )
    #     else:
    #         row = await connection.first(
    #             sql.text(
    #                 "SELECT relname FROM pg_class c join pg_namespace n on "
    #                 "n.oid=c.relnamespace where relkind='S' and "
    #                 "n.nspname=:schema and relname=:name"
    #             ).bindparams(
    #                 sql.bindparam(
    #                     "name", util.text_type(sequence_name), type_=sqltypes.Unicode,
    #                 ),
    #                 sql.bindparam(
    #                     "schema", util.text_type(schema), type_=sqltypes.Unicode,
    #                 ),
    #             )
    #         )
    #
    #     return bool(row)
    #
    # async def has_type(self, connection, type_name, schema=None):
    #     if schema is not None:
    #         query = """
    #         SELECT EXISTS (
    #             SELECT * FROM pg_catalog.pg_type t, pg_catalog.pg_namespace n
    #             WHERE t.typnamespace = n.oid
    #             AND t.typname = :typname
    #             AND n.nspname = :nspname
    #             )
    #             """
    #         query = sql.text(query)
    #     else:
    #         query = """
    #         SELECT EXISTS (
    #             SELECT * FROM pg_catalog.pg_type t
    #             WHERE t.typname = :typname
    #             AND pg_type_is_visible(t.oid)
    #             )
    #             """
    #         query = sql.text(query)
    #     query = query.bindparams(
    #         sql.bindparam(
    #             "typname", util.text_type(type_name), type_=sqltypes.Unicode,
    #         ),
    #     )
    #     if schema is not None:
    #         query = query.bindparams(
    #             sql.bindparam(
    #                 "nspname", util.text_type(schema), type_=sqltypes.Unicode,
    #             ),
    #         )
    #     return bool(await connection.scalar(query))
