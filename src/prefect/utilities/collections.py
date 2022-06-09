"""
Utilities for extensions of and operations on Python collections.
"""
import itertools
from collections import OrderedDict, defaultdict
from collections.abc import Iterator as IteratorABC
from collections.abc import Sequence, Set
from dataclasses import fields, is_dataclass
from enum import Enum, auto
from functools import partial
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Generic,
    Hashable,
    Iterable,
    Iterator,
    List,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)
from unittest.mock import Mock

import pydantic

import prefect
from prefect.utilities.asyncio import gather


class AutoEnum(str, Enum):
    """
    An enum class that automatically generates value from variable names.

    This guards against common errors where variable names are updated but values are
    not.

    In addition, because AutoEnums inherit from `str`, they are automatically
    JSON-serializable.

    See https://docs.python.org/3/library/enum.html#using-automatic-values

    Example:
        ```python
        class MyEnum(AutoEnum):
            RED = AutoEnum.auto() # equivalent to RED = 'RED'
            BLUE = AutoEnum.auto() # equivalent to BLUE = 'BLUE'
        ```
    """

    def _generate_next_value_(name, start, count, last_values):
        return name

    @staticmethod
    def auto():
        """
        Exposes `enum.auto()` to avoid requiring a second import to use `AutoEnum`
        """
        return auto()

    def __repr__(self) -> str:
        return f"{type(self).__name__}.{self.value}"


KT = TypeVar("KT")
VT = TypeVar("VT")


def dict_to_flatdict(
    dct: Dict[KT, Union[Any, Dict[KT, Any]]], _parent: Tuple[KT, ...] = None
) -> Dict[Tuple[KT, ...], Any]:
    """Converts a (nested) dictionary to a flattened representation.

    Each key of the flat dict will be a CompoundKey tuple containing the "chain of keys"
    for the corresponding value.

    Args:
        dct (dict): The dictionary to flatten
        _parent (Tuple, optional): The current parent for recursion

    Returns:
        A flattened dict of the same type as dct
    """
    typ = cast(Type[Dict[Tuple[KT, ...], Any]], type(dct))
    items: List[Tuple[Tuple[KT, ...], Any]] = []
    parent = _parent or tuple()

    for k, v in dct.items():
        k_parent = tuple(parent + (k,))
        if isinstance(v, dict):
            items.extend(dict_to_flatdict(v, _parent=k_parent).items())
        else:
            items.append((k_parent, v))
    return typ(items)


def flatdict_to_dict(
    dct: Dict[Tuple[KT, ...], VT]
) -> Dict[KT, Union[VT, Dict[KT, VT]]]:
    """Converts a flattened dictionary back to a nested dictionary.

    Args:
        dct (dict): The dictionary to be nested. Each key should be a tuple of keys
            as generated by `dict_to_flatdict`

    Returns
        A nested dict of the same type as dct
    """
    typ = type(dct)
    result = cast(Dict[KT, Union[VT, Dict[KT, VT]]], typ())
    for key_tuple, value in dct.items():
        current_dict = result
        for prefix_key in key_tuple[:-1]:
            # Build nested dictionaries up for the current key tuple
            # Use `setdefault` in case the nested dict has already been created
            current_dict = current_dict.setdefault(prefix_key, typ())  # type: ignore
        # Set the value
        current_dict[key_tuple[-1]] = value

    return result


T = TypeVar("T")


def ensure_iterable(obj: Union[T, Iterable[T]]) -> Iterable[T]:
    if isinstance(obj, Sequence) or isinstance(obj, Set):
        return obj
    obj = cast(T, obj)  # No longer in the iterable case
    return [obj]


def listrepr(objs: Iterable, sep=" ") -> str:
    return sep.join(repr(obj) for obj in objs)


