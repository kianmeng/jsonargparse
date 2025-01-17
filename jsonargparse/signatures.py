"""Metods to add arguments based on class/method/function signatures."""

import inspect
import re
from argparse import SUPPRESS
from functools import wraps
from typing import Any, Callable, List, Optional, Set, Tuple, Type, Union

from .actions import _ActionConfigLoad
from .namespace import Namespace
from .typehints import ActionTypeHint, ClassType, is_optional, LazyInitBaseClass
from .typing import is_final_class
from .util import get_import_path, is_subclass
from .optionals import (
    dataclasses_support,
    docstring_parser_support,
    import_dataclasses,
    import_docstring_parse,
)


__all__ = [
    'class_from_function',
    'compose_dataclasses',
    'SignatureArguments',
]


kinds = inspect._ParameterKind
inspect_empty = inspect._empty


class SignatureArguments:
    """Methods to add arguments based on signatures to an ArgumentParser instance."""

    def add_class_arguments(
        self,
        theclass: Type,
        nested_key: Optional[str] = None,
        as_group: bool = True,
        as_positional: bool = False,
        default: Optional[LazyInitBaseClass] = None,
        skip: Optional[Set[str]] = None,
        instantiate: bool = True,
        fail_untyped: bool = True,
        sub_configs: bool = False,
        linked_targets: Optional[Set[str]] = None,
    ) -> List[str]:
        """Adds arguments from a class based on its type hints and docstrings.

        Note: Keyword arguments without at least one valid type are ignored.

        Args:
            theclass: Class from which to add arguments.
            nested_key: Key for nested namespace.
            as_group: Whether arguments should be added to a new argument group.
            as_positional: Whether to add required parameters as positional arguments.
            default: Default value used to override parameter defaults. Must be lazy_instance.
            skip: Names of parameters that should be skipped.
            instantiate: Whether the class group should be instantiated by :code:`instantiate_classes`.
            fail_untyped: Whether to raise exception if a required parameter does not have a type.
            sub_configs: Whether subclass type hints should be loadable from inner config file.

        Returns:
            The list of arguments added.

        Raises:
            ValueError: When not given a class.
            ValueError: When there are required parameters without at least one valid type.
        """
        if not inspect.isclass(theclass):
            raise ValueError(f'Expected "theclass" parameter to be a class type, got: {theclass}.')
        if default and not (isinstance(default, LazyInitBaseClass) and isinstance(default, theclass)):
            raise ValueError(f'Expected "default" parameter to be a lazy instance of the class, got: {default}.')

        skip_first = not is_subclass(theclass, ClassFromFunctionBase)

        added_args = self._add_signature_arguments(
            inspect.getmro(theclass),
            nested_key,
            as_group,
            as_positional,
            skip,
            fail_untyped,
            sub_configs=sub_configs,
            docs_func=get_class_init_and_base_docstrings,
            sign_func=get_class_signature_functions,
            instantiate=instantiate,
            linked_targets=linked_targets,
            skip_first=skip_first,
        )

        if default:
            skip = skip or set()
            prefix = nested_key+'.' if nested_key else ''
            defaults = default.lazy_get_init_data().as_dict()
            if defaults:
                defaults = {prefix+k: v for k, v in defaults.items() if k not in skip}
                self.set_defaults(**defaults)  # type: ignore

        return added_args


    def add_method_arguments(
        self,
        theclass: Type,
        themethod: str,
        nested_key: Optional[str] = None,
        as_group: bool = True,
        as_positional: bool = False,
        skip: Optional[Set[str]] = None,
        fail_untyped: bool = True,
        sub_configs: bool = False,
    ) -> List[str]:
        """Adds arguments from a class based on its type hints and docstrings.

        Note: Keyword arguments without at least one valid type are ignored.

        Args:
            theclass: Class which includes the method.
            themethod: Name of the method for which to add arguments.
            nested_key: Key for nested namespace.
            as_group: Whether arguments should be added to a new argument group.
            as_positional: Whether to add required parameters as positional arguments.
            skip: Names of parameters that should be skipped.
            fail_untyped: Whether to raise exception if a required parameter does not have a type.
            sub_configs: Whether subclass type hints should be loadable from inner config file.

        Returns:
            The list of arguments added.

        Raises:
            ValueError: When not given a class or the name of a method of the class.
            ValueError: When there are required parameters without at least one valid type.
        """
        if not inspect.isclass(theclass):
            raise ValueError('Expected "theclass" argument to be a class object.')
        if not hasattr(theclass, themethod) or not callable(getattr(theclass, themethod)):
            raise ValueError('Expected "themethod" argument to be a callable member of the class.')

        skip_first = not isinstance(inspect.getattr_static(theclass, themethod), staticmethod)
        themethods = [getattr(x, themethod) for x in inspect.getmro(theclass) if hasattr(x, themethod)]

        return self._add_signature_arguments(themethods,
                                             nested_key,
                                             as_group,
                                             as_positional,
                                             skip,
                                             fail_untyped,
                                             sub_configs=sub_configs,
                                             skip_first=skip_first)


    def add_function_arguments(
        self,
        function: Callable,
        nested_key: Optional[str] = None,
        as_group: bool = True,
        as_positional: bool = False,
        skip: Optional[Set[str]] = None,
        fail_untyped: bool = True,
        sub_configs: bool = False,
    ) -> List[str]:
        """Adds arguments from a function based on its type hints and docstrings.

        Note: Keyword arguments without at least one valid type are ignored.

        Args:
            function: Function from which to add arguments.
            nested_key: Key for nested namespace.
            as_group: Whether arguments should be added to a new argument group.
            as_positional: Whether to add required parameters as positional arguments.
            skip: Names of parameters that should be skipped.
            fail_untyped: Whether to raise exception if a required parameter does not have a type.
            sub_configs: Whether subclass type hints should be loadable from inner config file.

        Returns:
            The list of arguments added.

        Raises:
            ValueError: When not given a callable.
            ValueError: When there are required parameters without at least one valid type.
        """
        if not callable(function):
            raise ValueError('Expected "function" argument to be a callable object.')

        return self._add_signature_arguments([function],
                                             nested_key,
                                             as_group,
                                             as_positional,
                                             skip,
                                             fail_untyped,
                                             sub_configs=sub_configs)


    def _add_signature_arguments(
        self,
        objects,
        nested_key: Optional[str],
        as_group: bool,
        as_positional: bool,
        skip: Optional[Set[str]],
        fail_untyped: bool,
        sub_configs: bool = False,
        docs_func: Callable = lambda x: [x.__doc__],
        sign_func: Callable = lambda x: [(v, v) for v in x],  # type: ignore
        skip_first: bool = False,
        instantiate: bool = True,
        linked_targets: Optional[Set[str]] = None,
    ) -> List[str]:
        """Adds arguments from parameters of objects based on signatures and docstrings.

        Args:
            objects: Objects from which to add signatures.
            nested_key: Key for nested namespace.
            as_group: Whether arguments should be added to a new argument group.
            as_positional: Whether to add required parameters as positional arguments.
            skip: Names of parameters that should be skipped.
            fail_untyped: Whether to raise exception if a required parameter does not have a type.
            sub_configs: Whether subclass type hints should be loadable from inner config file.
            docs_func: Function that returns docstrings for a given object.
            sign_func: Function that returns signature functions for a given object.
            skip_first: Whether to skip first argument, i.e., skip self of class methods.
            instantiate: Whether the class group should be instantiated by :code:`instantiate_classes`.

        Returns:
            The list of arguments added.

        Raises:
            ValueError: When there are required parameters without at least one valid type.
        """

        def update_has_args_kwargs(base, has_args=True, has_kwargs=True):
            params = list(inspect.signature(base).parameters.values())
            has_args &= any(p._kind == kinds.VAR_POSITIONAL for p in params)
            has_kwargs &= any(p._kind == kinds.VAR_KEYWORD for p in params)
            return has_args, has_kwargs

        ## Determine propagation of arguments ##
        signatures = sign_func(objects)
        add_types = [(True, True)]
        has_args, has_kwargs = update_has_args_kwargs(signatures[0][1])
        for num in range(1, len(signatures)):
            if not (has_args or has_kwargs):
                signatures = signatures[:num]
                break
            add_types.append((has_args, has_kwargs))
            has_args, has_kwargs = update_has_args_kwargs(signatures[num][1], has_args, has_kwargs)

        ## Gather docstrings ##
        doc_group, doc_params = self._gather_docstrings([s[0] for s in signatures], docs_func)

        ## Create group if requested ##
        group = self._create_group_if_requested(objects[0], nested_key, as_group, doc_group, instantiate=instantiate)

        ## Add objects arguments ##
        added_args: List[str] = []
        if skip is None:
            skip = set()
        for (obj, func), (add_args, add_kwargs) in zip(signatures, add_types):
            for num, param in enumerate(inspect.signature(func).parameters.values()):
                if skip_first and num == 0:
                    continue
                self._add_signature_parameter(
                    group,
                    nested_key,
                    param,
                    obj,
                    doc_params,
                    added_args,
                    skip,
                    fail_untyped=fail_untyped,
                    sub_configs=sub_configs,
                    linked_targets=linked_targets or set(),
                    as_positional=as_positional,
                    add_args=add_args,
                    add_kwargs=add_kwargs,
                )

        return added_args


    def _add_signature_parameter(
        self,
        group,
        nested_key: Optional[str],
        param,
        obj: Any,
        doc_params: dict,
        added_args: List[str],
        skip: Set[str],
        fail_untyped: bool = True,
        as_positional: bool = False,
        sub_configs: bool = False,
        instantiate: bool = True,
        linked_targets: Optional[Set[str]] = None,
        add_args: bool = True,
        add_kwargs: bool = True,
        default: Any = inspect_empty,
        **kwargs
    ):
        name = param.name
        kind = param._kind
        annotation = param.annotation
        if default == inspect_empty:
            default = param.default
        is_required = default == inspect_empty
        skip_message = f'Skipping parameter "{name}" from "{getattr(obj, "__name__", obj)}" because of: '
        if not fail_untyped and annotation == inspect_empty:
            annotation = Any
            default = None if is_required else default
            is_required = False
        if is_required and linked_targets is not None and name in linked_targets:
            default = None
            is_required = False
        if kind in {kinds.VAR_POSITIONAL, kinds.VAR_KEYWORD} or \
           (not is_required and name[0] == '_') or \
           (annotation == inspect_empty and not is_required and default is None):
            return
        elif is_required and not add_args:
            self.logger.debug(skip_message+'Positional parameter but *args not propagated.')  # type: ignore
            return
        elif not is_required and not add_kwargs:
            self.logger.debug(skip_message+'Keyword parameter but **kwargs not propagated.')  # type: ignore
            return
        elif name in skip:
            self.logger.debug(skip_message+'Parameter requested to be skipped.')  # type: ignore
            return
        if is_factory_class(default):
            default = obj.__dataclass_fields__[name].default_factory()
        if annotation == inspect_empty and not is_required:
            annotation = type(default)
        if 'help' not in kwargs:
            kwargs['help'] = doc_params.get(name)
        if not is_required:
            kwargs['default'] = default
            if default is None and not is_optional(annotation, object):
                annotation = Optional[annotation]
        elif not as_positional:
            kwargs['required'] = True
        is_subclass_typehint = False
        is_final_class_typehint = is_final_class(annotation)
        dest = (nested_key+'.' if nested_key else '') + name
        args = [dest if is_required and as_positional else '--'+dest]
        if annotation in {str, int, float, bool} or \
           is_subclass(annotation, (str, int, float)) or \
           is_final_class_typehint or \
           is_pure_dataclass(annotation):
            kwargs['type'] = annotation
        elif annotation != inspect_empty:
            try:
                is_subclass_typehint = ActionTypeHint.is_subclass_typehint(annotation)
                kwargs['type'] = annotation
                sub_add_kwargs = None
                if is_subclass_typehint:
                    prefix = name + '.init_args.'
                    subclass_skip = {s[len(prefix):] for s in skip if s.startswith(prefix)}
                    sub_add_kwargs = {'fail_untyped': fail_untyped, 'skip': subclass_skip}
                args = ActionTypeHint.prepare_add_argument(
                    args=args,
                    kwargs=kwargs,
                    enable_path=is_subclass_typehint and sub_configs,
                    container=group,
                    sub_add_kwargs=sub_add_kwargs,
                )
            except ValueError as ex:
                self.logger.debug(skip_message+str(ex))  # type: ignore
        if 'type' in kwargs or 'action' in kwargs:
            if dest in added_args:
                self.logger.debug(skip_message+'Argument already added.')  # type: ignore
            else:
                sub_add_kwargs = {
                    'fail_untyped': fail_untyped,
                    'sub_configs': sub_configs,
                    'instantiate': instantiate,
                }
                if is_final_class_typehint:
                    kwargs.update(sub_add_kwargs)
                action = group.add_argument(*args, **kwargs)
                action.sub_add_kwargs = sub_add_kwargs
                if is_subclass_typehint and len(subclass_skip) > 0:
                    action.sub_add_kwargs['skip'] = subclass_skip
                added_args.append(dest)
        elif is_required and fail_untyped:
            raise ValueError(f'Required parameter without a type for {obj} parameter "{name}".')


    def add_dataclass_arguments(
        self,
        theclass: Type,
        nested_key: str,
        default: Union[Type, dict] = None,
        as_group: bool = True,
        **kwargs
    ) -> List[str]:
        """Adds arguments from a dataclass based on its field types and docstrings.

        Args:
            theclass: Class from which to add arguments.
            nested_key: Key for nested namespace.
            default: Value for defaults. Must be instance of or kwargs for theclass.
            as_group: Whether arguments should be added to a new argument group.

        Returns:
            The list of arguments added.

        Raises:
            ValueError: When not given a dataclass.
            ValueError: When default is not instance of or kwargs for theclass.
        """
        dataclasses = import_dataclasses('add_dataclass_arguments')
        if not is_pure_dataclass(theclass):
            raise ValueError(f'Expected "theclass" argument to be a pure dataclass, given {theclass}')

        doc_group, doc_params = self._gather_docstrings([theclass], get_class_init_and_base_docstrings)
        for key in ['help', 'title']:
            if key in kwargs and kwargs[key] is not None:
                doc_group = strip_title(kwargs.pop(key))
        group = self._create_group_if_requested(theclass, nested_key, as_group, doc_group, config_load_type=theclass)

        defaults = {}
        if default is not None:
            if isinstance(default, dict):
                try:
                    default = theclass(**default)
                except TypeError:
                    pass
            if not isinstance(default, theclass):
                raise ValueError(f'Expected "default" argument to be an instance of "{theclass.__name__}" or its kwargs dict, given {default}')
            defaults = dataclasses.asdict(default)

        added_args: List[str] = []
        skip: Set[str] = set()
        params = inspect.signature(theclass.__init__).parameters
        for field in dataclasses.fields(theclass):
            self._add_signature_parameter(
                group,
                nested_key,
                params[field.name],
                theclass,
                doc_params,
                added_args,
                skip,
                default=defaults.get(field.name, inspect_empty),
            )

        return added_args


    def add_subclass_arguments(
        self,
        baseclass: Union[Type, Tuple[Type, ...]],
        nested_key: str,
        as_group: bool = True,
        skip: Optional[Set[str]] = None,
        instantiate: bool = True,
        required: bool = False,
        metavar: str = 'CONFIG | CLASS_NAME_OR_PATH | .ARG_NAME VALUE',
        help: str = 'One or more arguments specifying "class_path" and "init_args" for any subclass of %(baseclass_name)s.',
        **kwargs
    ):
        """Adds arguments to allow specifying any subclass of the given base class.

        This adds an argument that requires a dictionary with a "class_path"
        entry which must be a import dot notation expression. Optionally any
        init arguments for the class can be given in the "init_args" entry.
        Since subclasses can have different init arguments, the help does not
        show the details of the arguments of the base class. Instead a help
        argument is added that will print the details for a given class path.

        Args:
            baseclass: Base class or classes to use to check subclasses.
            nested_key: Key for nested namespace.
            as_group: Whether arguments should be added to a new argument group.
            skip: Names of parameters that should be skipped.
            required: Whether the argument group is required.
            metavar: Variable string to show in the argument's help.
            help: Description of argument to show in the help.
            **kwargs: Additional parameters like in add_class_arguments.

        Raises:
            ValueError: When given an invalid base class.
        """
        if is_final_class(baseclass):
            raise ValueError("Not allowed for classes that are final.")
        if type(baseclass) is not tuple:
            baseclass = (baseclass,)  # type: ignore
        if not all(inspect.isclass(c) for c in baseclass):
            raise ValueError('Expected "baseclass" argument to be a class or a tuple of classes.')

        doc_group = self._gather_docstrings(baseclass, get_class_init_and_base_docstrings)[0]
        group = self._create_group_if_requested(
            baseclass,
            nested_key,
            as_group,
            doc_group,
            config_load=False,
            required=required,
            instantiate=False,
        )

        added_args: List[str] = []
        if skip is None:
            skip = set()
        else:
            skip = {nested_key+'.init_args.'+s for s in skip}
        param = Namespace(name=nested_key, _kind=None, annotation=Union[baseclass])
        str_baseclass = '{' + ', '.join(get_import_path(x) for x in baseclass) + '}'
        kwargs.update({
            'metavar': metavar,
            'help': (help % {'baseclass_name': str_baseclass}),
        })
        if 'default' not in kwargs:
            kwargs['default'] = SUPPRESS
        self._add_signature_parameter(
            group,
            None,
            param,
            Union[baseclass],
            {},
            added_args,
            skip,
            sub_configs=True,
            instantiate=instantiate,
            **kwargs
        )


    def _gather_docstrings(self, objects, docs_func):
        doc_group = None
        doc_params = {}
        if docstring_parser_support:
            docstring_parse, docstring_error = import_docstring_parse('_gather_docstrings', True)
            for base in objects:
                for doc in docs_func(base):
                    try:
                        docstring = docstring_parse(doc)
                    except (ValueError, docstring_error) as ex:
                        self.logger.debug(f'Failed parsing docstring for {base}: {ex}')
                    else:
                        if docstring.short_description and not doc_group:
                            doc_group = docstring.short_description
                        for param in docstring.params:
                            if param.arg_name not in doc_params:
                                doc_params[param.arg_name] = param.description
        return strip_title(doc_group), doc_params


    def _create_group_if_requested(self, obj, nested_key, as_group, doc_group, config_load=True, config_load_type=None, required=False, instantiate=True):
        if required:
            if nested_key is None:
                raise ValueError('A nested_key is mandatory to make required.')
            self.required_args.add(nested_key)

        group = self
        if as_group:
            if doc_group is None:
                doc_group = str(obj)
            name = obj.__name__ if nested_key is None else nested_key
            group = self.add_argument_group(doc_group, name=name)
            if config_load and nested_key is not None:
                group.add_argument('--'+nested_key, action=_ActionConfigLoad(basetype=config_load_type))
            if inspect.isclass(obj) and nested_key is not None and instantiate:
                group.dest = nested_key
                group.group_class = obj
                group.instantiate_class = group_instantiate_class
        return group


