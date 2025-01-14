"""Tests for reloading generated pyi."""

from pytype import file_utils
from pytype.pytd import pytd_utils
from pytype.tests import test_base


class ReingestTest(test_base.BaseTest):
  """Tests for reloading the pyi we generate."""

  def test_type_parameter_bound(self):
    foo = self.Infer("""
      from typing import TypeVar
      T = TypeVar("T", bound=float)
      def f(x: T) -> T: return x
    """, deep=False)
    with file_utils.Tempdir() as d:
      d.create_file("foo.pyi", pytd_utils.Print(foo))
      _, errors = self.InferWithErrors("""
        import foo
        foo.f("")  # wrong-arg-types[e]
      """, pythonpath=[d.path])
      self.assertErrorRegexes(errors, {"e": r"float.*str"})

  def test_default_argument_type(self):
    foo = self.Infer("""
      from typing import Any, Callable, TypeVar
      T = TypeVar("T")
      def f(x):
        return True
      def g(x: Callable[[T], Any]) -> T: ...
    """)
    with file_utils.Tempdir() as d:
      d.create_file("foo.pyi", pytd_utils.Print(foo))
      self.Check("""
        import foo
        foo.g(foo.f).upper()
      """, pythonpath=[d.path])

  def test_duplicate_anystr_import(self):
    dep1 = self.Infer("""
      from typing import AnyStr
      def f(x: AnyStr) -> AnyStr:
        return x
    """)
    with file_utils.Tempdir() as d:
      d.create_file("dep1.pyi", pytd_utils.Print(dep1))
      dep2 = self.Infer("""
        from typing import AnyStr
        from dep1 import f
        def g(x: AnyStr) -> AnyStr:
          return x
      """, pythonpath=[d.path])
      d.create_file("dep2.pyi", pytd_utils.Print(dep2))
      self.Check("import dep2", pythonpath=[d.path])


class ReingestTestPy3(test_base.BaseTest):
  """Python 3 tests for reloading the pyi we generate."""

  def test_instantiate_pyi_class(self):
    foo = self.Infer("""
      import abc
      class Foo(metaclass=abc.ABCMeta):
        @abc.abstractmethod
        def foo(self):
          pass
      class Bar(Foo):
        def foo(self):
          pass
    """)
    with file_utils.Tempdir() as d:
      d.create_file("foo.pyi", pytd_utils.Print(foo))
      _, errors = self.InferWithErrors("""
        import foo
        foo.Foo()  # not-instantiable[e]
        foo.Bar()
      """, pythonpath=[d.path])
      self.assertErrorRegexes(errors, {"e": r"foo\.Foo.*foo"})

  def test_use_class_attribute_from_annotated_new(self):
    foo = self.Infer("""
      class Foo:
        def __new__(cls) -> "Foo":
          return cls()
      class Bar:
        FOO = Foo()
    """)
    with file_utils.Tempdir() as d:
      d.create_file("foo.pyi", pytd_utils.Print(foo))
      self.Check("""
        import foo
        print(foo.Bar.FOO)
      """, pythonpath=[d.path])


if __name__ == "__main__":
  test_base.main()