def extract_instances(
    objects: Iterable,
    types: Union[Type[T], Tuple[Type[T], ...]] = object,
) -> Union[List[T], Dict[Type[T], T]]:
    """
    Extract objects from a file and returns a dict of type -> instances

    Args:
        objects: An iterable of objects
        types: A type or tuple of types to extract, defaults to all objects

    Returns:
        If a single type is given: a list of instances of that type
        If a tuple of types is given: a mapping of type to a list of instances
    """
    types = ensure_iterable(types)

    # Create a mapping of type -> instance from the exec values
    ret = defaultdict(list)

    for o in objects:
        # We iterate here so that the key is the passed type rather than type(o)
        for type_ in types:
            if isinstance(o, type_):
                ret[type_].append(o)

    if len(types) == 1:
        return ret[types[0]]

    return ret


def batched_iterable(iterable: Iterable[T], size: int) -> Iterator[Tuple[T, ...]]:
    """
    Yield batches of a certain size from an iterable

    Args:
        iterable (Iterable): An iterable
        size (int): The batch size to return

    Yields:
        tuple: A batch of the iterable
    """
    it = iter(iterable)
    while True:
        batch = tuple(itertools.islice(it, size))
        if not batch:
            break
        yield batch


class Quote(Generic[T]):
    """
    Simple wrapper to mark an expression as a different type so it will not be coerced
    by Prefect. For example, if you want to return a state from a flow without having
    the flow assume that state.
    """

    def __init__(self, data: T) -> None:
        self.data = data

    def unquote(self) -> T:
        return self.data


def quote(expr: T) -> Quote[T]:
    """
    Create a `Quote` object

    Examples:
        >>> from prefect.utilities.collections import quote
        >>> x = quote(1)
        >>> x.unquote()
        1
    """
    return Quote(expr)


async def visit_collection(
    expr, visit_fn: Callable[[Any], Awaitable[Any]], return_data: bool = False
):
    """
    This function visits every element of an arbitrary Python collection. If an element
    is a Python collection, it will be visited recursively. If an element is not a
    collection, `visit_fn` will be called with the element. The return value of
    `visit_fn` can be used to alter the element if `return_data` is set.

    Note that when using `return_data` a copy of each collection is created to avoid
    mutating the original object. This may have significant performance penalities and
    should only be used if you intend to transform the collection.

    Supported types:
    - List
    - Tuple
    - Set
    - Dict (note: keys are also visited recursively)
    - Dataclass
    - Pydantic model

    Args:
        expr (Any): a Python object or expression
        visit_fn (Callable[[Any], Awaitable[Any]]): an async function that
            will be applied to every non-collection element of expr.
        return_data (bool): if `True`, a copy of `expr` containing data modified
            by `visit_fn` will be returned. This is slower than `return_data=False`
            (the default).
    """

    def visit_nested(expr):
        # Utility for a recursive call, preserving options.
        # Returns a `partial` for use with `gather`.
        return partial(
            visit_collection, expr, visit_fn=visit_fn, return_data=return_data
        )

    # Get the expression type; treat iterators like lists
    typ = list if isinstance(expr, IteratorABC) else type(expr)
    typ = cast(type, typ)  # mypy treats this as 'object' otherwise and complains

    # do not visit mock objects
    if isinstance(expr, Mock):
        return expr if return_data else None

    elif typ in (list, tuple, set):
        result = await gather(*[visit_nested(o) for o in expr])
        return typ(result) if return_data else None

    elif typ in (dict, OrderedDict):
        assert isinstance(expr, (dict, OrderedDict))  # typecheck assertion
        keys, values = zip(*expr.items()) if expr else ([], [])
        keys = await gather(*[visit_nested(k) for k in keys])
        values = await gather(*[visit_nested(v) for v in values])
        return typ(zip(keys, values)) if return_data else None

    elif is_dataclass(expr) and not isinstance(expr, type):
        values = await gather(
            *[visit_nested(getattr(expr, f.name)) for f in fields(expr)]
        )
        result = {field.name: value for field, value in zip(fields(expr), values)}
        return typ(**result) if return_data else None

    elif (
        # Recurse into Pydantic models but do _not_ do so for states/datadocs
        isinstance(expr, pydantic.BaseModel)
        and not isinstance(expr, prefect.orion.schemas.states.State)
        and not isinstance(expr, prefect.orion.schemas.data.DataDocument)
    ):
        # Pydantic does not expose Extras in `__fields__` and does not expose
        # private vars in `__dict__`, so we use of a a combination of `__dict__`
        # `__private_attributes__` instead.
        keys = [
            key
            for key in list(expr.__dict__.keys())
            + list(expr.__private_attributes__.keys())
        ]
        values = await gather(*[visit_nested(getattr(expr, key)) for key in keys])
        # breakpoint()
        result = {key: value for key, value in zip(keys, values)}
        model_result = {key: value for key, value in zip(keys, values) if value}
        breakpoint()
        if len(model_result) >= len(expr.__fields__):
            model_instance = typ(**model_result)
            for p_key in expr.__private_attributes__.keys():
                if result.get(p_key):
                    setattr(model_instance, p_key, result.get(p_key))
        else:
            model_instance = None
        return model_instance if return_data else None

    else:
        result = await visit_fn(expr)
        return result if return_data else None


