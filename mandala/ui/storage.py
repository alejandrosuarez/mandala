import datetime
from typing import Literal

from .viz import _get_colorized_diff
from .utils import MODES, debug_call, get_terminal_data, check_determinism
from .remote_utils import RemoteManager
from . import contexts

from ..common_imports import *
from ..core.config import Config
from ..core.model import Ref, Call, FuncOp, ValueRef, Delayed
from ..core.builtins_ import Builtins, ListRef, DictRef, SetRef
from ..core.wrapping import (
    wrap_inputs,
    wrap_outputs,
    causify_outputs,
    decausify,
    unwrap,
    contains_transient,
    contains_not_in_memory,
    compare_dfs_as_relations,
)
from ..core.tps import Type, AnyType, ListType, DictType, SetType
from ..core.sig import Signature
from ..core.utils import get_uid, OpKey

from ..storages.rel_impls.utils import Transactable, transaction, Connection
from ..storages.kv import InMemoryStorage, MultiProcInMemoryStorage, KVStore

if Config.has_duckdb:
    from ..storages.rel_impls.duckdb_impl import DuckDBRelStorage
from ..storages.rel_impls.sqlite_impl import SQLiteRelStorage
from ..storages.rels import RelAdapter, RemoteEventLogEntry, VersionAdapter
from ..storages.sigs import SigSyncer
from ..storages.remote_storage import RemoteStorage
from ..deps.tracers import TracerABC, DecTracer
from ..deps.versioner import Versioner, CodeState
from ..deps.utils import get_dep_key_from_func, extract_func_obj
from ..deps.model import DepKey, TerminalData