def group_instantiate_class(group, cfg):
    try:
        value, parent, key = cfg.get_value_and_parent(group.dest)
    except KeyError:
        value = {}
        parent = cfg
        key = group.dest
        assert '.' not in key
    parent[key] = group.group_class(**value)


def get_class_init_and_base_docstrings(value):
    return [value.__init__.__doc__, value.__doc__]


def get_class_signature_functions(classes):
    signatures = []
    for num, cls in enumerate(classes):
        if cls.__new__ is not object.__new__ and not any(cls.__new__ is c.__new__ for c in classes[num+1:]):
            signatures.append((cls, cls.__new__))
        if not any(cls.__init__ is c.__init__ for c in classes[num+1:]):
            signatures.append((cls, cls.__init__))
    return signatures


def strip_title(value):
    if value is not None:
        return re.sub(r'\.$', '', value.strip())


def is_factory_class(value):
    result = False
    if dataclasses_support:
        dataclasses = import_dataclasses('is_default_factory_class')
        result = value.__class__ == dataclasses._HAS_DEFAULT_FACTORY_CLASS
    return result


def is_pure_dataclass(value):
    if not dataclasses_support or not inspect.isclass(value):
        return False
    dataclasses = import_dataclasses('is_pure_dataclass')
    classes = [c for c in inspect.getmro(value) if c != object]
    return all(dataclasses.is_dataclass(c) for c in classes)


