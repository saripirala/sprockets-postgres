import asyncio
import contextlib
import logging
import operator
import os
import time
import typing
from distutils import util
from urllib import parse

import aiodns
import aiopg
import psycopg2
import pycares
from aiodns import error as aiodns_error
from aiopg import pool
from psycopg2 import errors, extras
from tornado import ioloop, web

LOGGER = logging.getLogger('sprockets-postgres')

DEFAULT_POSTGRES_CONNECTION_TIMEOUT = 10
DEFAULT_POSTGRES_CONNECTION_TTL = 300
DEFAULT_POSTGRES_HSTORE = 'FALSE'
DEFAULT_POSTGRES_JSON = 'FALSE'
DEFAULT_POSTGRES_MAX_POOL_SIZE = '10'
DEFAULT_POSTGRES_MIN_POOL_SIZE = '1'
DEFAULT_POSTGRES_QUERY_TIMEOUT = 120
DEFAULT_POSTGRES_UUID = 'TRUE'

QueryParameters = typing.Union[dict, list, tuple, None]
"""Type annotation for query parameters"""

Timeout = typing.Union[int, float, None]
"""Type annotation for timeout values"""


class QueryResult:
    """Contains the results of the query that was executed.

    :param row_count: The quantity of rows impacted by the query
    :param row: If a single row is returned, the data for that row
    :param rows: If more than one row is returned, this attribute is set as the
        list of rows, in order.

    """
    def __init__(self,
                 row_count: int,
                 row: typing.Optional[dict],
                 rows: typing.Optional[typing.List[dict]]):
        self._row_count = row_count
        self._row = row
        self._rows = rows

    def __repr__(self) -> str:
        return '<QueryResult row_count={}>'.format(self._row_count)

    def __iter__(self) -> typing.Iterator[dict]:
        """Iterate across all rows in the result"""
        for row in self.rows:
            yield row

    def __len__(self) -> int:
        """Returns the number of rows impacted by the query"""
        return self._row_count

    @property
    def row(self) -> typing.Optional[dict]:
        return self._row

    @property
    def row_count(self) -> int:
        """Return the number of rows for the result"""
        return self._row_count

    @property
    def rows(self) -> typing.List[dict]:
        """Return the result as a list of one or more rows"""
        if self.row_count == 1:
            return [self._row]
        return self._rows or []