from ..queries.workflow import CallStruct
from ..queries.weaver import (
    ValQuery,
    FuncQuery,
    traverse_all,
    StructOrientations,
)
from ..queries.viz import (
    visualize_graph,
    print_graph,
    get_names,
    extract_names_from_scope,
)
from ..queries.main import Querier
from ..queries.graphs import get_canonical_order, InducedSubgraph


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
        db_path: Optional[Union[str, Path]] = None,
        db_backend: str = Config.db_backend,
        spillover_dir: Optional[Union[str, Path]] = None,
        spillover_threshold_mb: Optional[float] = None,
        root: Optional[Union[Path, RemoteStorage]] = None,
        timestamp: Optional[datetime.datetime] = None,
        multiproc: bool = False,
        evict_on_commit: bool = Config.evict_on_commit,
        call_cache: Optional[KVStore] = None,
        obj_cache: Optional[KVStore] = None,
        signatures: Optional[Dict[Tuple[str, int], Signature]] = None,
        _read_only: bool = False,
        ### dependency tracking config
        deps_path: Optional[Union[Path, str]] = None,
        deps_package: Optional[str] = None,
        track_methods: bool = True,
        _strict_deps: bool = True,  # for testing only
        tracer_impl: Optional[type] = None,
    ):
        self.root = root
        if call_cache is None:
            call_cache = MultiProcInMemoryStorage() if multiproc else InMemoryStorage()
        call_cache_by_uid = (
            MultiProcInMemoryStorage() if multiproc else InMemoryStorage()
        )
        self.call_cache_by_causal = call_cache  # by causal_uid
        self.call_cache_by_uid = call_cache_by_uid  # by uid only for fast lookup
        if obj_cache is None:
            obj_cache = MultiProcInMemoryStorage() if multiproc else InMemoryStorage()
        self.obj_cache = obj_cache
        # all objects (inputs and outputs to operations, defaults) are saved here
        # stores the memoization tables
        if db_path is None and Config._persistent_storage_testing:
            # get a temp db path
            # generate a random filename
            db_name = f"db_{get_uid()}.db"
            db_path = Path(
                os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    f"../../temp_dbs/{db_name}",
                )
            ).resolve()
        self.db_path = db_path
        self.db_backend = db_backend
        self.evict_on_commit = evict_on_commit
        if Config.has_duckdb and db_backend == "duckdb":
            DBImplementation = DuckDBRelStorage
        else:
            DBImplementation = SQLiteRelStorage
        self.rel_storage = DBImplementation(
            address=None if db_path is None else str(db_path),
            _read_only=_read_only,
        )

        # manipulates the memoization tables
        self.rel_adapter = RelAdapter(
            rel_storage=self.rel_storage,
            spillover_dir=Path(spillover_dir) if spillover_dir is not None else None,
            spillover_threshold_mb=spillover_threshold_mb,
        )
        # self.versions_adapter = VersionAdapter(rel_adapter=rel_adapter)
        self.sig_adapter = self.rel_adapter.sig_adapter
        self.sig_syncer = SigSyncer(sig_adapter=self.sig_adapter, root=self.root)
        if signatures is not None:
            self.sig_adapter.dump_state(state=signatures)
        self.last_timestamp = (
            timestamp if timestamp is not None else datetime.datetime.fromtimestamp(0)
        )

        self.version_adapter = VersionAdapter(rel_adapter=self.rel_adapter)
        if deps_path is not None:
            deps_path = (
                Path(deps_path).absolute().resolve()
                if deps_path != "__main__"
                else "__main__"
            )
            roots = [] if deps_path == "__main__" else [deps_path]
            self._versioned = True
            current_versioner = self.version_adapter.load_state()
            if current_versioner is not None:
                if current_versioner.paths != roots:
                    raise ValueError(
                        f"Found existing versioner with roots {current_versioner.paths}, but "
                        f"was asked to use {roots}"
                    )
            else:
                versioner = Versioner(
                    paths=roots,
                    TracerCls=DecTracer if tracer_impl is None else tracer_impl,
                    strict=_strict_deps,
                    track_methods=track_methods,
                    package_name=deps_package,
                )
                self.version_adapter.dump_state(state=versioner)
        else:
            self._versioned = False

        if root is not None:
            self.remote_manager = RemoteManager(
                rel_adapter=self.rel_adapter,
                sig_adapter=self.sig_adapter,
                rel_storage=self.rel_storage,
                sig_syncer=self.sig_syncer,
                root=self.root,
            )
        else:
            self.remote_manager = None

        # set up builtins
        for func_op in Builtins.OPS.values():
            self.synchronize_op(func_op=func_op)

    @property
    def in_memory(self) -> bool:
        return self.db_path is None

    @transaction()
    def get_versioner(self, conn: Optional[Connection] = None) -> Versioner:
        result = self.version_adapter.load_state(conn=conn)
        if result is None:
            raise ValueError("This storage is not versioned.")
        return result

    @property
    def versioned(self) -> bool:
        return self._versioned

    ############################################################################
    ### `Transactable` interface
    ############################################################################
    def _get_connection(self) -> Connection:
        return self.rel_storage._get_connection()

    def _end_transaction(self, conn: Connection):
        return self.rel_storage._end_transaction(conn=conn)

    ############################################################################
    ### interacting with the caches and the database
    ############################################################################
    @transaction()
    def call_exists(
        self, uid: str, by_causal: bool, conn: Optional[Connection] = None
    ) -> bool:
        if by_causal:
            return self.call_cache_by_causal.exists(
                uid
            ) or self.rel_adapter.call_exists(uid=uid, by_causal=True, conn=conn)
        else:
            return self.call_cache_by_uid.exists(uid) or self.rel_adapter.call_exists(
                uid=uid, by_causal=False, conn=conn
            )

    @transaction()
    def call_get(
        self, uid: str, by_causal: bool, lazy: bool, conn: Optional[Connection] = None
    ) -> Call:
        if by_causal and self.call_cache_by_causal.exists(uid):
            return self.call_cache_by_causal.get(uid)
        elif not by_causal and self.call_cache_by_uid.exists(uid):
            return self.call_cache_by_uid.get(uid)
        else:
            lazy_call = self.rel_adapter.call_get_lazy(
                uid=uid, by_causal=by_causal, conn=conn
            )
            if not lazy:
                # load the values of the inputs and outputs
                inputs = {
                    k: self.obj_get(v.uid, conn=conn)
                    for k, v in lazy_call.inputs.items()
                }
                outputs = [self.obj_get(v.uid, conn=conn) for v in lazy_call.outputs]
                call_without_outputs = lazy_call.set_input_values(inputs=inputs)
                call = call_without_outputs.set_output_values(outputs=outputs)
                return call
            else:
                return lazy_call

    def cache_call_and_objs(self, call: Call) -> None:
        for vref in itertools.chain(call.inputs.values(), call.outputs):
            self.cache_obj(vref.uid, vref)
        self.cache_call(causal_uid=call.causal_uid, call=call)

    def cache_call(self, causal_uid: str, call: Call) -> None:
        self.call_cache_by_causal.set(causal_uid, call)
        self.call_cache_by_uid.set(call.uid, call)

    @transaction()
    def obj_get(self, obj_uid: str, conn: Optional[Connection] = None) -> Ref:
        if self.obj_cache.exists(obj_uid):
            return self.obj_cache.get(obj_uid)
        return self.rel_adapter.obj_get(uid=obj_uid, conn=conn)

    def cache_obj(self, obj_uid: str, vref: Ref) -> None:
        vref = vref.unlinked(keep_causal=False)
        self.obj_cache.set(obj_uid, vref)

    @transaction()
    def preload_objs(self, uids: List[str], conn: Optional[Connection] = None):
        """
        Put the objects with the given UIDs in the cache.
        """
        uids_not_in_cache = [uid for uid in uids if not self.obj_cache.exists(uid)]
        for uid, vref in zip(
            uids_not_in_cache,
            self.rel_adapter.obj_gets(uids=uids_not_in_cache, conn=conn),
        ):
            self.obj_cache.set(k=uid, v=vref)

    def evict_caches(self):
        for k in self.call_cache_by_causal.keys():
            self.call_cache_by_causal.delete(k=k)
        for k in self.obj_cache.keys():
            self.obj_cache.delete(k=k)

    @transaction()
    def commit(
        self,
        calls: Optional[List[Call]] = None,
        versioner: Optional[Versioner] = None,
        conn: Optional[Connection] = None,
    ):
        """
        Flush calls and objs from the cache that haven't yet been written to the database.
        """
        if calls is None:
            new_objs = {
                key: self.obj_cache.get(key) for key in self.obj_cache.dirty_entries
            }
            new_calls = [
                self.call_cache_by_causal.get(key)
                for key in self.call_cache_by_causal.dirty_entries
            ]
        else:
            new_objs = {}
            for call in calls:
                for vref in itertools.chain(call.inputs.values(), call.outputs):
                    new_objs[vref.uid] = vref
            new_calls = calls
        self.rel_adapter.obj_sets(new_objs, conn=conn)
        self.rel_adapter.upsert_calls(new_calls, conn=conn)
        if self.evict_on_commit:
            self.evict_caches()

        if versioner is not None:
            self.version_adapter.dump_state(state=versioner, conn=conn)

        # Remove dirty bits from cache.
        self.obj_cache.dirty_entries.clear()
        self.call_cache_by_causal.dirty_entries.clear()

    @transaction()
    def eval_df(
        self,
        full_uids_df: pd.DataFrame,
        drop_duplicates: bool = False,
        values: Literal["objs", "refs", "uids", "lazy"] = "objs",
        conn: Optional[Connection] = None,
    ) -> pd.DataFrame:
        uids_df = full_uids_df.applymap(lambda uid: uid.rsplit(".", 1)[0])
        if values in ("objs", "refs"):
            uids_to_collect = [
                item for _, column in uids_df.items() for _, item in column.items()
            ]
            self.preload_objs(uids_to_collect, conn=conn)
            if values == "objs":
                result = uids_df.applymap(
                    lambda uid: unwrap(self.obj_get(uid, conn=conn))
                )
            else:
                result = uids_df.applymap(lambda uid: self.obj_get(uid, conn=conn))
        elif values == "uids":
            result = uids_df
        elif values == "lazy":
            result = uids_df.applymap(lambda uid: Ref.from_uid(uid=uid))
        else:
            raise ValueError(
                f"Invalid value for `values`: {values}. Must be one of "
                "['objs', 'refs', 'uids', 'lazy']"
            )
        if drop_duplicates:
            result = result.drop_duplicates()
        return result

    @transaction()
    def get_table(
        self,
        func_interface: Union["funcs.FuncInterface", Any],
        meta: bool = False,
        values: Literal["objs", "uids", "refs", "lazy"] = "objs",
        drop_duplicates: bool = False,
        conn: Optional[Connection] = None,
    ) -> pd.DataFrame:
        full_uids_df = self.rel_storage.get_data(
            table=func_interface.func_op.sig.versioned_ui_name, conn=conn
        )
        if not meta:
            full_uids_df = full_uids_df.drop(columns=Config.special_call_cols)
        df = self.eval_df(
            full_uids_df=full_uids_df,
            values=values,
            drop_duplicates=drop_duplicates,
            conn=conn,
        )
        return df

    ############################################################################
    ### synchronization
    ############################################################################
    @transaction()
    def synchronize_op(
        self,
        func_op: FuncOp,
        conn: Optional[Connection] = None,
    ):
        # first, pull the current data from the remote!
        self.sig_syncer.sync_from_remote(conn=conn)
        # this step also sends the signature to the remote
        new_sig = self.sig_syncer.sync_from_local(sig=func_op.sig, conn=conn)
        func_op.sig = new_sig
        # to send any default values that were created by adding inputs
        self.sync_to_remote()

    @transaction()
    def synchronize(
        self, f: Union["funcs.FuncInterface", Any], conn: Optional[Connection] = None
    ):
        if f._is_invalidated:
            raise RuntimeError(
                "This function has been invalidated due to a change in the signature, and cannot be called"
            )
        if f._is_synchronized:
            if f._storage_id != id(self):
                raise RuntimeError(
                    "This function is already synchronized with a different storage object. Re-define the function to synchronize it with this storage object."
                )
            return
        self.synchronize_op(func_op=f.func_op, conn=conn)
        f._is_synchronized = True
        f._storage_id = id(self)

    ############################################################################
    ### versioning
    ############################################################################
    @transaction()
    def guess_code_state(
        self, versioner: Optional[Versioner] = None, conn: Optional[Connection] = None
    ) -> CodeState:
        if versioner is None:
            versioner = self.get_versioner(conn=conn)
        return versioner.guess_code_state()

    @transaction()
    def sync_code(
        self, conn: Optional[Connection] = None
    ) -> Tuple[Versioner, CodeState]:
        versioner = self.get_versioner(conn=conn)
        code_state = self.guess_code_state(versioner=versioner, conn=conn)
        versioner.sync_codebase(code_state=code_state)
        return versioner, code_state

    @transaction()
    def sync_component(
        self,
        component: types.FunctionType,
        is_semantic_change: Optional[bool],
        conn: Optional[Connection] = None,
    ):
        # low-level versioning
        dep_key = get_dep_key_from_func(func=component)
        versioner = self.get_versioner(conn=conn)
        code_state = self.guess_code_state(versioner=versioner, conn=conn)
        result = versioner.sync_component(
            component=dep_key,
            is_semantic_change=is_semantic_change,
            code_state=code_state,
        )
        self.version_adapter.dump_state(state=versioner, conn=conn)
        return result

    @transaction()
    def _show_version_data(
        self,
        f: Union[Callable, "funcs.FuncInterface"],
        deps: bool = True,
        meta: bool = False,
        plain: bool = False,
        compact: bool = False,
        conn: Optional[Connection] = None,
    ):
        # show the versions of a function, with/without its dependencies
        func = extract_func_obj(obj=f, strict=True)
        component = get_dep_key_from_func(func=func)
        versioner = self.get_versioner(conn=conn)
        if deps:
            versioner.show_versions(
                component=component,
                include_metadata=meta,
                plain=plain,
            )
        else:
            versioner.component_dags[component].show(
                compact=compact, plain=plain, include_metadata=meta
            )

    @transaction()
    def versions(
        self,
        f: Union[Callable, "funcs.FuncInterface"],
        meta: bool = False,
        plain: bool = False,
        conn: Optional[Connection] = None,
    ):
        self._show_version_data(
            f=f,
            deps=True,
            meta=meta,
            plain=plain,
            compact=False,
            conn=conn,
        )

    @transaction()
    def sources(
        self,
        f: Union[Callable, "funcs.FuncInterface"],
        meta: bool = False,
        plain: bool = False,
        compact: bool = False,
        conn: Optional[Connection] = None,
    ):
        func = extract_func_obj(obj=f, strict=True)
        component = get_dep_key_from_func(func=func)
        versioner = self.get_versioner(conn=conn)
        print(
            f"Revision history for the source code of function {component[1]} from module {component[0]} "
            '("===HEAD===" is the current version):'
        )
        versioner.component_dags[component].show(
            compact=compact, plain=plain, include_metadata=meta
        )

    @transaction()
    def code(
        self, version_id: str, meta: bool = False, conn: Optional[Connection] = None
    ):
        # show a copy-pastable version of the code for a given version id. Plain
        # by design.
        result = self.get_code(version_id=version_id, show=False, meta=meta, conn=conn)
        print(result)

    @transaction()
    def get_code(
        self,
        version_id: str,
        show: bool = True,
        meta: bool = False,
        conn: Optional[Connection] = None,
    ) -> str:
        versioner = self.get_versioner(conn=conn)
        for dag in versioner.component_dags.values():
            if version_id in dag.commits.keys():
                text = dag.get_content(commit=version_id)
                if show:
                    print(text)
                return text
        for (
            content_version,
            version,
        ) in versioner.get_flat_versions().items():
            if version_id == content_version:
                raw_string = versioner.present_dependencies(
                    commits=version.semantic_expansion,
                    include_metadata=meta,
                )
                if show:
                    print(raw_string)
                return raw_string
        raise ValueError(f"version id {version_id} not found")

    @transaction()
    def diff(
        self,
        id_1: str,
        id_2: str,
        context_lines: int = 2,
        conn: Optional[Connection] = None,
    ):
        code_1: str = self.get_code(version_id=id_1, show=False, conn=conn)
        code_2: str = self.get_code(version_id=id_2, show=False, conn=conn)
        print(
            _get_colorized_diff(current=code_1, new=code_2, context_lines=context_lines)
        )

    ############################################################################
    ### make calls in contexts
    ############################################################################
    @transaction()
    def _load_memoization_tables(
        self, evaluate: bool = False, conn: Optional[Connection] = None
    ) -> Dict[str, pd.DataFrame]:
        """
        Get a dict of {versioned internal name: memoization table} for all
        functions. Note that memoization tables are labeled by UI arg names.
        """
        sigs = self.sig_adapter.load_state(conn=conn)
        ui_to_internal = {
            sig.versioned_ui_name: sig.versioned_internal_name for sig in sigs.values()
        }
        ui_call_data = self.rel_adapter.get_all_call_data(conn=conn)
        call_data = {ui_to_internal[k]: v for k, v in ui_call_data.items()}
        if evaluate:
            call_data = {
                k: self.eval_df(full_uids_df=v, values="objs", conn=conn)
                for k, v in call_data.items()
            }
        return call_data

    @transaction()
    def get_compatible_semantic_versions(
        self,
        fqs: Set[FuncQuery],
        conn: Optional[Connection] = None,
    ) -> Tuple[Optional[Dict[OpKey, Set[str]]], Optional[Dict[DepKey, Set[str]]]]:
        if not self.versioned:
            return None, None
        if contexts.GlobalContext.current is not None:
            code_state = contexts.GlobalContext.current._code_state
        else:
            code_state = self.guess_code_state(
                versioner=self.get_versioner(), conn=conn
            )
        result_ops = {}
        result_deps = {}
        versioner = self.get_versioner(conn=conn)
        for func_query in fqs:
            sig = func_query.func_op.sig
            op_key = (sig.internal_name, sig.version)
            # dep_key = get_dep_key_from_func(func=func_query.func_op.func)
            dep_key = (func_query.func_op._module, func_query.func_op._qualname)
            if func_query.func_op._is_builtin:
                result_ops[op_key] = None
                result_deps[dep_key] = None
            else:
                versions = versioner.get_semantically_compatible_versions(
                    component=dep_key, code_state=code_state
                )
                result_ops[op_key] = set([v.semantic_version for v in versions])
                result_deps[dep_key] = result_ops[op_key]
        return result_ops, result_deps

    @transaction()
    def execute_query(
        self,
        selection: List[ValQuery],
        vqs: Set[ValQuery],
        fqs: Set[FuncQuery],
        names: Dict[ValQuery, str],
        values: Literal["objs", "refs", "uids", "lazy"] = "objs",
        engine: Optional[Literal["sql", "naive", "_test"]] = None,
        local: bool = False,
        verbose: bool = True,
        drop_duplicates: bool = True,
        visualize_steps_at: Optional[Path] = None,
        conn: Optional[Connection] = None,
    ) -> pd.DataFrame:
        """
        Execute the given queries and return the result as a pandas DataFrame.
        """
        if engine is None:
            engine = Config.query_engine

        def rename_cols(
            df: pd.DataFrame, selection: List[ValQuery], names: Dict[ValQuery, str]
        ):
            df.columns = [str(i) for i in range(len(df.columns))]
            cols = [names[query] for query in selection]
            df.rename(columns=dict(zip(df.columns, cols)), inplace=True)

        Querier.validate_query(vqs=vqs, fqs=fqs, selection=selection, names=names)
        context = contexts.GlobalContext.current
        if verbose:
            print(
                "Pattern-matching to the following computational graph (all constraints apply):"
            )
            print_graph(vqs=vqs, fqs=fqs, names=names, selection=selection)
        if visualize_steps_at is not None:
            assert engine == "naive"
        if engine in ["sql", "_test"]:
            version_constraints, _ = self.get_compatible_semantic_versions(
                fqs=fqs, conn=conn
            )
            call_uids = context._call_uids if local else None
            query = Querier.compile(
                selection=selection,
                vqs=vqs,
                fqs=fqs,
                version_constraints=version_constraints,
                filter_duplicates=drop_duplicates,
                call_uids=call_uids,
            )
            start = time.time()
            sql_uids_df = self.rel_storage.execute_df(query=str(query), conn=conn)
            end = time.time()
            logger.debug(f"SQL query took {round(end - start, 3)} seconds")
            rename_cols(df=sql_uids_df, selection=selection, names=names)
            uids_df = sql_uids_df
        if engine in ["naive", "_test"]:
            memoization_tables = self._load_memoization_tables(conn=conn)
            logger.debug("Executing query naively...")
            naive_uids_df = Querier.execute_naive(
                vqs=vqs,
                fqs=fqs,
                selection=selection,
                memoization_tables=memoization_tables,
                filter_duplicates=drop_duplicates,
                table_evaluator=self.eval_df,
                visualize_steps_at=visualize_steps_at,
            )
            rename_cols(df=naive_uids_df, selection=selection, names=names)
            uids_df = naive_uids_df
        if engine == "_test":
            outcome, reason = compare_dfs_as_relations(
                df_1=sql_uids_df, df_2=naive_uids_df, return_reason=True
            )
            assert outcome, reason
        return self.eval_df(full_uids_df=uids_df, values=values, conn=conn)

    def _get_graph_and_names(
        self,
        objs: Tuple[Union[Ref, ValQuery]],
        direction: Literal["forward", "backward", "both"] = "both",
        scope: Optional[Dict[str, Any]] = None,
        project: bool = False,
    ):
        vqs = {obj.query if isinstance(obj, Ref) else obj for obj in objs}
        vqs, fqs = traverse_all(vqs=vqs, direction=direction)
        hints = extract_names_from_scope(scope=scope) if scope is not None else {}
        g = InducedSubgraph(vqs=vqs, fqs=fqs)
        if project:
            v_proj, f_proj, _ = g.project()
            proj_hints = {v_proj[vq]: hints[vq] for vq in v_proj if vq in hints}
            names = get_names(
                hints=proj_hints,
                canonical_order=get_canonical_order(
                    vqs=set(v_proj.values()), fqs=set(f_proj.values())
                ),
            )
            final_vqs = set(v_proj.values())
            final_fqs = set(f_proj.values())
        else:
            v_proj = {vq: vq for vq in vqs}
            f_proj = {fq: fq for fq in fqs}
            names = get_names(
                hints=hints,
                canonical_order=get_canonical_order(vqs=set(vqs), fqs=set(fqs)),
            )
            final_vqs = vqs
            final_fqs = fqs
        return final_vqs, final_fqs, names, v_proj, f_proj

    def draw_graph(
        self,
        *objs: Union[Ref, ValQuery],
        traverse: Literal["forward", "backward", "both"] = "backward",
        project: bool = False,
        show_how: Literal["none", "browser", "inline", "open"] = "browser",
    ):
        scope = inspect.currentframe().f_back.f_locals
        vqs, fqs, names, v_proj, f_proj = self._get_graph_and_names(
            objs,
            direction=traverse,
            scope=scope,
            project=project,
        )
        visualize_graph(vqs, fqs, names=names, show_how=show_how)

    def print_graph(
        self,
        *objs: Union[Ref, ValQuery],
        project: bool = False,
        traverse: Literal["forward", "backward", "both"] = "backward",
    ):
        scope = inspect.currentframe().f_back.f_locals
        vqs, fqs, names, v_proj, f_proj = self._get_graph_and_names(
            objs,
            direction=traverse,
            scope=scope,
            project=project,
        )
        print_graph(
            vqs=vqs,
            fqs=fqs,
            names=names,
            selection=[
                v_proj[obj.query] if isinstance(obj, Ref) else obj for obj in objs
            ],
        )

    @transaction()
    def similar(
        self,
        *objs: Union[Ref, ValQuery],
        values: Literal["objs", "refs", "uids", "lazy"] = "objs",
        context: bool = False,
        verbose: Optional[bool] = None,
        local: bool = False,
        drop_duplicates: bool = True,
        engine: Literal["sql", "naive", "_test"] = None,
        _visualize_steps_at: Optional[Path] = None,
        conn: Optional[Connection] = None,
    ) -> pd.DataFrame:
        scope = inspect.currentframe().f_back.f_back.f_locals
        return self.df(
            *objs,
            direction="backward",
            scope=scope,
            values=values,
            context=context,
            skip_objs=False,
            verbose=verbose,
            local=local,
            drop_duplicates=drop_duplicates,
            engine=engine,
            _visualize_steps_at=_visualize_steps_at,
            conn=conn,
        )

    @transaction()
    def df(
        self,
        *objs: Union[Ref, ValQuery],
        direction: Literal["forward", "backward", "both"] = "both",
        values: Literal["objs", "refs", "uids", "lazy"] = "objs",
        context: bool = False,
        skip_objs: bool = False,
        verbose: Optional[bool] = None,
        local: bool = False,
        drop_duplicates: bool = True,
        engine: Literal["sql", "naive", "_test"] = None,
        _visualize_steps_at: Optional[Path] = None,
        scope: Optional[Dict[str, Any]] = None,
        conn: Optional[Connection] = None,
    ) -> pd.DataFrame:
        """
        Universal query method over computational graphs, both imperative and
        declarative.
        """
        if verbose is None:
            verbose = Config.verbose_queries
        if not all(isinstance(obj, (Ref, ValQuery)) for obj in objs):
            raise ValueError(
                "All arguments to df() must be either `Ref`s or `ValQuery`s."
            )
        #! important
        # We must sync any dirty cache elements to the db before performing a query.
        # If we don't, we'll query a store that might be missing calls and objs.
        self.commit(versioner=None)
        selection = [obj.query if isinstance(obj, Ref) else obj for obj in objs]
        # deps = get_deps(nodes=set(selection))
        vqs, fqs = traverse_all(vqs=set(selection), direction=direction)
        if scope is None:
            scope = inspect.currentframe().f_back.f_back.f_locals
        name_hints = extract_names_from_scope(scope=scope)
        v_map, f_map, target_selection, target_names = Querier.prepare_projection_query(
            vqs=vqs, fqs=fqs, selection=selection, name_hints=name_hints
        )
        target_vqs, target_fqs = set(v_map.values()), set(f_map.values())
        if context:
            g = InducedSubgraph(vqs=target_vqs, fqs=target_fqs)
            _, _, topsort = g.canonicalize()
            target_selection = [vq for vq in topsort if isinstance(vq, ValQuery)]
        df = self.execute_query(
            selection=target_selection,
            vqs=set(v_map.values()),
            fqs=set(f_map.values()),
            values=values,
            names=target_names,
            verbose=verbose,
            drop_duplicates=drop_duplicates,
            visualize_steps_at=_visualize_steps_at,
            engine=engine,
            local=local,
            conn=conn,
        )
        for col in df.columns:
            try:
                df = df.sort_values(by=col)
            except Exception:
                continue
        if skip_objs:
            # drop the dtypes that are objects
            df = df.select_dtypes(exclude=["object"])
        return df

    def _make_terminal_data(self, func_op: FuncOp, call: Call) -> TerminalData:
        terminal_data = TerminalData(
            op_internal_name=func_op.sig.internal_name,
            op_version=func_op.sig.version,
            call_content_version=call.content_version,
            call_semantic_version=call.semantic_version,
            dep_key=get_dep_key_from_func(func=func_op.func),
        )
        return terminal_data

    @transaction()
    def lookup_call(
        self,
        func_op: FuncOp,
        pre_call_uid: str,
        input_uids: Dict[str, str],
        input_causal_uids: Dict[str, str],
        lazy: bool,
        code_state: Optional[CodeState] = None,
        versioner: Optional[Versioner] = None,
        conn: Optional[Connection] = None,
    ) -> Optional[Call]:
        if not self.versioned:
            semantic_version = None
        else:
            assert code_state is not None
            component = get_dep_key_from_func(func=func_op.func)
            lookup_outcome = versioner.lookup_call(
                component=component, pre_call_uid=pre_call_uid, code_state=code_state
            )
            if lookup_outcome is None:
                return
            else:
                _, semantic_version = lookup_outcome
        causal_uid = func_op.get_call_causal_uid(
            input_uids=input_uids,
            input_causal_uids=input_causal_uids,
            semantic_version=semantic_version,
        )
        if self.call_exists(uid=causal_uid, by_causal=True, conn=conn):
            return self.call_get(uid=causal_uid, by_causal=True, lazy=lazy, conn=conn)
        call_uid = func_op.get_call_uid(
            pre_call_uid=pre_call_uid, semantic_version=semantic_version
        )
        if self.call_exists(uid=call_uid, by_causal=False, conn=conn):
            return self.call_get(uid=call_uid, by_causal=False, lazy=lazy, conn=conn)
        return None

    def call_batch(
        self, func_op: FuncOp, inputs: Dict[str, Ref]
    ) -> Tuple[List[Ref], CallStruct]:
        output_types = [Type.from_annotation(a) for a in func_op.output_annotations]
        outputs = [make_delayed(tp=tp) for tp in output_types]
        call_struct = CallStruct(func_op=func_op, inputs=inputs, outputs=outputs)
        return outputs, call_struct

    ############################################################################
    ### spawning contexts
    ############################################################################
    def _nest(self, **updates) -> contexts.Context:
        if contexts.GlobalContext.current is not None:
            return contexts.GlobalContext.current(**updates)
        else:
            result = contexts.Context(**updates)
            contexts.GlobalContext.current = result
            return result

    def __call__(self, **updates) -> contexts.Context:
        return self.run(**updates)

    def run(
        self,
        allow_calls: bool = True,
        debug_calls: bool = False,
        attach_call_to_outputs: bool = False,
        recompute_transient: bool = False,
        lazy: Optional[bool] = None,
        **updates,
    ) -> contexts.Context:
        # spawn context to execute or retrace calls
        lazy = not self.in_memory if lazy is None else lazy
        return self._nest(
            storage=self,
            allow_calls=allow_calls,
            debug_calls=debug_calls,
            recompute_transient=recompute_transient,
            _attach_call_to_outputs=attach_call_to_outputs,
            mode=MODES.run,
            lazy=lazy,
            **updates,
        )

    def query(self, **updates) -> contexts.Context:
        # spawn a context to define a query
        return self._nest(
            storage=self,
            mode=MODES.query,
            **updates,
        )

    def batch(self, **updates) -> contexts.Context:
        # spawn a context to execute calls in batch
        return self._nest(
            storage=self,
            mode=MODES.batch,
            **updates,
        )

    ############################################################################
    ### remote sync operations
    ############################################################################
    @transaction()
    def sync_from_remote(self, conn: Optional[Connection] = None):
        if self.remote_manager is not None:
            self.remote_manager.sync_from_remote(conn=conn)

    @transaction()
    def sync_to_remote(self, conn: Optional[Connection] = None):
        if self.remote_manager is not None:
            self.remote_manager.sync_to_remote(conn=conn)

    @transaction()
    def sync_with_remote(self, conn: Optional[Connection] = None):
        if self.remote_manager is not None:
            self.sync_to_remote(conn=conn)
            self.sync_from_remote(conn=conn)

    ############################################################################
    ### refactoring
    ############################################################################
    @property
    def is_clean(self) -> bool:
        """
        Check that the storage has no uncommitted calls or objects.
        """
        return self.call_cache_by_causal.is_clean and self.obj_cache.is_clean

    def _check_rename_precondition(self, func: "funcs.FuncInterface"):
        """
        In order to rename function data, the function must be synced with the
        storage, and the storage must be clean
        """
        if not func._is_synchronized:
            raise RuntimeError("Cannot rename while function is not synchronized.")
        if not self.is_clean:
            raise RuntimeError("Cannot rename while there is uncommited work.")

    @transaction()
    def rename_func(
        self,
        func: "funcs.FuncInterface",
        new_name: str,
        conn: Optional[Connection] = None,
    ) -> Signature:
        """
        Rename a memoized function.

        What happens here:
            - check renaming preconditions
            - check there is no name clash with the new name
            - rename the memoization table
            - update signature object
            - invalidate the function (making it impossible to compute with it)
        """
        self._check_rename_precondition(func=func)
        sig = self.sig_syncer.sync_rename_sig(
            sig=func.func_op.sig, new_name=new_name, conn=conn
        )
        func.invalidate()
        return sig

    @transaction()
    def rename_arg(
        self,
        func: "funcs.FuncInterface",
        name: str,
        new_name: str,
        conn: Optional[Connection] = None,
    ) -> Signature:
        """
        Rename memoized function argument.

        What happens here:
            - check renaming preconditions
            - update signature object
            - rename table
            - invalidate the function (making it impossible to compute with it)
        """
        self._check_rename_precondition(func=func)
        sig = self.sig_syncer.sync_rename_input(
            sig=func.func_op.sig, input_name=name, new_input_name=new_name, conn=conn
        )
        func.invalidate()
        return sig


from . import funcs

FuncInterface = funcs.FuncInterface


TP_TO_CLS = {
    AnyType: ValueRef,
    ListType: ListRef,
    DictType: DictRef,
    SetType: SetRef,
}


def make_delayed(tp: Type) -> Ref:
    return TP_TO_CLS[type(tp)](uid="", obj=Delayed(), in_memory=False)