"""A abstract virtual machine for python bytecode.

A VM for python byte code that uses pytype/pytd/cfg to generate a trace of the
program execution.
"""

# Because pytype takes too long:
# pytype: skip-file

# We have names like "byte_NOP":
# pylint: disable=invalid-name

# Bytecodes don't always use all their arguments:
# pylint: disable=unused-argument

import abc
import collections
import contextlib
import itertools
import logging
import os
import re
import reprlib
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from pytype import blocks
from pytype import compare
from pytype import constant_folding
from pytype import datatypes
from pytype import directors
from pytype import overlay_dict
from pytype import load_pytd
from pytype import metaclass
from pytype import metrics
from pytype import overlay as overlay_lib
from pytype import state as frame_state
from pytype import utils
from pytype.abstract import abstract
from pytype.abstract import abstract_utils
from pytype.abstract import class_mixin
from pytype.abstract import function
from pytype.abstract import mixin
from pytype.pyc import loadmarshal
from pytype.pyc import opcodes
from pytype.pyc import pyc
from pytype.pyi import parser
from pytype.pytd import mro
from pytype.pytd import slots
from pytype.pytd import visitors
from pytype.typegraph import cfg
from pytype.typegraph import cfg_utils

log = logging.getLogger(__name__)

_FUNCTION_TYPE_COMMENT_RE = re.compile(r"^\((.*)\)\s*->\s*(\S.*?)\s*$")

# Create a repr that won't overflow.
_TRUNCATE = 120
_TRUNCATE_STR = 72
repr_obj = reprlib.Repr()
repr_obj.maxother = _TRUNCATE
repr_obj.maxstring = _TRUNCATE_STR
repper = repr_obj.repr


Block = collections.namedtuple("Block", ["type", "level"])


class LocalOp(collections.namedtuple("_LocalOp", ["name", "op"])):
  ASSIGN = 1
  ANNOTATE = 2

  def is_assign(self):
    return self.op == self.ASSIGN

  def is_annotate(self):
    return self.op == self.ANNOTATE


_opcode_counter = metrics.MapCounter("vm_opcode")


class VirtualMachineError(Exception):
  """For raising errors in the operation of the VM."""


class _FindIgnoredTypeComments:
  """A visitor that finds type comments that will be ignored."""

  def __init__(self, type_comments):
    self._type_comments = type_comments
    # Lines will be removed from this set during visiting. Any lines that remain
    # at the end are type comments that will be ignored.
    self._ignored_type_lines = set(type_comments)

  def visit_code(self, code):
    """Interface for pyc.visit."""
    for op in code.code_iter:
      # Make sure we have attached the type comment to an opcode.
      if isinstance(op, blocks.STORE_OPCODES):
        if op.annotation:
          annot = op.annotation
          if self._type_comments.get(op.line) == annot:
            self._ignored_type_lines.discard(op.line)
      elif isinstance(op, opcodes.MAKE_FUNCTION):
        if op.annotation:
          _, line = op.annotation
          self._ignored_type_lines.discard(line)
    return code

  def ignored_lines(self):
    """Returns a set of lines that contain ignored type comments."""
    return self._ignored_type_lines


class _FinallyStateTracker:
  """Track return state for try/except/finally blocks."""
  # Used in vm.run_frame()

  RETURN_STATES = ("return", "exception")

  def __init__(self):
    self.stack = []

  def process(self, op, state, ctx) -> Optional[str]:
    """Store state.why, or return it from a stored state."""
    if ctx.vm.is_setup_except(op):
      self.stack.append([op, None])
    if isinstance(op, opcodes.END_FINALLY):
      if self.stack:
        _, why = self.stack.pop()
        if why:
          return why
    elif self.stack and state.why in self.RETURN_STATES:
      self.stack[-1][-1] = state.why

  def check_early_exit(self, state) -> bool:
    """Check if we are exiting the frame from within an except block."""
    return (state.block_stack and
            any(x.type == "finally" for x in state.block_stack) and
            state.why in self.RETURN_STATES)

  def __repr__(self):
    return repr(self.stack)


class _NameErrorDetails(abc.ABC):
  """Base class for detailed name error messages."""

  @abc.abstractmethod
  def to_error_message(self) -> str:
    ...


class _NameInInnerClassErrorDetails(_NameErrorDetails):

  def __init__(self, attr, class_name):
    self._attr = attr
    self._class_name = class_name

  def to_error_message(self):
    return (f"Cannot reference {self._attr!r} from class {self._class_name!r} "
            "before the class is fully defined")


class _NameInOuterClassErrorDetails(_NameErrorDetails):
  """Name error details for a name defined in an outer class."""

  def __init__(self, attr, prefix, class_name):
    self._attr = attr
    self._prefix = prefix
    self._class_name = class_name

  def to_error_message(self):
    full_attr_name = f"{self._class_name}.{self._attr}"
    if self._prefix:
      full_class_name = f"{self._prefix}.{self._class_name}"
    else:
      full_class_name = self._class_name
    return (f"Use {full_attr_name!r} to reference {self._attr!r} from class "
            f"{full_class_name!r}")


class _NameInOuterFunctionErrorDetails(_NameErrorDetails):

  def __init__(self, attr, outer_scope, inner_scope):
    self._attr = attr
    self._outer_scope = outer_scope
    self._inner_scope = inner_scope

  def to_error_message(self):
    keyword = "global" if "global" in self._outer_scope else "nonlocal"
    return (f"Add `{keyword} {self._attr}` in {self._inner_scope} to reference "
            f"{self._attr!r} from {self._outer_scope}")


