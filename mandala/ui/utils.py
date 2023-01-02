from ..common_imports import *
from ..core.model import FuncOp, Ref, Call, wrap
from ..core.sig import Signature
from ..core.utils import Hashing
from ..core.config import Config
from ..core.weaver import ValQuery, qwrap


class MODES:
    run = "run"
    query = "query"
    batch = "batch"


def wrap_inputs(inputs: Dict[str, Any]) -> Dict[str, Ref]:
    # check if we allow implicit wrapping
    if Config.autowrap_inputs:
        return {k: wrap(v) for k, v in inputs.items()}
    else:
        assert all(isinstance(v, Ref) for v in inputs.values())
        return inputs


def wrap_outputs(outputs: List[Any], call_uid: str) -> List[Ref]:
    """
    Wrap the outputs of a call as value references.

    If the function happens to return value references, they are returned as
    they are. Otherwise, a UID is assigned depending on the configuration
    settings of the library.
    """
    wrapped_outputs = []
    if Config.output_wrap_method == "content":
        uid_generator = lambda i, x: Hashing.get_content_hash(x)
    elif Config.output_wrap_method == "causal":
        uid_generator = lambda i, x: Hashing.get_content_hash(obj=(call_uid, i))
    else:
        raise ValueError()
    wrapped_outputs = [
        wrap(obj=x, uid=uid_generator(i, x)) if not isinstance(x, Ref) else x
        for i, x in enumerate(outputs)
    ]
    return wrapped_outputs


def bind_inputs(args, kwargs, mode: str, func_op: FuncOp) -> Dict[str, Any]:
    """
    Given args and kwargs passed by the user from python, this adds defaults
    and returns a dict where they are indexed via internal names.
    """
    bound_args = func_op.py_sig.bind(*args, **kwargs)
    if mode == MODES.run:
        bound_args.apply_defaults()
    inputs_dict = dict(bound_args.arguments)
    input_tps = func_op.input_types

    if mode == MODES.query:
        for k, v in inputs_dict.items():
            if not isinstance(v, ValQuery):
                inputs_dict[k] = qwrap(obj=v, tp=input_tps[k])
    return inputs_dict


def format_as_outputs(
    outputs: Union[List[Ref], List[ValQuery]]
) -> Union[None, Any, Tuple[Any]]:
    if len(outputs) == 0:
        return None
    elif len(outputs) == 1:
        return outputs[0]
    else:
        return tuple(outputs)
