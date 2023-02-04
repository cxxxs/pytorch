import functools
import inspect
import itertools
import logging
import math
import operator
import types
from typing import Dict, List

import torch
from torch import sym_float, sym_int

from .. import config, variables
from ..allowed_functions import is_allowed
from ..exc import unimplemented, Unsupported
from ..guards import GuardBuilder
from ..replay_record import DummyModule
from ..source import AttrSource, is_constant_source, SuperSource, TypeSource
from ..utils import (
    check_constant_args,
    check_unspec_python_args,
    istype,
    proxy_args_kwargs,
    specialize_args_kwargs,
)
from .base import MutableLocal, VariableTracker
from .constant import ConstantVariable
from .dicts import ConstDictVariable
from .lists import BaseListVariable, ListVariable, TupleVariable
from .tensor import DynamicShapeVariable, FakeItemVariable, UnspecializedPythonVariable
from .user_defined import UserDefinedVariable

log = logging.getLogger(__name__)


class BuiltinVariable(VariableTracker):
    @staticmethod
    @functools.lru_cache(None)
    def _constant_fold_functions():
        fns = {
            abs,
            all,
            any,
            bool,
            callable,
            chr,
            dict,
            divmod,
            float,
            int,
            len,
            list,
            max,
            min,
            ord,
            pow,
            repr,
            round,
            set,
            str,
            str.format,
            sum,
            tuple,
            type,
            operator.pos,
            operator.neg,
            operator.not_,
            operator.invert,
            operator.pow,
            operator.mul,
            operator.matmul,
            operator.floordiv,
            operator.truediv,
            operator.mod,
            operator.add,
            operator.sub,
            operator.getitem,
            operator.lshift,
            operator.rshift,
            operator.and_,
            operator.or_,
            operator.xor,
            operator.ipow,
            operator.imul,
            operator.imatmul,
            operator.ifloordiv,
            operator.itruediv,
            operator.imod,
            operator.iadd,
            operator.isub,
            operator.ilshift,
            operator.irshift,
            operator.iand,
            operator.ixor,
            operator.ior,
            operator.index,
        }
        fns.update(x for x in math.__dict__.values() if isinstance(x, type(math.sqrt)))
        return fns

    def can_constant_fold_through(self):
        return self.fn in self._constant_fold_functions()

    @staticmethod
    @functools.lru_cache(None)
    def _fx_graph_functions():
        fns = {
            operator.pos,
            operator.neg,
            operator.not_,
            operator.invert,
            operator.pow,
            operator.mul,
            operator.matmul,
            operator.floordiv,
            operator.truediv,
            operator.mod,
            operator.add,
            operator.sub,
            operator.getitem,
            operator.lshift,
            operator.rshift,
            operator.and_,
            operator.or_,
            operator.xor,
            operator.ipow,
            operator.imul,
            operator.imatmul,
            operator.ifloordiv,
            operator.itruediv,
            operator.imod,
            operator.iadd,
            operator.isub,
            operator.ilshift,
            operator.irshift,
            operator.iand,
            operator.ixor,
            operator.ior,
        }
        return fns

    @staticmethod
    @functools.lru_cache(None)
    def _reversible_binops():
        # function -> (forward magic method name, reverse magic method name)
        fns = {
            operator.add: ("__add__", "__radd__"),
            operator.sub: ("__sub__", "__rsub__"),
            operator.mul: ("__mul__", "__rmul__"),
            operator.truediv: ("__truediv__", "__rtruediv__"),
            operator.floordiv: ("__floordiv__", "__rfloordiv__"),
            operator.mod: ("__mod__", "__rmod__"),
            pow: ("__pow__", "__rpow__"),
            operator.pow: ("__pow__", "__rpow__"),
            # Don't support these for now, since the corresponding reverse magic methods
            # aren't defined on SymInt / SymFloat.
            # operator.matmul: ("__matmul__", "__rmatmul__"),
            # divmod: ("__divmod__", "__rdivmod__"),
            # operator.lshift: ("__lshift__", "__rlshift__"),
            # operator.rshift: ("__rshift__", "__rrshift__"),
            # operator.and_: ("__and__", "__rand__"),
            # operator.or_: ("__or__", "__ror__"),
            # operator.xor: ("__xor__", "__rxor__"),
        }
        return fns

    @staticmethod
    @functools.lru_cache(None)
    def _binop_handlers():
        # Multiple dispatch mechanism defining custom binop behavior for certain type
        # combinations. Handlers are attempted in order, and will be used if the type checks
        # match. They are expected to have the signature:
        # fn(tx, arg0: VariableTracker, arg1: VariableTracker, options) -> VariableTracker

        # Override table contains: op_fn -> [list of handlers]
        op_handlers = {}
        for (
            op,
            (forward_name, reverse_name),
        ) in BuiltinVariable._reversible_binops().items():
            handlers = []

            # User-defined args (highest precedence)
            def user_defined_handler(
                tx, a, b, options, forward_name=forward_name, reverse_name=reverse_name
            ):
                # Manually handle reversing logic if needed (e.g. call __radd__)

                # TODO: If we expand this to handle tensor args, we need to manually
                # handle cases like this:
                #
                # class A(int):
                #     def __radd__(self, other):
                #         print("woof")
                # torch.randn(3) + A(3)
                #
                # In this example, A.__radd__() is not called -> nothing is printed, because
                # Tensor.__add__ only does a subtype test against int and will ignore the subclass.
                # To be fully correct, we should not call A.__radd__() here, and there may be
                # other cases to reason about and add exceptions for.
                if isinstance(a, UserDefinedVariable):
                    return a.call_method(tx, forward_name, [b], {})
                else:
                    return b.call_method(tx, reverse_name, [a], {})

            handlers.append(
                ((UserDefinedVariable, VariableTracker), user_defined_handler)
            )
            handlers.append(
                ((VariableTracker, UserDefinedVariable), user_defined_handler)
            )

            # Dynamic shape args
            def dynamic_handler(tx, a, b, options, fn=op):
                from .builder import wrap_fx_proxy

                return wrap_fx_proxy(
                    tx,
                    tx.output.create_proxy(
                        "call_function", fn, *proxy_args_kwargs([a, b], {})
                    ),
                    **options,
                )

            handlers.append(((DynamicShapeVariable, VariableTracker), dynamic_handler))
            handlers.append(((VariableTracker, DynamicShapeVariable), dynamic_handler))

            op_handlers[op] = handlers

        # Special cases - lower precedence but still prefer these over constant folding

        # List-like addition (e.g. [1, 2] + [3, 4])
        list_like_addition_handlers = [
            # NB: Prefer the tuple-specific logic over base logic because of
            # some SizeVariable weirdness. Specifically, the tuple-specific logic
            # drops the subclass type (e.g. SizeVariable) and returns TupleVariables.
            (
                (TupleVariable, ConstantVariable),
                lambda tx, a, b, options: TupleVariable(
                    a.items + list(b.unpack_var_sequence(tx)), **options
                ),
            ),
            (
                (ConstantVariable, TupleVariable),
                lambda tx, a, b, options: TupleVariable(
                    list(a.unpack_var_sequence(tx)) + b.items, **options
                ),
            ),
            (
                (TupleVariable, TupleVariable),
                lambda tx, a, b, options: TupleVariable(a.items + b.items, **options),
            ),
            (
                (BaseListVariable, BaseListVariable),
                lambda tx, a, b, options: type(a)(a.items + b.items, **options),
            ),
        ]
        op_handlers[operator.add].extend(list_like_addition_handlers)

        # List-like expansion (e.g. [1, 2, 3] * 3)
        def expand_list_like(tx, lst, const, options):
            return lst.__class__(
                items=lst.items * const.as_python_constant(),
                mutable_local=MutableLocal(),
                **options,
            )

        list_like_expansion_handlers = [
            ((ListVariable, ConstantVariable), expand_list_like),
            ((TupleVariable, ConstantVariable), expand_list_like),
            (
                (ConstantVariable, ListVariable),
                lambda tx, a, b, options: expand_list_like(tx, b, a, options),
            ),
            (
                (ConstantVariable, TupleVariable),
                lambda tx, a, b, options: expand_list_like(tx, b, a, options),
            ),
        ]
        op_handlers[operator.mul].extend(list_like_expansion_handlers)

        return op_handlers

    @staticmethod
    def _find_binop_handler(op, a, b):
        handlers = BuiltinVariable._binop_handlers()
        if op not in handlers:
            return None

        # Return first handler that matches the type checks
        for ((type1, type2), handler) in handlers[op]:
            if isinstance(a, type1) and isinstance(b, type2):
                return handler

        return None

    def can_insert_in_graph(self):
        return self.fn in self._fx_graph_functions()

    def __init__(self, fn, **kwargs):
        super(BuiltinVariable, self).__init__(**kwargs)
        self.fn = fn

    def __str__(self):
        if self.fn is None:
            name = "None"
        else:
            name = self.fn.__name__

        return f"{self.__class__.__name__}({name})"

    def python_type(self):
        return type(self.fn)

    def as_python_constant(self):
        return self.fn

    def reconstruct(self, codegen):
        name = self.fn.__name__
        assert self.fn.__module__ == "builtins"
        assert name not in codegen.tx.f_globals, "shadowed global"
        return [codegen.create_load_global(name, add=True)]

    def constant_args(self, *args, **kwargs):
        return check_constant_args(args, kwargs)

    def tensor_args(self, *args, **kwargs):
        return any(
            isinstance(i, variables.TensorVariable)
            for i in itertools.chain(args, kwargs.values())
        ) and not any(
            isinstance(i, variables.GetAttrVariable)
            for i in itertools.chain(args, kwargs.values())
        )

    def unspec_python_args(self, *args, **kwargs):
        return check_unspec_python_args(args, kwargs)

    @staticmethod
    def unwrap_unspec_args_kwargs(args, kwargs):
        unwrapped_args = []
        unwrapped_kwargs = {}
        for x in args:
            if isinstance(
                x,
                (variables.UnspecializedPythonVariable,),
            ):
                unwrapped_args.append(x.raw_value)
            else:
                unwrapped_args.append(x.as_python_constant())
        for k, v in kwargs:
            if isinstance(
                x,
                (variables.UnspecializedPythonVariable,),
            ):
                unwrapped_kwargs.update({k: v.raw_value})
            else:
                unwrapped_kwargs.update({k: v.as_python_constant()})
        return unwrapped_args, unwrapped_kwargs

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        from .builder import wrap_fx_proxy, wrap_fx_proxy_cls

        constant_args = check_constant_args(args, kwargs)
        tensor_args = self.tensor_args(*args, **kwargs)
        unspec_python_args = self.unspec_python_args(*args, **kwargs)
        options = VariableTracker.propagate(self, args, kwargs.values())
        has_constant_handler = self.can_constant_fold_through() and (
            constant_args or unspec_python_args
        )
        assert isinstance(args, (list, tuple))
        assert isinstance(kwargs, dict)

        if (
            self.fn is operator.getitem
            and len(args) == 2
            and isinstance(args[1], variables.TensorVariable)
            and args[1].dtype == torch.bool
            and not config.dynamic_shapes
        ):
            unimplemented("dynamic Tensor.__getitem__(bool[])")

        # args[0] is list and args[1] is unspec
        if self.fn is operator.getitem and not isinstance(
            args[0], variables.TensorVariable
        ):
            tensor_args = False
            args, kwargs = specialize_args_kwargs(tx, args, kwargs)

        if (
            self.can_insert_in_graph()
            and tensor_args
            and not (
                self.fn is operator.getitem
                and isinstance(args[0], ConstDictVariable)
                and isinstance(args[1], variables.TensorVariable)
            )
        ):
            try:
                fn = self.fn
                if self.fn is operator.iadd and isinstance(
                    args[0], variables.ConstantVariable
                ):
                    # Work around weird bug in hf_T5
                    fn, args = operator.add, [args[1], args[0]]

                proxy = tx.output.create_proxy(
                    "call_function",
                    fn,
                    *proxy_args_kwargs(args, kwargs),
                )
                if any([isinstance(arg, FakeItemVariable) for arg in args]):
                    return wrap_fx_proxy_cls(
                        FakeItemVariable,
                        tx,
                        proxy,
                        **options,
                    )
                elif self.unspec_python_args(*args, **kwargs):
                    _args, _kwargs = self.unwrap_unspec_args_kwargs(args, kwargs)
                    raw_value = self.fn(*_args, **_kwargs)

                    need_unwrap = any(
                        x.need_unwrap
                        for x in itertools.chain(args, kwargs.values())
                        if isinstance(x, variables.UnspecializedPythonVariable)
                    )

                    return wrap_fx_proxy_cls(
                        UnspecializedPythonVariable,
                        tx,
                        proxy,
                        raw_value=raw_value,
                        need_unwrap=need_unwrap,
                        **options,
                    )
                elif all(isinstance(x, DynamicShapeVariable) for x in args):
                    return DynamicShapeVariable.create(tx, proxy, None, **options)
                else:
                    # Work around for vision_maskrcnn due to precision difference
                    # specialize the dividend when float divide by tensor
                    if self.fn is operator.truediv and isinstance(
                        args[0], variables.UnspecializedPythonVariable
                    ):
                        args[0] = args[0].convert_to_constant(tx)
                    return wrap_fx_proxy(tx, proxy, **options)

            except NotImplementedError:
                unimplemented(f"partial tensor op: {self} {args} {kwargs}")

        # Handle cases like int(torch.seed())
        # Also handle sym_float to sym_int cases
        if self.fn in (int, float) and isinstance(args[0], DynamicShapeVariable):
            fn_ = sym_int if self.fn is int else sym_float
            out = wrap_fx_proxy(
                tx=tx,
                proxy=tx.output.create_proxy(
                    "call_function",
                    fn_,
                    (args[0].as_proxy(),),
                    {},
                ),
                **options,
            )
            return out

        # Handle functions that are reversible (e.g. __add__ / __radd__)
        # NB: Tensor args are handled above and not here
        reversible_binops = self._reversible_binops()
        if self.fn in reversible_binops:
            assert len(kwargs) == 0 and len(args) == 2

            # Try to find a handler for the arg types; otherwise, fall through to constant handler
            binop_handler = BuiltinVariable._find_binop_handler(
                self.fn, args[0], args[1]
            )
            if binop_handler:
                return binop_handler(tx, args[0], args[1], options)

        handler = getattr(self, f"call_{self.fn.__name__}", None)
        if handler:
            try:
                inspect.signature(handler).bind(tx, *args, **kwargs)
            except TypeError as exc:
                if not has_constant_handler:
                    log.warning(
                        f"incorrect arg count {handler} {exc} and no constant handler"
                    )
                handler = None

        if handler:
            try:
                result = handler(tx, *args, **kwargs)
                if result is not None:
                    return result.add_options(options)
            except Unsupported as exc:
                if not has_constant_handler:
                    raise
                # Actually, we will handle this just fine
                exc.remove_from_stats()

        if has_constant_handler:
            args, kwargs = specialize_args_kwargs(tx, args, kwargs)
            # constant fold
            return variables.ConstantVariable(
                self.as_python_constant()(
                    *[x.as_python_constant() for x in args],
                    **{k: v.as_python_constant() for k, v in kwargs.items()},
                ),
                **options,
            )
        return super().call_function(tx, args, kwargs)

    def _call_min_max(self, tx, a, b):
        if self.tensor_args(a, b):
            if not isinstance(a, variables.TensorVariable):
                a, b = b, a
            assert isinstance(a, variables.TensorVariable)

            # result of an item call is a scalar convert to a tensor
            if isinstance(a, FakeItemVariable):
                a = variables.TorchVariable(torch.tensor).call_function(tx, [a], {})

            # Dynamic input does not get resolved, rather, gets stored as call_function
            if isinstance(a, DynamicShapeVariable):
                from .builder import wrap_fx_proxy

                return wrap_fx_proxy(
                    tx=tx,
                    proxy=tx.output.create_proxy(
                        "call_function",
                        self.fn,
                        *proxy_args_kwargs([a, b], {}),
                    ),
                    **VariableTracker.propagate(self, [a, b]),
                )

            # convert min/max to torch ops
            if b.is_python_constant():
                kwargs = {"min": b} if (self.fn is max) else {"max": b}
                result = variables.TorchVariable(torch.clamp).call_function(
                    tx, [a], kwargs
                )
            else:
                fn = {max: torch.maximum, min: torch.minimum}[self.fn]
                result = variables.TorchVariable(fn).call_function(tx, [a, b], {})

            # return unspec if both a, b are unspec or const
            if all(
                isinstance(
                    i,
                    (
                        variables.UnspecializedPythonVariable,
                        variables.ConstantVariable,
                    ),
                )
                for i in [a, b]
            ):

                if any([isinstance(val, FakeItemVariable) for val in [a, b]]):
                    return variables.FakeItemVariable.from_tensor_variable(result)

                if b.is_python_constant():
                    raw_b = b.as_python_constant()
                else:
                    raw_b = b.raw_value
                if self.fn is max:
                    raw_res = max(a.raw_value, raw_b)
                else:
                    raw_res = min(a.raw_value, raw_b)

                need_unwrap = any(
                    x.need_unwrap
                    for x in [a, b]
                    if isinstance(x, variables.UnspecializedPythonVariable)
                )
                return variables.UnspecializedPythonVariable.from_tensor_variable(
                    result, raw_res, need_unwrap
                )
            # otherwise return tensor
            else:
                return result
        elif isinstance(a, variables.ConstantVariable) and isinstance(
            b, variables.ConstantVariable
        ):
            if self.fn is max:
                return variables.ConstantVariable(max(a.value, b.value))
            else:
                return variables.ConstantVariable(min(a.value, b.value))
        elif isinstance(a, DynamicShapeVariable) or isinstance(b, DynamicShapeVariable):
            proxy = tx.output.create_proxy(
                "call_function", self.fn, *proxy_args_kwargs([a, b], {})
            )
            return DynamicShapeVariable.create(tx, proxy, None)
        else:

            unimplemented(f"unsupported min / max over args {str(a)}, {str(b)}")

    call_min = _call_min_max
    call_max = _call_min_max

    def call_range(self, tx, *args):
        if self.unspec_python_args(*args) or self.constant_args(*args):
            args, _ = specialize_args_kwargs(tx, args, {})
            return variables.RangeVariable(args)
        elif self._dynamic_args(*args):

            def guard_if_dyn(arg):
                if isinstance(arg, DynamicShapeVariable):
                    return arg.evaluate_expr(tx.output)
                return arg

            args = [variables.ConstantVariable(guard_if_dyn(arg)) for arg in args]
            return variables.RangeVariable(args)
        # None no-ops this handler and lets the driving function proceed
        return None

    def _dynamic_args(self, *args, **kwargs):
        return any([isinstance(x, DynamicShapeVariable) for x in args]) or any(
            [isinstance(x, DynamicShapeVariable) for x in kwargs.values()]
        )

    def call_slice(self, tx, *args):
        return variables.SliceVariable(args)

    def _dyn_proxy(self, tx, *args, **kwargs):
        from .builder import wrap_fx_proxy

        options = VariableTracker.propagate(self, args, kwargs.values())
        return wrap_fx_proxy(
            tx,
            tx.output.create_proxy(
                "call_function", self.fn, *proxy_args_kwargs(args, kwargs)
            ),
            **options,
        )

    def _call_iter_tuple_list(self, tx, obj=None, *args, **kwargs):
        if self._dynamic_args(*args, **kwargs):
            return self._dyn_proxy(tx, *args, **kwargs)
        cls = variables.BaseListVariable.cls_for(self.fn)
        if obj is None:
            return cls(
                [],
                mutable_local=MutableLocal(),
            )
        elif obj.has_unpack_var_sequence(tx):
            guards = set()
            if obj.source and not is_constant_source(obj.source):
                guards.add(obj.source.make_guard(GuardBuilder.LIST_LENGTH))
            return cls(
                list(obj.unpack_var_sequence(tx)),
                mutable_local=MutableLocal(),
                guards=guards,
            ).add_options(self, obj)

    call_iter = _call_iter_tuple_list
    call_tuple = _call_iter_tuple_list
    call_list = _call_iter_tuple_list

    def call_dict(self, tx, arg):
        if isinstance(arg, variables.ConstDictVariable):
            return arg.clone(mutable_local=MutableLocal())

    def call_zip(self, tx, *args):
        options = VariableTracker.propagate(self, args)
        if all(x.has_unpack_var_sequence(tx) for x in args):
            items = [
                variables.TupleVariable(list(item), **options)
                for item in zip(*[arg.unpack_var_sequence(tx) for arg in args])
            ]
            return variables.TupleVariable(items, **options)

    def call_enumerate(self, tx, *args):
        options = VariableTracker.propagate(self, args)
        if len(args) == 1:
            start = 0
        else:
            assert len(args) == 2
            assert isinstance(args[1], variables.ConstantVariable)
            start = args[1].as_python_constant()
        if args[0].has_unpack_var_sequence(tx):
            items = [
                variables.TupleVariable(
                    [variables.ConstantVariable(idx, **options), var],
                    **options,
                )
                for idx, var in enumerate(args[0].unpack_var_sequence(tx), start)
            ]
            return variables.TupleVariable(items, **options)

    def call_len(self, tx, *args, **kwargs):
        return args[0].call_method(tx, "__len__", args[1:], kwargs)

    def call_iadd(self, tx, *args, **kwargs):
        return args[0].call_method(tx, "__iadd__", args[1:], kwargs)

    def call_getitem(self, tx, *args, **kwargs):
        if self.unspec_python_args(*args, **kwargs):
            args, kwargs = specialize_args_kwargs(tx, args, kwargs)
        return args[0].call_method(tx, "__getitem__", args[1:], kwargs)

    def call_isinstance(self, tx, arg, isinstance_type):
        arg_type = arg.python_type()

        isinstance_type = isinstance_type.as_python_constant()

        if isinstance(arg, variables.TensorVariable) and arg.dtype is not None:
            return variables.ConstantVariable(arg.call_isinstance(isinstance_type))
        # UserDefinedObject with C extensions can have torch.Tensor attributes,
        # so break graph.
        if isinstance(arg, variables.UserDefinedObjectVariable) and isinstance(
            arg.value, types.MemberDescriptorType
        ):
            unimplemented(
                f"isinstance called on UserDefinedClass {arg} {isinstance_type}"
            )
        # handle __instancecheck__ defined in user class
        if (
            isinstance(arg, variables.UserDefinedObjectVariable)
            and "__instancecheck__" in isinstance_type.__class__.__dict__
        ):
            return variables.ConstantVariable(
                isinstance_type.__class__.__instancecheck__(isinstance_type, arg.value)
            )

        try:
            val = issubclass(arg_type, isinstance_type)
        except TypeError:
            val = arg_type is isinstance_type
        return variables.ConstantVariable(val)

    def call_super(self, tx, a, b):
        source = (
            None
            if a.source is None or b.source is None
            else SuperSource(a.source, b.source)
        )
        return variables.SuperVariable(a, b, source=source)

    def call_next(self, tx, arg):
        if isinstance(arg, variables.ListIteratorVariable):
            val, next_iter = arg.next_variables()
            tx.replace_all(arg, next_iter)
            return val
        elif isinstance(arg, variables.BaseListVariable):
            return arg.items[0].add_options(self, arg)

    def call_hasattr(self, tx, obj, attr):
        if attr.is_python_constant():
            name = attr.as_python_constant()
            return obj.call_hasattr(tx, name).add_options(self, obj, attr)

    def call_map(self, tx, fn, seq):
        if seq.has_unpack_var_sequence(tx):
            items = [fn.call_function(tx, [x], {}) for x in seq.unpack_var_sequence(tx)]
            return variables.TupleVariable(items).add_options(self, fn, seq)

    def call_sum(self, tx, seq, **kwargs):
        # Special case for sum on tuple of floats and ints
        if (
            isinstance(seq, (variables.ListVariable, variables.TupleVariable))
            and all(
                [
                    isinstance(x, variables.ConstantVariable)
                    and isinstance(x.value, (int, float))
                    for x in seq.items
                ]
            )
            and not kwargs
        ):
            new_list = [x.value for x in seq.items]
            return variables.ConstantVariable(sum(new_list))
        if seq.has_unpack_var_sequence(tx):
            start = kwargs.pop(
                "start", variables.ConstantVariable(0)
            ).as_python_constant()
            assert not kwargs
            items = seq.unpack_var_sequence(tx)[start:]
            return BuiltinVariable(functools.reduce).call_function(
                tx,
                [
                    BuiltinVariable(operator.add),
                    variables.TupleVariable(items),
                    variables.ConstantVariable(0).add_options(self, seq),
                ],
                {},
            )

    def call_reduce(self, tx, function, iterable, initializer=None):
        if iterable.has_unpack_var_sequence(tx):
            items = iterable.unpack_var_sequence(tx)
            if initializer is None:
                value, items = items[0], items[1:]
            else:
                value = initializer
            for element in items:
                value = function.call_function(tx, [value, element], {})
            return value

    def call_getattr(
        self, tx, obj: VariableTracker, name_var: VariableTracker, default=None
    ):
        from . import (
            ConstantVariable,
            GetAttrVariable,
            PythonModuleVariable,
            TorchVariable,
            UserFunctionVariable,
        )
        from .builder import VariableBuilder

        options = VariableTracker.propagate(self, obj, name_var)
        guards = options["guards"]
        name = name_var.as_python_constant()

        if not name_var.is_python_constant():
            unimplemented("non-const getattr() name")

        if tx.output.side_effects.is_attribute_mutation(obj):
            try:
                # re-read a pending side effect?
                return tx.output.side_effects.load_attr(obj, name).add_options(options)
            except KeyError:
                pass

        if default is not None:
            hasattr_var = self.call_hasattr(tx, obj, name_var)
            guards.update(hasattr_var.guards)
            assert hasattr_var.as_python_constant() in (True, False)
            if not hasattr_var.as_python_constant():
                return default.add_guards(guards)

        if obj.source:
            source = AttrSource(obj.source, name)
            options["source"] = source
        else:
            source = None

        if isinstance(obj, variables.NNModuleVariable):
            return obj.var_getattr(tx, name).add_options(options)
        elif isinstance(obj, variables.TensorVariable) and name == "grad":
            if source:
                # We are going to be raising this tensor as grapharg. So, ensure
                # that we have real grad value instead of fake tensor value.
                # Walk through the inputs of the subgraph and find if we already
                # have the original tensor stored in the graphargs.
                for grapharg in tx.output.graphargs:
                    if grapharg.source == source.base:
                        example_value = grapharg.example.grad
                        return VariableBuilder(tx, source)(example_value).add_options(
                            options
                        )
                unimplemented("tensor grad")
            else:
                unimplemented("tensor grad")
        elif isinstance(
            obj,
            (
                variables.TensorVariable,
                variables.NamedTupleVariable,
                variables.ConstantVariable,
                variables.UserDefinedClassVariable,
                variables.UserDefinedObjectVariable,
            ),
        ):
            try:
                return (
                    obj.var_getattr(tx, name).clone(source=source).add_options(options)
                )
            except NotImplementedError:
                return GetAttrVariable(obj, name, **options)
        elif isinstance(obj, TorchVariable):
            member = getattr(obj.value, name)
            if is_allowed(member):
                return TorchVariable(member, **options)
            elif ConstantVariable.is_literal(member):
                return ConstantVariable(member, **options)
            else:
                return VariableBuilder(tx, source)(member).add_guards(guards)
        elif isinstance(obj, (PythonModuleVariable, DummyModule)):
            member = obj.value.__dict__[name]

            if config.replay_record_enabled:
                tx.exec_recorder.record_module_access(obj.value, name, member)

            return VariableBuilder(tx, source)(member).add_guards(guards)
        elif istype(obj, UserFunctionVariable) and name in ("__name__", "__module__"):
            return ConstantVariable(
                getattr(obj.fn, name), **VariableTracker.propagate(obj)
            )
        else:
            try:
                return (
                    obj.var_getattr(tx, name).clone(source=source).add_options(options)
                )
            except NotImplementedError:
                return GetAttrVariable(obj, name, **options)

    def call_setattr(
        self, tx, obj: VariableTracker, name_var: VariableTracker, val: VariableTracker
    ):
        if isinstance(obj, (variables.BlackHoleVariable, variables.DataClassVariable)):
            return obj.call_method(tx, "__setattr__", [name_var, val], {})
        elif (
            tx.output.side_effects.is_attribute_mutation(obj)
            and name_var.is_python_constant()
        ):
            tx.output.side_effects.store_attr(obj, name_var.as_python_constant(), val)
            return val.add_options(self, obj, name_var)
        elif isinstance(obj, variables.UserDefinedObjectVariable):
            unimplemented(
                f"setattr(UserDefinedObjectVariable) {type(obj.value).__setattr__}"
            )
        elif isinstance(obj, variables.NNModuleVariable):
            obj.convert_to_unspecialized(tx)

    def call_type(self, tx, obj: VariableTracker):
        from .builder import VariableBuilder

        try:
            py_type = obj.python_type()
        except NotImplementedError:
            py_type = None

        if istype(obj, variables.TupleVariable):
            return BuiltinVariable(py_type).add_options(self, obj)

        if py_type is not None and obj.source:
            return VariableBuilder(tx, TypeSource(obj.source))(py_type).add_options(
                self, obj
            )

        unimplemented(f"type({obj})")

    def call_reversed(self, tx, obj: VariableTracker):
        if obj.has_unpack_var_sequence(tx):
            items = list(reversed(obj.unpack_var_sequence(tx)))
            return variables.TupleVariable(
                items, **VariableTracker.propagate(self, obj)
            )

    def call_chain(self, tx, *args):
        if all(obj.has_unpack_var_sequence(tx) for obj in args):
            items = []
            for obj in args:
                items.extend(obj.unpack_var_sequence(tx))
            return variables.TupleVariable(
                items, **VariableTracker.propagate(self, *args)
            )

    def call_islice(self, tx, iterable, *args):
        if iterable.has_unpack_var_sequence(tx) and all(
            x.is_python_constant() for x in args
        ):
            const_args = [x.as_python_constant() for x in args]
            items = iterable.unpack_var_sequence(tx)
            items = list(itertools.islice(items, *const_args))
            return variables.TupleVariable(
                items, **VariableTracker.propagate(self, iterable, *args)
            )

    # neg is a constant fold function, so we only get here if constant fold is not valid
    def call_neg(self, tx, a):
        if isinstance(a, DynamicShapeVariable):
            return DynamicShapeVariable.create(
                tx,
                (operator.neg)(a.as_proxy()),
                dyn_shape=None,
            )
        # None no-ops this handler and lets the driving function proceed
        return None

    def call_id(self, tx, *args):
        if len(args) > 0 and isinstance(args[0], variables.NNModuleVariable):
            nn_mod_variable = args[0]
            mod = tx.output.get_submodule(nn_mod_variable.module_key)
            return variables.ConstantVariable(id(mod))
        else:
            unimplemented(f"call_id with args {args}")

    def _comparison(self, tx, left, right):
        """
        Used to implement comparison operators for different types.
        For example, list1 < list2 is implemented differently from tensor1 < tensor2
        """
        from . import (
            BaseListVariable,
            ConstantVariable,
            TensorVariable,
            UserFunctionVariable,
        )
        from .tensor import (
            supported_const_comparison_ops,
            supported_tensor_comparison_ops,
        )

        op = self.fn

        def _unimplemented():
            unimplemented(f"comparison {typestr(left)} {op} {typestr(right)}")

        if isinstance(left, UserFunctionVariable):
            if op not in supported_const_comparison_ops.values():
                _unimplemented()
            if not isinstance(right, UserFunctionVariable):
                _unimplemented()
            return ConstantVariable(op(left.fn, right.fn))

        if isinstance(left, BaseListVariable):
            if not type(left) == type(right):  # Mismatch in BaseListVariable subclasses
                _unimplemented()
            return BaseListVariable.generic_list_compare(left, tx, op, right)

        if isinstance(left, TensorVariable):
            from .builder import wrap_fx_proxy

            if op not in supported_tensor_comparison_ops.values():
                _unimplemented()
            return wrap_fx_proxy(
                tx,
                op(left.as_proxy(), right.as_proxy()),
            )

        if isinstance(left, DynamicShapeVariable):
            if op not in supported_tensor_comparison_ops.values():
                _unimplemented()

            return DynamicShapeVariable.create(
                tx,
                op(left.as_proxy(), right.as_proxy()),
                dyn_shape=None,
            )

        _unimplemented()

    # and_ is a constant fold function, so we only get here if constant fold is not valid
    def call_and_(self, tx, a, b):
        if isinstance(a, DynamicShapeVariable) and isinstance(b, DynamicShapeVariable):
            return DynamicShapeVariable.create(
                tx,
                (operator.and_)(a.as_proxy(), b.as_proxy()),
                dyn_shape=None,
            )
        # None no-ops this handler and lets the driving function proceed
        return None

    call_eq = _comparison
    call_gt = _comparison
    call_lt = _comparison
    call_le = _comparison
    call_ne = _comparison
    call_is_ = _comparison
    call_is_not = _comparison
