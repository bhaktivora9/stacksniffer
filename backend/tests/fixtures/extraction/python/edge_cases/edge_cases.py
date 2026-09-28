from typing import Generic, Protocol, TypeVar

T = TypeVar("T")


class Repository(Protocol):
    def get(self, key: str) -> str: ...


class Box(Generic[T]):
    def __init__(self, item: T):
        self.item = item


def helper():
    return 1


def helper():
    return 2


def uses_shadowing(helper):
    return helper()


def factory():
    class Local:
        def run(self):
            return helper()
    return Local().run()


square = lambda x: pow(x, 2)
CACHE = dict(size=10)