M = TypeVar("M", bound=pydantic.BaseModel)


class PartialModel(Generic[M]):
    """
    A utility for creating a Pydantic model in several steps.

    Fields may be set at initialization, via attribute assignment, or at finalization
    when the concrete model is returned.

    Pydantic validation does not occur until finalization.

    Each field can only be set once and a `ValueError` will be raised on assignment if
    a field already has a value.

    Example:
        >>> class MyModel(pydantic.BaseModel):
        >>>     x: int
        >>>     y: str
        >>>     z: float
        >>>
        >>> partial_model = PartialModel(MyModel, x=1)
        >>> partial_model.y = "two"
        >>> model = partial_model.finalize(z=3.0)
    """

    def __init__(self, __model_cls: M, **kwargs: Any) -> None:
        self.fields = kwargs
        # Set fields first to avoid issues if `fields` is also set on the `model_cls`
        # in our custom `setattr` implementation.
        self.model_cls = __model_cls

        for name in kwargs.keys():
            self.raise_if_not_in_model(name)

    def finalize(self, **kwargs: Any) -> T:
        for name in kwargs.keys():
            self.raise_if_already_set(name)
            self.raise_if_not_in_model(name)
        return self.model_cls(**self.fields, **kwargs)

    def raise_if_already_set(self, name):
        if name in self.fields:
            raise ValueError(f"Field {name!r} has already been set.")

    def raise_if_not_in_model(self, name):
        if name not in self.model_cls.__fields__:
            raise ValueError(f"Field {name!r} is not present in the model.")

    def __setattr__(self, __name: str, __value: Any) -> None:
        if __name in {"fields", "model_cls"}:
            return super().__setattr__(__name, __value)

        self.raise_if_already_set(__name)
        self.raise_if_not_in_model(__name)
        self.fields[__name] = __value

    def __repr__(self) -> str:
        dsp_fields = ", ".join(f"{key}={repr(value)}" for key, value in self.fields)
        return f"PartialModel({self.model_cls.__name__}{dsp_fields})"


def remove_nested_keys(keys_to_remove: List[Hashable], obj):
    """
    Recurses a dictionary returns a copy without all keys that match an entry in
    `key_to_remove`. Return `obj` unchanged if not a dictionary.

    Args:
        keys_to_remove: A list of keys to remove from obj
        obj: The object to remove keys from.

    Returns:
        `obj` without keys matching an entry in `keys_to_remove` if `obj` is a dictionary.
        `obj` if `obj` is not a dictionary.
    """
    if not isinstance(obj, dict):
        return obj
    return {
        key: remove_nested_keys(keys_to_remove, value)
        for key, value in obj.items()
        if key not in keys_to_remove
    }