class PostgresConnector:
    """Wraps a :class:`aiopg.Cursor` instance for creating explicit
    transactions, calling stored procedures, and executing queries.

    Unless the :meth:`~sprockets_postgres.PostgresConnector.transaction`
    asynchronous :ref:`context-manager <python:typecontextmanager>` is used,
    each call to :meth:`~sprockets_postgres.PostgresConnector.callproc` and
    :meth:`~sprockets_postgres.PostgresConnector.execute` is an explicit
    transaction.

    .. note:: :class:`PostgresConnector` instances are created by
        :meth:`ApplicationMixin.postgres_connector
        <sprockets_postgres.ApplicationMixin.postgres_connector>` and should
        not be created directly.

    :param cursor: The cursor to use in the connector
    :type cursor: aiopg.Cursor
    :param on_error: The callback to invoke when an exception is caught
    :param on_duration: The callback to invoke when a query is complete and all
        of the data has been returned.
    :param timeout: A timeout value in seconds for executing queries. If
        unspecified, defaults to the ``POSTGRES_QUERY_TIMEOUT`` environment
        variable and if that is not specified, to the
        :const:`DEFAULT_POSTGRES_QUERY_TIMEOUT` value of ``120``
    :type timeout: :data:`~sprockets_postgres.Timeout`

    """
    def __init__(self,
                 cursor: aiopg.Cursor,
                 on_error: typing.Callable,
                 on_duration: typing.Optional[typing.Callable] = None,
                 timeout: Timeout = None):
        self.cursor = cursor
        self._on_error = on_error
        self._on_duration = on_duration
        self._timeout = timeout or int(
            os.environ.get(
                'POSTGRES_QUERY_TIMEOUT',
                DEFAULT_POSTGRES_QUERY_TIMEOUT))

    async def callproc(self,
                       name: str,
                       parameters: QueryParameters = None,
                       metric_name: str = '',
                       *,
                       timeout: Timeout = None) -> QueryResult:
        """Execute a stored procedure / function

        :param name: The stored procedure / function name to call
        :param parameters: Query parameters to pass when calling
        :type parameters: :data:`~sprockets_postgres.QueryParameters`
        :param metric_name: The metric name for duration recording and logging
        :param timeout: Timeout value to override the default or the value
            specified when creating the
            :class:`~sprockets_postgres.PostgresConnector`.
        :type timeout: :data:`~sprockets_postgres.Timeout`

        :raises asyncio.TimeoutError: when there is a query or network timeout
        :raises psycopg2.Error: when there is an exception raised by Postgres

        .. note: :exc:`psycopg2.Error` is the base exception for all
            :mod:`psycopg2` exceptions and the actual exception raised will
            likely be more specific.

        :rtype: :class:`~sprockets_postgres.QueryResult`

        """
        return await self._query(
            self.cursor.callproc,
            metric_name,
            procname=name,
            parameters=parameters,
            timeout=timeout)

    async def execute(self,
                      sql: str,
                      parameters: QueryParameters = None,
                      metric_name: str = '',
                      *,
                      timeout: Timeout = None) -> QueryResult:
        """Execute a query, specifying a name for the query, the SQL statement,
        and optional positional arguments to pass in with the query.

        Parameters may be provided as sequence or mapping and will be
        bound to variables in the operation.  Variables are specified
        either with positional ``%s`` or named ``%({name})s`` placeholders.

        :param sql: The SQL statement to execute
        :param parameters: Query parameters to pass as part of the execution
        :type parameters: :data:`~sprockets_postgres.QueryParameters`
        :param metric_name: The metric name for duration recording and logging
        :param timeout: Timeout value to override the default or the value
            specified when creating the
            :class:`~sprockets_postgres.PostgresConnector`.
        :type timeout: :data:`~sprockets_postgres.Timeout`

        :raises asyncio.TimeoutError: when there is a query or network timeout
        :raises psycopg2.Error: when there is an exception raised by Postgres

        .. note: :exc:`psycopg2.Error` is the base exception for all
            :mod:`psycopg2` exceptions and the actual exception raised will
            likely be more specific.

        :rtype: :class:`~sprockets_postgres.QueryResult`

        """
        return await self._query(
            self.cursor.execute,
            metric_name,
            operation=sql,
            parameters=parameters,
            timeout=timeout)

    @contextlib.asynccontextmanager
    async def transaction(self) \
            -> typing.AsyncContextManager['PostgresConnector']:
        """asynchronous :ref:`context-manager <python:typecontextmanager>`
        function that implements full ``BEGIN``, ``COMMIT``, and ``ROLLBACK``
        semantics. If there is a :exc:`psycopg2.Error` raised during the
        transaction, the entire transaction will be rolled back.

        If no exception is raised, the transaction will be committed when
        exiting the context manager.

        .. note:: This method is provided for edge case usage. As a
            generalization
            :meth:`sprockets_postgres.RequestHandlerMixin.postgres_transaction`
            should be used instead.

        *Usage Example*

        .. code-block::

            class RequestHandler(sprockets_postgres.RequestHandlerMixin,
                                 web.RequestHandler):

                async def post(self):
                    async with self.postgres_transaction() as transaction:
                        result1 = await transaction.execute(QUERY_ONE)
                        result2 = await transaction.execute(QUERY_TWO)
                        result3 = await transaction.execute(QUERY_THREE)

        :raises asyncio.TimeoutError: when there is a query or network timeout
            when starting the transaction
        :raises psycopg2.Error: when there is an exception raised by Postgres
            when starting the transaction

        .. note: :exc:`psycopg2.Error` is the base exception for all
            :mod:`psycopg2` exceptions and the actual exception raised will
            likely be more specific.

        """
        async with self.cursor.begin():
            yield self

    async def _query(self,
                     method: typing.Callable,
                     metric_name: str,
                     **kwargs):
        if kwargs['timeout'] is None:
            kwargs['timeout'] = self._timeout
        start_time = time.monotonic()
        try:
            await method(**kwargs)
        except (asyncio.TimeoutError, psycopg2.Error) as err:
            exc = self._on_error(metric_name, err)
            if exc:
                raise exc
        else:
            results = await self._query_results()
            if self._on_duration:
                self._on_duration(
                    metric_name, time.monotonic() - start_time)
            return results

    async def _query_results(self) -> QueryResult:
        count, row, rows = self.cursor.rowcount, None, None
        if self.cursor.rowcount == 1:
            try:
                row = dict(await self.cursor.fetchone())
            except psycopg2.ProgrammingError:
                pass
        elif self.cursor.rowcount > 1:
            try:
                rows = [dict(row) for row in await self.cursor.fetchall()]
            except psycopg2.ProgrammingError:
                pass
        return QueryResult(count, row, rows)


