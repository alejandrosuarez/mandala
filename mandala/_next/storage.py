from common_imports import *
import datetime
from model import *
import sqlite3
from model import __make_list__, __get_item__

from storage_utils import InMemCallStorage, SQLiteCallStorage, CachedDictStorage, SQLiteDictStorage, CachedCallStorage


class Storage:
    def __init__(self, db_path: Optional[str] = None):
        if db_path is not None:
            # if no file exists, create a sqlite db
            if not os.path.exists(db_path):
                # create a database with incremental vacuuming
                conn = self.conn()
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
                conn.execute("PRAGMA incremental_vacuum_threshold = 1024;")
                conn.close()
        else:
            db_path = ":memory:"
        self.db_path = db_path
        
        self.call_storage = SQLiteCallStorage(db_path, table_name="calls")
        self.calls = CachedCallStorage(persistent=self.call_storage)
        self.call_cache = self.calls.cache

        self.atoms = CachedDictStorage(
            persistent=SQLiteDictStorage(db_path, table="atoms")
        )
        self.shapes = CachedDictStorage(
            persistent=SQLiteDictStorage(db_path, table="shapes")
        )
        self.ops = CachedDictStorage(
            persistent=SQLiteDictStorage(db_path, table="ops")
        )

    
    def conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)
    
    def vacuum(self):
        with self.conn() as conn:
            conn.execute("VACUUM")
    
    def in_context(self) -> bool:
        return Context.current_context is not None

    ############################################################################ 
    ### refs interface
    ############################################################################ 
    def save_ref(self, ref: Ref):
        if ref.hid in self.shapes: # ensure idempotence
            return
        if isinstance(ref, AtomRef):
            self.atoms[ref.cid] = serialize(ref.obj)
            self.shapes[ref.hid] = ref.detached()
        elif isinstance(ref, ListRef):
            self.shapes[ref.hid] = ref.shape()
            for i, elt in enumerate(ref):
                self.save_ref(elt)
        else:
            raise NotImplementedError

    def load_ref(self, hid: str, lazy: bool = False) -> Ref:
        shape = self.shapes[hid]
        if isinstance(shape, AtomRef):
            if lazy:
                return shape.shallow_copy()
            else:
                return shape.attached(obj=deserialize(self.atoms[shape.cid]))
        elif isinstance(shape, ListRef):
            obj = []
            for i, elt in enumerate(shape):
                obj.append(self.load_ref(elt.hid, lazy=lazy))
            return shape.attached(obj=shape.obj)
        else:
            raise NotImplementedError
    
    def _drop_ref_hid(self, hid: str, verify: bool = False):
        """
        Internal only function to drop a ref by its hid when it is not connected
        to any calls.
        """
        if verify:
            assert not self.call_storage.exists_ref_hid(hid)
        self.shapes.drop(hid)
    
    def _drop_ref(self, cid: str, verify: bool = False):
        """
        Internal only function to drop a ref by its cid when it is not connected
        to any calls and is not in the `shapes` table.
        """
        if verify:
            assert cid not in [shape.cid for shape in self.shapes.persistent.values()]
        self.atoms.drop(cid)
    
    def cleanup_refs(self):
        """
        Remove all refs that are not connected to any calls.
        """
        ### first, remove hids that are not connected to any calls
        orphans = self.get_orphans()
        logging.info(f'Cleaning up {len(orphans)} orphaned refs.')
        for hid in orphans:
            self._drop_ref_hid(hid)
        ### next, remove cids that are not connected to any refs
        unreferenced_cids = self.get_unreferenced_cids()
        logging.info(f'Cleaning up {len(unreferenced_cids)} unreferenced cids.')
        for cid in unreferenced_cids:
            self._drop_ref(cid)

    ############################################################################ 
    ### calls interface
    ############################################################################ 
    def exists_call(self, hid: str) -> bool:
        return self.calls.exists(hid)

    def save_call(self, call: Call):
        if self.calls.exists(call.hid):
            return
        if not self.ops.exists(key=call.op.name):
            self.ops[call.op.name] = call.op.detached()
        for k, v in itertools.chain(call.inputs.items(), call.outputs.items()):
            self.save_ref(v)
        self.calls.save(call)
    
    def get_call(self, hid: str, lazy: bool) -> Call:
        if self.call_cache.exists(hid):
            call_data = self.call_cache.get_data(hid)
        else:
            with self.call_storage.get_conn() as conn:
                call_data = self.call_storage.get_data(hid, conn=conn)
        op_name = call_data["op_name"]
        return Call(op=self.ops[op_name], 
                    cid=call_data["cid"], 
                    hid=call_data["hid"], 
                    inputs={k: self.load_ref(v, lazy=lazy) for k, v in call_data["input_hids"].items()},
                    outputs={k: self.load_ref(v, lazy=lazy) for k, v in call_data["output_hids"].items()})

    def drop_calls(self, hids: Iterable[str], delete_dependents: bool):
        if delete_dependents:
            _, dependent_call_hids = self.call_storage.get_dependents(ref_hids=set(), call_hids=hids)
            hids |= dependent_call_hids
        logging.info(f'Dropping {len(hids)} calls.')
        for hid in hids:
            if self.call_cache.exists(hid):
                self.call_cache.drop(hid)
        for hid in hids:
            self.call_storage.drop(hid)
    
    ############################################################################
    ### managing the caches
    ############################################################################
    def preload_calls(self):
        df = self.call_storage.get_df()
        self.call_cache.df = df

    def commit(self):
        with self.conn() as conn:
            self.atoms.commit(conn=conn)
            self.shapes.commit(conn=conn)
            self.ops.commit(conn=conn)
            self.calls.commit(conn=conn)
    
    ############################################################################
    ### provenance queries
    ############################################################################
    def get_creators(self, ref_hids: Iterable[str]) -> List[Call]:
        if self.in_context():
            raise NotImplementedError("Method not supported while in a context.")
        call_hids = self.call_storage.get_creator_hids(ref_hids)
        return [self.get_call(call_hid, lazy=True) for call_hid in call_hids]
    
    def get_consumers(self, ref_hids: Iterable[str]) -> List[Call]:
        if self.in_context():
            raise NotImplementedError("Method not supported while in a context.")
        call_hids = self.call_storage.get_consumer_hids(ref_hids)
        return [self.get_call(call_hid, lazy=True) for call_hid in call_hids]
    
    def get_orphans(self) -> Set[str]:
        """
        Return the HIDs of the refs not connected to any calls.
        """
        if self.in_context():
            raise NotImplementedError("Method not supported while in a context.")
        all_hids = set(self.shapes.persistent.keys())
        df = self.call_storage.get_df()
        hids_in_calls = df['ref_history_id'].unique()
        return all_hids - set(hids_in_calls)
    
    def get_unreferenced_cids(self) -> Set[str]:
        """
        Return the CIDs of the refs that don't appear in any calls or in the `shapes` table.
        """
        if self.in_context():
            raise NotImplementedError("Method not supported while in a context.")
        all_cids = set(self.atoms.persistent.keys())
        df = self.call_storage.get_df()
        cids_in_calls = df['ref_content_id'].unique()
        cids_in_shapes = [shape.cid for shape in self.shapes.persistent.values()]
        return (all_cids - set(cids_in_calls)) - set(cids_in_shapes)
    
    ############################################################################
    ### 
    ############################################################################
    def _unwrap_atom(self, obj: Any) -> Any:
        assert isinstance(obj, AtomRef)
        if not obj.in_memory:
            ref = self.load_ref(hid=obj.hid, lazy=False)
            return ref.obj
        else:
            return obj.obj
    
    def _attach_atom(self, obj: AtomRef) -> AtomRef:
        assert isinstance(obj, AtomRef)
        if not obj.in_memory:
            obj.obj = deserialize(self.atoms[obj.cid])
            obj.in_memory = True
        return obj
    
    def unwrap(self, obj: Any) -> Any:
        return recurse_on_ref_collections(self._unwrap_atom, obj)
    
    def attach(self, obj: T) -> T:
        return recurse_on_ref_collections(self._attach_atom, obj)
    
    def get_struct_builder(self, tp: Type) -> Op:
        # the builtin op that will construct instances of this type
        if isinstance(tp, ListType):
            return __make_list__
        else:
            raise NotImplementedError
    
    def get_struct_inputs(self, tp: Type, val: Any) -> Dict[str, Any]:
        # given a value to be interpreted as an instance of a struct,
        # return the inputs that would be passed to the struct builder
        if isinstance(tp, ListType):
            return {f'elts_{i}': elt for i, elt in enumerate(val)}
        else:
            raise NotImplementedError
    
    def get_struct_tps(self, tp: Type, struct_inputs: Dict[str, Any]) -> Dict[str, Type]:
        # given the inputs to a struct builder, return the annotations
        # that would be passed to the struct builder
        if isinstance(tp, ListType):
            return {f'elts_{i}': tp.elt for i in range(len(struct_inputs))}
        else:
            raise NotImplementedError
    
    def construct(self, tp: Type, val: Any) -> Tuple[Ref, List[Call]]:
        if isinstance(val, Ref):
            return val, []
        if isinstance(tp, AtomType):
            return wrap_atom(val, None), []
        struct_builder = self.get_struct_builder(tp)
        struct_inputs = self.get_struct_inputs(tp=tp, val=val)
        struct_tps = self.get_struct_tps(tp=tp, struct_inputs=struct_inputs) 
        res, main_call, calls = self.call_internal(op=struct_builder, inputs=struct_inputs, input_tps=struct_tps)
        calls.append(main_call)
        output_ref = main_call.outputs[list(main_call.outputs.keys())[0]]
        return output_ref, calls
    
    def destruct(self, ref: Ref, tp: Type) -> Tuple[Ref, List[Call]]:
        """
        Given a value w/ correct hid but possibly incorrect internal hids,
        destructure it, and return a value with correct internal hids and the
        calls that were made to destructure the value.
        """
        destr_calls = []
        if isinstance(ref, AtomRef):
            return ref, destr_calls
        elif isinstance(ref, ListRef):
            assert isinstance(ref, ListRef)
            assert isinstance(tp, ListType)
            new_elts = []
            for i, elt in enumerate(ref):
                getitem_dict, item_call, _ = self.call_internal(
                    op=__get_item__, inputs={"obj": ref, "attr": i}, input_tps={"obj": tp.elt, "attr": AtomType()}
                )
                new_elt = getitem_dict["output_0"]
                destr_calls.append(item_call)
                new_elt, elt_subcalls = self.destruct(new_elt, tp=tp.elt)
                new_elts.append(new_elt)
                destr_calls.extend(elt_subcalls)
            res = ListRef(cid=ref.cid, hid=ref.hid, in_memory=True, obj=new_elts)
            return res, destr_calls
        else:
            raise NotImplementedError
    
    def call_internal(self,
                      op: Op,
                      inputs: Dict[str, Any], 
                      input_tps: Dict[str, Type]) -> Tuple[Dict[str, Ref], Call, List[Call]]:
        """
        Main function to call an op, operating on the representations used 
        internally by the storage.
        """
        ### wrap the inputs
        wrapped_inputs = {}
        input_calls = []
        for k, v in inputs.items():
            wrapped_inputs[k], struct_calls = self.construct(tp=input_tps[k], val=v)
            input_calls.extend(struct_calls)
        ### check for the call
        call_history_id = op.get_call_history_id(wrapped_inputs)
        if self.exists_call(hid=call_history_id):
            main_call = self.get_call(hid=call_history_id, lazy=True)
            return main_call.outputs, main_call, input_calls

        ### execute the call if it doesn't exist
        # call the function
        f, sig = op.f, inspect.signature(op.f)
        if op.__structural__:
            returns = f(**wrapped_inputs)
        else:
            #! guard against side effects
            cids_before = {k: v.cid for k, v in wrapped_inputs.items()}
            raw_values = {k: self.unwrap(v) for k, v in wrapped_inputs.items()}
            args, kwargs = dump_args(sig=sig, inputs=raw_values)
            #! call the function
            returns = f(*args, **kwargs)
            # capture changes in the inputs; TODO: this is hacky; ideally, we would 
            # avoid calling `construct` and instead recurse on the values, looking for differences.
            cids_after = {k: self.construct(tp=input_tps[k], val=v)[0].cid for k, v in raw_values.items()}
            changed_inputs = {k for k in cids_before if cids_before[k] != cids_after[k]}
            if len(changed_inputs) > 0:
                raise ValueError(f"Function {f.__name__} has side effects on inputs {changed_inputs}; aborting call.")
        # wrap the outputs
        outputs_dict, outputs_annotations = parse_returns(sig=sig, returns=returns, nout=op.nout, output_names=op.output_names)
        output_tps = {k: Type.from_annotation(annotation=v) for k, v in outputs_annotations.items()}
        call_content_id = op.get_call_content_id(wrapped_inputs)
        call_history_id = op.get_call_history_id(wrapped_inputs)
        output_history_ids = op.get_output_history_ids(call_history_id=call_history_id, output_names=list(outputs_dict.keys()))

        wrapped_outputs = {}
        output_calls = []
        for k, v in outputs_dict.items():
            if isinstance(v, Ref):
                wrapped_outputs[k] = v.with_hid(hid=output_history_ids[k])
            elif isinstance(output_tps[k], AtomType):
                wrapped_outputs[k] = wrap_atom(v, history_id=output_history_ids[k])
            else: # recurse on types
                start, _ = self.construct(tp=output_tps[k], val=v)
                start = start.with_hid(hid=output_history_ids[k])
                final, output_calls_for_output = self.destruct(start, tp=output_tps[k])
                output_calls.extend(output_calls_for_output)
                wrapped_outputs[k] = final
        main_call = Call(op=op, cid=call_content_id, hid=call_history_id, inputs=wrapped_inputs, outputs=wrapped_outputs)
        return main_call.outputs, main_call, input_calls + output_calls

    ############################################################################
    ### user-facing functions
    ############################################################################
    def cf(self, source: Union[Op, Ref, Iterable[Ref], Iterable[str]]) -> "ComputationFrame":
        """
        Main user-facing function to create a computation frame.
        """
        if isinstance(source, Op):
            return ComputationFrame.from_op(storage=self, f=source)
        elif isinstance(source, Ref):
            return ComputationFrame.from_refs(refs=[source], storage=self)
        elif all(isinstance(elt, Ref) for elt in source):
            return ComputationFrame.from_refs(refs=source, storage=self)
        elif all(isinstance(elt, str) for elt in source):
            # must be hids 
            refs = [self.load_ref(hid, lazy=True) for hid in source]
            return ComputationFrame.from_refs(refs=refs, storage=self)
        else:
            raise ValueError("Invalid input to `cf`")

    def call(self, __op__: Op, *args, __config__:Optional[dict] = None, **kwargs) -> Union[Tuple[Ref,...], Ref]:
        __config__ = {} if __config__ is None else __config__
        raw_inputs, input_annotations = parse_args(sig=inspect.signature(__op__.f), args=args, kwargs=kwargs, apply_defaults=True)
        input_tps = {k: Type.from_annotation(annotation=v) for k, v in input_annotations.items()}
        res, main_call, calls = self.call_internal(op=__op__, inputs=raw_inputs, input_tps=input_tps)
        if __config__.get('save_calls', False):
            self.save_call(main_call)
            for call in calls:
                self.save_call(call)
        ord_outputs = __op__.get_ordered_outputs(main_call.outputs)
        if len(ord_outputs) == 1:
            return ord_outputs[0]
        else:
            return ord_outputs
    
    def __enter__(self) -> "Storage":
        Context.current_context = Context(storage=self)
        return self
    
    def __exit__(self, exc_type, exc_value, traceback) -> None:
        Context.current_context = None
        self.commit()
    
    

from cf import ComputationFrame
from ui import Context