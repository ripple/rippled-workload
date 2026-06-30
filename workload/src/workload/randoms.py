from collections.abc import Sequence
from typing import cast

from antithesis.random import AntithesisRandom

_urand = AntithesisRandom()

# Typed wrappers: AntithesisRandom ships no py.typed, so its results are Any.
# Casting here gives every call site real types instead of leaking Any.


def choice[T](seq: Sequence[T]) -> T:
    return cast(T, _urand.choice(seq))


def sample[T](population: Sequence[T], k: int) -> list[T]:
    return cast("list[T]", _urand.sample(population, k))


def randint(a: int, b: int) -> int:
    return cast(int, _urand.randint(a, b))


def random() -> float:
    return cast(float, _urand.random())