class ApplicationMixin:
    """:class:`sprockets.http.app.Application` mixin for handling the
    connection to Postgres and exporting functions for querying the database,
    getting the status, and proving a cursor.

    Automatically creates and shuts down :class:`aiopg.Pool` on startup
    and shutdown by installing `on_start` and `shutdown` callbacks into the
    :class:`~sprockets.http.app.Application` instance.

    """
    POSTGRES_STATUS_TIMEOUT = 3

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._postgres_pool: typing.Optional[pool.Pool] = None
        self._postgres_connected: typing.Optional[asyncio.Event] = None
        self._postgres_reconnect: typing.Optional[asyncio.Lock] = None
        self._postgres_srv: bool = False
        self.runner_callbacks['on_start'].append(self._postgres_on_start)
        self.runner_callbacks['shutdown'].append(self._postgres_shutdown)

    @contextlib.asynccontextmanager
    async def postgres_connector(self,
                                 on_error: typing.Callable,
                                 on_duration: typing.Optional[
                                     typing.Callable] = None,
                                 timeout: Timeout = None,
                                 _attempt: int = 1) \
            -> typing.AsyncContextManager[PostgresConnector]:
        """Asynchronous :ref:`context-manager <python:typecontextmanager>`
        that returns a :class:`~sprockets_postgres.PostgresConnector` instance
        from the connection pool with a cursor.

        .. note:: This function is designed to work in conjunction with the
            :class:`~sprockets_postgres.RequestHandlerMixin` and is generally
            not invoked directly.

        :param on_error: A callback function that is invoked on exception. If
            an exception is returned from that function, it will raise it.
        :param on_duration: An optional callback function that is invoked after
            a query has completed to record the duration that encompasses
            both executing the query and retrieving the returned records, if
            any.
        :param timeout: Used to override the default query timeout.
        :type timeout: :data:`~sprockets_postgres.Timeout`

        :raises asyncio.TimeoutError: when the request to retrieve a connection
            from the pool times out.
        :raises psycopg2.OperationalError: when the application
            can not connect to the configured Postgres instance.
        :raises psycopg2.Error: when Postgres raises an exception during the
            creation of the cursor.

        .. note: :exc:`psycopg2.Error` is the base exception for all
            :mod:`psycopg2` exceptions and the actual exception raised will
            likely be more specific.

        """
        try:
            async with self._postgres_pool.acquire() as conn:
                async with conn.cursor(
                        cursor_factory=extras.RealDictCursor,
                        timeout=timeout) as cursor:
                    yield PostgresConnector(
                        cursor, on_error, on_duration, timeout)
        except (asyncio.TimeoutError, psycopg2.Error) as err:
            if isinstance(err, psycopg2.OperationalError) and _attempt == 1:
                LOGGER.critical('Disconnected from Postgres: %s', err)
                retry = True
                if not self._postgres_reconnect.locked():
                    async with self._postgres_reconnect:
                        retry = await self._postgres_connect()
                if retry:
                    await self._postgres_connected.wait()
                    async with self.postgres_connector(
                            on_error, on_duration, timeout,
                            _attempt + 1) as connector:
                        yield connector
                    return
            # Handle timeout error during cursor creation as
            # operational error
            elif isinstance(err, asyncio.TimeoutError):
                exc = on_error(
                    'postgres_connector', psycopg2.OperationalError(err))
            exc = on_error('postgres_connector', err)
            if exc:
                raise exc
            else:   # postgres_status.on_error does not return an exception
                yield None

    async def postgres_status(self) -> dict:
        """Invoke from the ``/status`` RequestHandler to check that there is
        a Postgres connection handler available and return info about the
        pool.

        The ``available`` item in the dictionary indicates that the
        application was able to perform a ``SELECT 1`` against the database
        using a :class:`~sprockets_postgres.PostgresConnector` instance.

        The ``pool_size`` item indicates the current quantity of open
        connections to Postgres.

        The ``pool_free`` item indicates the current number of idle
        connections available to process queries.

        *Example return value*

        .. code-block:: python

            {
                'available': True,
                'pool_size': 10,
                'pool_free': 8
            }

        """
        if not self._postgres_connected.is_set():
            return {
                'available': False,
                'pool_size': 0,
                'pool_free': 0
            }

        LOGGER.debug('Querying postgres status')
        query_error = asyncio.Event()

        def on_error(metric_name, exc) -> None:
            LOGGER.debug('Query Error for %r: %r', metric_name, exc)
            query_error.set()
            return None

        async with self.postgres_connector(
                on_error,
                timeout=self.POSTGRES_STATUS_TIMEOUT) as connector:
            if connector:
                await connector.execute('SELECT 1')

        return {
            'available': not query_error.is_set(),
            'pool_size': self._postgres_pool.size,
            'pool_free': self._postgres_pool.freesize
        }

    async def _postgres_connect(self) -> bool:
        """Setup the Postgres pool of connections"""
        self._postgres_connected.clear()

        parsed = parse.urlparse(os.environ['POSTGRES_URL'])
        if parsed.scheme.endswith('+srv'):
            self._postgres_srv = True
            try:
                url = await self._postgres_url_from_srv(parsed)
            except RuntimeError as error:
                LOGGER.critical(str(error))
                return False
        else:
            url = os.environ['POSTGRES_URL']

        if self._postgres_pool:
            self._postgres_pool.close()

        LOGGER.debug('Connecting to %s', url)
        try:
            self._postgres_pool = await pool.Pool.from_pool_fill(
                url,
                maxsize=int(
                    os.environ.get(
                        'POSTGRES_MAX_POOL_SIZE',
                        DEFAULT_POSTGRES_MAX_POOL_SIZE)),
                minsize=int(
                    os.environ.get(
                        'POSTGRES_MIN_POOL_SIZE',
                        DEFAULT_POSTGRES_MIN_POOL_SIZE)),
                timeout=int(
                    os.environ.get(
                        'POSTGRES_CONNECT_TIMEOUT',
                        DEFAULT_POSTGRES_CONNECTION_TIMEOUT)),
                enable_hstore=util.strtobool(
                    os.environ.get(
                        'POSTGRES_HSTORE', DEFAULT_POSTGRES_HSTORE)),
                enable_json=util.strtobool(
                    os.environ.get('POSTGRES_JSON', DEFAULT_POSTGRES_JSON)),
                enable_uuid=util.strtobool(
                    os.environ.get('POSTGRES_UUID', DEFAULT_POSTGRES_UUID)),
                echo=False,
                on_connect=None,
                pool_recycle=int(
                    os.environ.get(
                        'POSTGRES_CONNECTION_TTL',
                        DEFAULT_POSTGRES_CONNECTION_TTL)))
        except (psycopg2.OperationalError,
                psycopg2.Error) as error:  # pragma: nocover
            LOGGER.warning('Error connecting to PostgreSQL on startup: %s',
                           error)
            return False
        self._postgres_connected.set()
        LOGGER.debug('Connected to Postgres')
        return True

    async def _postgres_on_start(self,
                                 _app: web.Application,
                                 loop: ioloop.IOLoop):
        """Invoked as a startup step for the application

        This is invoked by the :class:`sprockets.http.app.Application` on start
        callback mechanism.

        """
        if 'POSTGRES_URL' not in os.environ:
            LOGGER.critical('Missing POSTGRES_URL environment variable')
            return self.stop(loop)

        self._postgres_connected = asyncio.Event()
        self._postgres_reconnect = asyncio.Lock()

        if not await self._postgres_connect():
            LOGGER.critical('PostgreSQL failed to connect, shutting down')
            return self.stop(loop)

    async def _postgres_shutdown(self, _ioloop: ioloop.IOLoop) -> None:
        """Shutdown the Postgres connections and wait for them to close.

        This is invoked by the :class:`sprockets.http.app.Application` shutdown
        callback mechanism.

        """
        if self._postgres_pool is not None:
            self._postgres_pool.close()
            await self._postgres_pool.wait_closed()

    async def _postgres_url_from_srv(self, parsed: parse.ParseResult) -> str:
        if parsed.scheme.startswith('postgresql+'):
            host_parts = parsed.hostname.split('.')
            records = await self._resolve_srv(
                '_{}._{}.{}'.format(
                    host_parts[0], 'postgresql', '.'.join(host_parts[1:])))
        elif parsed.scheme.startswith('aws+'):
            records = await self._resolve_srv(parsed.hostname)
        else:
            raise RuntimeError('Unsupported URI Scheme: {}'.format(
                parsed.scheme))

        if not records:
            raise RuntimeError('No SRV records found')

        netloc = []
        if parsed.username and not parsed.password:
            netloc.append('{}@'.format(parsed.username))
        elif parsed.username and parsed.password:
            netloc.append('{}:{}@'.format(parsed.username, parsed.password))
        netloc.append(','.join([
            '{}:{}'.format(r.host, r.port) for r in records]))
        return parse.urlunparse(
            ('postgresql', ''.join(netloc), parsed.path,
             parsed.params, parsed.query, ''))

    @staticmethod
    async def _resolve_srv(hostname: str) \
            -> typing.List[pycares.ares_query_srv_result]:
        resolver = aiodns.DNSResolver(loop=asyncio.get_event_loop())
        try:
            records = await resolver.query(hostname, 'SRV')
        except aiodns_error.DNSError as error:
            LOGGER.critical('DNS resolution error: %s', error)
            raise RuntimeError(str(error))
        s = sorted(records, key=operator.attrgetter('weight'), reverse=True)
        return sorted(s, key=operator.attrgetter('priority'))


