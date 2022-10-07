from duckdb import DuckDBPyConnection as Connection
from collections import defaultdict
import datetime
from pypika import Query, Table
import pyarrow.parquet as pq

from ..storages.rel_impls.utils import Transactable, transaction
from ..storages.kv import InMemoryStorage
from ..storages.rel_impls.duckdb_impl import DuckDBRelStorage
from ..storages.rels import RelAdapter, RemoteEventLogEntry, deserialize, SigAdapter
from ..storages.sigs import SigSyncer
from ..storages.remote_storage import RemoteStorage
from ..common_imports import *
from ..core.config import Config
from ..core.model import Call

from ..core.model import unwrap
from ..queries.weaver import ValQuery
from ..queries.compiler import traverse_all, Compiler


def execute_query(storage: "Storage", select_queries: List[ValQuery]) -> pd.DataFrame:
    """
    Execute the given queries and return the result as a pandas DataFrame.
    """
    if not select_queries:
        return pd.DataFrame()
    val_queries, func_queries = traverse_all(select_queries)
    compiler = Compiler(val_queries=val_queries, func_queries=func_queries)
    query = compiler.compile(select_queries=select_queries)
    df = storage.rel_storage.execute_df(query=str(query))

    # now, evaluate the table
    keys_to_collect = [item for _, column in df.items() for _, item in column.items()]
    storage.preload_objs(keys_to_collect)
    result = df.applymap(lambda key: unwrap(storage.obj_get(key)))

    # finally, name the columns
    cols = [
        f"unnamed_{i}" if query.column_name is None else query.column_name
        for i, query in zip(range(len((result.columns))), select_queries)
    ]
    result.rename(columns=dict(zip(result.columns, cols)), inplace=True)
    return result


class MODES:
    run = "run"
    query = "query"


class GlobalContext:
    current: Optional["Context"] = None


class Context:
    OVERRIDES = {}

    def __init__(
        self, storage: "Storage" = None, mode: str = MODES.run, lazy: bool = False
    ):
        self.storage = storage
        self.mode = self.OVERRIDES.get("mode", mode)
        self.lazy = self.OVERRIDES.get("lazy", lazy)
        self.updates = {}
        self._updates_stack = []

    def _backup_state(self, keys: Iterable[str]) -> Dict[str, Any]:
        res = {}
        for k in keys:
            cur_v = self.__dict__[f"{k}"]
            if k == "storage":  # gotta use a pointer
                res[k] = cur_v
            else:
                res[k] = copy.deepcopy(cur_v)
        return res

    def __enter__(self) -> "Context":
        if GlobalContext.current is None:
            GlobalContext.current = self
        ### verify update keys
        updates = self.updates
        if not all(k in ("storage", "mode", "lazy") for k in updates.keys()):
            raise ValueError(updates.keys())
        ### backup state
        before_update = self._backup_state(keys=updates.keys())
        self._updates_stack.append(before_update)
        ### apply updates
        for k, v in updates.items():
            if v is not None:
                self.__dict__[f"{k}"] = v
        # Load state from remote
        if self.storage is not None:
            # self.storage.sync_with_remote()
            self.storage.sync_from_remote()
        return self

    def _undo_updates(self):
        """
        Roll back the updates from the current level
        """
        if not self._updates_stack:
            raise RuntimeError("No context to exit from")
        ascent_updates = self._updates_stack.pop()
        for k, v in ascent_updates.items():
            self.__dict__[f"{k}"] = v
        # unlink from global if done
        if len(self._updates_stack) == 0:
            GlobalContext.current = None

    def __exit__(self, exc_type, exc_value, exc_traceback):
        exc = None
        try:
            # commit calls from temp partition to main and tabulate them
            if Config.autocommit:
                self.storage.commit()
            self.storage.sync_to_remote()
        except Exception as e:
            exc = e
        self._undo_updates()
        if exc is not None:
            raise exc
        if exc_type:
            raise exc_type(exc_value).with_traceback(exc_traceback)
        return None

    def __call__(self, storage: Optional["Storage"] = None, **updates):
        self.updates = {"storage": storage, **updates}
        return self

    def get_table(self, *queries: ValQuery) -> pd.DataFrame:
        # ! EXTREMELY IMPORTANT
        # We must sync any dirty cache elements to the DuckDB store before performing a query.
        # If we don't, we'll query a store that might be missing calls and objs.
        self.storage.commit()
        return execute_query(storage=self.storage, select_queries=list(queries))