def compose_dataclasses(*args):
    """Returns a pure dataclass inheriting all given dataclasses and properly handling __post_init__."""
    dataclasses = import_dataclasses('compose_dataclasses')

    @dataclasses.dataclass
    class ComposedDataclass(*args):
        def __post_init__(self):
            for arg in args:
                if hasattr(arg, '__post_init__'):
                    arg.__post_init__(self)

    return ComposedDataclass


class ClassFromFunctionBase:
    pass


def class_from_function(func: Callable[..., ClassType]) -> Type[ClassType]:
    """Creates a dynamic class which if instantiated is equivalent to calling func.

    Args:
        func: A function that returns an instance of a class. It must have a return type annotation.
    """
    func_return = inspect.signature(func).return_annotation
    if isinstance(func_return, str):
        caller_frame = inspect.currentframe().f_back  # type: ignore
        func_return = caller_frame.f_locals.get(func_return) or caller_frame.f_globals.get(func_return)  # type: ignore
        if func_return is None:
            raise ValueError(f'Unable to dereference {func_return} the return type of {func}.')

    @wraps(func)
    def __new__(cls, *args, **kwargs):
        return func(*args, **kwargs)

    class ClassFromFunction(func_return, ClassFromFunctionBase):  # type: ignore
        pass

    ClassFromFunction.__new__ = __new__  # type: ignore
    ClassFromFunction.__doc__ = func.__doc__
    ClassFromFunction.__name__ = func.__name__
    return ClassFromFunction