class VirtualMachine:
  """A bytecode VM that generates a cfg as it executes."""

  # This class is defined inside VirtualMachine so abstract.py can use it.
  class VirtualMachineRecursionError(Exception):
    pass

  def __init__(self, ctx):
    """Construct a TypegraphVirtualMachine."""
    self.ctx = ctx  # context.Context
    # The call stack of frames.
    self.frames: List[Union[frame_state.Frame, frame_state.SimpleFrame]] = []
    # The current frame.
    self.frame: Union[frame_state.Frame, frame_state.SimpleFrame] = None
    # A map from names to the late annotations that depend on them. Every
    # LateAnnotation depends on a single undefined name, so once that name is
    # defined, we immediately resolve the annotation.
    self.late_annotations: Dict[str, List[abstract.LateAnnotation]] = (
        collections.defaultdict(list))
    # Memoize which overlays are loaded.
    self.loaded_overlays: Dict[str, overlay_lib.Overlay] = {}
    self.has_unknown_wildcard_imports: bool = False
    # pyformat: disable
    self.opcode_traces: List[Tuple[
        Optional[opcodes.Opcode],
        Any,
        Tuple[Optional[abstract.BaseValue], ...]
    ]] = []
    # pyformat: enable
    # Track the order of creation of local vars, for attrs and dataclasses.
    self.local_ops: Dict[str, List[LocalOp]] = {}
    # Record the annotated and original values of locals.
    self.annotated_locals: Dict[str, Dict[str, abstract_utils.Local]] = {}
    self.filename: str = None

    self._maximum_depth = None  # set by run_program() and analyze()
    self._functions_type_params_check = []
    self._concrete_classes = []
    self._director = None
    self._analyzing = False  # Are we in self.analyze()?
    self._importing = False  # Are we importing another file?
    self._trace_opcodes = True  # whether to trace opcodes
    self._fold_constants = True
    # If set, we will generate LateAnnotations with this stack rather than
    # logging name errors.
    self._late_annotations_stack = None
    # Mapping of Variables to python variable names. {id: int -> name: str}
    # Note that we don't need to scope this to the frame because we don't reuse
    # variable ids.
    self._var_names = {}

  @property
  def current_local_ops(self):
    return self.local_ops[self.frame.f_code.co_name]

  @property
  def current_annotated_locals(self):
    return self.annotated_locals[self.frame.f_code.co_name]

  @contextlib.contextmanager
  def _suppress_opcode_tracing(self):
    old_trace_opcodes = self._trace_opcodes
    self._trace_opcodes = False
    try:
      yield
    finally:
      self._trace_opcodes = old_trace_opcodes

  @contextlib.contextmanager
  def generate_late_annotations(self, stack):
    old_late_annotations_stack = self._late_annotations_stack
    self._late_annotations_stack = stack
    try:
      yield
    finally:
      self._late_annotations_stack = old_late_annotations_stack

  def trace_opcode(self, op, symbol, val):
    """Record trace data for other tools to use."""
    if not self._trace_opcodes:
      return

    if self.frame and not op:
      op = self.frame.current_opcode
    if not op:
      # If we don't have a current opcode, don't emit a trace.
      return

    def get_data(v):
      data = getattr(v, "data", None)
      # Sometimes v is a binding.
      return [data] if data and not isinstance(data, list) else data

    # isinstance(val, tuple) generates false positives for internal classes that
    # are namedtuples.
    if val.__class__ == tuple:
      data = tuple(get_data(v) for v in val)
    else:
      data = (get_data(val),)
    rec = (op, symbol, data)
    self.opcode_traces.append(rec)

  def lookup_builtin(self, name):
    try:
      return self.ctx.loader.builtins.Lookup(name)
    except KeyError:
      return self.ctx.loader.typing.Lookup(name)

  def remaining_depth(self):
    return self._maximum_depth - len(self.frames)

  def is_at_maximum_depth(self):
    return len(self.frames) > self._maximum_depth

  def run_instruction(self, op, state):
    """Run a single bytecode instruction.

    Args:
      op: An opcode, instance of pyc.opcodes.Opcode
      state: An instance of state.FrameState, the state just before running
        this instruction.
    Returns:
      A tuple (why, state). "why" is the reason (if any) that this opcode aborts
      this function (e.g. through a 'raise'), or None otherwise. "state" is the
      FrameState right after this instruction that should roll over to the
      subsequent instruction.
    Raises:
      VirtualMachineError: if a fatal error occurs.
    """
    _opcode_counter.inc(op.name)
    self.frame.current_opcode = op
    self._importing = "IMPORT" in op.__class__.__name__
    if log.isEnabledFor(logging.INFO):
      self.log_opcode(op, state)
    # dispatch
    bytecode_fn = getattr(self, "byte_%s" % op.name, None)
    if bytecode_fn is None:
      raise VirtualMachineError("Unknown opcode: %s" % op.name)
    state = bytecode_fn(state, op)
    if state.why in ("reraise", "NoReturn"):
      state = state.set_why("exception")
    self.frame.current_opcode = None
    return state

  def run_frame(self, frame, node, annotated_locals=None):
    """Run a frame (typically belonging to a method)."""
    self.push_frame(frame)
    frame.states[frame.f_code.first_opcode] = frame_state.FrameState.init(
        node, self.ctx)
    frame_name = frame.f_code.co_name
    if frame_name not in self.local_ops or frame_name != "<module>":
      # abstract_utils.eval_expr creates a temporary frame called "<module>". We
      # don't care to track locals for this frame and don't want it to overwrite
      # the locals of the actual module frame.
      self.local_ops[frame_name] = []
      self.annotated_locals[frame_name] = annotated_locals or {}
    else:
      assert annotated_locals is None
    can_return = False
    return_nodes = []
    finally_tracker = _FinallyStateTracker()
    for block in frame.f_code.order:
      state = frame.states.get(block[0])
      if not state:
        log.warning("Skipping block %d, nothing connects to it.", block.id)
        continue
      self.frame.current_block = block
      op = None
      for op in block:
        state = self.run_instruction(op, state)
        # Check if we have to carry forward the return state from an except
        # block to the END_FINALLY opcode.
        new_why = finally_tracker.process(op, state, self.ctx)
        if new_why:
          state = state.set_why(new_why)
        if state.why:
          # we can't process this block any further
          break
      if state.why:
        # If we raise an exception or return in an except block do not
        # execute any target blocks it has added.
        if finally_tracker.check_early_exit(state):
          for target in self.frame.targets[block.id]:
            del frame.states[target]
        # return, raise, or yield. Leave the current frame.
        can_return |= state.why in ("return", "yield")
        return_nodes.append(state.node)
      elif op.carry_on_to_next():
        # We're starting a new block, so start a new CFG node. We don't want
        # nodes to overlap the boundary of blocks.
        state = state.forward_cfg_node()
        frame.states[op.next] = state.merge_into(frame.states.get(op.next))
    self._update_excluded_types(node)
    self.pop_frame(frame)
    if not return_nodes:
      # Happens if the function never returns. (E.g. an infinite loop)
      assert not frame.return_variable.bindings
      frame.return_variable.AddBinding(self.ctx.convert.unsolvable, [], node)
    else:
      node = self.ctx.join_cfg_nodes(return_nodes)
      if not can_return:
        assert not frame.return_variable.bindings
        # We purposely don't check NoReturn against this function's
        # annotated return type. Raising an error in an unimplemented function
        # and documenting the intended return type in an annotation is a
        # common pattern.
        self._set_frame_return(node, frame,
                               self.ctx.convert.no_return.to_variable(node))
    return node, frame.return_variable

  def _update_excluded_types(self, node):
    """Update the excluded_types attribute of functions in the current frame."""
    if not self.frame.func:
      return
    func = self.frame.func.data
    if isinstance(func, abstract.BoundFunction):
      func = func.underlying
    if not isinstance(func, abstract.SignedFunction):
      return
    # If we have code like:
    #   def f(x: T):
    #     def g(x: T): ...
    # then TypeVar T needs to be added to both f and g's excluded_types
    # attribute to avoid 'appears only once in signature' errors for T.
    # Similarly, any TypeVars that appear in variable annotations in a
    # function body also need to be added to excluded_types.
    for name, local in self.current_annotated_locals.items():
      typ = local.get_type(node, name)
      if typ:
        func.signature.excluded_types.update(
            p.name for p in self.ctx.annotation_utils.get_type_parameters(typ))
      if local.orig:
        for v in local.orig.data:
          if isinstance(v, abstract.BoundFunction):
            v = v.underlying
          if isinstance(v, abstract.SignedFunction):
            v.signature.excluded_types |= func.signature.type_params
            func.signature.excluded_types |= v.signature.type_params

  def push_block(self, state, t, level=None):
    if level is None:
      level = len(state.data_stack)
    return state.push_block(Block(t, level))

  def push_frame(self, frame):
    self.frames.append(frame)
    self.frame = frame

  def pop_frame(self, frame):
    popped_frame = self.frames.pop()
    assert popped_frame == frame
    if self.frames:
      self.frame = self.frames[-1]
    else:
      self.frame = None

  def module_name(self):
    if self.frame.f_code.co_filename:
      return ".".join(re.sub(
          r"\.py$", "", self.frame.f_code.co_filename).split(os.sep)[-2:])
    else:
      return ""

  def log_opcode(self, op, state):
    """Write a multi-line log message, including backtrace and stack."""
    if not log.isEnabledFor(logging.INFO):
      return
    indent = " > " * (len(self.frames) - 1)
    stack_rep = repper(state.data_stack)
    block_stack_rep = repper(state.block_stack)
    module_name = self.module_name()
    if module_name:
      name = self.frame.f_code.co_name
      log.info("%s | index: %d, %r, module: %s line: %d",
               indent, op.index, name, module_name, op.line)
    else:
      log.info("%s | index: %d, line: %d",
               indent, op.index, op.line)
    log.info("%s | data_stack: %s", indent, stack_rep)
    log.info("%s | block_stack: %s", indent, block_stack_rep)
    log.info("%s | node: <%d>%s", indent, state.node.id, state.node.name)
    log.info("%s ## %s", indent, utils.maybe_truncate(str(op), _TRUNCATE))

  def repper(self, s):
    return repr_obj.repr(s)

  def _call(
      self, state, obj, method_name, args
  ) -> Tuple[frame_state.FrameState, cfg.Variable]:
    state, method = self.load_attr(state, obj, method_name)
    return self.call_function_with_state(state, method, args)

  def _process_base_class(self, node, base):
    """Process a base class for InterpreterClass creation."""
    new_base = self.ctx.program.NewVariable()
    for b in base.bindings:
      base_val = b.data
      if isinstance(b.data, abstract.AnnotationContainer):
        base_val = base_val.base_cls
      # A class like `class Foo(List["Foo"])` would lead to infinite recursion
      # when instantiated because we attempt to recursively instantiate its
      # parameters, so we replace any late annotations with Any.
      # TODO(rechen): only replace the current class's name. We should keep
      # other late annotations in order to support things like:
      #   class Foo(List["Bar"]): ...
      #   class Bar: ...
      base_val = self.ctx.annotation_utils.remove_late_annotations(base_val)
      if isinstance(base_val, abstract.Union):
        # Union[A,B,...] is a valid base class, but we need to flatten it into a
        # single base variable.
        for o in base_val.options:
          new_base.AddBinding(o, {b}, node)
      else:
        new_base.AddBinding(base_val, {b}, node)
    base = new_base
    if not any(isinstance(t, (class_mixin.Class, abstract.AMBIGUOUS_OR_EMPTY))
               for t in base.data):
      self.ctx.errorlog.base_class_error(self.frames, base)
    return base

  def _filter_out_metaclasses(self, bases):
    """Process the temporary classes created by six.with_metaclass.

    six.with_metaclass constructs an anonymous class holding a metaclass and a
    list of base classes; if we find instances in `bases`, store the first
    metaclass we find and remove all metaclasses from `bases`.

    Args:
      bases: The list of base classes for the class being constructed.

    Returns:
      A tuple of (metaclass, base classes)
    """
    non_meta = []
    meta = None
    for base in bases:
      with_metaclass = False
      for b in base.data:
        if isinstance(b, metaclass.WithMetaclassInstance):
          with_metaclass = True
          if not meta:
            # Only the first metaclass gets applied.
            meta = b.cls.to_variable(self.ctx.root_node)
          non_meta.extend(b.bases)
      if not with_metaclass:
        non_meta.append(base)
    return meta, non_meta

  def _expand_generic_protocols(self, node, bases):
    """Expand Protocol[T, ...] to Protocol, Generic[T, ...]."""
    expanded_bases = []
    for base in bases:
      if any(abstract_utils.is_generic_protocol(b) for b in base.data):
        protocol_base = self.ctx.program.NewVariable()
        generic_base = self.ctx.program.NewVariable()
        generic_cls = self.ctx.convert.name_to_value("typing.Generic")
        for b in base.bindings:
          if abstract_utils.is_generic_protocol(b.data):
            protocol_base.AddBinding(b.data.base_cls, {b}, node)
            generic_base.AddBinding(
                abstract.ParameterizedClass(generic_cls,
                                            b.data.formal_type_parameters,
                                            self.ctx, b.data.template), {b},
                node)
          else:
            protocol_base.PasteBinding(b)
        expanded_bases.append(protocol_base)
        expanded_bases.append(generic_base)
      else:
        expanded_bases.append(base)
    return expanded_bases

  def make_class(self, node, name_var, bases, class_dict_var, cls_var,
                 new_class_var=None, is_decorated=False, class_type=None):
    """Create a class with the name, bases and methods given.

    Args:
      node: The current CFG node.
      name_var: Class name.
      bases: Base classes.
      class_dict_var: Members of the class, as a Variable containing an
          abstract.Dict value.
      cls_var: The class's metaclass, if any.
      new_class_var: If not None, make_class() will return new_class_var with
          the newly constructed class added as a binding. Otherwise, a new
          variable if returned.
      is_decorated: True if the class definition has a decorator.
      class_type: The internal type to build an instance of. Defaults to
          abstract.InterpreterClass. If set, must be a subclass of
          abstract.InterpreterClass.


    Returns:
      A node and an instance of class_type.
    """
    name = abstract_utils.get_atomic_python_constant(name_var)
    log.info("Declaring class %s", name)
    try:
      class_dict = abstract_utils.get_atomic_value(class_dict_var)
    except abstract_utils.ConversionError:
      log.error("Error initializing class %r", name)
      return self.ctx.convert.create_new_unknown(node)
    # Handle six.with_metaclass.
    metacls, bases = self._filter_out_metaclasses(bases)
    if metacls:
      cls_var = metacls
    # Flatten Unions in the bases
    bases = [self._process_base_class(node, base) for base in bases]
    # Expand Protocol[T, ...] to Protocol, Generic[T, ...]
    bases = self._expand_generic_protocols(node, bases)
    if not bases:
      # A parent-less class inherits from classobj in Python 2 and from object
      # in Python 3.
      base = self.ctx.convert.object_type
      bases = [base.to_variable(self.ctx.root_node)]
    if (isinstance(class_dict, abstract.Unsolvable) or
        not isinstance(class_dict, mixin.PythonConstant)):
      # An unsolvable appears here if the vm hit maximum depth and gave up on
      # analyzing the class we're now building. Otherwise, if class_dict isn't
      # a constant, then it's an abstract dictionary, and we don't have enough
      # information to continue building the class.
      var = self.ctx.new_unsolvable(node)
    else:
      if cls_var is None:
        cls_var = class_dict.members.get("__metaclass__")
        if cls_var:
          # This way of declaring metaclasses no longer works in Python 3.
          self.ctx.errorlog.ignored_metaclass(
              self.frames, name,
              cls_var.data[0].full_name if cls_var.bindings else "Any")
      if cls_var and all(v.data.full_name == "builtins.type"
                         for v in cls_var.bindings):
        cls_var = None
      # pylint: disable=g-long-ternary
      cls = abstract_utils.get_atomic_value(
          cls_var, default=self.ctx.convert.unsolvable) if cls_var else None
      if ("__annotations__" not in class_dict.members and
          name in self.annotated_locals):
        # Stores type comments in an __annotations__ member as if they were
        # PEP 526-style variable annotations, so that we can type-check
        # attribute assignments.
        annotations_dict = self.annotated_locals[name]
        if any(local.typ for local in annotations_dict.values()):
          annotations_member = abstract.AnnotationsDict(
              annotations_dict, self.ctx).to_variable(node)
          class_dict.members["__annotations__"] = annotations_member
          class_dict.pyval["__annotations__"] = annotations_member
      try:
        if not class_type:
          class_type = abstract.InterpreterClass
        elif class_type is not abstract.InterpreterClass:
          assert issubclass(class_type, abstract.InterpreterClass)
        val = class_type(name, bases, class_dict.pyval, cls, self.ctx)
        val.is_decorated = is_decorated
      except mro.MROError as e:
        self.ctx.errorlog.mro_error(self.frames, name, e.mro_seqs)
        var = self.ctx.new_unsolvable(node)
      except abstract_utils.GenericTypeError as e:
        self.ctx.errorlog.invalid_annotation(self.frames, e.annot, e.error)
        var = self.ctx.new_unsolvable(node)
      else:
        if new_class_var:
          var = new_class_var
        else:
          var = self.ctx.program.NewVariable()
        var.AddBinding(val, class_dict_var.bindings, node)
        node = val.call_metaclass_init(node)
        node = val.call_init_subclass(node)
        if not val.is_abstract:
          # Since a class decorator could have made the class inherit from
          # ABCMeta, we have to mark concrete classes now and check for
          # abstract methods at postprocessing time.
          self._concrete_classes.append((val, self.simple_stack()))
    self.trace_opcode(None, name, var)
    return node, var

  def _check_defaults(self, node, method):
    """Check parameter defaults against annotations."""
    if not method.signature.has_param_annotations:
      return
    _, args = self.create_method_arguments(node, method, use_defaults=True)
    positional_names = method.get_positional_names()
    # We may need to call match_args multiple times to find all type errors.
    needs_checking = True
    while needs_checking:
      try:
        method.match_args(node, args)
      except function.FailedFunctionCall as e:
        if not isinstance(e, function.InvalidParameters):
          raise AssertionError("Unexpected argument matching error: %s" %
                               e.__class__.__name__) from e
        arg_name = e.bad_call.bad_param.name
        expected_type = e.bad_call.bad_param.expected
        for name, value in e.bad_call.passed_args:
          if name != arg_name:
            continue
          if value == self.ctx.convert.ellipsis:
            # `...` should be a valid default parameter value for overloads.
            # Unfortunately, the is_overload attribute is not yet set when
            # _check_defaults runs, so we instead check that the method body is
            # empty. As a side effect, `...` is allowed as a default value for
            # any method that does nothing except return None.
            should_report = not method.has_empty_body()
          else:
            should_report = True
          if should_report:
            self.ctx.errorlog.annotation_type_mismatch(self.frames,
                                                       expected_type,
                                                       value.to_binding(node),
                                                       arg_name)
          # Replace the bad default with Any so we can call match_args again to
          # find other type errors.
          try:
            pos = positional_names.index(name)
          except ValueError:
            args.namedargs[name] = self.ctx.new_unsolvable(node)
          else:
            args = args._replace(posargs=args.posargs[:pos] +
                                 (self.ctx.new_unsolvable(node),) +
                                 args.posargs[pos + 1:])
          break
        else:
          raise AssertionError(
              "Mismatched parameter %s not found in passed_args" %
              arg_name) from e
      else:
        needs_checking = False

  def _make_function(self, name, node, code, globs, defaults, kw_defaults,
                     closure=None, annotations=None):
    """Create a function or closure given the arguments."""
    if closure:
      closure = tuple(
          c for c in abstract_utils.get_atomic_python_constant(closure))
      log.info("closure: %r", closure)
    if not name:
      name = abstract_utils.get_atomic_python_constant(code).co_name
    if not name:
      name = "<lambda>"
    val = abstract.InterpreterFunction.make(
        name,
        code=abstract_utils.get_atomic_python_constant(code),
        f_locals=self.frame.f_locals,
        f_globals=globs,
        defaults=defaults,
        kw_defaults=kw_defaults,
        closure=closure,
        annotations=annotations,
        ctx=self.ctx)
    var = self.ctx.program.NewVariable()
    var.AddBinding(val, code.bindings, node)
    self._check_defaults(node, val)
    if val.signature.annotations:
      self._functions_type_params_check.append((val, self.frame.current_opcode))
    return var

  def make_native_function(self, name, method):
    return abstract.NativeFunction(name, method, self.ctx)

  def make_frame(
      self, node, code, f_globals, f_locals, callargs=None, closure=None,
      new_locals=False, func=None, first_arg=None, substs=()):
    """Create a new frame object, using the given args, globals and locals."""
    if any(code is f.f_code for f in self.frames):
      log.info("Detected recursion in %s", code.co_name or code.co_filename)
      raise self.VirtualMachineRecursionError()

    log.info("make_frame: callargs=%s, f_globals=[%s@%x], f_locals=[%s@%x]",
             self.repper(callargs),
             type(f_globals).__name__, id(f_globals),
             type(f_locals).__name__, id(f_locals))

    # Implement NEWLOCALS flag. See Objects/frameobject.c in CPython.
    # (Also allow to override this with a parameter, Python 3 doesn't always set
    #  it to the right value, e.g. for class-level code.)
    if code.has_newlocals() or new_locals:
      f_locals = abstract.LazyConcreteDict("locals", {}, self.ctx)

    return frame_state.Frame(node, self.ctx, code, f_globals, f_locals,
                             self.frame, callargs or {}, closure, func,
                             first_arg, substs)

  def simple_stack(self, opcode=None):
    """Get a stack of simple frames.

    Args:
      opcode: Optionally, an opcode to create a stack for.

    Returns:
      If an opcode is provided, a stack with a single frame at that opcode.
      Otherwise, the VM's current stack converted to simple frames.
    """
    if opcode is not None:
      return (frame_state.SimpleFrame(opcode),)
    elif self.frame:
      # Simple stacks are used for things like late annotations, which don't
      # need tracebacks in their errors, so we convert just the current frame.
      return (frame_state.SimpleFrame(self.frame.current_opcode),)
    else:
      return ()

  def stack(self, func):
    """Get a frame stack for the given function for error reporting."""
    if isinstance(func, abstract.INTERPRETER_FUNCTION_TYPES) and (
        not self.frame or not self.frame.current_opcode):
      return self.simple_stack(func.get_first_opcode())
    else:
      return self.frames

  def push_abstract_exception(self, state):
    tb = self.ctx.convert.build_list(state.node, [])
    value = self.ctx.convert.create_new_unknown(state.node)
    exctype = self.ctx.convert.create_new_unknown(state.node)
    return state.push(tb, value, exctype)

  def resume_frame(self, node, frame):
    frame.f_back = self.frame
    log.info("resume_frame: %r", frame)
    node, val = self.run_frame(frame, node)
    frame.f_back = None
    return node, val

  def compile_src(self, src, filename=None, mode="exec"):
    """Compile the given source code."""
    code = pyc.compile_src(
        src,
        python_version=self.ctx.python_version,
        python_exe=self.ctx.options.python_exe,
        filename=filename,
        mode=mode)
    code = blocks.process_code(code, self.ctx.python_version)
    if mode == "exec":
      self._director.adjust_line_numbers(code)
    return blocks.merge_annotations(code, self._director.annotations,
                                    self._director.docstrings)

  def run_bytecode(self, node, code, f_globals=None, f_locals=None):
    """Run the given bytecode."""
    if f_globals is not None:
      assert f_locals
    else:
      assert not self.frames
      assert f_locals is None
      # __name__, __doc__, and __package__ are unused placeholder values.
      f_globals = f_locals = abstract.LazyConcreteDict(
          "globals", {
              "__builtins__": self.ctx.loader.builtins,
              "__name__": "__main__",
              "__file__": code.co_filename,
              "__doc__": None,
              "__package__": None,
          }, self.ctx)
      # __name__ is retrieved by class bodies. So make sure that it's preloaded,
      # otherwise we won't properly cache the first class initialization.
      f_globals.load_lazy_attribute("__name__")
    frame = self.make_frame(node, code, f_globals=f_globals, f_locals=f_locals)
    node, return_var = self.run_frame(frame, node)
    return node, frame.f_globals, frame.f_locals, return_var

  def run_program(self, src, filename, maximum_depth):
    """Run the code and return the CFG nodes.

    Args:
      src: The program source code.
      filename: The filename the source is from.
      maximum_depth: Maximum depth to follow call chains.
    Returns:
      A tuple (CFGNode, set) containing the last CFGNode of the program as
        well as all the top-level names defined by it.
    """
    director = directors.Director(
        src, self.ctx.errorlog, filename, self.ctx.options.disable,
        self.ctx.python_version)

    # This modifies the errorlog passed to the constructor.  Kind of ugly,
    # but there isn't a better way to wire both pieces together.
    self.ctx.errorlog.set_error_filter(director.should_report_error)
    self._director = director
    self.filename = filename

    self._maximum_depth = maximum_depth

    code = self.compile_src(src, filename=filename)
    visitor = _FindIgnoredTypeComments(self._director.type_comments)
    pyc.visit(code, visitor)
    for line in visitor.ignored_lines():
      self.ctx.errorlog.ignored_type_comment(self.filename, line,
                                             self._director.type_comments[line])

    if self._fold_constants:
      # Disabled while the feature is in development.
      code = constant_folding.optimize(code)

    node = self.ctx.root_node.ConnectNew("init")
    node, f_globals, f_locals, _ = self.run_bytecode(node, code)
    logging.info("Done running bytecode, postprocessing globals")
    # Check for abstract methods on non-abstract classes.
    for val, frames in self._concrete_classes:
      if not val.is_abstract:
        for member in sum((var.data for var in val.members.values()), []):
          if isinstance(member, abstract.Function) and member.is_abstract:
            unwrapped = abstract_utils.maybe_unwrap_decorated_function(member)
            name = unwrapped.data[0].name if unwrapped else member.name
            self.ctx.errorlog.ignored_abstractmethod(frames, val.name, name)
    for annot in itertools.chain.from_iterable(self.late_annotations.values()):
      # If `annot` has already been resolved, this is a no-op. Otherwise, it
      # contains a real name error that will be logged when we resolve it now.
      annot.resolve(node, f_globals, f_locals)
    self.late_annotations = None  # prevent adding unresolvable annotations
    assert not self.frames, "Frames left over!"
    log.info("Final node: <%d>%s", node.id, node.name)
    return node, f_globals.members

  def _base(self, cls):
    if isinstance(cls, abstract.ParameterizedClass):
      return cls.base_cls
    return cls

  def _overrides(self, node, subcls, supercls, attr):
    """Check whether subcls_var overrides or newly defines the given attribute.

    Args:
      node: The current node.
      subcls: A potential subclass.
      supercls: A potential superclass.
      attr: An attribute name.

    Returns:
      True if subcls_var is a subclass of supercls_var and overrides or newly
      defines the attribute. False otherwise.
    """
    if subcls and supercls and supercls in subcls.mro:
      subcls = self._base(subcls)
      supercls = self._base(supercls)
      for cls in subcls.mro:
        if cls == supercls:
          break
        if isinstance(cls, mixin.LazyMembers):
          cls.load_lazy_attribute(attr)
        if attr in cls.members and cls.members[attr].bindings:
          return True
    return False

  def get_var_name(self, var):
    """Get the python variable name corresponding to a Variable."""
    # Variables in _var_names correspond to LOAD_* opcodes, which means they
    # have been retrieved from a symbol table like locals() directly by name.
    if var.id in self._var_names:
      return self._var_names[var.id]
    # Look through the source set of a variable's bindings to find the variable
    # created by a LOAD operation. If a variable has multiple sources, don't try
    # to match it to a name.
    sources = set()
    for b in var.bindings:
      for o in b.origins:
        for s in o.source_sets:
          sources |= s
    if len(sources) == 1:
      s = next(iter(sources))
      return self._var_names.get(s.variable.id)
    return None

  def _call_binop_on_bindings(self, node, name, xval, yval):
    """Call a binary operator on two cfg.Binding objects."""
    rname = slots.REVERSE_NAME_MAPPING.get(name)
    if rname and isinstance(xval.data, abstract.AMBIGUOUS_OR_EMPTY):
      # If the reverse operator is possible and x is ambiguous, then we have no
      # way of determining whether __{op} or __r{op}__ is called.  Technically,
      # the result is also unknown if y is ambiguous, but it is almost always
      # reasonable to assume that, e.g., "hello " + y is a string, even though
      # y could define __radd__.
      return node, self.ctx.program.NewVariable([self.ctx.convert.unsolvable],
                                                [xval, yval], node)
    options = [(xval, yval, name)]
    if rname:
      options.append((yval, xval, rname))
      if self._overrides(node, yval.data.cls, xval.data.cls, rname):
        # If y is a subclass of x and defines its own reverse operator, then we
        # need to try y.__r{op}__ before x.__{op}__.
        options.reverse()
    error = None
    for left_val, right_val, attr_name in options:
      if (isinstance(left_val.data, class_mixin.Class) and
          attr_name == "__getitem__"):
        # We're parameterizing a type annotation. Set valself to None to
        # differentiate this action from a real __getitem__ call on the class.
        valself = None
      else:
        valself = left_val
      node, attr_var = self.ctx.attribute_handler.get_attribute(
          node, left_val.data, attr_name, valself)
      if attr_var and attr_var.bindings:
        args = function.Args(posargs=(right_val.AssignToNewVariable(),))
        try:
          return function.call_function(
              self.ctx, node, attr_var, args, fallback_to_unsolvable=False)
        except (function.DictKeyMissing, function.FailedFunctionCall) as e:
          # It's possible that this call failed because the function returned
          # NotImplemented.  See, e.g.,
          # test_operators.ReverseTest.check_reverse(), in which 1 {op} Bar()
          # ends up using Bar.__r{op}__. Thus, we need to save the error and
          # try the other operator.
          if e > error:
            error = e
    if error:
      raise error  # pylint: disable=raising-bad-type
    else:
      return node, None

  def call_binary_operator(self, state, name, x, y, report_errors=False):
    """Map a binary operator to "magic methods" (__add__ etc.)."""
    results = []
    log.debug("Calling binary operator %s", name)
    nodes = []
    error = None
    for xval in x.bindings:
      for yval in y.bindings:
        try:
          node, ret = self._call_binop_on_bindings(state.node, name, xval, yval)
        except (function.DictKeyMissing, function.FailedFunctionCall) as e:
          if e > error:
            error = e
        else:
          if ret:
            nodes.append(node)
            results.append(ret)
    if nodes:
      state = state.change_cfg_node(self.ctx.join_cfg_nodes(nodes))
    result = self.ctx.join_variables(state.node, results)
    log.debug("Result: %r %r", result, result.data)
    if not result.bindings and report_errors:
      if error is None:
        if self.ctx.options.report_errors:
          self.ctx.errorlog.unsupported_operands(self.frames, name, x, y)
        result = self.ctx.new_unsolvable(state.node)
      elif isinstance(error, function.DictKeyMissing):
        state, result = error.get_return(state)
      else:
        if self.ctx.options.report_errors:
          self.ctx.errorlog.invalid_function_call(self.frames, error)
        state, result = error.get_return(state)
    return state, result

  def call_inplace_operator(self, state, iname, x, y):
    """Try to call a method like __iadd__, possibly fall back to __add__."""
    state, attr = self.load_attr_noerror(state, x, iname)
    if attr is None:
      log.info("No inplace operator %s on %r", iname, x)
      name = iname.replace("i", "", 1)  # __iadd__ -> __add__ etc.
      state = state.forward_cfg_node()
      state, ret = self.call_binary_operator(
          state, name, x, y, report_errors=True)
    else:
      # TODO(b/159039220): If x is a Variable with distinct types, both __add__
      # and __iadd__ might happen.
      try:
        state, ret = self.call_function_with_state(state, attr, (y,),
                                                   fallback_to_unsolvable=False)
      except function.FailedFunctionCall as e:
        self.ctx.errorlog.invalid_function_call(self.frames, e)
        state, ret = e.get_return(state)
    return state, ret

  def binary_operator(self, state, name, report_errors=True):
    state, (x, y) = state.popn(2)
    with self._suppress_opcode_tracing():  # don't trace the magic method call
      state, ret = self.call_binary_operator(
          state, name, x, y, report_errors=report_errors)
    self.trace_opcode(None, name, ret)
    return state.push(ret)

  def inplace_operator(self, state, name):
    state, (x, y) = state.popn(2)
    state, ret = self.call_inplace_operator(state, name, x, y)
    return state.push(ret)

  def trace_unknown(self, *args):
    """Fired whenever we create a variable containing 'Unknown'."""
    return NotImplemented

  def trace_call(self, *args):
    """Fired whenever we call a builtin using unknown parameters."""
    return NotImplemented

  def trace_functiondef(self, *args):
    return NotImplemented

  def trace_classdef(self, *args):
    return NotImplemented

  def trace_namedtuple(self, *args):
    return NotImplemented

  def call_init(self, node, unused_instance):
    # This dummy implementation is overwritten in analyze.py.
    return node

  def init_class(self, node, cls, extra_key=None):
    # This dummy implementation is overwritten in analyze.py.
    del cls, extra_key
    return node, None

  def call_function_with_state(self, state, funcv, posargs, namedargs=None,
                               starargs=None, starstarargs=None,
                               fallback_to_unsolvable=True):
    """Call a function with the given state."""
    assert starargs is None or isinstance(starargs, cfg.Variable)
    assert starstarargs is None or isinstance(starstarargs, cfg.Variable)
    args = function.Args(
        posargs=posargs, namedargs=namedargs, starargs=starargs,
        starstarargs=starstarargs)
    node, ret = function.call_function(
        self.ctx,
        state.node,
        funcv,
        args,
        fallback_to_unsolvable,
        allow_noreturn=True)
    if ret.data == [self.ctx.convert.no_return]:
      state = state.set_why("NoReturn")
    state = state.change_cfg_node(node)
    if len(funcv.data) == 1:
      # Check for test assertions that narrow the type of a variable.
      state = self._check_test_assert(state, funcv, posargs)
    return state, ret

  def call_with_fake_args(self, node0, funcv):
    """Attempt to call the given function with made-up arguments."""
    return node0, self.ctx.new_unsolvable(node0)

  def _process_decorator(self, func, posargs):
    """Specific processing for decorated functions."""
    if len(posargs) != 1 or not posargs[0].bindings:
      return
    # Assume the decorator and decorated function have one binding each.
    # (This is valid due to how decorated functions/classes are created.)
    decorator = func.data[0]
    fn = posargs[0].data[0]
    # TODO(b/153760963) We also need to check if the CALL_FUNCTION opcode has
    # the same line number as the function declaration (it should suffice to
    # check that the opcode lineno is in the set of decorators; we just need a
    # way to access the line number here). Otherwise any function call taking a
    # single function as an arg could trigger this code.
    if fn.is_decorated:
      log.info("Decorating %s with %s", fn.full_name, decorator.full_name)

  def call_function_from_stack(self, state, num, starargs, starstarargs):
    """Pop arguments for a function and call it."""

    namedargs = abstract.Dict(self.ctx)

    def set_named_arg(node, key, val):
      # If we have no bindings for val, fall back to unsolvable.
      # See test_closures.ClosuresTest.test_undefined_var
      if val.bindings:
        namedargs.setitem_slot(node, key, val)
      else:
        namedargs.setitem_slot(node, key, self.ctx.new_unsolvable(node))

    # The way arguments are put on the stack changed in python 3.6:
    #   https://github.com/python/cpython/blob/3.5/Python/ceval.c#L4712
    #   https://github.com/python/cpython/blob/3.6/Python/ceval.c#L4806
    if self.ctx.python_version < (3, 6):
      num_kw, num_pos = divmod(num, 256)

      for _ in range(num_kw):
        state, (key, val) = state.popn(2)
        set_named_arg(state.node, key, val)
      state, posargs = state.popn(num_pos)
    else:
      state, args = state.popn(num)
      if starstarargs:
        kwnames = abstract_utils.get_atomic_python_constant(starstarargs, tuple)
        n = len(args) - len(kwnames)
        for key, arg in zip(kwnames, args[n:]):
          set_named_arg(state.node, key, arg)
        posargs = args[0:n]
        starstarargs = None
      else:
        posargs = args
    state, func = state.pop()
    self._process_decorator(func, posargs)
    state, ret = self.call_function_with_state(
        state, func, posargs, namedargs, starargs, starstarargs)
    return state.push(ret)

  def get_globals_dict(self):
    """Get a real python dict of the globals."""
    return self.frame.f_globals

  def load_from(self, state, store, name, discard_concrete_values=False):
    """Load an item out of locals, globals, or builtins."""
    assert isinstance(store, abstract.SimpleValue)
    assert isinstance(store, mixin.LazyMembers)
    store.load_lazy_attribute(name)
    bindings = store.members[name].Bindings(state.node)
    if not bindings:
      raise KeyError(name)
    ret = self.ctx.program.NewVariable()
    self._filter_none_and_paste_bindings(
        state.node, bindings, ret,
        discard_concrete_values=discard_concrete_values)
    self._var_names[ret.id] = name
    return state, ret

  def load_local(self, state, name):
    """Called when a local is loaded onto the stack.

    Uses the name to retrieve the value from the current locals().

    Args:
      state: The current VM state.
      name: Name of the local

    Returns:
      A tuple of the state and the value (cfg.Variable)
    """
    try:
      return self.load_from(state, self.frame.f_locals, name)
    except KeyError:
      # A variable has been declared but not defined, e.g.,
      #   constant: str
      return state, self._load_annotation(state.node, name)

  def load_global(self, state, name):
    # The concrete value of typing.TYPE_CHECKING should be preserved; otherwise,
    # concrete values are converted to abstract instances of their types, as we
    # generally can't assume that globals are constant.
    return self.load_from(
        state, self.frame.f_globals, name,
        discard_concrete_values=name != "TYPE_CHECKING")

  def load_special_builtin(self, name):
    if name == "__any_object__":
      # For type_inferencer/tests/test_pgms/*.py, must be a new object
      # each time.
      return abstract.Unknown(self.ctx)
    else:
      return self.ctx.special_builtins.get(name)

  def load_builtin(self, state, name):
    if name == "__undefined__":
      # For values that don't exist. (Unlike None, which is a valid object)
      return state, self.ctx.convert.empty.to_variable(self.ctx.root_node)
    special = self.load_special_builtin(name)
    if special:
      return state, special.to_variable(state.node)
    else:
      return self.load_from(state, self.frame.f_builtins, name)

  def load_constant(self, state, op, raw_const):
    const = self.ctx.convert.constant_to_var(raw_const, node=state.node)
    self.trace_opcode(op, raw_const, const)
    return state.push(const)

  def _load_annotation(self, node, name):
    annots = abstract_utils.get_annotations_dict(self.frame.f_locals.members)
    if annots:
      typ = annots.get_type(node, name)
      if typ:
        _, ret = self.ctx.annotation_utils.init_annotation(node, name, typ)
        return ret
    raise KeyError(name)

  def _record_local(self, node, op, name, typ, orig_val=None):
    """Record a type annotation on a local variable.

    This method records three types of local operations:
      - An annotation, e.g., `x: int`. In this case, `typ` is PyTDClass(int) and
        `orig_val` is None.
      - An assignment, e.g., `x = 0`. In this case, `typ` is None and `orig_val`
        is Instance(int).
      - An annotated assignment, e.g., `x: int = None`. In this case, `typ` is
        PyTDClass(int) and `orig_val` is Instance(None).

    Args:
      node: The current node.
      op: The current opcode.
      name: The variable name.
      typ: The annotation.
      orig_val: The original value, if any.
    """
    if orig_val:
      self.current_local_ops.append(LocalOp(name, LocalOp.ASSIGN))
    if typ:
      self.current_local_ops.append(LocalOp(name, LocalOp.ANNOTATE))
    self._update_annotations_dict(
        node, op, name, typ, orig_val, self.current_annotated_locals)

  def _update_annotations_dict(
      self, node, op, name, typ, orig_val, annotations_dict):
    if name in annotations_dict:
      annotations_dict[name].update(node, op, typ, orig_val)
    else:
      annotations_dict[name] = abstract_utils.Local(node, op, typ, orig_val,
                                                    self.ctx)

  def _store_value(self, state, name, value, local):
    """Store 'value' under 'name'."""
    if local:
      target = self.frame.f_locals
    else:
      target = self.frame.f_globals
    node = self.ctx.attribute_handler.set_attribute(state.node, target, name,
                                                    value)
    if target is self.frame.f_globals and self.late_annotations:
      for annot in self.late_annotations[name]:
        annot.resolve(node, self.frame.f_globals, self.frame.f_locals)
    return state.change_cfg_node(node)

  def store_local(self, state, name, value):
    """Called when a local is written."""
    return self._store_value(state, name, value, local=True)

  def store_global(self, state, name, value):
    """Same as store_local except for globals."""
    return self._store_value(state, name, value, local=False)

  def _remove_recursion(self, node, name, value):
    """Remove any recursion in the named value."""
    if not value.data or any(not isinstance(v, mixin.NestedAnnotation)
                             for v in value.data):
      return value
    stack = self.simple_stack()
    typ = self.ctx.annotation_utils.extract_annotation(node, value, name, stack)
    if self.late_annotations:
      recursive_annots = set(self.late_annotations[name])
    else:
      recursive_annots = set()
    for late_annot in self.ctx.annotation_utils.get_late_annotations(typ):
      if late_annot in recursive_annots:
        self.ctx.errorlog.not_supported_yet(
            stack,
            "Recursive type annotations",
            details="In annotation %r on %s" % (late_annot.expr, name))
        typ = self.ctx.annotation_utils.remove_late_annotations(typ)
        break
    return typ.to_variable(node)

  def _apply_annotation(
      self, state, op, name, orig_val, annotations_dict, check_types):
    """Applies the type annotation, if any, associated with this object."""
    typ, value = self.ctx.annotation_utils.apply_annotation(
        state.node, op, name, orig_val)
    if annotations_dict is not None:
      if annotations_dict is self.current_annotated_locals:
        self._record_local(state.node, op, name, typ, orig_val)
      elif name not in annotations_dict or not annotations_dict[name].typ:
        # When updating non-local annotations, we only record the first one
        # encountered so that if, say, an instance attribute is annotated in
        # both __init__ and another method, the __init__ annotation is used.
        self._update_annotations_dict(
            state.node, op, name, typ, orig_val, annotations_dict)
      if typ is None and name in annotations_dict:
        typ = annotations_dict[name].get_type(state.node, name)
        if typ == self.ctx.convert.unsolvable:
          # An Any annotation can be used to essentially turn off inference in
          # cases where it is causing false positives or other issues.
          value = self.ctx.new_unsolvable(state.node)
    if check_types:
      self.check_annotation_type_mismatch(
          state.node, name, typ, orig_val, self.frames, allow_none=True)
    return value

  def check_annotation_type_mismatch(
      self, node, name, typ, value, stack, allow_none, details=None):
    """Checks for a mismatch between a variable's annotation and value.

    Args:
      node: node
      name: variable name
      typ: variable annotation
      value: variable value
      stack: a frame stack for error reporting
      allow_none: whether a value of None is allowed for any type
      details: any additional details to add to the error message
    """
    if not typ or not value:
      return
    if (value.data == [self.ctx.convert.ellipsis] or
        allow_none and value.data == [self.ctx.convert.none]):
      return
    contained_type = abstract_utils.match_type_container(
        typ, ("typing.ClassVar", "dataclasses.InitVar"))
    if contained_type:
      typ = contained_type
    bad = self.ctx.matcher(node).bad_matches(value, typ)
    for view, *_ in bad:
      binding = view[value]
      self.ctx.errorlog.annotation_type_mismatch(stack, typ, binding, name,
                                                 details)

  def _pop_and_store(self, state, op, name, local):
    """Pop a value off the stack and store it in a variable."""
    state, orig_val = state.pop()
    annotations_dict = self.current_annotated_locals if local else None
    value = self._apply_annotation(
        state, op, name, orig_val, annotations_dict, check_types=True)
    value = self._remove_recursion(state.node, name, value)
    state = state.forward_cfg_node()
    state = self._store_value(state, name, value, local)
    self.trace_opcode(op, name, value)
    return state.forward_cfg_node()

  def _del_name(self, op, state, name, local):
    """Called when a local or global is deleted."""
    value = abstract.Deleted(self.ctx).to_variable(state.node)
    state = state.forward_cfg_node()
    state = self._store_value(state, name, value, local)
    self.trace_opcode(op, name, value)
    return state.forward_cfg_node()

  def _retrieve_attr(self, node, obj, attr):
    """Load an attribute from an object."""
    assert isinstance(obj, cfg.Variable), obj
    if (attr == "__class__" and self.ctx.callself_stack and
        obj.data == self.ctx.callself_stack[-1].data):
      return node, self.ctx.new_unsolvable(node), []
    # Resolve the value independently for each value of obj
    result = self.ctx.program.NewVariable()
    log.debug("getting attr %s from %r", attr, obj)
    nodes = []
    values_without_attribute = []
    for val in obj.bindings:
      node2, attr_var = self.ctx.attribute_handler.get_attribute(
          node, val.data, attr, val)
      if attr_var is None or not attr_var.bindings:
        log.debug("No %s on %s", attr, val.data.__class__)
        values_without_attribute.append(val)
        continue
      log.debug("got choice for attr %s from %r of %r (0x%x): %r", attr, obj,
                val.data, id(val.data), attr_var)
      self._filter_none_and_paste_bindings(node2, attr_var.bindings, result)
      nodes.append(node2)
    if nodes:
      return self.ctx.join_cfg_nodes(nodes), result, values_without_attribute
    else:
      return node, None, values_without_attribute

  def _data_is_none(self, x):
    assert isinstance(x, abstract.BaseValue)
    return x.cls == self.ctx.convert.none_type

  def _var_is_none(self, v):
    assert isinstance(v, cfg.Variable)
    return v.bindings and all(self._data_is_none(b.data) for b in v.bindings)

  def _delete_item(self, state, obj, arg):
    state, _ = self._call(state, obj, "__delitem__", (arg,))
    return state

  def load_attr(self, state, obj, attr):
    """Try loading an attribute, and report errors."""
    node, result, errors = self._retrieve_attr(state.node, obj, attr)
    self._attribute_error_detection(state, attr, errors)
    if result is None:
      result = self.ctx.new_unsolvable(node)
    return state.change_cfg_node(node), result

  def _attribute_error_detection(self, state, attr, errors):
    if not self.ctx.options.report_errors:
      return
    for error in errors:
      combination = [error]
      if self.frame.func:
        combination.append(self.frame.func)
      if state.node.HasCombination(combination):
        self.ctx.errorlog.attribute_error(self.frames, error, attr)

  def _filter_none_and_paste_bindings(self, node, bindings, var,
                                      discard_concrete_values=False):
    """Paste the bindings into var, filtering out false positives on None."""
    for b in bindings:
      if self._has_strict_none_origins(b):
        if (discard_concrete_values and
            isinstance(b.data, mixin.PythonConstant) and
            not isinstance(b.data.pyval, str)):
          # We need to keep constant strings as they may be forward references.
          var.AddBinding(
              self.ctx.convert.get_maybe_abstract_instance(b.data), [b], node)
        else:
          var.PasteBinding(b, node)
      else:
        var.AddBinding(self.ctx.convert.unsolvable, [b], node)

  def _has_strict_none_origins(self, binding):
    """Whether the binding has any possible origins, with None filtering.

    Determines whether the binding has any possibly visible origins at the
    current node once we've filtered out false positives on None. The caller
    still must call HasCombination() to find out whether these origins are
    actually reachable.

    Args:
      binding: A cfg.Binding.

    Returns:
      True if there are possibly visible origins, else False.
    """
    if not self._analyzing:
      return True
    has_any_none_origin = False
    walker = cfg_utils.walk_binding(
        binding, keep_binding=lambda b: self._data_is_none(b.data))
    origin = None
    while True:
      try:
        origin = walker.send(origin)
      except StopIteration:
        break
      for source_set in origin.source_sets:
        if not source_set:
          if self.ctx.program.is_reachable(
              src=self.frame.node, dst=origin.where):
            # Checking for reachability works because the current part of the
            # graph hasn't been connected back to the analyze node yet. Since
            # the walker doesn't preserve information about the relationship
            # among origins, we pretend we have a disjunction over source sets.
            return True
          has_any_none_origin = True
    return not has_any_none_origin

  def load_attr_noerror(self, state, obj, attr):
    """Try loading an attribute, ignore errors."""
    node, result, _ = self._retrieve_attr(state.node, obj, attr)
    return state.change_cfg_node(node), result

  def store_attr(self, state, obj, attr, value):
    """Set an attribute on an object."""
    assert isinstance(obj, cfg.Variable)
    assert isinstance(attr, str)
    if not obj.bindings:
      log.info("Ignoring setattr on %r", obj)
      return state
    nodes = []
    for val in obj.Filter(state.node):
      # TODO(b/172045608): Check whether val.data is a descriptor (i.e. has
      # "__set__")
      nodes.append(
          self.ctx.attribute_handler.set_attribute(state.node, val.data, attr,
                                                   value))
    if nodes:
      return state.change_cfg_node(self.ctx.join_cfg_nodes(nodes))
    else:
      return state

  def del_attr(self, state, obj, attr):
    """Delete an attribute."""
    log.info("Attribute removal does not do anything in the abstract "
             "interpreter")
    return state

  def del_subscr(self, state, obj, subscr):
    return self._delete_item(state, obj, subscr)

  def pop_varargs(self, state):
    """Retrieve a varargs tuple from the stack. Used by call_function."""
    return state.pop()

  def pop_kwargs(self, state):
    """Retrieve a kwargs dictionary from the stack. Used by call_function."""
    return state.pop()

  def import_module(self, name, full_name, level):
    """Import a module and return the module object or None."""
    if self.ctx.options.strict_import:
      # Do not import new modules if we aren't in an IMPORT statement.
      # The exception is if we have an implicit "package" module (e.g.
      # `import a.b.c` adds `a.b` to the list of instantiable modules.)
      if not (self._importing or self.ctx.loader.has_module_prefix(full_name)):
        return None
    try:
      module = self._import_module(name, level)
      # Since we have explicitly imported full_name, add it to the prefix list.
      self.ctx.loader.add_module_prefixes(full_name)
    except (parser.ParseError, load_pytd.BadDependencyError,
            visitors.ContainerError, visitors.SymbolLookupError) as e:
      self.ctx.errorlog.pyi_error(self.frames, full_name, e)
      module = self.ctx.convert.unsolvable
    return module

  def _maybe_load_overlay(self, name):
    """Check if a module path is in the overlay dictionary."""
    if name not in overlay_dict.overlays:
      return None
    if name == "chex" and not self.ctx.options.chex_overlay:
      # TODO(b/185807105): Enable --chex-overlay by default.
      return None
    if name in self.loaded_overlays:
      overlay = self.loaded_overlays[name]
    else:
      overlay = overlay_dict.overlays[name](self.ctx)
      # The overlay should be available only if the underlying pyi is.
      if overlay.ast:
        self.loaded_overlays[name] = overlay
      else:
        overlay = self.loaded_overlays[name] = None
    return overlay

  @utils.memoize
  def _import_module(self, name, level):
    """Import the module and return the module object.

    Args:
      name: Name of the module. E.g. "sys".
      level: Specifies whether to use absolute or relative imports.
        -1: (Python <= 3.1) "Normal" import. Try both relative and absolute.
         0: Absolute import.
         1: "from . import abc"
         2: "from .. import abc"
         etc.
    Returns:
      An instance of abstract.Module or None if we couldn't find the module.
    """
    if name:
      if level <= 0:
        assert level in [-1, 0]
        overlay = self._maybe_load_overlay(name)
        if overlay:
          return overlay
        if level == -1 and self.ctx.loader.base_module:
          # Python 2 tries relative imports first.
          ast = (
              self.ctx.loader.import_relative_name(name) or
              self.ctx.loader.import_name(name))
        else:
          ast = self.ctx.loader.import_name(name)
      else:
        # "from .x import *"
        base = self.ctx.loader.import_relative(level)
        if base is None:
          return None
        full_name = base.name + "." + name
        overlay = self._maybe_load_overlay(full_name)
        if overlay:
          return overlay
        ast = self.ctx.loader.import_name(full_name)
    else:
      assert level > 0
      ast = self.ctx.loader.import_relative(level)
    if ast:
      return self.ctx.convert.constant_to_value(
          ast, subst=datatypes.AliasingDict(), node=self.ctx.root_node)
    else:
      return None

  def unary_operator(self, state, name):
    state, x = state.pop()
    state, result = self._call(state, x, name, ())
    state = state.push(result)
    return state

  def _is_classmethod_cls_arg(self, var):
    """True if var is the first arg of a class method in the current frame."""
    if not (self.frame.func and self.frame.first_arg):
      return False

    func = self.frame.func.data
    if func.is_classmethod or func.name.rsplit(".")[-1] == "__new__":
      is_cls = not set(var.data) - set(self.frame.first_arg.data)
      return is_cls
    return False

  def expand_bool_result(self, node, left, right, name, maybe_predicate):
    """Common functionality for 'is' and 'is not'."""
    if (self._is_classmethod_cls_arg(left) or
        self._is_classmethod_cls_arg(right)):
      # If cls is the first argument of a classmethod, it could be bound to
      # either the defining class or one of its subclasses, so `is` is
      # ambiguous.
      return self.ctx.new_unsolvable(node)

    result = self.ctx.program.NewVariable()
    for x in left.bindings:
      for y in right.bindings:
        pyval = maybe_predicate(x.data, y.data)
        result.AddBinding(
            self.ctx.convert.bool_values[pyval], source_set=(x, y), where=node)

    return result

  def _get_aiter(self, state, obj):
    """Get an async iterator from an object."""
    state, func = self.load_attr(state, obj, "__aiter__")
    if func:
      return self.call_function_with_state(state, func, ())
    else:
      return state, self.ctx.new_unsolvable(state.node)

  def _get_iter(self, state, seq, report_errors=True):
    """Get an iterator from a sequence."""
    # TODO(rechen): We should iterate through seq's bindings, in order to fetch
    # the attribute on the sequence's class, but two problems prevent us from
    # doing so:
    # - Iterating through individual bindings causes a performance regression.
    # - Because __getitem__ is used for annotations, pytype sometime thinks the
    #   class attribute is AnnotationClass.getitem_slot.
    state, func = self.load_attr_noerror(state, seq, "__iter__")
    if func:
      # Call __iter__()
      state, itr = self.call_function_with_state(state, func, ())
    else:
      node, func, missing = self._retrieve_attr(state.node, seq, "__getitem__")
      state = state.change_cfg_node(node)
      if func:
        # Call __getitem__(int).
        state, item = self.call_function_with_state(
            state, func, (self.ctx.convert.build_int(state.node),))
        # Create a new iterator from the returned value.
        itr = abstract.Iterator(self.ctx, item).to_variable(state.node)
      else:
        itr = self.ctx.program.NewVariable()
      if report_errors and self.ctx.options.report_errors:
        for m in missing:
          if state.node.HasCombination([m]):
            self.ctx.errorlog.attribute_error(self.frames, m, "__iter__")
    return state, itr

  def byte_UNARY_NOT(self, state, op):
    """Implement the UNARY_NOT bytecode."""
    state, var = state.pop()
    true_bindings = [
        b for b in var.bindings if compare.compatible_with(b.data, True)]
    false_bindings = [
        b for b in var.bindings if compare.compatible_with(b.data, False)]
    if len(true_bindings) == len(false_bindings) == len(var.bindings):
      # No useful information from bindings, use a generic bool value.
      # This is merely an optimization rather than building separate True/False
      # values each with the same bindings as var.
      result = self.ctx.convert.build_bool(state.node)
    else:
      # Build a result with True/False values, each bound to appropriate
      # bindings.  Note that bindings that are True get attached to a result
      # that is False and vice versa because this is a NOT operation.
      result = self.ctx.program.NewVariable()
      for b in true_bindings:
        result.AddBinding(
            self.ctx.convert.bool_values[False],
            source_set=(b,),
            where=state.node)
      for b in false_bindings:
        result.AddBinding(
            self.ctx.convert.bool_values[True],
            source_set=(b,),
            where=state.node)
    state = state.push(result)
    return state

  def byte_UNARY_NEGATIVE(self, state, op):
    return self.unary_operator(state, "__neg__")

  def byte_UNARY_POSITIVE(self, state, op):
    return self.unary_operator(state, "__pos__")

  def byte_UNARY_INVERT(self, state, op):
    return self.unary_operator(state, "__invert__")

  def byte_BINARY_MATRIX_MULTIPLY(self, state, op):
    return self.binary_operator(state, "__matmul__")

  def byte_BINARY_ADD(self, state, op):
    return self.binary_operator(state, "__add__")

  def byte_BINARY_SUBTRACT(self, state, op):
    return self.binary_operator(state, "__sub__")

  def byte_BINARY_MULTIPLY(self, state, op):
    return self.binary_operator(state, "__mul__")

  def byte_BINARY_MODULO(self, state, op):
    return self.binary_operator(state, "__mod__")

  def byte_BINARY_LSHIFT(self, state, op):
    return self.binary_operator(state, "__lshift__")

  def byte_BINARY_RSHIFT(self, state, op):
    return self.binary_operator(state, "__rshift__")

  def byte_BINARY_AND(self, state, op):
    return self.binary_operator(state, "__and__")

  def byte_BINARY_XOR(self, state, op):
    return self.binary_operator(state, "__xor__")

  def byte_BINARY_OR(self, state, op):
    return self.binary_operator(state, "__or__")

  def byte_BINARY_FLOOR_DIVIDE(self, state, op):
    return self.binary_operator(state, "__floordiv__")

  def byte_BINARY_TRUE_DIVIDE(self, state, op):
    return self.binary_operator(state, "__truediv__")

  def byte_BINARY_POWER(self, state, op):
    return self.binary_operator(state, "__pow__")

  def byte_BINARY_SUBSCR(self, state, op):
    return self.binary_operator(state, "__getitem__")

  def byte_INPLACE_MATRIX_MULTIPLY(self, state, op):
    return self.inplace_operator(state, "__imatmul__")

  def byte_INPLACE_ADD(self, state, op):
    return self.inplace_operator(state, "__iadd__")

  def byte_INPLACE_SUBTRACT(self, state, op):
    return self.inplace_operator(state, "__isub__")

  def byte_INPLACE_MULTIPLY(self, state, op):
    return self.inplace_operator(state, "__imul__")

  def byte_INPLACE_MODULO(self, state, op):
    return self.inplace_operator(state, "__imod__")

  def byte_INPLACE_POWER(self, state, op):
    return self.inplace_operator(state, "__ipow__")

  def byte_INPLACE_LSHIFT(self, state, op):
    return self.inplace_operator(state, "__ilshift__")

  def byte_INPLACE_RSHIFT(self, state, op):
    return self.inplace_operator(state, "__irshift__")

  def byte_INPLACE_AND(self, state, op):
    return self.inplace_operator(state, "__iand__")

  def byte_INPLACE_XOR(self, state, op):
    return self.inplace_operator(state, "__ixor__")

  def byte_INPLACE_OR(self, state, op):
    return self.inplace_operator(state, "__ior__")

  def byte_INPLACE_FLOOR_DIVIDE(self, state, op):
    return self.inplace_operator(state, "__ifloordiv__")

  def byte_INPLACE_TRUE_DIVIDE(self, state, op):
    return self.inplace_operator(state, "__itruediv__")

  def byte_LOAD_CONST(self, state, op):
    try:
      raw_const = self.frame.f_code.co_consts[op.arg]
    except IndexError:
      # We have tried to access an undefined closure variable.
      # There is an associated LOAD_DEREF failure where the error will be
      # raised, so we just return unsolvable here.
      # See test_closures.ClosuresTest.test_undefined_var
      return state.push(self.ctx.new_unsolvable(state.node))
    return self.load_constant(state, op, raw_const)

  def byte_LOAD_FOLDED_CONST(self, state, op):
    const = op.arg
    state, var = constant_folding.build_folded_type(self.ctx, state, const)
    return state.push(var)

  def byte_POP_TOP(self, state, op):
    return state.pop_and_discard()

  def byte_DUP_TOP(self, state, op):
    return state.push(state.top())

  def byte_DUP_TOP_TWO(self, state, op):
    state, (a, b) = state.popn(2)
    return state.push(a, b, a, b)

  def byte_ROT_TWO(self, state, op):
    state, (a, b) = state.popn(2)
    return state.push(b, a)

  def byte_ROT_THREE(self, state, op):
    state, (a, b, c) = state.popn(3)
    return state.push(c, a, b)

  def byte_ROT_FOUR(self, state, op):
    state, (a, b, c, d) = state.popn(4)
    return state.push(d, a, b, c)

  def _is_private(self, name):
    return name.startswith("_") and not name.startswith("__")

  def _get_scopes(
      self, state, names: Sequence[str]
  ) -> Sequence[Union[abstract.InterpreterClass, abstract.InterpreterFunction]]:
    """Gets the class or function objects for a sequence of nested scope names.

    For example, if the code under analysis is:
      class Foo:
        def f(self):
          def g(): ...
    then when called with ['Foo', 'f', 'g'], this method returns
    [InterpreterClass(Foo), InterpreterFunction(f), InterpreterFunction(g)].

    Arguments:
      state: The current state.
      names: A sequence of names for consecutive nested scopes in the module
        under analysis. Must start with a module-level name.

    Returns:
      The class or function object corresponding to each name in 'names'.
    """
    scopes = []
    for name in names:
      prev = scopes[-1] if scopes else None
      if not prev:
        try:
          _, var = self.load_global(state, name)
        except KeyError:
          break
      elif isinstance(prev, abstract.InterpreterClass):
        if name in prev.members:
          var = prev.members[name]
        else:
          break
      else:
        assert isinstance(prev, abstract.InterpreterFunction)
        # For last_frame to be populated, 'prev' has to have been called at
        # least once. This has to be true for all functions except the innermost
        # one, since pytype cannot detect a nested function without analyzing
        # the code that defines the nested function.
        if prev.last_frame and name in prev.last_frame.f_locals.pyval:
          var = prev.last_frame.f_locals.pyval[name]
        else:
          break
      try:
        scopes.append(abstract_utils.get_atomic_value(
            var, (abstract.InterpreterClass, abstract.InterpreterFunction)))
      except abstract_utils.ConversionError:
        break
    return scopes

  def _get_name_error_details(self, state, name: str) -> _NameErrorDetails:
    """Gets a detailed error message for [name-error]."""
    # 'name' is not defined in the current scope. To help the user better
    # understand UnboundLocalError and other similarly confusing errors, we look
    # for definitions of 'name' in outer scopes so we can print a more
    # informative error message.

    # Starting from the current (innermost) frame and moving outward, pytype
    # represents any classes with their own frames until it hits the first
    # function. It represents that function with its own frame and all remaining
    # frames with a single SimpleFrame. For example, if we have:
    #   def f():
    #     class C:
    #       def g():
    #         class D:
    #           class E:
    # then self.frames looks like:
    #   [SimpleFrame(), Frame(f.<locals>.C.g), Frame(D), Frame(E)]
    class_frames = []
    first_function_frame = None
    for frame in reversed(self.frames):
      if not frame.func:
        break
      if frame.func.data.is_class_builder:
        class_frames.append(frame)
      else:
        first_function_frame = frame
        break

    # Nested function names include ".<locals>" after each outer function.
    clean = lambda func_name: func_name.replace(".<locals>", "")

    if first_function_frame:
      # Functions have fully qualified names, so we can use the name of
      # first_function_frame to look up the remaining frames.
      parts = clean(first_function_frame.func.data.name).split(".")
      if first_function_frame is self.frame:
        parts = parts[:-1]
    else:
      parts = []

    # Check if 'name' is defined in one of the outer classes and functions.
    # Scope 'None' represents the global scope.
    prefix, class_name_parts = None, []
    for scope in itertools.chain(
        reversed(self._get_scopes(state, parts)), [None]):
      if class_name_parts:
        # We have located a class that 'name' is defined in and are now
        # constructing the name by which the class should be referenced.
        if isinstance(scope, abstract.InterpreterClass):
          class_name_parts.append(scope.name)
        elif scope:
          prefix = clean(scope.name)
          break
      elif isinstance(scope, abstract.InterpreterClass):
        if name in scope.members:
          # The user may have intended to reference <Class>.<name>
          class_name_parts.append(scope.name)
      else:
        outer_scope = None
        if scope:
          # 'name' is defined in an outer function but not accessible, so it
          # must be redefined in the current frame (an UnboundLocalError).
          # Note that it is safe to assume that annotated_locals corresponds to
          # 'scope' (rather than a different function with the same name) only
          # when 'last_frame' is empty, since the latter being empty means that
          # 'scope' is actively under analysis.
          if ((scope.last_frame and name in scope.last_frame.f_locals.pyval) or
              (not scope.last_frame and
               name in self.annotated_locals[scope.name.rsplit(".", 1)[-1]])):
            outer_scope = f"function {clean(scope.name)!r}"
        else:
          try:
            _ = self.load_global(state, name)
          except KeyError:
            pass
          else:
            outer_scope = "global scope"
        if outer_scope:
          if self.frame.func.data.is_class_builder:
            class_name = ".".join(parts + [
                class_frame.func.data.name
                for class_frame in reversed(class_frames)])
            inner_scope = f"class {class_name!r}"
          else:
            inner_scope = f"function {clean(self.frame.func.data.name)!r}"
          return _NameInOuterFunctionErrorDetails(
              name, outer_scope, inner_scope)
    if class_name_parts:
      return _NameInOuterClassErrorDetails(
          name, prefix, ".".join(reversed(class_name_parts)))

    # Check if 'name' is defined in one of the classes with their own frames.
    if class_frames:
      for i, frame in enumerate(class_frames[1:]):
        if name in self.annotated_locals[frame.func.data.name]:
          class_parts = [part.func.data.name
                         for part in reversed(class_frames[i+1:])]
          class_name = ".".join(parts + class_parts)
          return _NameInInnerClassErrorDetails(name, class_name)
    return None

  def _name_error_or_late_annotation(self, state, name):
    """Returns a late annotation or returns Any and logs a name error."""
    if self._late_annotations_stack and self.late_annotations is not None:
      annot = abstract.LateAnnotation(name, self._late_annotations_stack,
                                      self.ctx)
      log.info("Created %r", annot)
      self.late_annotations[name].append(annot)
      return annot
    else:
      details = self._get_name_error_details(state, name)
      if details:
        details = "Note: " + details.to_error_message()
      self.ctx.errorlog.name_error(self.frames, name, details=details)
      return self.ctx.convert.unsolvable

  def byte_LOAD_NAME(self, state, op):
    """Load a name. Can be a local, global, or builtin."""
    name = self.frame.f_code.co_names[op.arg]
    try:
      state, val = self.load_local(state, name)
    except KeyError:
      try:
        state, val = self.load_global(state, name)
      except KeyError as e:
        try:
          if self._is_private(name):
            # Private names must be explicitly imported.
            self.trace_opcode(op, name, None)
            raise KeyError(name) from e
          state, val = self.load_builtin(state, name)
        except KeyError:
          if self._is_private(name) or not self.has_unknown_wildcard_imports:
            one_val = self._name_error_or_late_annotation(state, name)
          else:
            one_val = self.ctx.convert.unsolvable
          self.trace_opcode(op, name, None)
          return state.push(one_val.to_variable(state.node))
    self.check_for_deleted(state, name, val)
    self.trace_opcode(op, name, val)
    return state.push(val)

  def byte_STORE_NAME(self, state, op):
    name = self.frame.f_code.co_names[op.arg]
    return self._pop_and_store(state, op, name, local=True)

  def byte_DELETE_NAME(self, state, op):
    name = self.frame.f_code.co_names[op.arg]
    return self._del_name(op, state, name, local=True)

  def byte_LOAD_FAST(self, state, op):
    """Load a local. Unlike LOAD_NAME, it doesn't fall back to globals."""
    name = self.frame.f_code.co_varnames[op.arg]
    try:
      state, val = self.load_local(state, name)
    except KeyError:
      val = self._name_error_or_late_annotation(state, name).to_variable(
          state.node)
    self.check_for_deleted(state, name, val)
    self.trace_opcode(op, name, val)
    return state.push(val)

  def byte_STORE_FAST(self, state, op):
    name = self.frame.f_code.co_varnames[op.arg]
    return self._pop_and_store(state, op, name, local=True)

  def byte_DELETE_FAST(self, state, op):
    name = self.frame.f_code.co_varnames[op.arg]
    return self._del_name(op, state, name, local=True)

  def byte_LOAD_GLOBAL(self, state, op):
    """Load a global variable, or fall back to trying to load a builtin."""
    name = self.frame.f_code.co_names[op.arg]
    if name == "None":
      # Load None itself as a constant to avoid the None filtering done on
      # variables. This workaround is safe because assigning to None is a
      # syntax error.
      return self.load_constant(state, op, None)
    try:
      state, val = self.load_global(state, name)
    except KeyError:
      try:
        state, val = self.load_builtin(state, name)
      except KeyError:
        self.trace_opcode(op, name, None)
        ret = self._name_error_or_late_annotation(state, name)
        return state.push(ret.to_variable(state.node))
    self.check_for_deleted(state, name, val)
    self.trace_opcode(op, name, val)
    return state.push(val)

  def byte_STORE_GLOBAL(self, state, op):
    name = self.frame.f_code.co_names[op.arg]
    return self._pop_and_store(state, op, name, local=False)

  def byte_DELETE_GLOBAL(self, state, op):
    name = self.frame.f_code.co_names[op.arg]
    return self._del_name(op, state, name, local=False)

  def get_closure_var_name(self, arg):
    n_cellvars = len(self.frame.f_code.co_cellvars)
    if arg < n_cellvars:
      name = self.frame.f_code.co_cellvars[arg]
    else:
      name = self.frame.f_code.co_freevars[arg - n_cellvars]
    return name

  def check_for_deleted(self, state, name, var):
    if any(isinstance(x, abstract.Deleted) for x in var.Data(state.node)):
      # Referencing a deleted variable
      # TODO(mdemello): A "use-after-delete" error would be more helpful.
      self.ctx.errorlog.name_error(self.frames, name)

  def load_closure_cell(self, state, op, check_bindings=False):
    """Retrieve the value out of a closure cell.

    Used to generate the 'closure' tuple for MAKE_CLOSURE.

    Each entry in that tuple is typically retrieved using LOAD_CLOSURE.

    Args:
      state: The current VM state.
      op: The opcode. op.arg is the index of a "cell variable": This corresponds
        to an entry in co_cellvars or co_freevars and is a variable that's bound
        into a closure.
      check_bindings: Whether to check the retrieved value for bindings.
    Returns:
      A new state.
    """
    cell = self.frame.cells[op.arg]
    # If we have closed over a variable in an inner function, then invoked the
    # inner function before the variable is defined, raise a name error here.
    # See test_closures.ClosuresTest.test_undefined_var
    if check_bindings and not cell.bindings:
      self.ctx.errorlog.name_error(self.frames, op.pretty_arg)
    visible_bindings = cell.Filter(state.node, strict=False)
    if len(visible_bindings) != len(cell.bindings):
      # We need to filter here because the closure will be analyzed outside of
      # its creating context, when information about what values are visible
      # has been lost.
      new_cell = self.ctx.program.NewVariable()
      if visible_bindings:
        for b in visible_bindings:
          new_cell.AddBinding(b.data, {b}, state.node)
      else:
        # See test_closures.ClosuresTest.test_no_visible_bindings.
        new_cell.AddBinding(self.ctx.convert.unsolvable)
      # Update the cell because the DELETE_DEREF implementation works on
      # variable identity.
      self.frame.cells[op.arg] = cell = new_cell
    name = self.get_closure_var_name(op.arg)
    self.check_for_deleted(state, name, cell)
    self.trace_opcode(op, name, cell)
    return state.push(cell)

  def byte_LOAD_CLOSURE(self, state, op):
    """Retrieves a value out of a cell."""
    return self.load_closure_cell(state, op)

  def byte_LOAD_DEREF(self, state, op):
    """Retrieves a value out of a cell."""
    return self.load_closure_cell(state, op, True)

  def byte_STORE_DEREF(self, state, op):
    """Stores a value in a closure cell."""
    state, value = state.pop()
    assert isinstance(value, cfg.Variable)
    name = self.get_closure_var_name(op.arg)
    value = self._apply_annotation(
        state, op, name, value, self.current_annotated_locals, check_types=True)
    state = state.forward_cfg_node()
    self.frame.cells[op.arg].PasteVariable(value, state.node)
    state = state.forward_cfg_node()
    self.trace_opcode(op, name, value)
    return state

  def byte_DELETE_DEREF(self, state, op):
    value = abstract.Deleted(self.ctx).to_variable(state.node)
    name = self.get_closure_var_name(op.arg)
    state = state.forward_cfg_node()
    self.frame.cells[op.arg].PasteVariable(value, state.node)
    state = state.forward_cfg_node()
    self.trace_opcode(op, name, value)
    return state

  def byte_LOAD_CLASSDEREF(self, state, op):
    """Retrieves a value out of either locals or a closure cell."""
    name = self.get_closure_var_name(op.arg)
    try:
      state, val = self.load_local(state, name)
      self.trace_opcode(op, name, val)
      return state.push(val)
    except KeyError:
      return self.load_closure_cell(state, op)

  def _cmp_rel(self, state, op_name, x, y):
    """Implementation of relational operators CMP_(LT|LE|EQ|NE|GE|GT).

    Args:
      state: Initial FrameState.
      op_name: An operator name, e.g., "EQ".
      x: A variable of the lhs value.
      y: A variable of the rhs value.

    Returns:
      A tuple of the new FrameState and the return variable.
    """
    ret = self.ctx.program.NewVariable()
    # A variable of the values without a special cmp_rel implementation. Needed
    # because overloaded __eq__ implementations do not necessarily return a
    # bool; see, e.g., test_overloaded in test_cmp.
    leftover_x = self.ctx.program.NewVariable()
    leftover_y = self.ctx.program.NewVariable()
    for b1 in x.bindings:
      for b2 in y.bindings:
        try:
          op = getattr(slots, op_name)
          val = compare.cmp_rel(self.ctx, op, b1.data, b2.data)
        except compare.CmpTypeError:
          val = None
          if state.node.HasCombination([b1, b2]):
            self.ctx.errorlog.unsupported_operands(self.frames, op, x, y)
        if val is None:
          leftover_x.AddBinding(b1.data, {b1}, state.node)
          leftover_y.AddBinding(b2.data, {b2}, state.node)
        else:
          ret.AddBinding(self.ctx.convert.bool_values[val], {b1, b2},
                         state.node)
    if leftover_x.bindings:
      op = "__%s__" % op_name.lower()
      state, leftover_ret = self.call_binary_operator(
          state, op, leftover_x, leftover_y)
      ret.PasteVariable(leftover_ret, state.node)
    return state, ret

  def _coerce_to_bool(self, node, var, true_val=True):
    """Coerce the values in a variable to bools."""
    bool_var = self.ctx.program.NewVariable()
    for b in var.bindings:
      v = b.data
      if isinstance(v, mixin.PythonConstant) and isinstance(v.pyval, bool):
        const = v.pyval is true_val
      elif not compare.compatible_with(v, True):
        const = not true_val
      elif not compare.compatible_with(v, False):
        const = true_val
      else:
        const = None
      bool_var.AddBinding(self.ctx.convert.bool_values[const], {b}, node)
    return bool_var

  def _cmp_in(self, state, item, seq, true_val=True):
    """Implementation of CMP_IN/CMP_NOT_IN."""
    state, has_contains = self.load_attr_noerror(state, seq, "__contains__")
    if has_contains:
      state, ret = self.call_binary_operator(state, "__contains__", seq, item,
                                             report_errors=True)
      if ret.bindings:
        ret = self._coerce_to_bool(state.node, ret, true_val=true_val)
    else:
      # For an object without a __contains__ method, cmp_in falls back to
      # checking item against the items produced by seq's iterator.
      state, itr = self._get_iter(state, seq, report_errors=False)
      if len(itr.bindings) < len(seq.bindings):
        # seq does not have any of __contains__, __iter__, and __getitem__.
        # (The last two are checked by _get_iter.)
        self.ctx.errorlog.unsupported_operands(self.frames, "__contains__", seq,
                                               item)
      ret = self.ctx.convert.build_bool(state.node)
    return state, ret

  def _cmp_is_always_supported(self, op_arg):
    """Checks if the comparison should always succeed."""
    return op_arg in slots.CMP_ALWAYS_SUPPORTED

  def _instantiate_exception(self, node, exc_type):
    """Instantiate an exception type.

    Args:
      node: The current node.
      exc_type: A cfg.Variable of the exception type.

    Returns:
      A tuple of a cfg.Variable of the instantiated type and a list of
      the flattened exception types in the data of exc_type. None takes the
      place of invalid types.
    """
    value = self.ctx.program.NewVariable()
    types = []
    stack = list(exc_type.data)
    while stack:
      e = stack.pop()
      if isinstance(e, abstract.Tuple):
        for sub_exc_type in e.pyval:
          sub_value, sub_types = self._instantiate_exception(node, sub_exc_type)
          value.PasteVariable(sub_value)
          types.extend(sub_types)
      elif (isinstance(e, abstract.Instance) and
            e.cls.full_name == "builtins.tuple"):
        sub_exc_type = e.get_instance_type_parameter(abstract_utils.T)
        sub_value, sub_types = self._instantiate_exception(node, sub_exc_type)
        value.PasteVariable(sub_value)
        types.extend(sub_types)
      elif isinstance(e, class_mixin.Class) and any(
          base.full_name == "builtins.BaseException" or
          isinstance(base, abstract.AMBIGUOUS_OR_EMPTY) for base in e.mro):
        node, instance = self.init_class(node, e)
        value.PasteVariable(instance)
        types.append(e)
      elif isinstance(e, abstract.Union):
        stack.extend(e.options)
      else:
        if not isinstance(e, abstract.AMBIGUOUS_OR_EMPTY):
          if isinstance(e, class_mixin.Class):
            mro_seqs = [e.mro] if isinstance(e, class_mixin.Class) else []
            msg = "%s does not inherit from BaseException" % e.name
          else:
            mro_seqs = []
            msg = "Not a class"
          self.ctx.errorlog.mro_error(
              self.frames, e.name, mro_seqs, details=msg)
        value.AddBinding(self.ctx.convert.unsolvable, [], node)
        types.append(None)
    return value, types

  def _replace_abstract_exception(self, state, exc_type):
    """Replace unknowns added by push_abstract_exception with precise values."""
    # When the `try` block is set up, push_abstract_exception pushes on
    # unknowns for the value and exception type. At the beginning of the
    # `except` block, when we know the exception being caught, we can replace
    # the unknowns with more useful variables.
    value, types = self._instantiate_exception(state.node, exc_type)
    if None in types:
      exc_type = self.ctx.new_unsolvable(state.node)
    if self.ctx.python_version >= (3, 8):
      # See SETUP_FINALLY: in 3.8+, we push the exception on twice.
      state, (_, _, tb, _, _) = state.popn(5)
      state = state.push(value, exc_type, tb, value, exc_type)
    else:
      state, _ = state.popn(2)
      state = state.push(value, exc_type)
    return state

  def _compare_op(self, state, op_arg):
    """Pops and compares the top two stack values and pushes a boolean."""
    state, (x, y) = state.popn(2)
    # Explicit, redundant, switch statement, to make it easier to address the
    # behavior of individual compare operations:
    if op_arg == slots.CMP_LT:
      state, ret = self._cmp_rel(state, "LT", x, y)
    elif op_arg == slots.CMP_LE:
      state, ret = self._cmp_rel(state, "LE", x, y)
    elif op_arg == slots.CMP_EQ:
      state, ret = self._cmp_rel(state, "EQ", x, y)
    elif op_arg == slots.CMP_NE:
      state, ret = self._cmp_rel(state, "NE", x, y)
    elif op_arg == slots.CMP_GT:
      state, ret = self._cmp_rel(state, "GT", x, y)
    elif op_arg == slots.CMP_GE:
      state, ret = self._cmp_rel(state, "GE", x, y)
    elif op_arg == slots.CMP_IS:
      ret = self.expand_bool_result(state.node, x, y,
                                    "is_cmp", frame_state.is_cmp)
    elif op_arg == slots.CMP_IS_NOT:
      ret = self.expand_bool_result(state.node, x, y,
                                    "is_not_cmp", frame_state.is_not_cmp)
    elif op_arg == slots.CMP_NOT_IN:
      state, ret = self._cmp_in(state, x, y, true_val=False)
    elif op_arg == slots.CMP_IN:
      state, ret = self._cmp_in(state, x, y)
    elif op_arg == slots.CMP_EXC_MATCH:
      state = self._replace_abstract_exception(state, y)
      ret = self.ctx.convert.build_bool(state.node)
    else:
      raise VirtualMachineError("Invalid argument to COMPARE_OP: %d" % op_arg)
    if not ret.bindings and self._cmp_is_always_supported(op_arg):
      # Some comparison operations are always supported, depending on the target
      # Python version. In this case, always return a (boolean) value.
      # (https://docs.python.org/2/library/stdtypes.html#comparisons or
      # (https://docs.python.org/3/library/stdtypes.html#comparisons)
      ret.AddBinding(self.ctx.convert.primitive_class_instances[bool], [],
                     state.node)
    return state.push(ret)

  def byte_COMPARE_OP(self, state, op):
    return self._compare_op(state, op.arg)

  def byte_IS_OP(self, state, op):
    if op.arg:
      op_arg = slots.CMP_IS_NOT
    else:
      op_arg = slots.CMP_IS
    return self._compare_op(state, op_arg)

  def byte_CONTAINS_OP(self, state, op):
    if op.arg:
      op_arg = slots.CMP_NOT_IN
    else:
      op_arg = slots.CMP_IN
    return self._compare_op(state, op_arg)

  def byte_LOAD_ATTR(self, state, op):
    """Pop an object, and retrieve a named attribute from it."""
    name = self.frame.f_code.co_names[op.arg]
    state, obj = state.pop()
    log.debug("LOAD_ATTR: %r %r", obj, name)
    with self._suppress_opcode_tracing():
      # LOAD_ATTR for @property methods generates an extra opcode trace for the
      # implicit function call, which we do not want.
      state, val = self.load_attr(state, obj, name)
    # We need to trace both the object and the attribute.
    self.trace_opcode(op, name, (obj, val))
    return state.push(val)

  def byte_STORE_ATTR(self, state, op):
    """Store an attribute."""
    name = self.frame.f_code.co_names[op.arg]
    state, (val, obj) = state.popn(2)
    # If `obj` is a single class or an instance of one, then grab its
    # __annotations__ dict so we can type-check the new attribute value.
    check_attribute_types = True
    try:
      obj_val = abstract_utils.get_atomic_value(obj)
    except abstract_utils.ConversionError:
      annotations_dict = None
    else:
      if isinstance(obj_val, abstract.InterpreterClass):
        maybe_cls = obj_val
      else:
        maybe_cls = obj_val.cls
      if isinstance(maybe_cls, abstract.InterpreterClass):
        if ("__annotations__" not in maybe_cls.members and
            op.line in self._director.annotations):
          # The class has no annotated class attributes but does have an
          # annotated instance attribute.
          annotations_dict = abstract.AnnotationsDict({}, self.ctx)
          maybe_cls.members["__annotations__"] = annotations_dict.to_variable(
              self.ctx.root_node)
        annotations_dict = abstract_utils.get_annotations_dict(
            maybe_cls.members)
        if annotations_dict:
          annotations_dict = annotations_dict.annotated_locals
      elif (isinstance(maybe_cls, abstract.PyTDClass) and
            maybe_cls != self.ctx.convert.type_type):
        node, attr = self.ctx.attribute_handler.get_attribute(
            state.node, obj_val, name)
        if attr:
          typ = self.ctx.convert.merge_classes(attr.data)
          annotations_dict = {
              name: abstract_utils.Local(state.node, op, typ, None, self.ctx)
          }
          state = state.change_cfg_node(node)
        else:
          annotations_dict = None
        # In a PyTDClass, we can't distinguish between an inferred type and an
        # annotation. Even though we don't check against the attribute type, we
        # still apply it so that setting an attribute value on an instance of a
        # class doesn't affect the attribute type in other instances.
        check_attribute_types = False
      else:
        annotations_dict = None
    val = self._apply_annotation(
        state, op, name, val, annotations_dict, check_attribute_types)
    state = state.forward_cfg_node()
    state = self.store_attr(state, obj, name, val)
    state = state.forward_cfg_node()
    # We need to trace both the object and the attribute.
    self.trace_opcode(op, name, (obj, val))
    return state

  def byte_DELETE_ATTR(self, state, op):
    name = self.frame.f_code.co_names[op.arg]
    state, obj = state.pop()
    return self.del_attr(state, obj, name)

  def store_subscr(self, state, obj, key, val):
    state, _ = self._call(state, obj, "__setitem__", (key, val))
    return state

  def byte_STORE_SUBSCR(self, state, op):
    """Implement obj[subscr] = val."""
    state, (val, obj, subscr) = state.popn(3)
    state = state.forward_cfg_node()
    # Check whether obj is the __annotations__ dict.
    if len(obj.data) == 1 and isinstance(obj.data[0], abstract.AnnotationsDict):
      try:
        name = abstract_utils.get_atomic_python_constant(subscr, str)
      except abstract_utils.ConversionError:
        pass
      else:
        allowed_type_params = (
            self.frame.type_params
            | self.ctx.annotation_utils.get_callable_type_parameter_names(val))
        typ = self.ctx.annotation_utils.extract_annotation(
            state.node,
            val,
            name,
            self.simple_stack(),
            allowed_type_params=allowed_type_params)
        self._record_annotation(state.node, op, name, typ)
    state = self.store_subscr(state, obj, subscr, val)
    return state

  def byte_DELETE_SUBSCR(self, state, op):
    state, (obj, subscr) = state.popn(2)
    return self.del_subscr(state, obj, subscr)

  def byte_BUILD_TUPLE(self, state, op):
    count = op.arg
    state, elts = state.popn(count)
    return state.push(self.ctx.convert.build_tuple(state.node, elts))

  def byte_BUILD_LIST(self, state, op):
    count = op.arg
    state, elts = state.popn(count)
    state = state.push(self.ctx.convert.build_list(state.node, elts))
    return state.forward_cfg_node()

  def byte_BUILD_SET(self, state, op):
    count = op.arg
    state, elts = state.popn(count)
    return state.push(self.ctx.convert.build_set(state.node, elts))

  def byte_BUILD_MAP(self, state, op):
    """Build a dictionary."""
    the_map = self.ctx.convert.build_map(state.node)
    state, args = state.popn(2 * op.arg)
    for i in range(op.arg):
      key, val = args[2*i], args[2*i+1]
      state = self.store_subscr(state, the_map, key, val)
    return state.push(the_map)

  def _get_literal_sequence(self, data):
    """Helper function for _unpack_sequence."""
    try:
      return self.ctx.convert.value_to_constant(data, tuple)
    except abstract_utils.ConversionError:
      # Fall back to looking for a literal list and converting to a tuple
      try:
        return tuple(self.ctx.convert.value_to_constant(data, list))
      except abstract_utils.ConversionError:
        for base in data.cls.mro:
          if isinstance(base, abstract.TupleClass) and not base.formal:
            # We've found a TupleClass with concrete parameters, which means
            # we're a subclass of a heterogeneous tuple (usually a
            # typing.NamedTuple instance).
            new_data = self.ctx.convert.merge_values(
                base.instantiate(self.ctx.root_node).data)
            return self._get_literal_sequence(new_data)
        return None

  def _restructure_tuple(self, state, tup, pre, post):
    """Collapse the middle part of a tuple into a List variable."""
    before = tup[0:pre]
    if post > 0:
      after = tup[-post:]
      rest = tup[pre:-post]
    else:
      after = ()
      rest = tup[pre:]
    rest = self.ctx.convert.build_list(state.node, rest)
    return before + (rest,) + after

  def _unpack_sequence(self, state, n_before, n_after=-1):
    """Pops a tuple (or other iterable) and pushes it onto the VM's stack.

    Supports destructuring assignment with potentially a single list variable
    that slurps up the remaining elements:
    1. a, b, c = ...  # UNPACK_SEQUENCE
    2. a, *b, c = ... # UNPACK_EX

    Args:
      state: The current VM state
      n_before: Number of elements before the list (n_elements for case 1)
      n_after: Number of elements after the list (-1 for case 1)
    Returns:
      The new state.
    """
    assert n_after >= -1
    state, seq = state.pop()
    options = []
    nontuple_seq = self.ctx.program.NewVariable()
    has_slurp = n_after > -1
    count = n_before + n_after + 1
    for b in abstract_utils.expand_type_parameter_instances(seq.bindings):
      tup = self._get_literal_sequence(b.data)
      if tup:
        if has_slurp and len(tup) >= count:
          options.append(self._restructure_tuple(state, tup, n_before, n_after))
          continue
        elif len(tup) == count:
          options.append(tup)
          continue
        else:
          self.ctx.errorlog.bad_unpacking(self.frames, len(tup), count)
      nontuple_seq.AddBinding(b.data, {b}, state.node)
    if nontuple_seq.bindings:
      state, itr = self._get_iter(state, nontuple_seq)
      state, result = self._call(state, itr, "__next__", ())
      # For a non-literal iterable, next() should always return the same type T,
      # so we can iterate `count` times in both UNPACK_SEQUENCE and UNPACK_EX,
      # and assign the slurp variable type List[T].
      option = [result for _ in range(count)]
      if has_slurp:
        option[n_before] = self.ctx.convert.build_list_of_type(
            state.node, result)
      options.append(option)
    values = tuple(
        self.ctx.convert.build_content(value) for value in zip(*options))
    for value in reversed(values):
      if not value.bindings:
        # For something like
        #   for i, j in enumerate(()):
        #     print j
        # there are no bindings for j, so we have to add an empty binding
        # to avoid a name error on the print statement.
        value = self.ctx.convert.empty.to_variable(state.node)
      state = state.push(value)
    return state

  def byte_UNPACK_SEQUENCE(self, state, op):
    return self._unpack_sequence(state, op.arg)

  def byte_UNPACK_EX(self, state, op):
    n_before = op.arg & 0xff
    n_after = op.arg >> 8
    return self._unpack_sequence(state, n_before, n_after)

  def byte_BUILD_SLICE(self, state, op):
    if op.arg == 2:
      state, (x, y) = state.popn(2)
      return state.push(self.ctx.convert.build_slice(state.node, x, y))
    elif op.arg == 3:
      state, (x, y, z) = state.popn(3)
      return state.push(self.ctx.convert.build_slice(state.node, x, y, z))
    else:       # pragma: no cover
      raise VirtualMachineError("Strange BUILD_SLICE count: %r" % op.arg)

  def byte_LIST_APPEND(self, state, op):
    # Used by the compiler e.g. for [x for x in ...]
    count = op.arg
    state, val = state.pop()
    the_list = state.peek(count)
    state, _ = self._call(state, the_list, "append", (val,))
    return state

  def byte_LIST_EXTEND(self, state, op):
    """Pops top-of-stack and uses it to extend the list at stack[op.arg]."""
    state, update = state.pop()
    target = state.peek(op.arg)
    if not all(abstract_utils.is_concrete_list(v) for v in target.data):
      state, _ = self._call(state, target, "extend", (update,))
      return state

    # Is the list we're constructing going to be the argument list for a
    # function call? If so, we will keep any abstract.Splat objects around so we
    # can unpack the function arguments precisely. Otherwise, splats will be
    # converted to indefinite iterables.
    keep_splats = False
    next_op = op
    while next_op:
      next_op = next_op.next
      if isinstance(next_op, opcodes.CALL_FUNCTION_EX):
        keep_splats = True
        break
      elif next_op.__class__ in blocks.STORE_OPCODES:
        break

    update_elements = self._unpack_iterable(state.node, update)
    if not keep_splats and any(
        abstract_utils.is_var_splat(x) for x in update_elements):
      for target_value in target.data:
        self._merge_indefinite_iterables(
            state.node, target_value, update_elements)
    else:
      for target_value in target.data:
        target_value.pyval.extend(update_elements)
        for update_value in update.data:
          update_param = update_value.get_instance_type_parameter(
              abstract_utils.T, state.node)
          # We use Instance.merge_instance_type_parameter because the List
          # implementation also sets could_contain_anything to True.
          abstract.Instance.merge_instance_type_parameter(
              target_value, state.node, abstract_utils.T, update_param)
    return state

  def byte_SET_ADD(self, state, op):
    # Used by the compiler e.g. for {x for x in ...}
    count = op.arg
    state, val = state.pop()
    the_set = state.peek(count)
    state, _ = self._call(state, the_set, "add", (val,))
    return state

  def byte_SET_UPDATE(self, state, op):
    state, update = state.pop()
    target = state.peek(op.arg)
    state, _ = self._call(state, target, "update", (update,))
    return state

  def byte_MAP_ADD(self, state, op):
    """Implements the MAP_ADD opcode."""
    # Used by the compiler e.g. for {x, y for x, y in ...}
    count = op.arg
    # In 3.8+, the value is at the top of the stack, followed by the key. Before
    # that, it's the other way around.
    state, item = state.popn(2)
    if self.ctx.python_version >= (3, 8):
      key, val = item
    else:
      val, key = item
    the_map = state.peek(count)
    state, _ = self._call(state, the_map, "__setitem__", (key, val))
    return state

  def byte_DICT_MERGE(self, state, op):
    # DICT_MERGE is like DICT_UPDATE but raises an exception for duplicate keys.
    return self.byte_DICT_UPDATE(state, op)

  def byte_DICT_UPDATE(self, state, op):
    """Pops top-of-stack and uses it to update the dict at stack[op.arg]."""
    state, update = state.pop()
    target = state.peek(op.arg)

    def pytd_update(state):
      state, _ = self._call(state, target, "update", (update,))
      return state

    if not all(abstract_utils.is_concrete_dict(v) for v in target.data):
      return pytd_update(state)
    try:
      update_value = abstract_utils.get_atomic_python_constant(update, dict)
    except abstract_utils.ConversionError:
      return pytd_update(state)
    for abstract_target_value in target.data:
      for k, v in update_value.items():
        abstract_target_value.set_str_item(state.node, k, v)
    return state

  def byte_PRINT_EXPR(self, state, op):
    # Only used in the interactive interpreter, not in modules.
    return state.pop_and_discard()

  def _jump_if(self, state, op, pop=False, jump_if=False, or_pop=False):
    """Implementation of various _JUMP_IF bytecodes.

    Args:
      state: Initial FrameState.
      op: An opcode.
      pop: True if a value is popped off the stack regardless.
      jump_if: True or False (indicates which value will lead to a jump).
      or_pop: True if a value is popped off the stack only when the jump is
          not taken.
    Returns:
      The new FrameState.
    """
    assert not (pop and or_pop)
    # Determine the conditions.  Assume jump-if-true, then swap conditions
    # if necessary.
    if pop:
      state, value = state.pop()
    else:
      value = state.top()
    jump, normal = frame_state.split_conditions(
        state.node, value)
    if not jump_if:
      jump, normal = normal, jump
    # Jump.
    if jump is not frame_state.UNSATISFIABLE:
      if jump:
        assert jump.binding
        else_state = state.forward_cfg_node(jump.binding).forward_cfg_node()
      else:
        else_state = state.forward_cfg_node()
      self.store_jump(op.target, else_state)
    else:
      else_state = None
    # Don't jump.
    if or_pop:
      state = state.pop_and_discard()
    if normal is frame_state.UNSATISFIABLE:
      return state.set_why("unsatisfiable")
    elif not else_state and not normal:
      return state  # We didn't actually branch.
    else:
      return state.forward_cfg_node(normal.binding if normal else None)

  def byte_JUMP_IF_TRUE_OR_POP(self, state, op):
    return self._jump_if(state, op, jump_if=True, or_pop=True)

  def byte_JUMP_IF_FALSE_OR_POP(self, state, op):
    return self._jump_if(state, op, jump_if=False, or_pop=True)

  def byte_JUMP_IF_TRUE(self, state, op):
    return self._jump_if(state, op, jump_if=True)

  def byte_JUMP_IF_FALSE(self, state, op):
    return self._jump_if(state, op, jump_if=False)

  def byte_POP_JUMP_IF_TRUE(self, state, op):
    return self._jump_if(state, op, pop=True, jump_if=True)

  def byte_POP_JUMP_IF_FALSE(self, state, op):
    return self._jump_if(state, op, pop=True, jump_if=False)

  def byte_JUMP_FORWARD(self, state, op):
    self.store_jump(op.target, state.forward_cfg_node())
    return state

  def byte_JUMP_ABSOLUTE(self, state, op):
    self.store_jump(op.target, state.forward_cfg_node())
    return state

  def byte_JUMP_IF_NOT_EXC_MATCH(self, state, op):
    state, (unused_exc, exc_type) = state.popn(2)
    # At runtime, this opcode calls isinstance(exc, exc_type) and pushes the
    # result onto the stack. Instead, we use exc_type to refine the type of the
    # exception instance still on the stack and push on an indefinite result for
    # the isinstance call.
    state = self._replace_abstract_exception(state, exc_type)
    state = state.push(self.ctx.convert.bool_values[None].to_variable(
        state.node))
    return self._jump_if(state, op, pop=True, jump_if=False)

  def byte_SETUP_LOOP(self, state, op):
    # We ignore the implicit jump in SETUP_LOOP; the interpreter never takes it.
    return self.push_block(state, "loop")

  def byte_GET_ITER(self, state, op):
    """Get the iterator for an object."""
    state, seq = state.pop()
    state, itr = self._get_iter(state, seq)
    # Push the iterator onto the stack and return.
    return state.push(itr)

  def store_jump(self, target, state):
    assert target
    self.frame.targets[self.frame.current_block.id].append(target)
    self.frame.states[target] = state.merge_into(self.frame.states.get(target))

  def byte_FOR_ITER(self, state, op):
    self.store_jump(op.target, state.pop_and_discard())
    state, f = self.load_attr(state, state.top(), "__next__")
    state = state.push(f)
    return self.call_function_from_stack(state, 0, None, None)

  def _revert_state_to(self, state, name):
    while state.block_stack[-1].type != name:
      state, block = state.pop_block()
      while block.level < len(state.data_stack):
        state = state.pop_and_discard()
    return state

  def byte_BREAK_LOOP(self, state, op):
    new_state, block = self._revert_state_to(state, "loop").pop_block()
    while block.level < len(new_state.data_stack):
      new_state = new_state.pop_and_discard()
    self.store_jump(op.block_target, new_state)
    return state

  def byte_CONTINUE_LOOP(self, state, op):
    new_state = self._revert_state_to(state, "loop")
    self.store_jump(op.target, new_state)
    return state

  def _setup_except(self, state, op):
    """Sets up an except block."""
    # Assume that it's possible to throw the exception at the first
    # instruction of the code:
    jump_state = self.push_abstract_exception(state)
    if self.ctx.python_version >= (3, 8):
      # I have no idea why we need to push the exception twice! See
      # test_exceptions.TestExceptions.test_reuse_name for a test that fails if
      # we don't do this.
      jump_state = self.push_abstract_exception(jump_state)
    self.store_jump(op.target, jump_state)
    return self.push_block(state, "setup-except")

  # Note: this opcode is removed in Python 3.8.
  def byte_SETUP_EXCEPT(self, state, op):
    return self._setup_except(state, op)

  def is_setup_except(self, op):
    """Check whether op is equivalent to a SETUP_EXCEPT opcode."""
    # In Python 3.8+, exception setup is done using the SETUP_FINALLY opcode.
    # Before that, there was a separate SETUP_EXCEPT opcode.
    if self.ctx.python_version >= (3, 8):
      if isinstance(op, opcodes.SETUP_FINALLY):
        for i, block in enumerate(self.frame.f_code.order):
          if block.id == op.arg:
            if not any(isinstance(o, opcodes.BEGIN_FINALLY)
                       for o in self.frame.f_code.order[i-1]):
              return True
            break
      return False
    else:
      return isinstance(op, opcodes.SETUP_EXCEPT)

  def byte_SETUP_FINALLY(self, state, op):
    """Implements the SETUP_FINALLY opcode."""
    # In Python 3.8+, SETUP_FINALLY handles setup for both except and finally
    # blocks. Examine the targeted block to determine which setup to do.
    if self.is_setup_except(op):
      return self._setup_except(state, op)
    # Emulate finally by connecting the try to the finally block (with
    # empty reason/why/continuation):
    self.store_jump(op.target,
                    state.push(self.ctx.convert.build_none(state.node)))
    return self.push_block(state, "finally")

  # New python3.8+ exception handling opcodes:
  # BEGIN_FINALLY, END_ASYNC_FOR, CALL_FINALLY, POP_FINALLY

  def byte_BEGIN_FINALLY(self, state, op):
    return state.push(self.ctx.convert.build_none(state.node))

  def byte_CALL_FINALLY(self, state, op):
    return state

  def byte_END_ASYNC_FOR(self, state, op):
    return state

  def byte_POP_FINALLY(self, state, op):
    """Implements POP_FINALLY."""
    preserve_tos = op.arg
    if preserve_tos:
      state, saved_tos = state.pop()
    state, tos = state.pop()
    if any(d != self.ctx.convert.none and d.cls != self.ctx.convert.int_type
           for d in tos.data):
      state, _ = state.popn(5)
    if preserve_tos:
      state = state.push(saved_tos)
    return state

  def byte_POP_BLOCK(self, state, op):
    state, _ = state.pop_block()
    return state

  def byte_RAISE_VARARGS(self, state, op):
    """Raise an exception."""
    argc = op.arg
    state, _ = state.popn(argc)
    if argc == 0 and state.exception:
      return state.set_why("reraise")
    else:
      state = state.set_exception()
      return state.set_why("exception")

  def byte_POP_EXCEPT(self, state, op):  # Python 3 only
    # We don't push the special except-handler block, so we don't need to
    # pop it, either.
    if self.ctx.python_version >= (3, 8):
      state, _ = state.popn(3)
    return state

  def byte_SETUP_WITH(self, state, op):
    """Starts a 'with' statement. Will push a block."""
    state, ctxmgr = state.pop()
    level = len(state.data_stack)
    state, exit_method = self.load_attr(state, ctxmgr, "__exit__")
    state = state.push(exit_method)
    state, ctxmgr_obj = self._call(state, ctxmgr, "__enter__", ())
    state = self.push_block(state, "finally", level)
    return state.push(ctxmgr_obj)

  def _with_cleanup_start(self, state, op):
    """Implements WITH_CLEANUP_START before Python 3.8."""
    state, u = state.pop()  # pop 'None'
    state, exit_func = state.pop()
    state = state.push(u)
    state = state.push(self.ctx.convert.build_none(state.node))
    v = self.ctx.convert.build_none(state.node)
    w = self.ctx.convert.build_none(state.node)
    state, suppress_exception = self.call_function_with_state(
        state, exit_func, (u, v, w))
    return state.push(suppress_exception)

  def _with_cleanup_start_3_8(self, state, op):
    """Implements WITH_CLEANUP_START in Python 3.8+."""
    tos = state.top()
    if tos.data == [self.ctx.convert.none]:
      return self._with_cleanup_start(state, op)
    state, (w, v, u, *rest, exit_func) = state.popn(7)
    state = state.push(*rest)
    state = state.push(self.ctx.convert.build_none(state.node))
    state = state.push(w, v, u)
    state, suppress_exception = self.call_function_with_state(
        state, exit_func, (u, v, w))
    return state.push(suppress_exception)

  def byte_WITH_CLEANUP_START(self, state, op):
    """Called to start cleaning up a with block. Calls the exit handlers etc."""
    if self.ctx.python_version >= (3, 8):
      return self._with_cleanup_start_3_8(state, op)
    else:
      return self._with_cleanup_start(state, op)

  def byte_WITH_CLEANUP_FINISH(self, state, op):
    """Called to finish cleaning up a with block."""
    state, suppress_exception = state.pop()
    state, second = state.pop()
    if (suppress_exception.data == [self.ctx.convert.true] and
        second.data != [self.ctx.convert.none]):
      state = state.push(self.ctx.convert.build_none(state.node))
    return state

  def _convert_kw_defaults(self, values):
    kw_defaults = {}
    for i in range(0, len(values), 2):
      key_var, value = values[i:i + 2]
      key = abstract_utils.get_atomic_python_constant(key_var)
      kw_defaults[key] = value
    return kw_defaults

  def _get_extra_function_args(self, state, arg):
    """Get function annotations and defaults from the stack. (Python3.5-)."""
    num_pos_defaults = arg & 0xff
    num_kw_defaults = (arg >> 8) & 0xff
    state, raw_annotations = state.popn((arg >> 16) & 0x7fff)
    state, kw_defaults = state.popn(2 * num_kw_defaults)
    state, pos_defaults = state.popn(num_pos_defaults)
    free_vars = None  # Python < 3.6 does not handle closure vars here.
    kw_defaults = self._convert_kw_defaults(kw_defaults)
    annot = self.ctx.annotation_utils.convert_function_annotations(
        state.node, raw_annotations)
    return state, pos_defaults, kw_defaults, annot, free_vars

  def _get_extra_function_args_3_6(self, state, arg):
    """Get function annotations and defaults from the stack (Python3.6+)."""
    free_vars = None
    pos_defaults = ()
    kw_defaults = {}
    annot = {}
    if arg & loadmarshal.MAKE_FUNCTION_HAS_FREE_VARS:
      state, free_vars = state.pop()
    if arg & loadmarshal.MAKE_FUNCTION_HAS_ANNOTATIONS:
      state, packed_annot = state.pop()
      annot = abstract_utils.get_atomic_python_constant(packed_annot, dict)
      for k in annot.keys():
        annot[k] = self.ctx.annotation_utils.convert_function_type_annotation(
            k, annot[k])
    if arg & loadmarshal.MAKE_FUNCTION_HAS_KW_DEFAULTS:
      state, packed_kw_def = state.pop()
      kw_defaults = abstract_utils.get_atomic_python_constant(
          packed_kw_def, dict)
    if arg & loadmarshal.MAKE_FUNCTION_HAS_POS_DEFAULTS:
      state, packed_pos_def = state.pop()
      pos_defaults = abstract_utils.get_atomic_python_constant(
          packed_pos_def, tuple)
    annot = self.ctx.annotation_utils.convert_annotations_list(
        state.node, annot.items())
    return state, pos_defaults, kw_defaults, annot, free_vars

  def _process_function_type_comment(self, node, op, func):
    """Modifies annotations from a function type comment.

    Checks if a type comment is present for the function.  If so, the type
    comment is used to populate annotations.  It is an error to have
    a type comment when annotations is not empty.

    Args:
      node: The current node.
      op: An opcode (used to determine filename and line number).
      func: An abstract.InterpreterFunction.
    """
    if not op.annotation:
      return

    comment, lineno = op.annotation

    # It is an error to use a type comment on an annotated function.
    if func.signature.annotations:
      self.ctx.errorlog.redundant_function_type_comment(op.code.co_filename,
                                                        lineno)
      return

    # Parse the comment, use a fake Opcode that is similar to the original
    # opcode except that it is set to the line number of the type comment.
    # This ensures that errors are printed with an accurate line number.
    fake_stack = self.simple_stack(op.at_line(lineno))
    m = _FUNCTION_TYPE_COMMENT_RE.match(comment)
    if not m:
      self.ctx.errorlog.invalid_function_type_comment(fake_stack, comment)
      return
    args, return_type = m.groups()

    if args != "...":
      annot = args.strip()
      try:
        self.ctx.annotation_utils.eval_multi_arg_annotation(
            node, func, annot, fake_stack)
      except abstract_utils.ConversionError:
        self.ctx.errorlog.invalid_function_type_comment(
            fake_stack, annot, details="Must be constant.")

    ret = self.ctx.convert.build_string(None, return_type)
    func.signature.set_annotation(
        "return",
        self.ctx.annotation_utils.extract_annotation(node, ret, "return",
                                                     fake_stack))

  def byte_MAKE_FUNCTION(self, state, op):
    """Create a function and push it onto the stack."""
    state, name_var = state.pop()
    name = abstract_utils.get_atomic_python_constant(name_var)
    state, code = state.pop()
    if self.ctx.python_version >= (3, 6):
      get_args = self._get_extra_function_args_3_6
    else:
      get_args = self._get_extra_function_args
    state, defaults, kw_defaults, annot, free_vars = get_args(state, op.arg)
    globs = self.get_globals_dict()
    fn = self._make_function(name, state.node, code, globs, defaults,
                             kw_defaults, annotations=annot, closure=free_vars)
    if op.line in self._director.decorators:
      fn.data[0].is_decorated = True
    self._process_function_type_comment(state.node, op, fn.data[0])
    self.trace_opcode(op, name, fn)
    self.trace_functiondef(fn)
    return state.push(fn)

  def byte_MAKE_CLOSURE(self, state, op):
    """Make a function that binds local variables."""
    state, name_var = state.pop()
    name = abstract_utils.get_atomic_python_constant(name_var)
    state, (closure, code) = state.popn(2)
    state, defaults, kw_defaults, annot, _ = (
        self._get_extra_function_args(state, op.arg))
    globs = self.get_globals_dict()
    fn = self._make_function(name, state.node, code, globs, defaults,
                             kw_defaults, annotations=annot, closure=closure)
    self.trace_functiondef(fn)
    return state.push(fn)

  def byte_CALL_FUNCTION(self, state, op):
    return self.call_function_from_stack(state, op.arg, None, None)

  def byte_CALL_FUNCTION_VAR(self, state, op):
    state, starargs = self.pop_varargs(state)
    starargs = self._ensure_unpacked_starargs(state.node, starargs)
    return self.call_function_from_stack(state, op.arg, starargs, None)

  def byte_CALL_FUNCTION_KW(self, state, op):
    state, kwargs = self.pop_kwargs(state)
    return self.call_function_from_stack(state, op.arg, None, kwargs)

  def byte_CALL_FUNCTION_VAR_KW(self, state, op):
    state, kwargs = self.pop_kwargs(state)
    state, starargs = self.pop_varargs(state)
    starargs = self._ensure_unpacked_starargs(state.node, starargs)
    return self.call_function_from_stack(state, op.arg, starargs, kwargs)

  def byte_CALL_FUNCTION_EX(self, state, op):
    """Call a function."""
    if op.arg & loadmarshal.CALL_FUNCTION_EX_HAS_KWARGS:
      state, starstarargs = state.pop()
    else:
      starstarargs = None
    state, starargs = state.pop()
    starargs = self._ensure_unpacked_starargs(state.node, starargs)
    state, fn = state.pop()
    # TODO(mdemello): fix function.Args() to properly init namedargs,
    # and remove this.
    namedargs = abstract.Dict(self.ctx)
    state, ret = self.call_function_with_state(
        state, fn, (), namedargs=namedargs, starargs=starargs,
        starstarargs=starstarargs)
    return state.push(ret)

  def byte_YIELD_VALUE(self, state, op):
    """Yield a value from a generator."""
    state, ret = state.pop()
    value = self.frame.yield_variable.AssignToNewVariable(state.node)
    value.PasteVariable(ret, state.node)
    self.frame.yield_variable = value
    if self.frame.check_return:
      ret_type = self.frame.allowed_returns
      self._check_return(state.node, ret,
                         ret_type.get_formal_type_parameter(abstract_utils.T))
      _, send_var = self.init_class(
          state.node,
          ret_type.get_formal_type_parameter(abstract_utils.T2))
      return state.push(send_var)
    return state.push(self.ctx.new_unsolvable(state.node))

  def byte_IMPORT_NAME(self, state, op):
    """Import a single module."""
    full_name = self.frame.f_code.co_names[op.arg]
    # The identifiers in the (unused) fromlist are repeated in IMPORT_FROM.
    state, (level_var, fromlist) = state.popn(2)
    if op.line in self._director.ignore:
      # "import name  # type: ignore"
      self.trace_opcode(op, full_name, None)
      return state.push(self.ctx.new_unsolvable(state.node))
    # The IMPORT_NAME for an "import a.b.c" will push the module "a".
    # However, for "from a.b.c import Foo" it'll push the module "a.b.c". Those
    # two cases are distinguished by whether fromlist is None or not.
    if self._var_is_none(fromlist):
      name = full_name.split(".", 1)[0]  # "a.b.c" -> "a"
    else:
      name = full_name
    level = abstract_utils.get_atomic_python_constant(level_var)
    module = self.import_module(name, full_name, level)
    if module is None:
      log.warning("Couldn't find module %r", name)
      self.ctx.errorlog.import_error(self.frames, name)
      module = self.ctx.convert.unsolvable
    mod = module.to_variable(state.node)
    self.trace_opcode(op, full_name, mod)
    return state.push(mod)

  def byte_IMPORT_FROM(self, state, op):
    """IMPORT_FROM is mostly like LOAD_ATTR but doesn't pop the container."""
    name = self.frame.f_code.co_names[op.arg]
    if op.line in self._director.ignore:
      # "from x import y  # type: ignore"
      # TODO(mdemello): Should we add some sort of signal data to indicate that
      # this should be treated as resolvable even though there is no module?
      self.trace_opcode(op, name, None)
      return state.push(self.ctx.new_unsolvable(state.node))
    module = state.top()
    state, attr = self.load_attr_noerror(state, module, name)
    if attr is None:
      full_name = module.data[0].name + "." + name
      self.ctx.errorlog.import_error(self.frames, full_name)
      attr = self.ctx.new_unsolvable(state.node)
    self.trace_opcode(op, name, attr)
    return state.push(attr)

  def byte_LOAD_BUILD_CLASS(self, state, op):
    cls = abstract.BuildClass(self.ctx).to_variable(state.node)
    if op.line in self._director.decorators:
      # Will be copied into the abstract.InterpreterClass
      cls.data[0].is_decorated = True
    self.trace_opcode(op, "", cls)
    return state.push(cls)

  def byte_END_FINALLY(self, state, op):
    """Implementation of the END_FINALLY opcode."""
    state, exc = state.pop()
    if self._var_is_none(exc):
      return state
    else:
      log.info("Popping exception %r", exc)
      state = state.pop_and_discard()
      state = state.pop_and_discard()
      # If a pending exception makes it all the way out of an "except" block,
      # no handler matched, hence Python re-raises the exception.
      return state.set_why("reraise")

  def _check_return(self, node, actual, formal):
    return False  # overridden in analyze.py

  def _set_frame_return(self, node, frame, var):
    if frame.allowed_returns is not None:
      _, retvar = self.init_class(node, frame.allowed_returns)
    else:
      retvar = var
    frame.return_variable.PasteVariable(retvar, node)

  def byte_RETURN_VALUE(self, state, op):
    """Get and check the return value."""
    state, var = state.pop()
    if self.frame.check_return:
      if self.frame.f_code.has_generator():
        ret_type = self.frame.allowed_returns
        self._check_return(state.node, var,
                           ret_type.get_formal_type_parameter(abstract_utils.V))
      elif not self.frame.f_code.has_async_generator():
        self._check_return(state.node, var, self.frame.allowed_returns)
    self._set_frame_return(state.node, self.frame, var)
    return state.set_why("return")

  def byte_IMPORT_STAR(self, state, op):
    """Pops a module and stores all its contents in locals()."""
    # TODO(b/159041010): this doesn't use __all__ properly.
    state, mod_var = state.pop()
    mod = abstract_utils.get_atomic_value(mod_var)
    # TODO(rechen): Is mod ever an unknown?
    if isinstance(mod, (abstract.Unknown, abstract.Unsolvable)):
      self.has_unknown_wildcard_imports = True
      return state
    log.info("%r", mod)
    for name, var in mod.items():
      if name[0] != "_" or name == "__getattr__":
        state = self.store_local(state, name, var)
    return state

  def byte_SETUP_ANNOTATIONS(self, state, op):
    """Sets up variable annotations in locals()."""
    annotations = abstract.AnnotationsDict(self.current_annotated_locals,
                                           self.ctx).to_variable(state.node)
    return self.store_local(state, "__annotations__", annotations)

  def _record_annotation(self, node, op, name, typ):
    # Annotations in self._director are handled by _apply_annotation.
    if self.frame.current_opcode.line not in self._director.annotations:
      self._record_local(node, op, name, typ)

  def byte_STORE_ANNOTATION(self, state, op):
    """Implementation of the STORE_ANNOTATION opcode."""
    state, annotations_var = self.load_local(state, "__annotations__")
    name = self.frame.f_code.co_names[op.arg]
    state, value = state.pop()
    allowed_type_params = (
        self.frame.type_params
        | self.ctx.annotation_utils.get_callable_type_parameter_names(value))
    typ = self.ctx.annotation_utils.extract_annotation(
        state.node,
        value,
        name,
        self.simple_stack(),
        allowed_type_params=allowed_type_params)
    self._record_annotation(state.node, op, name, typ)
    key = self.ctx.convert.primitive_class_instances[str]
    state = self.store_subscr(
        state, annotations_var, key.to_variable(state.node), value)
    return self.store_local(state, "__annotations__", annotations_var)

  def byte_GET_YIELD_FROM_ITER(self, state, op):
    # TODO(mdemello): We should check if TOS is a generator iterator or
    # coroutine first, and do nothing if it is, else call GET_ITER
    return self.byte_GET_ITER(state, op)

  def _merge_tuple_bindings(self, node, var):
    """Merge a set of heterogeneous tuples from var's bindings."""
    # Helper function for _unpack_iterable. We have already checked that all the
    # tuples are the same length.
    if len(var.bindings) == 1:
      return var
    length = var.data[0].tuple_length
    seq = [self.ctx.program.NewVariable() for _ in range(length)]
    for tup in var.data:
      for i in range(length):
        seq[i].PasteVariable(tup.pyval[i])
    return seq

  def _unpack_iterable(self, node, var):
    """Unpack an iterable."""
    elements = []
    try:
      itr = abstract_utils.get_atomic_python_constant(
          var, collections.abc.Iterable)
    except abstract_utils.ConversionError:
      if abstract_utils.is_var_indefinite_iterable(var):
        elements.append(abstract.Splat(self.ctx, var).to_variable(node))
      elif (all(isinstance(d, abstract.Tuple) for d in var.data) and
            all(d.tuple_length == var.data[0].tuple_length for d in var.data)):
        # If we have a set of bindings to tuples all of the same length, treat
        # them as a definite tuple with union-typed fields.
        vs = self._merge_tuple_bindings(node, var)
        elements.extend(vs)
      elif (any(isinstance(x, abstract.Unsolvable) for x in var.data) or
            all(isinstance(x, abstract.Unknown) for x in var.data)):
        # If we have an unsolvable or unknown we are unpacking as an iterable,
        # make sure it is treated as a tuple and not a single value.
        v = self.ctx.convert.tuple_type.instantiate(node)
        elements.append(abstract.Splat(self.ctx, v).to_variable(node))
      else:
        # If we reach here we have tried to unpack something that wasn't
        # iterable. Wrap it in a splat and let the matcher raise an error.
        elements.append(abstract.Splat(self.ctx, var).to_variable(node))
    else:
      for v in itr:
        # Some iterable constants (e.g., tuples) already contain variables,
        # whereas others (e.g., strings) need to be wrapped.
        if isinstance(v, cfg.Variable):
          elements.append(v)
        else:
          elements.append(self.ctx.convert.constant_to_var(v))
    return elements

  def _pop_and_unpack_list(self, state, count):
    """Pop count iterables off the stack and concatenate."""
    state, iterables = state.popn(count)
    elements = []
    for var in iterables:
      elements.extend(self._unpack_iterable(state.node, var))
    return state, elements

  def _merge_indefinite_iterables(self, node, target, iterables_to_merge):
    for var in iterables_to_merge:
      if abstract_utils.is_var_splat(var):
        for val in abstract_utils.unwrap_splat(var).data:
          p = val.get_instance_type_parameter(abstract_utils.T)
          target.merge_instance_type_parameter(node, abstract_utils.T, p)
      else:
        target.merge_instance_type_parameter(node, abstract_utils.T, var)

  def _unpack_and_build(self, state, count, build_concrete, container_type):
    state, seq = self._pop_and_unpack_list(state, count)
    if any(abstract_utils.is_var_splat(x) for x in seq):
      retval = abstract.Instance(container_type, self.ctx)
      self._merge_indefinite_iterables(state.node, retval, seq)
      ret = retval.to_variable(state.node)
    else:
      ret = build_concrete(state.node, seq)
    return state.push(ret)

  def _build_function_args_tuple(self, node, seq):
    # If we are building function call args, do not collapse indefinite
    # subsequences into a single tuple[x, ...], but allow them to be concrete
    # elements to match against function parameters and *args.
    tup = self.ctx.convert.tuple_to_value(seq)
    tup.is_unpacked_function_args = True
    return tup.to_variable(node)

  def _ensure_unpacked_starargs(self, node, starargs):
    """Unpack starargs if it has not been done already."""
    # TODO(mdemello): If we *have* unpacked the arg in a previous opcode will it
    # always have a single binding?
    if not any(isinstance(x, abstract.Tuple) and x.is_unpacked_function_args
               for x in starargs.data):
      seq = self._unpack_iterable(node, starargs)
      starargs = self._build_function_args_tuple(node, seq)
    return starargs

  def byte_BUILD_LIST_UNPACK(self, state, op):
    return self._unpack_and_build(state, op.arg, self.ctx.convert.build_list,
                                  self.ctx.convert.list_type)

  def byte_LIST_TO_TUPLE(self, state, op):
    """Convert the list at the top of the stack to a tuple."""
    del op  # unused
    state, lst_var = state.pop()
    tup_var = self.ctx.program.NewVariable()
    for b in lst_var.bindings:
      if abstract_utils.is_concrete_list(b.data):
        tup_var.AddBinding(
            self.ctx.convert.tuple_to_value(b.data.pyval), {b}, state.node)
      else:
        param = b.data.get_instance_type_parameter(abstract_utils.T)
        tup = abstract.Instance(self.ctx.convert.tuple_type, self.ctx)
        tup.merge_instance_type_parameter(state.node, abstract_utils.T, param)
        tup_var.AddBinding(tup, {b}, state.node)
    return state.push(tup_var)

  def _build_map_unpack(self, state, arg_list):
    """Merge a list of kw dicts into a single dict."""
    args = abstract.Dict(self.ctx)
    for arg in arg_list:
      for data in arg.data:
        args.update(state.node, data)
    args = args.to_variable(state.node)
    return args

  def byte_BUILD_MAP_UNPACK(self, state, op):
    state, maps = state.popn(op.arg)
    args = self._build_map_unpack(state, maps)
    return state.push(args)

  def byte_BUILD_MAP_UNPACK_WITH_CALL(self, state, op):
    if self.ctx.python_version >= (3, 6):
      state, maps = state.popn(op.arg)
    else:
      state, maps = state.popn(op.arg & 0xff)
    args = self._build_map_unpack(state, maps)
    return state.push(args)

  def byte_BUILD_TUPLE_UNPACK(self, state, op):
    return self._unpack_and_build(state, op.arg, self.ctx.convert.build_tuple,
                                  self.ctx.convert.tuple_type)

  def byte_BUILD_TUPLE_UNPACK_WITH_CALL(self, state, op):
    state, seq = self._pop_and_unpack_list(state, op.arg)
    ret = self._build_function_args_tuple(state.node, seq)
    return state.push(ret)

  def byte_BUILD_SET_UNPACK(self, state, op):
    return self._unpack_and_build(state, op.arg, self.ctx.convert.build_set,
                                  self.ctx.convert.set_type)

  def byte_SETUP_ASYNC_WITH(self, state, op):
    state, res = state.pop()
    level = len(state.data_stack)
    state = self.push_block(state, "finally", level)
    return state.push(res)

  def byte_FORMAT_VALUE(self, state, op):
    if op.arg & loadmarshal.FVS_MASK:
      state = state.pop_and_discard()
    # FORMAT_VALUE pops, formats and pushes back a string, so we just need to
    # push a new string onto the stack.
    state = state.pop_and_discard()
    ret = abstract.Instance(self.ctx.convert.str_type, self.ctx)
    return state.push(ret.to_variable(state.node))

  def byte_BUILD_CONST_KEY_MAP(self, state, op):
    state, keys = state.pop()
    keys = abstract_utils.get_atomic_python_constant(keys, tuple)
    the_map = self.ctx.convert.build_map(state.node)
    assert len(keys) == op.arg
    for key in reversed(keys):
      state, val = state.pop()
      state = self.store_subscr(state, the_map, key, val)
    return state.push(the_map)

  def byte_BUILD_STRING(self, state, op):
    # TODO(mdemello): Test this.
    state, _ = state.popn(op.arg)
    ret = abstract.Instance(self.ctx.convert.str_type, self.ctx)
    return state.push(ret.to_variable(state.node))

  def byte_GET_AITER(self, state, op):
    """Implementation of the GET_AITER opcode."""
    state, obj = state.pop()
    state, itr = self._get_aiter(state, obj)
    # Push the iterator onto the stack and return.
    state = state.push(itr)
    return state

  def byte_GET_ANEXT(self, state, op):
    """Implementation of the GET_ANEXT opcode."""
    state, ret = self._call(state, state.top(), "__anext__", ())
    if not self._check_return(state.node, ret, self.ctx.convert.awaitable_type):
      ret = self.ctx.new_unsolvable(state.node)
    return state.push(ret)

  def byte_BEFORE_ASYNC_WITH(self, state, op):
    """Implementation of the BEFORE_ASYNC_WITH opcode."""
    # Pop a context manager and push its `__aexit__` and `__aenter__()`.
    state, ctxmgr = state.pop()
    state, aexit_method = self.load_attr(state, ctxmgr, "__aexit__")
    state = state.push(aexit_method)
    state, ctxmgr_obj = self._call(state, ctxmgr, "__aenter__", ())
    return state.push(ctxmgr_obj)

  def _to_coroutine(self, state, obj, top=True):
    """Convert any awaitables and generators in obj to coroutines.

    Implements the GET_AWAITABLE opcode, which returns obj unchanged if it is a
    coroutine or generator and otherwise resolves obj.__await__
    (https://docs.python.org/3/library/dis.html#opcode-GET_AWAITABLE). So that
    we don't have to handle awaitable generators specially, our implementation
    converts generators to coroutines.

    Args:
      state: The current state.
      obj: The object, a cfg.Variable.
      top: Whether this is the top-level recursive call, to prevent incorrectly
        recursing into the result of obj.__await__.

    Returns:
      A tuple of the state and a cfg.Variable of coroutines.
    """
    bad_bindings = []
    for b in obj.bindings:
      if self.ctx.matcher(state.node).match_var_against_type(
          obj, self.ctx.convert.coroutine_type, {}, {obj: b}) is None:
        bad_bindings.append(b)
    if not bad_bindings:  # there are no non-coroutines
      return state, obj
    ret = self.ctx.program.NewVariable()
    for b in obj.bindings:
      state = self._binding_to_coroutine(state, b, bad_bindings, ret, top)
    return state, ret

  def _binding_to_coroutine(self, state, b, bad_bindings, ret, top):
    """Helper for _to_coroutine.

    Args:
      state: The current state.
      b: A cfg.Binding.
      bad_bindings: Bindings that are not coroutines.
      ret: A return variable that this helper will add to.
      top: Whether this is the top-level recursive call.

    Returns:
      The state.
    """
    if b not in bad_bindings:  # this is already a coroutine
      ret.PasteBinding(b)
      return state
    if self.ctx.matcher(state.node).match_var_against_type(
        b.variable, self.ctx.convert.generator_type, {},
        {b.variable: b}) is not None:
      # This is a generator; convert it to a coroutine. This conversion is
      # necessary even though generator-based coroutines convert their return
      # values themselves because __await__ can return a generator.
      ret_param = b.data.get_instance_type_parameter(abstract_utils.V)
      coroutine = abstract.Coroutine(self.ctx, ret_param, state.node)
      ret.AddBinding(coroutine, [b], state.node)
      return state
    # This is neither a coroutine or a generator; call __await__.
    if not top:  # we've already called __await__
      ret.PasteBinding(b)
      return state
    _, await_method = self.ctx.attribute_handler.get_attribute(
        state.node, b.data, "__await__", b)
    if await_method is None or not await_method.bindings:
      # We don't need to log an error here; byte_GET_AWAITABLE will check
      # that the final result is awaitable.
      ret.PasteBinding(b)
      return state
    state, await_obj = self.call_function_with_state(state, await_method, ())
    state, subret = self._to_coroutine(state, await_obj, top=False)
    ret.PasteVariable(subret)
    return state

  def byte_GET_AWAITABLE(self, state, op):
    """Implementation of the GET_AWAITABLE opcode."""
    state, obj = state.pop()
    state, ret = self._to_coroutine(state, obj)
    if not self._check_return(state.node, ret, self.ctx.convert.awaitable_type):
      ret = self.ctx.new_unsolvable(state.node)
    return state.push(ret)

  def byte_YIELD_FROM(self, state, op):
    """Implementation of the YIELD_FROM opcode."""
    state, unused_none_var = state.pop()
    state, var = state.pop()
    result = self.ctx.program.NewVariable()
    for b in var.bindings:
      val = b.data
      if isinstance(val, (abstract.Generator,
                          abstract.Coroutine, abstract.Unsolvable)):
        ret_var = val.get_instance_type_parameter(abstract_utils.V)
        result.PasteVariable(ret_var, state.node, {b})
      elif (isinstance(val, abstract.Instance)
            and isinstance(val.cls,
                           (abstract.ParameterizedClass, abstract.PyTDClass))
            and val.cls.full_name in ("typing.Awaitable",
                                      "builtins.coroutine",
                                      "builtins.generator")):
        if val.cls.full_name == "typing.Awaitable":
          ret_var = val.get_instance_type_parameter(abstract_utils.T)
        else:
          ret_var = val.get_instance_type_parameter(abstract_utils.V)
        result.PasteVariable(ret_var, state.node, {b})
      else:
        result.AddBinding(val, {b}, state.node)
    return state.push(result)

  def byte_LOAD_METHOD(self, state, op):
    """Implementation of the LOAD_METHOD opcode."""
    name = self.frame.f_code.co_names[op.arg]
    state, self_obj = state.pop()
    state, result = self.load_attr(state, self_obj, name)
    # https://docs.python.org/3/library/dis.html#opcode-LOAD_METHOD says that
    # this opcode should push two values onto the stack: either the unbound
    # method and its `self` or NULL and the bound method. However, pushing only
    # the bound method and modifying CALL_METHOD accordingly works in all cases
    # we've tested.
    self.trace_opcode(op, name, (self_obj, result))
    return state.push(result)

  def _store_new_var_in_local(self, state, var, new_var):
    """Assign a new var to a variable in locals."""
    varname = self.get_var_name(var)
    if not varname or varname not in self.frame.f_locals.pyval:
      # We cannot store the new value back in locals.
      return state
    # TODO(mdemello): Do we need to create two new nodes for this? Code copied
    # from _pop_and_store, but there might be a reason to create two nodes there
    # that does not apply here.
    state = state.forward_cfg_node()
    state = self._store_value(state, varname, new_var, local=True)
    state = state.forward_cfg_node()
    return state

  def _narrow(self, state, var, pred):
    """Narrow a variable by removing bindings that do not satisfy pred."""
    keep = [b for b in var.bindings if pred(b.data)]
    if len(keep) == len(var.bindings):
      # Nothing to narrow.
      return state
    out = self.ctx.program.NewVariable()
    for b in keep:
      out.AddBinding(b.data, {b}, state.node)
    return self._store_new_var_in_local(state, var, out)

  def _set_type_from_assert_isinstance(self, state, var, class_spec):
    # TODO(mdemello): If we want to cast var to typ via an assertion, should
    # we check that at least one binding of var is compatible with typ?
    classes = []
    abstract_utils.flatten(class_spec, classes)
    node, new_type = self.init_class(state.node,
                                     self.ctx.convert.merge_values(classes))
    state = state.change_cfg_node(node)
    return self._store_new_var_in_local(state, var, new_type)

  def _check_test_assert(self, state, func, args):
    """Narrow the types of variables based on test assertions."""
    # We need a variable to narrow
    if not args:
      return state
    var = args[0]
    f = func.data[0]
    if not isinstance(f, abstract.BoundFunction) or len(f.callself.data) != 1:
      return state
    cls = f.callself.data[0].cls
    if not (isinstance(cls, class_mixin.Class) and cls.is_test_class()):
      return state
    if f.name == "assertIsNotNone":
      if len(args) == 1:
        pred = lambda v: not self._data_is_none(v)
        state = self._narrow(state, var, pred)
    elif f.name == "assertIsInstance":
      if len(args) == 2:
        class_spec = args[1].data[0]
        state = self._set_type_from_assert_isinstance(state, var, class_spec)
    return state

  def byte_CALL_METHOD(self, state, op):
    state, args = state.popn(op.arg)
    state, func = state.pop()
    state, result = self.call_function_with_state(state, func, args)
    return state.push(result)

  def byte_RERAISE(self, state, op):
    del op  # unused
    state, _ = state.popn(3)
    return state.set_why("reraise")

  def byte_WITH_EXCEPT_START(self, state, op):
    del op  # unused
    func = state.peek(7)
    args = state.topn(3)
    state, result = self.call_function_with_state(state, func, args)
    return state.push(result)

  def byte_LOAD_ASSERTION_ERROR(self, state, op):
    del op  # unused
    assertion_error = self.ctx.convert.name_to_value("builtins.AssertionError")
    return state.push(assertion_error.to_variable(state.node))

  # Stub implementations for opcodes new in Python 3.10.

  def byte_GET_LEN(self, state, op):
    del op
    return state

  def byte_MATCH_MAPPING(self, state, op):
    del op
    return state

  def byte_MATCH_SEQUENCE(self, state, op):
    del op
    return state

  def byte_MATCH_KEYS(self, state, op):
    del op
    return state

  def byte_COPY_DICT_WITHOUT_KEYS(self, state, op):
    del op
    return state

  def byte_ROT_N(self, state, op):
    del op
    return state

  def byte_GEN_START(self, state, op):
    del op
    return state

  def byte_MATCH_CLASS(self, state, op):
    del op
    return state