class RunContext(Context):
    OVERRIDES = {"mode": MODES.run, "lazy": False}


class QueryContext(Context):
    OVERRIDES = {
        "mode": MODES.query,
    }


run = RunContext()
query = QueryContext()


class Storage(Transactable):
    """
    Groups together all the components of the storage system.

    Responsible for things that require multiple components to work together,
    e.g.
        - committing: moving calls from the "temporary" partition to the "main"
        partition. See also `CallStorage`.
        - synchronizing: connecting an operation with the storage and performing
        any necessary updates
    """

    def __init__(
        self,
        root: Optional[Union[Path, RemoteStorage]] = None,
        timestamp: Optional[datetime.datetime] = None,
    ):
        self.root = root
        self.call_cache = InMemoryStorage()
        self.obj_cache = InMemoryStorage()
        # all objects (inputs and outputs to operations, defaults) are saved here
        # stores the memoization tables
        self.rel_storage = DuckDBRelStorage()
        # manipulates the memoization tables
        self.rel_adapter = RelAdapter(rel_storage=self.rel_storage)
        self.sig_adapter = self.rel_adapter.sig_adapter
        self.sig_syncer = SigSyncer(sig_adapter=self.sig_adapter, root=self.root)
        # stores the signatures of the operations connected to this storage
        # (name, version) -> signature
        # self.sigs: Dict[Tuple[str, int], Signature] = {}

        # self.remote_sync_manager = None
        # # manage remote storage
        # if isinstance(root, RemoteStorage):
        #     self.remote_sync_manager = RemoteSyncManager(
        #         local_storage=self, remote_storage=root
        #     )
        self.last_timestamp = (
            timestamp if timestamp is not None else datetime.datetime.fromtimestamp(0)
        )

    ############################################################################
    ### `Transactable` interface
    ############################################################################
    def _get_connection(self) -> Connection:
        return self.rel_storage._get_connection()

    def _end_transaction(self, conn: Connection):
        return self.rel_storage._end_transaction(conn=conn)

    ############################################################################
    def call_exists(self, call_uid: str) -> bool:
        return self.call_cache.exists(call_uid) or self.rel_adapter.call_exists(
            call_uid
        )

    def call_get(self, call_uid: str) -> Call:
        if self.call_cache.exists(call_uid):
            return self.call_cache.get(call_uid)
        else:
            return self.rel_adapter.call_get(call_uid)

    def call_set(self, call_uid: str, call: Call) -> None:
        self.call_cache.set(call_uid, call)

    def obj_get(self, obj_uid: str) -> Any:
        if self.obj_cache.exists(obj_uid):
            return self.obj_cache.get(obj_uid)
        return self.rel_adapter.obj_get(uid=obj_uid)

    def obj_set(self, obj_uid: str, value: Any) -> None:
        self.obj_cache.set(obj_uid, value)

    def preload_objs(self, keys: list[str]):
        keys_not_in_cache = [key for key in keys if not self.obj_cache.exists(key)]
        for idx, row in self.rel_adapter.obj_gets(keys_not_in_cache).iterrows():
            self.obj_cache.set(k=row[Config.uid_col], v=row["value"])

    def evict_caches(self):
        for k in self.call_cache.keys():
            self.call_cache.delete(k=k)
        for k in self.obj_cache.keys():
            self.obj_cache.delete(k=k)

    def commit(self):
        """
        Flush calls and objs from the cache that haven't yet been written to DuckDB.
        """
        new_objs = {
            key: self.obj_cache.get(key) for key in self.obj_cache.dirty_entries
        }
        new_calls = [self.call_cache.get(key) for key in self.call_cache.dirty_entries]
        self.rel_adapter.obj_sets(new_objs)
        self.rel_adapter.upsert_calls(new_calls)
        if Config.evict_on_commit:
            self.evict_caches()

        # Remove dirty bits from cache.
        self.obj_cache.dirty_entries.clear()
        self.call_cache.dirty_entries.clear()

    ############################################################################
    ### remote sync operations
    ############################################################################
    @transaction()
    def bundle_to_remote(
        self, conn: Optional[Connection] = None
    ) -> RemoteEventLogEntry:
        """
        Collect the new calls according to the event log, and pack them into a
        dict of binary blobs to be sent off to the remote server.

        NOTE: this also renames tables and columns to their immutable internal
        names.
        """
        # Bundle event log and referenced calls into tables.
        # event_log_df = self.rel_storage.get_data(
        #     self.rel_adapter.EVENT_LOG_TABLE, conn=conn
        # )
        event_log_df = self.rel_adapter.get_event_log(conn=conn)
        tables_with_changes = {}
        table_names_with_changes = event_log_df["table"].unique()

        event_log_table = Table(self.rel_adapter.EVENT_LOG_TABLE)
        for table_name in table_names_with_changes:
            table = Table(table_name)
            tables_with_changes[table_name] = self.rel_storage.execute_arrow(
                query=Query.from_(table)
                .join(event_log_table)
                .on(table[Config.uid_col] == event_log_table[Config.uid_col])
                .select(table.star),
                conn=conn,
            )
        # pass to internal names
        # evaluated_tables = {
        #     k: self.rel_adapter.evaluate_call_table(ta=v)
        #     for k, v in tables_with_changes.items()
        #     if k != Config.vref_table
        # }
        # logger.debug(f"Sending tables with changes: {evaluated_tables}")
        tables_with_changes = self.sig_adapter.rename_tables(
            tables_with_changes, to="internal"
        )
        output = {}
        for table_name, table in tables_with_changes.items():
            buffer = io.BytesIO()
            pq.write_table(table, buffer)
            output[table_name] = buffer.getvalue()
        self.rel_adapter.clear_event_log(conn=conn)
        return output

    @transaction()
    def apply_from_remote(
        self, changes: list[RemoteEventLogEntry], conn: Optional[Connection] = None
    ):
        """
        Apply new calls from the remote server.

        NOTE: this also renames tables and columns to their UI names.
        """
        for raw_changeset in changes:
            changeset_data = {}
            for table_name, serialized_table in raw_changeset.items():
                buffer = io.BytesIO(serialized_table)
                deserialized_table = pq.read_table(buffer)
                changeset_data[table_name] = deserialized_table
            # pass to UI names
            changeset_data = self.sig_adapter.rename_tables(
                tables=changeset_data, to="ui"
            )
            for table_name, deserialized_table in changeset_data.items():
                self.rel_storage.upsert(table_name, deserialized_table, conn=conn)
        # evaluated_tables = {k: self.rel_adapter.evaluate_call_table(ta=v, conn=conn) for k, v in data.items() if k != Config.vref_table}
        # logger.debug(f'Applied tables from remote: {evaluated_tables}')

    def sync_from_remote(self):
        """
        Pull new calls from the remote server.

        Note that the server's schema (i.e. signatures) can be a super-schema of
        the local schema, but all local schema elements must be present in the
        remote schema, because this is enforced by how schema updates are
        performed.
        """
        if not isinstance(self.root, RemoteStorage):
            return
        # apply signature changes from the server first, because the new calls
        # from the server may depend on the new schema.
        self.sig_syncer.sync_from_remote()
        # next, pull new calls
        new_log_entries, timestamp = self.root.get_log_entries_since(
            self.last_timestamp
        )
        self.apply_from_remote(new_log_entries)
        self.last_timestamp = timestamp
        logger.debug("synced from remote")

    def sync_to_remote(self):
        """
        Send calls to the remote server.

        As with `sync_from_remote`, the server may have a super-schema of the
        local schema. The current signatures are first pulled and applied to the
        local schema.
        """
        if not isinstance(self.root, RemoteStorage):
            # todo: there should be a way to completely ignore the event log
            # when there's no remote
            self.rel_adapter.clear_event_log()
        else:
            # collect new work and send it to the server
            changes = self.bundle_to_remote()
            self.root.save_event_log_entry(changes)
            # apply signature changes from the server
            # self.sig_adapter.sync_from_remote()
            logger.debug("synced to remote")

    def sync_with_remote(self):
        if not isinstance(self.root, RemoteStorage):
            return
        self.sync_to_remote()
        self.sync_from_remote()

    ############################################################################
    ### signature sync, renaming, refactoring
    ############################################################################
    @property
    def is_clean(self) -> bool:
        """
        Check that the storage has no uncommitted calls or objects.
        """
        return (
            self.call_cache.is_clean and self.obj_cache.is_clean
        )  # and self.rel_adapter.event_log_is_clean()

    ############################################################################
    ### creating contexts
    ############################################################################
    def run(self, **kwargs) -> Context:
        return run(storage=self, **kwargs)

    def query(self, **kwargs) -> Context:
        return query(storage=self, **kwargs)