class RequestHandlerMixin:
    """
    A RequestHandler mixin class exposing functions for querying the database,
    recording the duration to either :mod:`sprockets-influxdb
    <sprockets_influxdb>` or :mod:`sprockets.mixins.metrics`, and
    handling exceptions.

    """
    async def postgres_callproc(self,
                                name: str,
                                parameters: QueryParameters = None,
                                metric_name: str = '',
                                *,
                                timeout: Timeout = None) -> QueryResult:
        """Execute a stored procedure / function

        :param name: The stored procedure / function name to call
        :param parameters: Query parameters to pass when calling
        :type parameters: :data:`~sprockets_postgres.QueryParameters`
        :param metric_name: The metric name for duration recording and logging
        :param timeout: Timeout value to override the default or the value
            specified when creating the
            :class:`~sprockets_postgres.PostgresConnector`.
        :type timeout: :data:`~sprockets_postgres.Timeout`

        :raises asyncio.TimeoutError: when there is a query or network timeout
        :raises psycopg2.Error: when there is an exception raised by Postgres

        .. note: :exc:`psycopg2.Error` is the base exception for all
            :mod:`psycopg2` exceptions and the actual exception raised will
            likely be more specific.

        :rtype: :class:`~sprockets_postgres.QueryResult`

        """
        async with self.application.postgres_connector(
                self._on_postgres_error,
                self._on_postgres_timing,
                timeout) as connector:
            return await connector.callproc(
                name, parameters, metric_name, timeout=timeout)

    async def postgres_execute(self,
                               sql: str,
                               parameters: QueryParameters = None,
                               metric_name: str = '',
                               *,
                               timeout: Timeout = None) -> QueryResult:
        """Execute a query, specifying a name for the query, the SQL statement,
        and optional positional arguments to pass in with the query.

        Parameters may be provided as sequence or mapping and will be
        bound to variables in the operation.  Variables are specified
        either with positional ``%s`` or named ``%({name})s`` placeholders.

        :param sql: The SQL statement to execute
        :param parameters: Query parameters to pass as part of the execution
        :type parameters: :data:`~sprockets_postgres.QueryParameters`
        :param metric_name: The metric name for duration recording and logging
        :param timeout: Timeout value to override the default or the value
            specified when creating the
            :class:`~sprockets_postgres.PostgresConnector`.
        :type timeout: :data:`~sprockets_postgres.Timeout`

        :raises asyncio.TimeoutError: when there is a query or network timeout
        :raises psycopg2.Error: when there is an exception raised by Postgres

        .. note: :exc:`psycopg2.Error` is the base exception for all
            :mod:`psycopg2` exceptions and the actual exception raised will
            likely be more specific.

        :rtype: :class:`~sprockets_postgres.QueryResult`

        """
        async with self.application.postgres_connector(
                self._on_postgres_error,
                self._on_postgres_timing,
                timeout) as connector:
            return await connector.execute(
                sql, parameters, metric_name, timeout=timeout)

    @contextlib.asynccontextmanager
    async def postgres_transaction(self, timeout: Timeout = None) \
            -> typing.AsyncContextManager[PostgresConnector]:
        """asynchronous :ref:`context-manager <python:typecontextmanager>`
        function that implements full ``BEGIN``, ``COMMIT``, and ``ROLLBACK``
        semantics. If there is a :exc:`psycopg2.Error` raised during the
        transaction, the entire transaction will be rolled back.

        If no exception is raised, the transaction will be committed when
        exiting the context manager.

        *Usage Example*

        .. code-block:: python

           class RequestHandler(sprockets_postgres.RequestHandlerMixin,
                                web.RequestHandler):

           async def post(self):
               async with self.postgres_transaction() as transaction:
                   result1 = await transaction.execute(QUERY_ONE)
                   result2 = await transaction.execute(QUERY_TWO)
                   result3 = await transaction.execute(QUERY_THREE)


        :param timeout: Timeout value to override the default or the value
            specified when creating the
            :class:`~sprockets_postgres.PostgresConnector`.
        :type timeout: :data:`~sprockets_postgres.Timeout`

        :raises asyncio.TimeoutError: when there is a query or network timeout
            when starting the transaction
        :raises psycopg2.Error: when there is an exception raised by Postgres
            when starting the transaction

        .. note: :exc:`psycopg2.Error` is the base exception for all
            :mod:`psycopg2` exceptions and the actual exception raised will
            likely be more specific.

        """
        async with self.application.postgres_connector(
                self._on_postgres_error,
                self._on_postgres_timing,
                timeout) as connector:
            async with connector.transaction():
                yield connector

    def _on_postgres_error(self,
                           metric_name: str,
                           exc: Exception) -> typing.Optional[Exception]:
        """Override for different error handling behaviors

        Return an exception if you would like for it to be raised, or swallow
        it here.

        """
        LOGGER.error('%s in %s for %s (%s)',
                     exc.__class__.__name__, self.__class__.__name__,
                     metric_name, str(exc).split('\n')[0])
        if isinstance(exc, psycopg2.OperationalError):
            raise web.HTTPError(503, reason='Database Connection Error')
        elif isinstance(exc, asyncio.TimeoutError):
            raise web.HTTPError(500, reason='Query Timeout')
        elif isinstance(exc, errors.UniqueViolation):
            raise web.HTTPError(409, reason='Unique Violation')
        elif isinstance(exc, psycopg2.Error):
            raise web.HTTPError(500, reason='Database Error')
        return exc

    def _on_postgres_timing(self,
                            metric_name: str,
                            duration: float) -> None:
        """Override for custom metric recording. As a default behavior it will
        attempt to detect `sprockets-influxdb
        <https://sprockets-influxdb.readthedocs.io/>`_ and
        `sprockets.mixins.metrics
        <https://sprocketsmixinsmetrics.readthedocs.io/en/latest/>`_ and
        record the metrics using them if they are available. If they are not
        available, it will record the query duration to the `DEBUG` log.

        :param metric_name: The name of the metric to record
        :param duration: The duration to record for the metric

        """
        if hasattr(self, 'influxdb'):  # sprockets-influxdb
            self.influxdb.set_field(metric_name, duration)
        elif hasattr(self, 'record_timing'):  # sprockets.mixins.metrics
            self.record_timing(duration, metric_name)
        else:
            LOGGER.debug('Postgres query %s duration: %s',
                         metric_name, duration)
