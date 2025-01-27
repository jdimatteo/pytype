"""Test comparison operators."""

from pytype.tests import test_base


class InstanceUnequalityTest(test_base.BaseTest):

  def test_is(self):
    """SomeType is not be the same as AnotherType."""
    self.Check("""
      from typing import Optional
      def f(x: Optional[str]) -> NoneType:
        if x is None:
          return x
        else:
          return None
      """)


class ContainsFallbackTest(test_base.BaseTest):
  """Tests the __contains__ -> __iter__ -> __getitem__ fallbacks."""

  def test_overload_contains(self):
    self.CheckWithErrors("""
      class F:
        def __contains__(self, x: int):
          if not isinstance(x, int):
            raise TypeError("__contains__ only takes int")
          return True
      1 in F()
      "not int" in F()  # unsupported-operands
    """)

  def test_fallback_iter(self):
    self.Check("""
      class F:
        def __iter__(self):
          pass
      1 in F()
      "not int" in F()
    """)

  def test_fallback_getitem(self):
    self.Check("""
      class F:
        def __getitem__(self, key):
          pass
      1 in F()
      "not int" in F()
    """)


class NotImplementedTest(test_base.BaseTest):
  """Tests handling of the NotImplemented builtin."""

  def test_return_annotation(self):
    self.Check("""
      class Foo:
        def __eq__(self, other) -> bool:
          if isinstance(other, Foo):
            return id(self) == id(other)
          else:
            return NotImplemented
    """)

  def test_infer_return_type(self):
    ty = self.Infer("""
      class Foo:
        def __eq__(self, other):
          if isinstance(other, Foo):
            return id(self) == id(other)
          else:
            return NotImplemented
    """)
    self.assertTypesMatchPytd(ty, """
      class Foo:
        def __eq__(self, other) -> bool: ...
    """)


class CmpOpTest(test_base.BaseTest):
  """Tests comparison operator behavior in Python 3."""

  def test_lt(self):
    # In Python 3, comparisons between two types that don't define their own
    # comparison dunder methods is not guaranteed to succeed, except for ==, !=,
    # is and is not.
    # pytype infers a boolean value for those comparisons that always succeed,
    # and currently infers Any for ones that don't.
    # Comparison between types is necessary to trigger the "comparison always
    # succeeds" behavior in vm.py.
    ty = self.Infer("res = (1).__class__ < ''.__class__")
    self.assertTypesMatchPytd(ty, """
      from typing import Any
      res: Any
    """)


if __name__ == "__main__":
  test_base.main()
